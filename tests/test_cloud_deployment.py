import base64
import os
import runpy
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_cloud_run.py"


def test_deployment_transmits_secrets_through_private_files_and_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(SCRIPT))
    command = cast(Callable[..., str], namespace["command"])
    secret = "private-recovery-header-value"

    def execute(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert all(secret not in arg for arg in args)
        flags = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--flags-file=")))
        assert flags.stat().st_mode & 0o077 == 0
        assert secret in flags.read_text()
        assert kwargs["input"] == secret
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", execute)
    assert (
        command(
            ["gcloud", "scheduler", "jobs", "create", "http", "recovery"],
            flags={"--headers": {"Authorization": f"Bearer {secret}"}},
            input_text=secret,
        )
        == "ok"
    )


def test_incomplete_deployment_configuration_cannot_modify_cloud_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(SCRIPT))
    main = cast(Callable[[list[str]], None], namespace["main"])
    for key in list(os.environ):
        if key in {"DATABASE_URL", "SENDGRID_API_KEY"}:
            monkeypatch.delenv(key)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("incomplete configuration must fail before cloud mutations")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="is required"):
        main(["--project", "senpilot-demo"])


@pytest.mark.parametrize(
    "key,invalid",
    [
        ("SENDGRID_INBOUND_PUBLIC_KEY", "REPLACE_WITH_VALUE"),
        ("SENDGRID_INBOUND_PUBLIC_KEY", "invalid-base64-key"),
        ("SUPABASE_S3_ENDPOINT", "REPLACE_WITH_VALUE"),
        ("SUPABASE_REGION", "REPLACE_WITH_VALUE"),
        ("SUPABASE_S3_ACCESS_KEY_ID", "REPLACE_WITH_VALUE"),
        ("OPENAI_API_KEY", "REPLACE_WITH_VALUE"),
    ],
)
def test_placeholder_credentials_are_rejected_before_creating_cloud_resources(
    monkeypatch: pytest.MonkeyPatch, key: str, invalid: str
) -> None:
    public = base64.b64encode(
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ).decode()
    values = {
        "DATABASE_URL": "postgresql://postgres:secret@localhost/postgres",
        "AGENT_EMAIL": "agent@requests.brianan.ca",
        "ALLOWED_SENDERS": "tester@example.com",
        "SENDGRID_INBOUND_PUBLIC_KEY": public,
        "SENDGRID_API_KEY": "test-sendgrid-key",
        "SUPABASE_S3_ENDPOINT": "https://test.storage.supabase.co/storage/v1/s3",
        "SUPABASE_REGION": "ca-central-1",
        "SUPABASE_S3_ACCESS_KEY_ID": "test-access-key",
        "SUPABASE_S3_SECRET_ACCESS_KEY": "test-secret-key",
        "OPENAI_API_KEY": "test-openai-key",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, invalid if name == key else value)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("example placeholders must fail before cloud mutations")

    namespace = runpy.run_path(str(SCRIPT))
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ValueError, match=key):
        cast(Callable[[list[str]], None], namespace["main"])(["--project", "senpilot-demo"])


def test_ai_credential_is_deployed_only_to_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    public = base64.b64encode(
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ).decode()
    for key, value in {
        "DATABASE_URL": "postgresql://postgres:secret@localhost/postgres",
        "AGENT_EMAIL": "agent@example.com",
        "ALLOWED_SENDERS": "*",
        "SENDGRID_INBOUND_PUBLIC_KEY": public,
        "SENDGRID_API_KEY": "test-sendgrid-key",
        "SUPABASE_S3_ENDPOINT": "https://test.storage.supabase.co/storage/v1/s3",
        "SUPABASE_REGION": "ca-central-1",
        "SUPABASE_S3_ACCESS_KEY_ID": "test-access",
        "SUPABASE_S3_SECRET_ACCESS_KEY": "test-secret",
        "OPENAI_API_KEY": "private-ai-key",
        "OPENAI_MODEL": "gpt-5.6-terra",
    }.items():
        monkeypatch.setenv(key, value)
    calls: list[tuple[list[str], dict[str, object], str | None]] = []

    def execute(args: list[str], *, flags=None, input_text=None) -> str:
        calls.append((args, flags or {}, input_text))
        if args[1:3] == ["auth", "list"]:
            return "tester@example.com"
        if args[1:4] == ["billing", "projects", "describe"]:
            return "true"
        return ""

    async def migrate(url: str) -> None:
        pass

    namespace = runpy.run_path(str(SCRIPT))
    main = cast(Callable[[list[str]], None], namespace["main"])
    monkeypatch.setitem(main.__globals__, "command", execute)
    monkeypatch.setitem(main.__globals__, "migrate", migrate)
    main(["--project", "senpilot-demo"])
    worker = next(flags for args, flags, _ in calls if args[1:4] == ["run", "jobs", "deploy"])
    api = next(flags for args, flags, _ in calls if args[1:3] == ["run", "deploy"])
    assert worker["--set-secrets"]["OPENAI_API_KEY"] == "senpilot-openai-api-key:latest"
    assert worker["--set-env-vars"]["OPENAI_MODEL"] == "gpt-5.6-terra"
    assert "OPENAI_API_KEY" not in api["--set-secrets"]
    for args, flags, data in calls:
        assert "private-ai-key" not in str(args) + str(flags)
        if data == "private-ai-key":
            assert args[1:4] == ["secrets", "versions", "add"]
    grants = [
        args
        for args, _, _ in calls
        if args[1:4] == ["secrets", "add-iam-policy-binding", "senpilot-openai-api-key"]
    ]
    assert len(grants) == 1
    assert (
        "--member=serviceAccount:senpilot-worker@senpilot-demo.iam.gserviceaccount.com" in grants[0]
    )
