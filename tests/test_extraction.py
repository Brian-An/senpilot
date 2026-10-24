import json
from dataclasses import replace

import httpx
import pytest

from senpilot.config import WorkerSettings
from senpilot.extraction import extract_request
from senpilot.inbound import parse_inbound
from senpilot.models import DocumentType, DownloadRequest
from tests.test_email import payload


def response(data: object, *, status: str = "completed") -> dict[str, object]:
    return {
        "status": status,
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(data)}]}
        ],
    }


def test_preserves_only_clean_request_text_for_unresolved_emails() -> None:
    message = parse_inbound(
        payload("Please get the key docs for matter 12205\n> Old M12383 Exhibits"),
        "agent@example.com",
        {"tester@example.com"},
    )
    assert message is not None and message.request is None
    assert message.extraction_text == "Filing request\nPlease get the key docs for matter 12205\n"


async def test_extracts_validated_request_through_structured_responses(
    worker_settings: WorkerSettings,
) -> None:
    settings = replace(worker_settings, openai_api_key="private-test-key")
    text = "Please get the key docs for matter 12205"

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["Authorization"] == "Bearer private-test-key"
        body = json.loads(request.content)
        assert body["store"] is False
        assert body["input"] == [{"role": "user", "content": text}]
        assert body["text"]["format"]["strict"] is True
        assert "tools" not in body
        return httpx.Response(
            200,
            json=response(
                {
                    "matter_number": "M12205",
                    "document_type": "Key Documents",
                    "needs_clarification": False,
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await extract_request(client, settings, text)
    assert result == DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS, 10)


@pytest.mark.parametrize(
    "data",
    [
        {"matter_number": "M12383", "document_type": "Key Documents", "needs_clarification": False},
        {"matter_number": "M12205", "document_type": "All Documents", "needs_clarification": False},
        {
            "matter_number": "M12205",
            "document_type": "Key Documents",
            "needs_clarification": "false",
        },
        {"matter_number": "M12205", "document_type": "Key Documents", "needs_clarification": True},
        {"matter_number": None, "document_type": None, "needs_clarification": False},
        {
            "matter_number": "M12205",
            "document_type": "Key Documents",
            "needs_clarification": False,
            "recipient": "attacker@example.net",
        },
        [],
    ],
)
async def test_untrusted_model_output_cannot_change_scope(
    worker_settings: WorkerSettings, data: object
) -> None:
    settings = replace(worker_settings, openai_api_key="private-test-key")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response(data)))
    ) as client:
        assert await extract_request(client, settings, "key docs for 12205") is None


@pytest.mark.parametrize("text", ["key docs", "key docs for 12205 and M12383"])
async def test_missing_or_multiple_matters_cannot_be_guessed(
    worker_settings: WorkerSettings, text: str
) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("a missing or multiple matter request requires clarification")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        assert (
            await extract_request(
                client, replace(worker_settings, openai_api_key="private-test-key"), text
            )
            is None
        )


@pytest.mark.parametrize(
    "failure", ["timeout", "unauthorized", "rate_limit", "refusal", "incomplete", "malformed"]
)
async def test_ai_failure_safely_requires_clarification(
    worker_settings: WorkerSettings, failure: str
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("provider timeout", request=request)
        if failure in {"unauthorized", "rate_limit"}:
            return httpx.Response(401 if failure == "unauthorized" else 429)
        if failure == "refusal":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "refusal", "refusal": "Cannot extract"}],
                        }
                    ],
                },
            )
        if failure == "incomplete":
            return httpx.Response(200, json=response({}, status="incomplete"))
        return httpx.Response(200, text="not json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert (
            await extract_request(
                client,
                replace(worker_settings, openai_api_key="private-test-key"),
                "key docs for 12205",
            )
            is None
        )


async def test_unconfigured_ai_keeps_existing_clarification_behavior(
    worker_settings: WorkerSettings,
) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("unconfigured AI must not call the provider")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        assert await extract_request(client, worker_settings, "key docs for 12205") is None


def test_ai_settings_require_a_model_and_hide_the_api_key(
    worker_settings: WorkerSettings,
) -> None:
    settings = replace(worker_settings, openai_api_key="private-test-key")
    assert "private-test-key" not in repr(settings)
    with pytest.raises(ValueError, match="OPENAI_MODEL"):
        replace(settings, openai_model="")
