"""Start finite Cloud Run executions using the attached service identity."""

import httpx

TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)


async def start_job(client: httpx.AsyncClient, job_name: str) -> None:
    identity = await client.get(TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=3)
    identity.raise_for_status()
    token = identity.json()["access_token"]
    if not isinstance(token, str) or not token:
        raise ValueError("service identity did not return an access token")
    response = await client.post(
        f"https://run.googleapis.com/v2/{job_name}:run",
        headers={"Authorization": f"Bearer {token}"},
        json={},
        timeout=10,
    )
    response.raise_for_status()
