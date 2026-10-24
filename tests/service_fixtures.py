import os
from collections.abc import Iterator
from urllib.parse import urlsplit

import boto3
import psycopg
import pytest
from moto.server import ThreadedMotoServer

from senpilot.config import WorkerSettings
from senpilot.jobs import JobStore, migrate
from senpilot.storage import ArtifactStore

TEST_DATABASE = os.environ.get(
    "SENPILOT_TEST_DATABASE_URL",
    "postgresql://postgres:senpilot-test@127.0.0.1:55432/senpilot_test",
)


@pytest.fixture
async def store():
    target = urlsplit(TEST_DATABASE)
    if target.path != "/senpilot_test" or target.hostname not in {
        "localhost",
        "127.0.0.1",
        "senpilot-test-postgres",
    }:
        raise RuntimeError("Integration tests require the isolated local senpilot_test database")
    await migrate(TEST_DATABASE)
    async with await psycopg.AsyncConnection.connect(TEST_DATABASE, autocommit=True) as connection:
        await connection.execute("TRUNCATE senpilot_private.artifacts, senpilot_private.jobs")
    async with JobStore(TEST_DATABASE) as jobs:
        yield jobs


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
        TEST_DATABASE,
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
