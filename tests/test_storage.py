import hashlib
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto.server import ThreadedMotoServer

from senpilot.config import WorkerSettings
from senpilot.storage import ArtifactStore


@pytest.fixture(scope="session")
def s3_endpoint() -> Iterator[str]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def worker_settings(s3_endpoint: str) -> WorkerSettings:
    return WorkerSettings(
        "postgresql://postgres:senpilot-test@127.0.0.1:55432/senpilot_test",
        "agent@example.com",
        "test-api-key",
        s3_endpoint,
        "us-east-1",
        "test-access-key",
        "test-secret-key",
    )


@pytest.fixture
def artifacts(worker_settings: WorkerSettings) -> ArtifactStore:
    client = boto3.client(
        "s3",
        endpoint_url=worker_settings.s3_endpoint,
        region_name="us-east-1",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
    )
    client.create_bucket(Bucket=worker_settings.bucket)
    return ArtifactStore(worker_settings)


async def test_private_multipart_upload_and_verified_download(
    artifacts: ArtifactStore, tmp_path: Path
) -> None:
    source = tmp_path / "filings.zip"
    source.write_bytes(b"x" * (9 * 1024 * 1024))
    await artifacts.put("job/filings.zip", source)
    restored = tmp_path / "restored.zip"
    await artifacts.get("job/filings.zip", restored)
    with restored.open("rb") as contents:
        assert (
            hashlib.file_digest(contents, "sha256").hexdigest()
            == hashlib.sha256(source.read_bytes()).hexdigest()
        )
    await artifacts.delete("job/filings.zip")
    with pytest.raises(ClientError):
        await artifacts.get("job/filings.zip", tmp_path / "missing.zip")


async def test_corrupt_stored_artifact_is_not_emailed(
    artifacts: ArtifactStore, worker_settings: WorkerSettings, tmp_path: Path
) -> None:
    client = boto3.client(
        "s3",
        endpoint_url=worker_settings.s3_endpoint,
        region_name="us-east-1",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
    )
    client.put_object(
        Bucket=worker_settings.bucket,
        Key="job/corrupt.zip",
        Body=b"corrupt",
        Metadata={"sha256": "0" * 64},
    )
    with pytest.raises(ValueError, match="integrity"):
        await artifacts.get("job/corrupt.zip", tmp_path / "corrupt.zip")


async def test_oversized_artifact_is_not_uploaded(artifacts: ArtifactStore, tmp_path: Path) -> None:
    path = tmp_path / "large.zip"
    with path.open("wb") as source:
        source.truncate(26_000_000)
    with pytest.raises(ValueError, match="attachment"):
        await artifacts.put("job/large.zip", path)
