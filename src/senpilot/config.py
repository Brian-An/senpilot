"""Server-side configuration loaded from the deployment environment."""

import os
import re
from dataclasses import dataclass, field
from email.utils import parseaddr


def address(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("email address contains invalid characters")
    result = parseaddr(value, strict=True)[1].lower()
    if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", result):
        raise ValueError("invalid email address")
    return result


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True)
class WebSettings:
    database_url: str = field(repr=False)
    agent_email: str
    allowed_senders: frozenset[str]
    public_key: str = field(repr=False)

    @classmethod
    def from_env(cls) -> "WebSettings":
        senders = frozenset(address(v.strip()) for v in required("ALLOWED_SENDERS").split(","))
        return cls(
            required("DATABASE_URL"),
            address(required("AGENT_EMAIL")),
            senders,
            required("SENDGRID_INBOUND_PUBLIC_KEY"),
        )


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str = field(repr=False)
    agent_email: str
    sendgrid_api_key: str = field(repr=False)
    s3_endpoint: str
    s3_region: str
    s3_access_key: str = field(repr=False)
    s3_secret_key: str = field(repr=False)
    bucket: str = "senpilot-filings"
    sendgrid_api_base: str = "https://api.sendgrid.com"

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        return cls(
            required("DATABASE_URL"),
            address(required("AGENT_EMAIL")),
            required("SENDGRID_API_KEY"),
            required("SUPABASE_S3_ENDPOINT"),
            required("SUPABASE_REGION"),
            required("SUPABASE_S3_ACCESS_KEY_ID"),
            required("SUPABASE_S3_SECRET_ACCESS_KEY"),
            os.environ.get("SUPABASE_STORAGE_BUCKET", "senpilot-filings"),
            os.environ.get("SENDGRID_API_BASE", "https://api.sendgrid.com"),
        )
