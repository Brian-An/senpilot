import asyncio
import secrets
from uuid import uuid4

import psycopg
import pytest

from senpilot.inbound import IncomingEmail
from senpilot.jobs import JobStore, LeaseLost, migrate
from senpilot.models import DocumentType, DownloadRequest
from tests.service_fixtures import TEST_DATABASE


async def test_schema_owner_can_migrate_without_database_creation_privileges(
    store: JobStore,
) -> None:
    role = f"senpilot_backend_{uuid4().hex[:12]}"
    password = secrets.token_urlsafe(32)
    async with await psycopg.AsyncConnection.connect(TEST_DATABASE, autocommit=True) as connection:
        await connection.execute(
            psycopg.sql.SQL("CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}").format(
                psycopg.sql.Identifier(role), psycopg.sql.Literal(password)
            )
        )
        try:
            await connection.execute(
                psycopg.sql.SQL("ALTER SCHEMA senpilot_private OWNER TO {}").format(
                    psycopg.sql.Identifier(role)
                )
            )
            for table in ("jobs", "artifacts"):
                await connection.execute(
                    psycopg.sql.SQL("ALTER TABLE senpilot_private.{} OWNER TO {}").format(
                        psycopg.sql.Identifier(table), psycopg.sql.Identifier(role)
                    )
                )
            limited = psycopg.conninfo.make_conninfo(TEST_DATABASE, user=role, password=password)
            await migrate(limited)
            async with JobStore(limited) as database:
                await database.enqueue(incoming())
                assert await database.claim(uuid4()) is not None
        finally:
            await connection.execute("ALTER SCHEMA senpilot_private OWNER TO postgres")
            for table in ("jobs", "artifacts"):
                await connection.execute(
                    psycopg.sql.SQL("ALTER TABLE senpilot_private.{} OWNER TO postgres").format(
                        psycopg.sql.Identifier(table)
                    )
                )
            await connection.execute(
                psycopg.sql.SQL("DROP ROLE {}").format(psycopg.sql.Identifier(role))
            )


def incoming(identifier: str = "request") -> IncomingEmail:
    return IncomingEmail(
        "tester@example.com",
        "Filing request",
        f"<{identifier}@example.com>",
        DownloadRequest("M12205", DocumentType.OTHER_DOCUMENTS),
        None,
    )


async def test_replayed_email_creates_only_one_job(store: JobStore) -> None:
    first = await store.enqueue(incoming())
    second = await store.enqueue(incoming())
    assert first == second
    assert await store.claim(uuid4()) is not None
    assert await store.claim(uuid4()) is None


async def test_concurrent_claims_cannot_take_the_same_job(store: JobStore) -> None:
    await store.enqueue(incoming())
    claims = await asyncio.gather(store.claim(uuid4()), store.claim(uuid4()))
    assert sum(job is not None for job in claims) == 1


async def test_lost_owner_cannot_prepare_or_send_a_job(store: JobStore) -> None:
    job_id = await store.enqueue(incoming())
    owner = uuid4()
    job = await store.claim(owner)
    assert job is not None
    async with await psycopg.AsyncConnection.connect(TEST_DATABASE, autocommit=True) as connection:
        await connection.execute(
            "UPDATE senpilot_private.jobs SET lease_until=now()-interval '1 second' WHERE id=%s",
            (job_id,),
        )
    recovered = await store.claim(uuid4())
    assert recovered is not None and recovered.id == job_id
    with pytest.raises(LeaseLost):
        await store.prepare(job, "Reply", None, None, {})


async def test_prepared_reply_survives_worker_restart(store: JobStore) -> None:
    await store.enqueue(incoming())
    job = await store.claim(uuid4())
    assert job is not None
    await store.prepare(
        job, "Ready to send", "job/filings.zip", "filings.zip", {"status": "complete"}
    )
    async with await psycopg.AsyncConnection.connect(TEST_DATABASE, autocommit=True) as connection:
        await connection.execute(
            "UPDATE senpilot_private.jobs SET lease_until=now()-interval '1 second'"
        )
    recovered = await store.claim(uuid4())
    assert recovered is not None
    assert recovered.state == "ready"
    assert recovered.reply_text == "Ready to send"
    assert recovered.artifact_key == "job/filings.zip"


async def test_delivery_uncertainty_does_not_automatically_resend(store: JobStore) -> None:
    await store.enqueue(incoming())
    job = await store.claim(uuid4())
    assert job is not None
    await store.prepare(job, "Reply", None, None, {})
    await store.begin_send(job)
    async with await psycopg.AsyncConnection.connect(TEST_DATABASE, autocommit=True) as connection:
        await connection.execute(
            "UPDATE senpilot_private.jobs SET lease_until=now()-interval '1 second'"
        )
    assert await store.claim(uuid4()) is None
    assert await store.state(job.id) == "uncertain"


async def test_successful_send_is_final(store: JobStore) -> None:
    await store.enqueue(incoming())
    job = await store.claim(uuid4())
    assert job is not None
    await store.prepare(job, "Reply", None, None, {})
    await store.begin_send(job)
    await store.sent(job, "provider-message-id")
    assert await store.state(job.id) == "sent"
    assert await store.claim(uuid4()) is None
