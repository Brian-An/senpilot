"""Send threaded replies through Twilio SendGrid's HTTP API."""

import base64
from email import policy
from email.parser import BytesParser
from uuid import UUID

import httpx

from senpilot.config import address
from senpilot.replies import ComposedReply


class DeliveryRejected(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"SendGrid rejected the reply (HTTP {status})")
        self.retryable = status == 429


class DeliveryUncertain(RuntimeError):
    pass


async def send_reply(
    client: httpx.AsyncClient, api_base: str, api_key: str, reply: ComposedReply, job_id: UUID
) -> str:
    message = BytesParser(policy=policy.default).parsebytes(reply.mime)
    body = message.get_body(preferencelist=("plain",))
    if body is None:
        raise ValueError("reply must contain a plain text body")
    payload: dict[str, object] = {
        "personalizations": [{"to": [{"email": address(str(message["To"]))}]}],
        "from": {"email": address(str(message["From"]))},
        "subject": str(message["Subject"]),
        "content": [{"type": "text/plain", "value": body.get_content()}],
        "headers": {
            name: str(message[name])
            for name in ("In-Reply-To", "References", "Auto-Submitted", "Message-ID")
        },
        "custom_args": {"senpilot_job_id": str(job_id)},
        "tracking_settings": {
            "click_tracking": {"enable": False},
            "open_tracking": {"enable": False},
        },
    }
    attachments = []
    for attachment in message.iter_attachments():
        content = attachment.get_payload(decode=True)
        if not isinstance(content, bytes):
            raise ValueError("invalid email attachment")
        attachments.append(
            {
                "content": base64.b64encode(content).decode(),
                "filename": attachment.get_filename(),
                "type": "application/zip",
                "disposition": "attachment",
            }
        )
    if attachments:
        payload["attachments"] = attachments
    try:
        response = await client.post(
            api_base.rstrip("/") + "/v3/mail/send",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60,
        )
    except httpx.TransportError as exc:
        raise DeliveryUncertain("SendGrid acceptance could not be confirmed") from exc
    if 400 <= response.status_code < 500 and response.status_code != 408:
        raise DeliveryRejected(response.status_code)
    if response.status_code != 202 or not response.headers.get("X-Message-Id"):
        raise DeliveryUncertain("SendGrid acceptance could not be confirmed")
    return response.headers["X-Message-Id"]
