"""Create grounded, threaded email replies and enforce encoded message limits."""

from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

MAX_MESSAGE_BYTES = 25_000_000
MAX_ATTACHMENT_BYTES = 18_000_000
TOO_LARGE = "The ZIP is too large to attach through email. No attachment was sent."


@dataclass(frozen=True)
class ComposedReply:
    mime: bytes
    attached: bool


def compose_reply(
    agent: str,
    recipient: str,
    subject: str,
    in_reply_to: str,
    body: str,
    attachment: Path | None = None,
    *,
    message_id: str | None = None,
) -> ComposedReply:
    oversized = attachment is not None and attachment.stat().st_size > MAX_ATTACHMENT_BYTES
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = agent
    message["To"] = recipient
    message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    message["In-Reply-To"] = in_reply_to
    message["References"] = in_reply_to
    message["Message-ID"] = message_id or make_msgid(domain=agent.split("@", 1)[1])
    message["Auto-Submitted"] = "auto-replied"
    message.set_content(body + (f"\n\n{TOO_LARGE}" if oversized else ""))
    attached = attachment is not None and not oversized
    if attached and attachment is not None:
        message.add_attachment(
            attachment.read_bytes(), maintype="application", subtype="zip", filename=attachment.name
        )
    mime = message.as_bytes()
    if len(mime) > MAX_MESSAGE_BYTES:
        if attachment is not None:
            return compose_reply(
                agent,
                recipient,
                subject,
                in_reply_to,
                body + f"\n\n{TOO_LARGE}",
                message_id=message_id,
            )
        raise ValueError("reply exceeds email message size limit")
    return ComposedReply(mime, attached)


def summarize_manifest(manifest: dict[str, object]) -> str:
    matter = manifest.get("matter")
    if not isinstance(matter, dict):
        return "The requested matter could not be retrieved. Please try again later."
    lines = [
        f"{matter.get('matter_number')}: {matter.get('title')}",
        f"Type: {matter.get('matter_type')}. Category: {matter.get('category')}.",
        f"Status: {matter.get('status')}. Outcome: {matter.get('outcome')}.",
    ]
    dates = matter.get("dates", {})
    if isinstance(dates, dict):
        lines.extend(f"{label}: {value or 'not provided'}." for label, value in dates.items())
    counts = matter.get("category_counts", {})
    if isinstance(counts, dict):
        lines.append("Category counts: " + ", ".join(f"{v} {k}" for k, v in counts.items()) + ".")
        lines.append("Category entries can overlap and are not a unique-file total.")
    request = manifest.get("request", {})
    if isinstance(request, dict) and isinstance(counts, dict):
        category = str(request.get("document_type", "documents"))
        lines.append(
            f"Downloaded {manifest.get('downloaded_count', 0)} of "
            f"{counts.get(category, 0)} {category} in website displayed order."
        )
    if manifest.get("status") in {"partial", "failed"}:
        lines.append(
            "Some requested files could not be retrieved; only verified files are included."
        )
    return "\n".join(lines)
