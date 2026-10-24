"""Private Supabase Storage artifacts through its S3-compatible interface."""

import asyncio
import hashlib
from pathlib import Path
from typing import Protocol, TypedDict, cast

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from senpilot.config import WorkerSettings
from senpilot.replies import MAX_ATTACHMENT_BYTES


class ObjectHead(TypedDict):
    ContentLength: int
    Metadata: dict[str, str]


class S3Client(Protocol):
    def upload_file(
        self,
        Filename: str,
        Bucket: str,
        Key: str,
        *,
        ExtraArgs: dict[str, object],
        Config: TransferConfig,
    ) -> None: ...
    def download_file(
        self, Bucket: str, Key: str, Filename: str, *, Config: TransferConfig
    ) -> None: ...
    def head_object(self, *, Bucket: str, Key: str) -> ObjectHead: ...
    def delete_object(self, *, Bucket: str, Key: str) -> object: ...
    def close(self) -> None: ...


class ArtifactStore:
    def __init__(self, settings: WorkerSettings) -> None:
        self.bucket = settings.bucket
        # boto3 exposes service-specific methods through a dynamically generated client.
        self.client = cast(
            S3Client,
            boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint,
                region_name=settings.s3_region,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                    retries={"mode": "standard", "max_attempts": 3},
                    connect_timeout=10,
                    read_timeout=60,
                ),
            ),
        )
        self.transfer = TransferConfig(multipart_threshold=8 * 1024 * 1024, max_concurrency=2)

    async def put(self, key: str, path: Path) -> None:
        if path.stat().st_size > MAX_ATTACHMENT_BYTES:
            raise ValueError("artifact exceeds attachment limit")

        def upload() -> None:
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            self.client.upload_file(
                str(path),
                self.bucket,
                key,
                ExtraArgs={"ContentType": "application/zip", "Metadata": {"sha256": digest}},
                Config=self.transfer,
            )

        await asyncio.to_thread(upload)

    async def get(self, key: str, path: Path) -> None:
        def download() -> None:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            if head["ContentLength"] > MAX_ATTACHMENT_BYTES:
                raise ValueError("stored artifact exceeds attachment limit")
            self.client.download_file(self.bucket, key, str(path), Config=self.transfer)
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if digest != head.get("Metadata", {}).get("sha256"):
                path.unlink(missing_ok=True)
                raise ValueError("stored artifact integrity check failed")

        await asyncio.to_thread(download)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

    def close(self) -> None:
        self.client.close()
