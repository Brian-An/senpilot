import asyncio
import base64
import json
import threading
import time
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

import boto3
import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from senpilot.config import WebSettings, WorkerSettings
from senpilot.jobs import JobStore
from senpilot.storage import ArtifactStore
from senpilot.web import create_app
from senpilot.worker import Worker, run_worker
from tests.service_fixtures import TEST_DATABASE
from tests.test_jobs import incoming


@pytest.mark.parametrize("mode", ["ai", "rules", "ambiguous", "mixed", "unavailable"])
async def test_hybrid_extraction_from_signed_email_to_download_or_clarification(
    store: JobStore,
    artifacts: ArtifactStore,
    worker_settings: WorkerSettings,
    sender: "OutboundServer",
    fixture_url: str,
    mode: str,
) -> None:
    text = {
        "ai": "Please grab the key docs for matter 12205",
        "rules": "Please send Key Documents for M12205",
        "ambiguous": "Please get hearing records for matter 12205",
        "mixed": "Please send Exhibits and hearing audio for M12205",
        "unavailable": "Please grab the key docs for matter 12205",
    }[mode]
    body, headers, public = signed_email(text)
    app = create_app(
        WebSettings(TEST_DATABASE, "agent@example.com", frozenset({"*"}), public), store
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api,
    ):
        response = await api.post("/webhooks/sendgrid/inbound", content=body, headers=headers)
        assert response.status_code == 200
        assert (
            await api.post("/webhooks/sendgrid/inbound", content=body, headers=headers)
        ).json() == response.json()

    ai_calls = []
    settings = replace(
        worker_settings,
        openai_api_key="private-test-key",
        sendgrid_api_base=f"http://127.0.0.1:{sender.server_port}",
    )
    async with httpx.AsyncClient() as outbound:

        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.host != "api.openai.com":
                return await outbound.send(request)
            ai_calls.append(request)
            if mode == "rules":
                pytest.fail("an exact request must bypass AI")
            if mode == "unavailable":
                return httpx.Response(503)
            from tests.test_extraction import response as ai_response

            return httpx.Response(
                200,
                json=ai_response(
                    {
                        "matter_number": "M12205",
                        "document_type": "Key Documents" if mode == "ai" else None,
                        "needs_clarification": mode in {"ambiguous", "mixed"},
                    }
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            worker = Worker(settings, store, artifacts, client, download_base_url=fixture_url)
            assert await worker.run_once()
            assert not await worker.run_once()
    assert len(ai_calls) == (0 if mode == "rules" else 1)
    assert len(sender.received) == 1
    message = sender.received[0]
    assert message["personalizations"][0]["to"] == [{"email": "tester@example.com"}]
    if mode in {"ai", "rules"}:
        assert "Key Documents" in message["content"][0]["value"]
        content = base64.b64decode(message["attachments"][0]["content"])
        with zipfile.ZipFile(BytesIO(content)) as archive:
            assert len(archive.namelist()) == 2
    else:
        assert "Please reply" in message["content"][0]["value"]
        assert not message.get("attachments")


class OutboundServer(ThreadingHTTPServer):
    received: list[dict[str, object]]
    status: int = 202


class OutboundHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.received.append(json.loads(body))
        self.send_response(self.server.status)
        self.send_header("X-Message-Id", "provider-id")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.mark.parametrize("mode,count", [("drain", 2), ("once", 1)])
async def test_finite_worker_sends_queued_replies_and_exits(
    store: JobStore,
    worker_settings: WorkerSettings,
    sender: OutboundServer,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    count: int,
) -> None:
    config = replace(worker_settings, sendgrid_api_base=f"http://127.0.0.1:{sender.server_port}")
    monkeypatch.setattr(WorkerSettings, "from_env", lambda: config)
    identifiers = []
    for number in range(2):
        email = replace(
            incoming(str(number)), request=None, clarification="Please clarify the request."
        )
        identifiers.append(await store.enqueue(email))
    await asyncio.wait_for(run_worker(**{mode: True}), timeout=10)
    assert len(sender.received) == count
    assert [await store.state(identifier) for identifier in identifiers] == (
        ["sent", "sent"] if mode == "drain" else ["sent", "queued"]
    )


@pytest.fixture
def sender() -> Iterator[OutboundServer]:
    server = OutboundServer(("127.0.0.1", 0), OutboundHandler)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def signed_email(
    text: str,
    *,
    spf: str = "pass",
    dkim: str = "{}",
    envelope_sender: str = "tester@example.com",
    latin1: bool = False,
) -> tuple[bytes, dict[str, str], str]:
    fields = {
        "envelope": json.dumps({"from": envelope_sender, "to": ["agent@example.com"]}),
        "subject": "Filing request",
        "text": text,
        "headers": "Message-ID: <request@example.com>\r\nFrom: tester@example.com\r\n",
        "SPF": spf,
        "dkim": dkim,
        "charsets": json.dumps({"text": "iso-8859-1" if latin1 else "utf-8"}),
    }
    request = httpx.Request(
        "POST",
        "http://test/webhooks/sendgrid/inbound",
        files={
            name: (None, value.encode("iso-8859-1") if name == "text" and latin1 else value)
            for name, value in fields.items()
        },
    )
    body = request.read()
    key = ec.generate_private_key(ec.SECP256R1())
    public = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode()
    timestamp = str(int(time.time()))
    signature = base64.b64encode(
        key.sign(timestamp.encode() + body, ec.ECDSA(hashes.SHA256()))
    ).decode()
    return (
        body,
        {
            "Content-Type": request.headers["Content-Type"],
            "X-Twilio-Email-Event-Webhook-Timestamp": timestamp,
            "X-Twilio-Email-Event-Webhook-Signature": signature,
        },
        public,
    )


async def test_health_checks_use_paths_available_through_cloud_run(store: JobStore) -> None:
    app = create_app(WebSettings(TEST_DATABASE, "agent@example.com", frozenset(), "unused"), store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert (await client.get("/ready")).json() == {"status": "ready"}


@pytest.mark.parametrize("spf,expected", [("pass", "queued"), ("fail", "ignored")])
async def test_public_agent_accepts_authenticated_senders_and_rejects_spoofing(
    store: JobStore, spf: str, expected: str
) -> None:
    body, headers, public = signed_email("Key Documents M12205", spf=spf)
    config = WebSettings(TEST_DATABASE, "agent@example.com", frozenset({"*"}), public)
    app = create_app(config, store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post("/webhooks/sendgrid/inbound", content=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == expected


def test_public_agent_configuration_accepts_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "DATABASE_URL": TEST_DATABASE,
        "AGENT_EMAIL": "agent@example.com",
        "ALLOWED_SENDERS": "*",
        "SENDGRID_INBOUND_PUBLIC_KEY": "test-key",
    }.items():
        monkeypatch.setenv(key, value)
    assert WebSettings.from_env().allowed_senders == frozenset({"*"})


@pytest.mark.parametrize("scenario", ["normal", "large"])
async def test_signed_email_to_real_download_and_reply(
    store: JobStore,
    artifacts: ArtifactStore,
    worker_settings: WorkerSettings,
    sender: OutboundServer,
    fixture_url: str,
    scenario: str,
) -> None:
    body, headers, public = signed_email("Please send Other Documents from M12205")
    settings = WebSettings(
        worker_settings.database_url, "agent@example.com", frozenset({"tester@example.com"}), public
    )
    app = create_app(settings, store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api,
    ):
        assert (await api.get("/ready")).status_code == 200
        response = await api.post("/webhooks/sendgrid/inbound", content=body, headers=headers)
        assert response.status_code == 200
        assert (
            await api.post("/webhooks/sendgrid/inbound", content=body, headers=headers)
        ).json() == response.json()
    worker_settings = replace(
        worker_settings, sendgrid_api_base=f"http://127.0.0.1:{sender.server_port}"
    )
    async with httpx.AsyncClient() as client:
        worker = Worker(
            worker_settings,
            store,
            artifacts,
            client,
            download_base_url=f"{fixture_url}?case={scenario}",
        )
        assert await worker.run_once()
        assert not await worker.run_once()
    assert len(sender.received) == 1
    message = sender.received[0]
    assert message["personalizations"][0]["to"] == [{"email": "tester@example.com"}]
    assert message["headers"]["In-Reply-To"] == "<request@example.com>"
    assert "Halifax Regional Water Commission" in message["content"][0]["value"]
    if scenario == "normal":
        content = base64.b64decode(message["attachments"][0]["content"])
        with zipfile.ZipFile(BytesIO(content)) as archive:
            assert len(archive.namelist()) == 10
    else:
        assert not message.get("attachments")
        assert "too large to attach" in message["content"][0]["value"]


@pytest.mark.parametrize(
    "options,expected",
    [
        ({"spf": "fail"}, "ignored"),
        ({"envelope_sender": "bounce@mailer.example.com"}, "queued"),
        ({"spf": "fail", "dkim": "{@example.com : pass}"}, "queued"),
        ({"spf": "pass", "envelope_sender": "attacker@other.example.net"}, "ignored"),
        ({"latin1": True}, "queued"),
    ],
)
async def test_sender_authentication_and_charset_handling(
    store: JobStore,
    worker_settings: WorkerSettings,
    options: dict[str, object],
    expected: str,
) -> None:
    body, headers, public = signed_email("André requests M12205 Exhibits", **options)
    app = create_app(
        WebSettings(
            worker_settings.database_url,
            "agent@example.com",
            frozenset({"tester@example.com"}),
            public,
        ),
        store,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api,
    ):
        response = await api.post("/webhooks/sendgrid/inbound", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == expected


async def test_signature_failure_does_not_enqueue(
    store: JobStore, worker_settings: WorkerSettings
) -> None:
    body, headers, public = signed_email("M12205 Exhibits")
    app = create_app(
        WebSettings(
            worker_settings.database_url,
            "agent@example.com",
            frozenset({"tester@example.com"}),
            public,
        ),
        store,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api,
    ):
        response = await api.post(
            "/webhooks/sendgrid/inbound", content=body + b" ", headers=headers
        )
        assert response.status_code == 401
    assert await store.claim(__import__("uuid").uuid4()) is None


async def test_ambiguous_request_gets_a_clarification_without_downloading(
    store: JobStore,
    artifacts: ArtifactStore,
    worker_settings: WorkerSettings,
    sender: OutboundServer,
) -> None:
    from senpilot.inbound import CLARIFICATION, IncomingEmail

    await store.enqueue(
        IncomingEmail("tester@example.com", "Request", "<request@example.com>", None, CLARIFICATION)
    )
    settings = replace(worker_settings, sendgrid_api_base=f"http://127.0.0.1:{sender.server_port}")
    async with httpx.AsyncClient() as client:
        assert await Worker(
            settings, store, artifacts, client, download_base_url="http://127.0.0.1:1"
        ).run_once()
    assert "both" in sender.received[0]["content"][0]["value"]
    assert not sender.received[0].get("attachments")


async def test_uncertain_delivery_is_not_automatically_repeated(
    store: JobStore,
    artifacts: ArtifactStore,
    worker_settings: WorkerSettings,
    sender: OutboundServer,
) -> None:
    from senpilot.inbound import CLARIFICATION, IncomingEmail

    job_id = await store.enqueue(
        IncomingEmail("tester@example.com", "Request", "<request@example.com>", None, CLARIFICATION)
    )
    sender.status = 503
    settings = replace(worker_settings, sendgrid_api_base=f"http://127.0.0.1:{sender.server_port}")
    async with httpx.AsyncClient() as client:
        worker = Worker(settings, store, artifacts, client)
        await worker.run_once()
        assert await store.state(job_id) == "uncertain"
        assert not await worker.run_once()
    assert len(sender.received) == 1


async def test_retention_cleans_upload_interrupted_before_preparation(
    store: JobStore,
    artifacts: ArtifactStore,
    worker_settings: WorkerSettings,
    sender: OutboundServer,
    fixture_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await store.enqueue(incoming())
    original = store.prepare

    async def fail_prepare(*args: object, **kwargs: object) -> None:
        raise ConnectionError("simulated interruption after upload")

    monkeypatch.setattr(store, "prepare", fail_prepare)
    settings = replace(worker_settings, sendgrid_api_base=f"http://127.0.0.1:{sender.server_port}")
    async with httpx.AsyncClient() as http:
        await Worker(settings, store, artifacts, http, download_base_url=fixture_url).run_once()
        monkeypatch.setattr(store, "prepare", original)
        async with await psycopg.AsyncConnection.connect(
            settings.database_url, autocommit=True
        ) as db:
            await db.execute(
                "UPDATE senpilot_private.jobs SET available_at=now()-interval '1 second' "
                "WHERE id=%s",
                (job_id,),
            )
        worker = Worker(settings, store, artifacts, http, download_base_url=fixture_url)
        await worker.run_once()
        assert await store.state(job_id) == "sent"
        async with await psycopg.AsyncConnection.connect(
            settings.database_url, autocommit=True
        ) as db:
            await db.execute(
                "UPDATE senpilot_private.jobs SET updated_at=now()-interval '8 days' WHERE id=%s",
                (job_id,),
            )
        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
        )
        assert (
            len(
                client.list_objects_v2(Bucket=settings.bucket, Prefix=str(job_id)).get(
                    "Contents", []
                )
            )
            == 2
        )
        await worker.cleanup()
        assert not client.list_objects_v2(Bucket=settings.bucket, Prefix=str(job_id)).get(
            "Contents", []
        )
        client.close()


async def test_signed_email_receives_zip_after_site_layout_recovery(
    store: JobStore,
    artifacts: ArtifactStore,
    worker_settings: WorkerSettings,
    sender: OutboundServer,
    redesigned_site: tuple[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    import senpilot.worker as worker_module
    from senpilot.models import DownloadRequest, DownloadResult
    from senpilot.recovery import BrowserRecovery
    from tests.recovery_fixtures import scripted_model

    real_download = worker_module.download_matter

    async def quick_download(
        request: DownloadRequest,
        root: Path,
        *,
        base_url: str,
        recovery: BrowserRecovery | None = None,
    ) -> DownloadResult:
        return await real_download(
            request, root, base_url=base_url, timeout_ms=200, recovery=recovery
        )

    monkeypatch.setattr(worker_module, "download_matter", quick_download)
    body, headers, public = signed_email("Please send Key Documents for M12205")
    app = create_app(
        WebSettings(TEST_DATABASE, "agent@example.com", frozenset({"*"}), public), store
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api,
    ):
        response = await api.post("/webhooks/sendgrid/inbound", content=body, headers=headers)
        assert response.status_code == 200
    settings = replace(
        worker_settings,
        openai_api_key="test-key",
        sendgrid_api_base=f"http://127.0.0.1:{sender.server_port}",
    )
    async with httpx.AsyncClient() as outbound:

        async def route(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.openai.com":
                return scripted_model(request)
            return await outbound.send(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
            assert await Worker(
                settings, store, artifacts, client, download_base_url=redesigned_site[0]
            ).run_once()
    assert len(sender.received) == 1
    reply = sender.received[0]
    assert "Halifax Regional Water Commission" in reply["content"][0]["value"]
    assert "attachments" in reply
    with zipfile.ZipFile(BytesIO(base64.b64decode(reply["attachments"][0]["content"]))) as archive:
        assert len(archive.namelist()) == 2
        assert archive.testzip() is None
