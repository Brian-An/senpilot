"""PostgreSQL job queue with leased ownership and explicit delivery uncertainty."""

import hashlib
from dataclasses import asdict, dataclass
from importlib.resources import files
from types import TracebackType
from typing import cast
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from senpilot.inbound import IncomingEmail


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Job:
    id: UUID
    owner: UUID
    state: str
    email: dict[str, object]
    reply_text: str | None
    artifact_key: str | None
    artifact_filename: str | None
    manifest: dict[str, object] | None


async def migrate(database_url: str) -> None:
    sql = files("senpilot").joinpath("schema.sql").read_text()
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        await connection.execute("SELECT pg_advisory_xact_lock(hashtext('senpilot-schema'))")
        await connection.execute(sql)


class JobStore:
    def __init__(self, database_url: str) -> None:
        self.pool = AsyncConnectionPool(
            database_url,
            min_size=1,
            max_size=4,
            open=False,
            timeout=10,
            kwargs={"autocommit": True, "row_factory": dict_row, "connect_timeout": 10},
        )

    async def __aenter__(self) -> "JobStore":
        await self.pool.open(wait=True, timeout=15)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.pool.close()

    async def _one(self, sql: str, params: tuple[object, ...] = ()) -> dict[str, object] | None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(sql, params)
            # psycopg's dynamically decoded database values cross an untyped boundary.
            return cast(dict[str, object] | None, await cursor.fetchone())

    async def enqueue(self, email: IncomingEmail) -> UUID:
        dedup = hashlib.sha256(f"{email.sender}\x00{email.message_id}".encode()).hexdigest()
        row = await self._one(
            """INSERT INTO senpilot_private.jobs (id,dedup_key,email) VALUES (%s,%s,%s)
               ON CONFLICT (dedup_key) DO UPDATE SET dedup_key=EXCLUDED.dedup_key RETURNING id""",
            (uuid4(), dedup, Jsonb(asdict(email))),
        )
        assert row is not None
        return cast(UUID, row["id"])

    async def claim(self, owner: UUID) -> Job | None:
        async with self.pool.connection() as connection:
            await connection.execute(
                """UPDATE senpilot_private.jobs SET state='uncertain', owner=NULL,
                   lease_until=NULL, last_error='worker expired during email delivery',
                   updated_at=now()
                   WHERE state='sending' AND lease_until<now()"""
            )
            await connection.execute(
                """UPDATE senpilot_private.jobs SET state='failed',owner=NULL,lease_until=NULL,
                   last_error='retry limit reached',updated_at=now()
                   WHERE state IN ('processing','ready') AND lease_until<now() AND attempts>=3"""
            )
        row = await self._one(
            """WITH candidate AS (
                 SELECT id FROM senpilot_private.jobs WHERE attempts<3 AND (
                   (state='queued' AND available_at<=now()) OR
                   (state IN ('processing','ready') AND lease_until<now()))
                 ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
               ) UPDATE senpilot_private.jobs j SET
                 state=CASE WHEN j.reply_text IS NOT NULL THEN 'ready' ELSE 'processing' END,
                 owner=%s,lease_until=now()+interval '5 minutes',
                 attempts=attempts+1,updated_at=now()
               FROM candidate WHERE j.id=candidate.id RETURNING j.*""",
            (owner,),
        )
        if row is None:
            return None
        return Job(
            cast(UUID, row["id"]),
            owner,
            str(row["state"]),
            cast(dict[str, object], row["email"]),
            cast(str | None, row["reply_text"]),
            cast(str | None, row["artifact_key"]),
            cast(str | None, row["artifact_filename"]),
            cast(dict[str, object] | None, row["manifest"]),
        )

    async def _owned(self, sql: str, job: Job, params: tuple[object, ...] = ()) -> None:
        row = await self._one(sql, (*params, job.id, job.owner))
        if row is None:
            raise LeaseLost(f"job {job.id} is not owned by this worker")

    async def heartbeat(self, job: Job) -> None:
        await self._owned(
            """UPDATE senpilot_private.jobs SET lease_until=now()+interval '5 minutes'
               WHERE id=%s AND owner=%s AND lease_until>now()
               AND state IN ('processing','ready','sending') RETURNING id""",
            job,
        )

    async def prepare(
        self,
        job: Job,
        body: str,
        artifact_key: str | None,
        filename: str | None,
        manifest: dict[str, object],
    ) -> None:
        await self._owned(
            """UPDATE senpilot_private.jobs SET state='ready',reply_text=%s,artifact_key=%s,
               artifact_filename=%s,manifest=%s,updated_at=now() WHERE id=%s AND owner=%s
               AND state='processing' AND lease_until>now() RETURNING id""",
            job,
            (body, artifact_key, filename, Jsonb(manifest)),
        )

    async def record_artifact(self, job: Job, key: str) -> None:
        await self._owned(
            """INSERT INTO senpilot_private.artifacts (object_key,job_id)
               SELECT %s,id FROM senpilot_private.jobs WHERE id=%s AND owner=%s
               AND state='processing' AND lease_until>now()
               ON CONFLICT(object_key) DO UPDATE SET object_key=EXCLUDED.object_key
               RETURNING job_id""",
            job,
            (key,),
        )

    async def begin_send(self, job: Job) -> None:
        await self._owned(
            """UPDATE senpilot_private.jobs SET state='sending',updated_at=now()
               WHERE id=%s AND owner=%s AND state='ready' AND lease_until>now() RETURNING id""",
            job,
        )

    async def sent(self, job: Job, provider_message_id: str) -> None:
        await self._owned(
            """UPDATE senpilot_private.jobs SET state='sent',provider_message_id=%s,owner=NULL,
               lease_until=NULL,updated_at=now() WHERE id=%s AND owner=%s
               AND state='sending' RETURNING id""",
            job,
            (provider_message_id,),
        )

    async def uncertain(self, job: Job, reason: str) -> None:
        await self._owned(
            """UPDATE senpilot_private.jobs SET state='uncertain',last_error=%s,owner=NULL,
               lease_until=NULL,updated_at=now() WHERE id=%s AND owner=%s
               AND state='sending' RETURNING id""",
            job,
            (reason,),
        )

    async def retry(self, job: Job, reason: str) -> None:
        await self._owned(
            """UPDATE senpilot_private.jobs SET
               state=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END,
               last_error=%s,owner=NULL,lease_until=NULL,available_at=now()+interval '30 seconds',
               updated_at=now() WHERE id=%s AND owner=%s
               AND state IN ('processing','ready') AND lease_until>now() RETURNING id""",
            job,
            (reason,),
        )

    async def rejected(self, job: Job, reason: str, *, retryable: bool = False) -> None:
        await self._owned(
            """UPDATE senpilot_private.jobs SET
               state=CASE WHEN %s AND attempts<3 THEN 'queued' ELSE 'failed' END,
               last_error=%s,owner=NULL,lease_until=NULL,
               available_at=now()+interval '30 seconds',updated_at=now()
               WHERE id=%s AND owner=%s AND state='sending' RETURNING id""",
            job,
            (retryable, reason),
        )

    async def state(self, job_id: UUID) -> str | None:
        row = await self._one("SELECT state FROM senpilot_private.jobs WHERE id=%s", (job_id,))
        return str(row["state"]) if row else None

    async def healthy(self) -> bool:
        await self._one("SELECT 1 FROM senpilot_private.jobs LIMIT 1")
        return True

    async def has_work(self) -> bool:
        row = await self._one(
            """SELECT EXISTS (
                 SELECT 1 FROM senpilot_private.jobs WHERE
                   (state='queued' AND available_at<=now() AND attempts<3) OR
                   (state IN ('processing','ready','sending') AND lease_until<now())
               ) OR EXISTS (
                 SELECT 1 FROM senpilot_private.artifacts a
                 JOIN senpilot_private.jobs j ON j.id=a.job_id
                 WHERE j.state IN ('sent','failed','uncertain')
                 AND j.updated_at<now()-interval '7 days'
               ) AS pending"""
        )
        assert row is not None
        return bool(row["pending"])

    async def expired_artifacts(self) -> list[tuple[UUID, str]]:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """SELECT j.id,a.object_key FROM senpilot_private.artifacts a
                   JOIN senpilot_private.jobs j ON j.id=a.job_id
                   WHERE j.state IN ('sent','failed','uncertain')
                   AND j.updated_at<now()-interval '7 days' LIMIT 100"""
            )
            return [(cast(UUID, r["id"]), str(r["object_key"])) for r in await cursor.fetchall()]

    async def artifact_deleted(self, job_id: UUID, key: str) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                "DELETE FROM senpilot_private.artifacts WHERE job_id=%s AND object_key=%s",
                (job_id, key),
            )
            await connection.execute(
                """UPDATE senpilot_private.jobs SET artifact_key=NULL
                   WHERE id=%s AND artifact_key=%s""",
                (job_id, key),
            )
