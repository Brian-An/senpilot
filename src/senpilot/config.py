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
    cloud_run_job: str = ""
    dispatch_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.cloud_run_job and not self.dispatch_token:
            return
        if (
            not re.fullmatch(
                r"projects/[a-z0-9-]+/locations/[a-z0-9-]+/jobs/[a-z][a-z0-9-]*",
                self.cloud_run_job,
            )
            or len(self.dispatch_token) < 32
        ):
            raise ValueError(
                "Cloud Run requires SENPILOT_CLOUD_RUN_JOB and a 32-character DISPATCH_TOKEN"
            )

    @classmethod
    def from_env(cls) -> "WebSettings":
        allowed = required("ALLOWED_SENDERS")
        senders = (
            frozenset({"*"})
            if allowed == "*"
            else frozenset(address(v.strip()) for v in allowed.split(","))
        )
        return cls(
            required("DATABASE_URL"),
            address(required("AGENT_EMAIL")),
            senders,
            required("SENDGRID_INBOUND_PUBLIC_KEY"),
            os.environ.get("SENPILOT_CLOUD_RUN_JOB", ""),
            os.environ.get("DISPATCH_TOKEN", ""),
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
    openai_api_key: str = field(default="", repr=False)
    openai_model: str = "gpt-5.6-terra"

    def __post_init__(self) -> None:
        if self.openai_api_key and not self.openai_model.strip():
            raise ValueError("OPENAI_MODEL is required when OPENAI_API_KEY is configured")

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
            os.environ.get("OPENAI_API_KEY", "").strip(),
            os.environ.get("OPENAI_MODEL", "gpt-5.6-terra").strip(),
        )
