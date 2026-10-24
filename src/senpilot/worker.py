"""Process durable email jobs without repeating uncertain external deliveries."""

import asyncio
import json
import logging
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx

from senpilot.config import WorkerSettings
from senpilot.jobs import Job, JobStore, LeaseLost
from senpilot.models import DocumentType, DownloadRequest
from senpilot.pipeline import download_matter
from senpilot.replies import TOO_LARGE, ComposedReply, compose_reply, summarize_manifest
from senpilot.sendgrid import DeliveryRejected, DeliveryUncertain, send_reply
from senpilot.storage import ArtifactStore
from senpilot.uarb import SOURCE_URL

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        settings: WorkerSettings,
        store: JobStore,
        artifacts: ArtifactStore,
        client: httpx.AsyncClient,
        *,
        download_base_url: str = SOURCE_URL,
    ) -> None:
        self.settings = settings
        self.store = store
        self.artifacts = artifacts
        self.client = client
        self.owner = uuid4()
        self.download_base_url = download_base_url

    async def _heartbeat(self, job: Job) -> None:
        while True:
            await asyncio.sleep(30)
            await self.store.heartbeat(job)

    async def run_once(self) -> bool:
        job = await self.store.claim(self.owner)
        if job is None:
            return False
        processing = asyncio.create_task(self._process(job))
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            done, _ = await asyncio.wait(
                {processing, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                await heartbeat
            await processing
        except LeaseLost:
            logger.error("Job %s lost its lease", job.id)
        except Exception as exc:
            logger.error("Job %s failed (%s)", job.id, type(exc).__name__)
            try:
                await self.store.retry(job, f"processing failed: {type(exc).__name__}")
            except LeaseLost:
                logger.error("Job %s requires delivery reconciliation or lease recovery", job.id)
        finally:
            for task in (processing, heartbeat):
                if not task.done():
                    task.cancel()
            await asyncio.gather(processing, heartbeat, return_exceptions=True)
        return True

    def _compose(self, job: Job, body: str, attachment: Path | None) -> ComposedReply:
        return compose_reply(
            self.settings.agent_email,
            str(job.email["sender"]),
            str(job.email["subject"]),
            str(job.email["message_id"]),
            body,
            attachment,
            message_id=f"<{job.id}@{self.settings.agent_email.split('@', 1)[1]}>",
        )

    async def _process(self, job: Job) -> None:
        with tempfile.TemporaryDirectory(prefix="senpilot-job-") as temporary:
            root = Path(temporary)
            attachment: Path | None = None
            if job.state == "processing":
                request = job.email.get("request")
                manifest: dict[str, object] = {}
                key: str | None = None
                filename: str | None = None
                if isinstance(request, dict):
                    result = await download_matter(
                        DownloadRequest(
                            str(request["matter_number"]),
                            DocumentType(str(request["document_type"])),
                            int(request["limit"]),
                        ),
                        root,
                        base_url=self.download_base_url,
                    )
                    manifest = cast(dict[str, object], json.loads(result.manifest_path.read_text()))
                    body = summarize_manifest(manifest)
                    preview = self._compose(job, body, result.zip_path)
                    if result.zip_path is not None and preview.attached:
                        attachment = result.zip_path
                        filename = attachment.name
                        key = f"{job.id}/{job.owner}/{filename}"
                        await self.store.record_artifact(job, key)
                        await self.artifacts.put(key, attachment)
                    elif result.zip_path is not None:
                        body += f"\n\n{TOO_LARGE}"
                else:
                    body = str(job.email["clarification"])
                await self.store.prepare(job, body, key, filename, manifest)
                job = replace(
                    job,
                    state="ready",
                    reply_text=body,
                    artifact_key=key,
                    artifact_filename=filename,
                    manifest=manifest,
                )
            elif job.artifact_key:
                filename = job.artifact_filename
                if not filename or Path(filename).name != filename:
                    raise ValueError("invalid prepared artifact filename")
                attachment = root / filename
                await self.artifacts.get(job.artifact_key, attachment)
            if job.reply_text is None:
                raise ValueError("prepared job has no reply")
            reply = self._compose(job, job.reply_text, attachment)
            await self.store.begin_send(job)
            try:
                provider_id = await send_reply(
                    self.client,
                    self.settings.sendgrid_api_base,
                    self.settings.sendgrid_api_key,
                    reply,
                    job.id,
                )
            except DeliveryUncertain as exc:
                await self.store.uncertain(job, str(exc))
            except DeliveryRejected as exc:
                await self.store.rejected(job, str(exc), retryable=exc.retryable)
            else:
                await self.store.sent(job, provider_id)

    async def cleanup(self) -> None:
        for job_id, key in await self.store.expired_artifacts():
            await self.artifacts.delete(key)
            await self.store.artifact_deleted(job_id, key)


async def run_worker(*, once: bool = False) -> None:
    settings = WorkerSettings.from_env()
    artifacts = ArtifactStore(settings)
    try:
        async with JobStore(settings.database_url) as store, httpx.AsyncClient() as client:
            worker = Worker(settings, store, artifacts, client)
            last_cleanup = 0.0
            while True:
                if time.monotonic() - last_cleanup > 3600:
                    await worker.cleanup()
                    last_cleanup = time.monotonic()
                worked = await worker.run_once()
                if once:
                    return
                if not worked:
                    await asyncio.sleep(5)
    finally:
        artifacts.close()
