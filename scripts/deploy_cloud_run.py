"""Deploy Senpilot from an ignored local environment using an authenticated gcloud CLI."""

import argparse
import asyncio
import base64
import json
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from senpilot.config import WebSettings, WorkerSettings
from senpilot.jobs import migrate

ROOT = Path(__file__).resolve().parents[1]
API_SECRETS = ("DATABASE_URL", "DISPATCH_TOKEN")
WORKER_SECRETS = ("DATABASE_URL", "SENDGRID_API_KEY", "SUPABASE_S3_SECRET_ACCESS_KEY")


def command(
    args: list[str], *, flags: dict[str, object] | None = None, input_text: str | None = None
) -> str:
    with tempfile.TemporaryDirectory(prefix="senpilot-deploy-") as temporary:
        if flags:
            path = Path(temporary) / "flags.json"
            path.write_text(json.dumps(flags))
            path.chmod(0o600)
            args = [*args, f"--flags-file={path}"]
        result = subprocess.run(
            args, input=input_text, capture_output=True, text=True, cwd=ROOT, check=False
        )
        if result.returncode:
            # CLI errors can echo parsed secret flags or input; keep diagnostics credential-free.
            raise RuntimeError(f"{' '.join(args[:3])} failed with exit code {result.returncode}")
        return result.stdout.strip()


