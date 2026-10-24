import hashlib
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError

from senpilot.config import WorkerSettings
from senpilot.storage import ArtifactStore


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
