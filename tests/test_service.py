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
from senpilot.worker import Worker
from tests.test_jobs import incoming


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
        assert (await api.get("/readyz")).status_code == 200
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