def secret_name(key: str) -> str:
    return "senpilot-" + key.lower().replace("_", "-")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--worker-memory", default="4Gi")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", args.project):
        raise ValueError("invalid Google project ID")
    if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]", args.region):
        raise ValueError("invalid Google region")
    if not re.fullmatch(r"[1-4]Gi", args.worker_memory):
        raise ValueError("worker memory must be between 1Gi and 4Gi for the configured CPU")
    web = WebSettings.from_env()
    worker = WorkerSettings.from_env()
    values = {
        "DATABASE_URL": worker.database_url,
        "SENDGRID_API_KEY": worker.sendgrid_api_key,
        "SUPABASE_S3_SECRET_ACCESS_KEY": worker.s3_secret_key,
    }
    configuration = {
        **values,
        "SENDGRID_INBOUND_PUBLIC_KEY": web.public_key,
        "SUPABASE_S3_ENDPOINT": worker.s3_endpoint,
        "SUPABASE_REGION": worker.s3_region,
        "SUPABASE_S3_ACCESS_KEY_ID": worker.s3_access_key,
        "ALLOWED_SENDERS": ",".join(web.allowed_senders),
    }
    for key, value in configuration.items():
        if any(
            marker in value
            for marker in (
                "REPLACE_WITH",
                "PROJECT_REF",
                "PASSWORD",
                "POOLER_HOST",
                "your-test-address",
            )
        ):
            raise ValueError(f"{key} still contains an example placeholder")
    try:
        public_key = serialization.load_der_public_key(
            base64.b64decode(web.public_key, validate=True)
        )
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "SENDGRID_INBOUND_PUBLIC_KEY must be a base64 DER elliptic-curve key"
        ) from exc
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise ValueError("SENDGRID_INBOUND_PUBLIC_KEY must be an elliptic-curve key")

    def gcloud(*parts: str, flags: dict[str, object] | None = None, data: str | None = None) -> str:
        return command(
            ["gcloud", *parts, f"--project={args.project}", "--quiet"],
            flags=flags,
            input_text=data,
        )

    if not gcloud("auth", "list", "--filter=status:ACTIVE", "--format=value(account)"):
        raise RuntimeError("Sign in with gcloud auth login before deploying")
    if (
        gcloud(
            "billing", "projects", "describe", args.project, "--format=value(billingEnabled)"
        ).lower()
        != "true"
    ):
        raise RuntimeError("The Google project requires an enabled billing account")
    print("Enabling Cloud Run, Artifact Registry, Secret Manager, and Scheduler", flush=True)
    gcloud(
        "services",
        "enable",
        "run.googleapis.com",
        "artifactregistry.googleapis.com",
        "secretmanager.googleapis.com",
        "cloudscheduler.googleapis.com",
    )
    accounts = gcloud("iam", "service-accounts", "list", "--format=value(email)").splitlines()
    api_account = f"senpilot-api@{args.project}.iam.gserviceaccount.com"
    worker_account = f"senpilot-worker@{args.project}.iam.gserviceaccount.com"
    for account in (api_account, worker_account):
        if account not in accounts:
            gcloud("iam", "service-accounts", "create", account.split("@", 1)[0])

    existing = gcloud("secrets", "list", "--format=value(name)").splitlines()
    existing = [name.rsplit("/", 1)[-1] for name in existing]
    recovery_name = secret_name("DISPATCH_TOKEN")
    values["DISPATCH_TOKEN"] = web.dispatch_token or (
        gcloud("secrets", "versions", "access", "latest", f"--secret={recovery_name}")
        if recovery_name in existing
        else secrets.token_urlsafe(32)
    )
    for key, value in values.items():
        name = secret_name(key)
        if name not in existing:
            gcloud("secrets", "create", name, "--replication-policy=automatic")
        gcloud("secrets", "versions", "add", name, "--data-file=-", data=value)
        for account, keys in ((api_account, API_SECRETS), (worker_account, WORKER_SECRETS)):
            if key in keys:
                gcloud(
                    "secrets",
                    "add-iam-policy-binding",
                    name,
                    f"--member=serviceAccount:{account}",
                    "--role=roles/secretmanager.secretAccessor",
                )

    repositories = gcloud(
        "artifacts", "repositories", "list", f"--location={args.region}", "--format=value(name)"
    ).splitlines()
    if not any(name.rsplit("/", 1)[-1] == "senpilot" for name in repositories):
        gcloud(
            "artifacts",
            "repositories",
            "create",
            "senpilot",
            "--repository-format=docker",
            f"--location={args.region}",
        )
    revision = command(["git", "rev-parse", "--short=12", "HEAD"])
    registry = f"{args.region}-docker.pkg.dev"
    image = f"{registry}/{args.project}/senpilot/app:{revision}"
    print("Building and pushing the Linux amd64 container locally", flush=True)
    gcloud("auth", "configure-docker", registry)
    command(["docker", "buildx", "build", "--platform=linux/amd64", "--load", "--tag", image, "."])
    command(["docker", "push", image])
    print("Initializing the private queue and deploying the finite worker", flush=True)
    asyncio.run(migrate(worker.database_url))
    common = {"--image": image, "--region": args.region, "--cpu": "1"}
    gcloud(
        "run",
        "jobs",
        "deploy",
        "senpilot-worker",
        flags={
            **common,
            "--service-account": worker_account,
            "--memory": args.worker_memory,
            "--tasks": 1,
            "--parallelism": 1,
            "--max-retries": 0,
            "--task-timeout": "3600s",
            "--command": "/usr/bin/tini",
            "--args": ["--", "/app/.venv/bin/senpilot", "worker", "--drain"],
            "--set-env-vars": {
                "AGENT_EMAIL": worker.agent_email,
                "SUPABASE_S3_ENDPOINT": worker.s3_endpoint,
                "SUPABASE_REGION": worker.s3_region,
                "SUPABASE_S3_ACCESS_KEY_ID": worker.s3_access_key,
                "SUPABASE_STORAGE_BUCKET": worker.bucket,
            },
            "--set-secrets": {key: f"{secret_name(key)}:latest" for key in WORKER_SECRETS},
        },
    )
    gcloud(
        "run",
        "jobs",
        "add-iam-policy-binding",
        "senpilot-worker",
        f"--region={args.region}",
        f"--member=serviceAccount:{api_account}",
        "--role=roles/run.invoker",
    )
    print("Deploying the webhook with zero minimum instances", flush=True)
    gcloud(
        "run",
        "deploy",
        "senpilot-api",
        flags={
            **common,
            "--service-account": api_account,
            "--memory": "512Mi",
            "--port": 8080,
            "--min-instances": 0,
            "--max-instances": 1,
            "--concurrency": 4,
            "--timeout": "60s",
            "--allow-unauthenticated": True,
            "--cpu-throttling": True,
            "--no-cpu-boost": True,
            "--startup-probe": {
                "httpGet.path": "/ready",
                "httpGet.port": 8080,
                "periodSeconds": 10,
                "timeoutSeconds": 5,
                "failureThreshold": 24,
            },
            "--set-env-vars": {
                "AGENT_EMAIL": web.agent_email,
                "ALLOWED_SENDERS": ",".join(sorted(web.allowed_senders)),
                "SENDGRID_INBOUND_PUBLIC_KEY": web.public_key,
                "SENPILOT_CLOUD_RUN_JOB": (
                    f"projects/{args.project}/locations/{args.region}/jobs/senpilot-worker"
                ),
            },
            "--set-secrets": {key: f"{secret_name(key)}:latest" for key in API_SECRETS},
        },
    )
    url = gcloud(
        "run",
        "services",
        "describe",
        "senpilot-api",
        f"--region={args.region}",
        "--format=value(status.url)",
    )
    schedules = gcloud(
        "scheduler", "jobs", "list", f"--location={args.region}", "--format=value(name)"
    ).splitlines()
    exists = any(name.rsplit("/", 1)[-1] == "senpilot-recovery" for name in schedules)
    print("Configuring recovery checks without launching idle worker jobs", flush=True)
    gcloud(
        "scheduler",
        "jobs",
        "update" if exists else "create",
        "http",
        "senpilot-recovery",
        flags={
            "--location": args.region,
            "--schedule": "*/5 * * * *",
            "--time-zone": "Etc/UTC",
            "--uri": f"{url}/internal/dispatch",
            "--http-method": "POST",
            "--attempt-deadline": "60s",
            "--update-headers" if exists else "--headers": {
                "Authorization": f"Bearer {values['DISPATCH_TOKEN']}"
            },
        },
    )
    print(f"API deployed: {url}\nSendGrid destination: {url}/webhooks/sendgrid/inbound", flush=True)


if __name__ == "__main__":
    main()
