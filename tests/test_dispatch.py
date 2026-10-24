from dataclasses import replace
from uuid import uuid4

import httpx
import psycopg
import pytest

from senpilot.config import WebSettings
from senpilot.jobs import JobStore
from senpilot.web import create_app
from tests.service_fixtures import TEST_DATABASE
from tests.test_jobs import incoming
from tests.test_service import signed_email

JOB_NAME = "projects/senpilot-demo/locations/us-central1/jobs/senpilot-worker"
TOKEN = "test-recovery-token-with-at-least-32-characters"


def settings(public_key: str) -> WebSettings:
    return WebSettings(
        TEST_DATABASE,
        "agent@example.com",
        frozenset({"tester@example.com"}),
        public_key,
        cloud_run_job=JOB_NAME,
        dispatch_token=TOKEN,
    )


@pytest.mark.parametrize("first_status", [200, 503])
async def test_persisted_email_starts_cloud_job_and_survives_dispatch_failure(
    store: JobStore, first_status: int
) -> None:
    body, headers, public = signed_email("Other Documents M12205")
    executions: list[httpx.Request] = []

    async def google(request: httpx.Request) -> httpx.Response:
        if request.url.host == "metadata.google.internal":
            assert request.headers["Metadata-Flavor"] == "Google"
            return httpx.Response(200, json={"access_token": "test-service-token"})
        assert request.url == f"https://run.googleapis.com/v2/{JOB_NAME}:run"
        assert request.headers["Authorization"] == "Bearer test-service-token"
        assert await store.has_work()
        executions.append(request)
        return httpx.Response(first_status if len(executions) == 1 else 200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as google_client:
        app = create_app(settings(public), store, dispatch_client=google_client)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            response = await client.post(
                "/webhooks/sendgrid/inbound", content=body, headers=headers
            )
            assert response.status_code == (200 if first_status == 200 else 503)
            assert await store.has_work()
            replay = await client.post("/webhooks/sendgrid/inbound", content=body, headers=headers)
            assert replay.status_code == 200
    assert len(executions) == 2
    assert await store.claim(uuid4()) is not None
    assert await store.claim(uuid4()) is None


async def test_recovery_requires_token_and_skips_empty_or_leased_work(store: JobStore) -> None:
    executions: list[str] = []

    def google(request: httpx.Request) -> httpx.Response:
        if request.url.host == "metadata.google.internal":
            return httpx.Response(200, json={"access_token": "test-service-token"})
        executions.append(str(request.url))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as google_client:
        app = create_app(settings("unused-signing-key"), store, dispatch_client=google_client)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            assert (await client.post("/internal/dispatch")).status_code == 401
            auth = {"Authorization": f"Bearer {TOKEN}"}
            idle = await client.post("/internal/dispatch", headers=auth)
            assert idle.json() == {"status": "idle"}
            assert executions == []
            await store.enqueue(incoming())
            started = await client.post("/internal/dispatch", headers=auth)
            assert started.json() == {"status": "started"}
            job = await store.claim(uuid4())
            assert job is not None
            assert (await client.post("/internal/dispatch", headers=auth)).json() == {
                "status": "idle"
            }
            async with await psycopg.AsyncConnection.connect(
                TEST_DATABASE, autocommit=True
            ) as connection:
                await connection.execute(
                    "UPDATE senpilot_private.jobs SET lease_until=now()-interval '1 second'"
                )
            recovered = await client.post("/internal/dispatch", headers=auth)
            assert recovered.json() == {"status": "started"}
            assert len(executions) == 2


async def test_due_work_includes_retry_and_cleanup_but_not_final_deliveries(
    store: JobStore,
) -> None:
    assert not await store.has_work()
    await store.enqueue(incoming())
    assert await store.has_work()
    job = await store.claim(uuid4())
    assert job is not None
    assert not await store.has_work()
    await store.record_artifact(job, "job/filings.zip")
    await store.retry(job, "temporary failure")
    assert not await store.has_work()
    async with await psycopg.AsyncConnection.connect(TEST_DATABASE, autocommit=True) as connection:
        await connection.execute(
            "UPDATE senpilot_private.jobs SET available_at=now()-interval '1 second'"
        )
        assert await store.has_work()
        await connection.execute(
            "UPDATE senpilot_private.jobs SET state='uncertain',owner=NULL,lease_until=NULL"
        )
        assert not await store.has_work()
        await connection.execute(
            "UPDATE senpilot_private.jobs SET updated_at=now()-interval '8 days'"
        )
    assert await store.has_work()
    await store.artifact_deleted(job.id, "job/filings.zip")
    assert not await store.has_work()


@pytest.mark.parametrize(
    "job,token",
    [(JOB_NAME, ""), (JOB_NAME, "short"), ("https://example.com", TOKEN), ("", TOKEN)],
)
def test_cloud_run_configuration_fails_closed(job: str, token: str) -> None:
    with pytest.raises(ValueError):
        replace(settings("public-key"), cloud_run_job=job, dispatch_token=token)
