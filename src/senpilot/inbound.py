"""Authenticate SendGrid requests and interpret incoming email requests."""

import base64
import binascii
import json
import re
import time
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser, Parser

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from senpilot.config import address
from senpilot.models import DocumentType, DownloadRequest

CLARIFICATION = (
    "Please reply with both a matter number (for example M12205) and one document type: "
    "Exhibits, Key Documents, Other Documents, Transcripts, or Recordings. "
    "Please request one matter and one type per email."
)


@dataclass(frozen=True)
class IncomingEmail:
    sender: str
    subject: str
    message_id: str
    request: DownloadRequest | None
    clarification: str | None


def verify_signature(
    body: bytes, timestamp: str, signature: str, key: str, *, now: float | None = None
) -> None:
    if not re.fullmatch(r"[0-9]{1,12}", timestamp):
        raise ValueError("invalid webhook timestamp")
    age = (time.time() if now is None else now) - int(timestamp)
    # A bounded retry window and message deduplication permit delayed provider retries.
    if age < -300 or age > 72 * 3600:
        raise ValueError("webhook timestamp is outside the accepted window")
    try:
        public_key = serialization.load_der_public_key(base64.b64decode(key, validate=True))
        if not isinstance(public_key, ec.EllipticCurvePublicKey):
            raise ValueError("webhook key must be an elliptic curve public key")
        public_key.verify(
            base64.b64decode(signature, validate=True),
            timestamp.encode() + body,
            ec.ECDSA(hashes.SHA256()),
        )
    except (InvalidSignature, ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("invalid webhook signature") from exc


def parse_sendgrid(
    body: bytes, content_type: str, agent_email: str, allowed_senders: frozenset[str]
) -> IncomingEmail | None:
    if not content_type.lower().startswith("multipart/form-data;") or any(
        c in content_type for c in "\r\n"
    ):
        raise ValueError("multipart form data is required")
    form = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    if not form.is_multipart():
        raise ValueError("invalid multipart payload")
    raw_fields: dict[str, bytes] = {}
    for part in form.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if isinstance(name, str) and part.get_filename() is None:
            data = part.get_payload(decode=True)
            if isinstance(data, bytes):
                raw_fields[name] = data
    charsets = json.loads(raw_fields.get("charsets", b"{}").decode("utf-8"))
    if not isinstance(charsets, dict):
        raise ValueError("invalid charset metadata")
    fields: dict[str, str] = {}
    for name, data in raw_fields.items():
        charset = charsets.get(name, "utf-8")
        if not isinstance(charset, str):
            raise ValueError("invalid field charset")
        try:
            fields[name] = data.decode(charset)
        except LookupError as exc:
            raise ValueError("unsupported field charset") from exc
    envelope = json.loads(fields.get("envelope", "{}"))
    if not isinstance(envelope, dict) or not isinstance(envelope.get("to"), list):
        raise ValueError("invalid email envelope")
    recipients = [address(v) for v in envelope["to"] if isinstance(v, str)]
    if agent_email not in recipients:
        return None
    headers = Parser(policy=policy.default).parsestr(fields.get("headers", ""))
    if len(headers.get_all("From", [])) != 1:
        raise ValueError("one From header is required")
    sender = address(str(headers["From"]))
    envelope_sender = address(str(envelope.get("from", "")))
    sender_domain = sender.rsplit("@", 1)[1]
    envelope_domain = envelope_sender.rsplit("@", 1)[1]

    def aligned(domain: str) -> bool:
        return (
            domain == sender_domain
            or domain.endswith("." + sender_domain)
            or sender_domain.endswith("." + domain)
        )

    spf = fields.get("SPF", "").strip().lower() == "pass" and aligned(envelope_domain)
    # SendGrid's DKIM result uses a domain/status map that can omit JSON quotes.
    dkim = False
    for entry in fields.get("dkim", "{}").strip("{}").split(","):
        domain, separator, result = entry.partition(":")
        domain = domain.strip(" \"'@").lower()
        if separator and result.strip(" \"'").lower() == "pass" and domain and aligned(domain):
            dkim = True
    if not (spf or dkim):
        return None
    canonical: dict[str, object] = {
        "sender": sender,
        "recipient": agent_email,
        "subject": fields.get("subject", ""),
        "body-plain": fields.get("text", ""),
        "message-headers": [[k, " ".join(str(v).splitlines())] for k, v in headers.items()],
    }
    return parse_inbound(canonical, agent_email, allowed_senders)


def _text(payload: dict[str, object], name: str, limit: int = 20_000) -> str:
    value = payload.get(name, "")
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"invalid {name} field")
    return value


def parse_inbound(
    payload: dict[str, object], agent_email: str, allowed_senders: set[str] | frozenset[str]
) -> IncomingEmail | None:
    sender = address(_text(payload, "sender", 512))
    recipient = address(_text(payload, "recipient", 512))
    if sender == agent_email or sender not in allowed_senders or recipient != agent_email:
        return None
    headers: dict[str, str] = {}
    raw_headers = payload.get("message-headers", [])
    if not isinstance(raw_headers, list):
        raise ValueError("message headers must be a list")
    for pair in raw_headers:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(v, str) for v in pair)
        ):
            raise ValueError("invalid message header")
        name, value = pair
        if "\r" in value or "\n" in value:
            raise ValueError("invalid message header value")
        headers[name.lower()] = value
    if headers.get("auto-submitted", "no").lower() != "no":
        return None
    if headers.get("precedence", "").lower() in {"bulk", "list", "junk"} or "list-id" in headers:
        return None
    message_id = headers.get("message-id", "")
    if not re.fullmatch(r"<[^<>\s]{1,500}>", message_id):
        raise ValueError("a valid Message-Id header is required")
    subject = _text(payload, "subject", 500)
    if "\n" in subject or "\r" in subject:
        raise ValueError("invalid subject")
    body = (
        _text(payload, "stripped-text")
        if "stripped-text" in payload
        else _text(payload, "body-plain")
    )
    body = re.split(r"(?mi)^\s*>|^On .+wrote:\s*$|^-{2,}\s*Original Message", body)[0]
    text = subject + "\n" + body
    matters = {m.upper() for m in re.findall(r"\bM[0-9]{5}\b", text, flags=re.I)}
    categories = [
        category
        for category in DocumentType
        if re.search(rf"\b{re.escape(category.value)}\b", text, flags=re.I)
    ]
    request = None
    if len(matters) == 1 and len(categories) == 1:
        request = DownloadRequest(matters.pop(), categories[0])
    return IncomingEmail(sender, subject, message_id, request, None if request else CLARIFICATION)
