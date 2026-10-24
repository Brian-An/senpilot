"""Signed inbound email webhook and deployment health endpoints."""

import logging
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from typing import cast

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request
from psycopg_pool import PoolTimeout

from senpilot.config import WebSettings
from senpilot.dispatch import start_job
from senpilot.inbound import parse_sendgrid, verify_signature
from senpilot.jobs import JobStore

logger = logging.getLogger(__name__)
MAX_WEBHOOK_BYTES = 1024 * 1024


def create_app(
    settings: WebSettings | None = None,
    store: JobStore | None = None,
    *,
    dispatch_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or WebSettings.from_env()
        async with AsyncExitStack() as stack:
            database = store or await stack.enter_async_context(JobStore(config.database_url))
            app.state.config = config
            app.state.database = database
            app.state.dispatch_client = dispatch_client or await stack.enter_async_context(
                httpx.AsyncClient()
            )
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthy() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready(request: Request) -> dict[str, str]:
        database = cast(JobStore, request.app.state.database)
        try:
            await database.healthy()
        except (psycopg.Error, PoolTimeout) as exc:
            raise HTTPException(503, "database unavailable") from exc
        return {"status": "ready"}

    async def dispatch(request: Request) -> str:
        config = cast(WebSettings, request.app.state.config)
        database = cast(JobStore, request.app.state.database)
        try:
            if not await database.has_work():
                return "idle"
            await start_job(request.app.state.dispatch_client, config.cloud_run_job)
        except (httpx.HTTPError, ValueError, KeyError, psycopg.Error, PoolTimeout) as exc:
            logger.error("Cloud Run dispatch failed (%s)", type(exc).__name__)
            raise HTTPException(503, "request saved; worker dispatch will retry") from exc
        return "started"

    @app.post("/internal/dispatch")
    async def recover(request: Request) -> dict[str, str]:
        config = cast(WebSettings, request.app.state.config)
        if not config.cloud_run_job:
            raise HTTPException(404, "Cloud Run dispatch is disabled")
        if not secrets.compare_digest(
            request.headers.get("Authorization", "").encode(),
            f"Bearer {config.dispatch_token}".encode(),
        ):
            raise HTTPException(401, "invalid dispatch authorization")
        return {"status": await dispatch(request)}

    @app.post("/webhooks/sendgrid/inbound")
    async def inbound(request: Request) -> dict[str, str]:
        config = cast(WebSettings, request.app.state.config)
        database = cast(JobStore, request.app.state.database)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_WEBHOOK_BYTES:
                raise HTTPException(413, "email request exceeds the supported size")
        body = bytes(raw)
        try:
            verify_signature(
                body,
                request.headers.get("X-Twilio-Email-Event-Webhook-Timestamp", ""),
                request.headers.get("X-Twilio-Email-Event-Webhook-Signature", ""),
                config.public_key,
            )
        except ValueError as exc:
            raise HTTPException(401, "invalid webhook signature") from exc
        try:
            message = parse_sendgrid(
                body,
                request.headers.get("Content-Type", ""),
                config.agent_email,
                config.allowed_senders,
            )
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, "invalid email payload") from exc
        if message is None:
            return {"status": "ignored"}
        try:
            job_id = await database.enqueue(message)
        except (psycopg.Error, PoolTimeout) as exc:
            logger.error("Inbound email could not be queued")
            raise HTTPException(503, "queue unavailable; retry delivery") from exc
        if config.cloud_run_job:
            await dispatch(request)
        return {"status": "queued", "job_id": str(job_id)}

    return app


app = create_app()
