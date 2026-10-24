import json
from pathlib import Path

import httpx
import pytest

from senpilot.models import DocumentType, DownloadRequest
from senpilot.pipeline import download_matter
from senpilot.recovery import BrowserRecovery
from tests.recovery_fixtures import scripted_model


@pytest.mark.parametrize(
    "scenario", ["normal", "delayed", "focusable", "direct", "deep_rows", "slowclose"]
)
async def test_recovers_when_source_classes_and_search_controls_change(
    redesigned_site: tuple[str, list[str]], tmp_path: Path, scenario: str
) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(scripted_model)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS),
            tmp_path,
            base_url=redesigned_site[0] + "?case=" + scenario,
            timeout_ms=250,
            recovery=BrowserRecovery(client, "test-key", "test-model"),
        )
    assert result.status == "complete", json.loads(result.manifest_path.read_text()).get("error")
    assert result.downloaded_count == 2


@pytest.mark.parametrize(
    "category,limit,count",
    [
        (DocumentType.OTHER_DOCUMENTS, 10, 10),
        (DocumentType.TRANSCRIPTS, 10, 0),
    ],
)
async def test_recovery_enforces_file_limit_and_source_zero_counts(
    redesigned_site: tuple[str, list[str]],
    tmp_path: Path,
    category: DocumentType,
    limit: int,
    count: int,
) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(scripted_model)) as client:
        result = await download_matter(
            DownloadRequest("M12205", category, limit),
            tmp_path,
            base_url=redesigned_site[0],
            timeout_ms=200,
            recovery=BrowserRecovery(client, "test-key", "test-model"),
        )
    assert result.downloaded_count == count
    assert result.status == ("empty" if count == 0 else "complete")
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["recovery_attempted"]
    assert len(manifest["recovery_trace"]) <= 50
    if count:
        import zipfile

        with zipfile.ZipFile(result.zip_path) as archive:
            assert len(archive.namelist()) == 10
            assert archive.testzip() is None


@pytest.mark.parametrize("scenario", ["wrong", "restricted", "external", "popup"])
async def test_recovery_never_downloads_another_matter_or_restricted_or_external_files(
    redesigned_site: tuple[str, list[str]],
    tmp_path: Path,
    scenario: str,
) -> None:
    before = len(redesigned_site[1])
    async with httpx.AsyncClient(transport=httpx.MockTransport(scripted_model)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS),
            tmp_path,
            base_url=redesigned_site[0] + "?case=" + scenario,
            timeout_ms=200,
            download_timeout_ms=250,
            recovery=BrowserRecovery(client, "test-key", "test-model"),
        )
    assert result.status == "failed"
    assert result.downloaded_count == 0
    assert result.zip_path is None
    assert not any(path.startswith(("/file", "/trap")) for path in redesigned_site[1][before:])


@pytest.mark.parametrize(
    "mode",
    ["unknown_ref", "wrong_category", "missing_count", "malformed", "refusal", "server_error"],
)
async def test_invalid_ai_evidence_or_response_stops_without_downloads(
    redesigned_site: tuple[str, list[str]],
    tmp_path: Path,
    mode: str,
) -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        if mode == "malformed":
            return httpx.Response(
                200, json={"status": "completed", "output": [{"type": "message", "content": None}]}
            )
        if mode == "refusal":
            return httpx.Response(200, json={"status": "completed", "output": []})
        if mode == "server_error":
            return httpx.Response(503, text="private provider diagnostics")
        response = scripted_model(request)
        envelope = response.json()
        action = json.loads(envelope["output"][0]["content"][0]["text"])
        if mode == "unknown_ref":
            action["target"] = 9999
        elif action["action"] == "read_matter":
            if mode == "wrong_category":
                action["matter"]["categories"][1]["control"] = action["matter"]["categories"][2][
                    "control"
                ]
            else:
                action["matter"]["categories"] = action["matter"]["categories"][:-1]
        envelope["output"][0]["content"][0]["text"] = json.dumps(action)
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS),
            tmp_path,
            base_url=redesigned_site[0],
            timeout_ms=200,
            recovery=BrowserRecovery(client, "test-key", "test-model"),
        )
    assert result.status == "failed"
    assert result.downloaded_count == 0
    assert "private provider diagnostics" not in result.manifest_path.read_text()


async def test_normal_adapter_never_calls_ai(fixture_url: str, tmp_path: Path) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("the known layout must bypass AI recovery")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS),
            tmp_path,
            base_url=fixture_url,
            recovery=BrowserRecovery(client, "test-key", "test-model"),
        )
    assert result.status == "complete"
    assert not json.loads(result.manifest_path.read_text())["recovery_attempted"]


async def test_recovery_has_a_hard_action_budget(redesigned_site, tmp_path):
    def repeat(request):
        envelope = scripted_model(request).json()
        envelope["output"][0]["content"][0]["text"] = json.dumps(
            {"action": "scroll", "target": None, "matter": None, "document": None}
        )
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(repeat)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS),
            tmp_path,
            base_url=redesigned_site[0],
            timeout_ms=200,
            recovery=BrowserRecovery(client, "test-key", "test-model", max_steps=3),
        )
    manifest = json.loads(result.manifest_path.read_text())
    assert result.status == "failed"
    assert len(manifest["recovery_trace"]) == 3
    assert "action limit" in manifest["error"]


async def test_model_cannot_skip_the_first_source_entry(redesigned_site, tmp_path):
    def skip(request):
        envelope = scripted_model(request).json()
        action = json.loads(envelope["output"][0]["content"][0]["text"])
        if action["action"] == "open_document":
            nodes = json.loads(json.loads(request.content)["input"][0]["content"][0]["text"])[
                "nodes"
            ]
            second = next(n for n in nodes if n["tag"] == "article" and "200001" in n["text"])["id"]
            inside = [n for n in nodes if second in n["ancestors"]]

            def ref(text):
                return next(n["id"] for n in inside if n["text"] == text)

            action["target"] = ref("Retrieve attachment")
            action["document"] = {
                "row": second,
                "identifier": ref("200001"),
                "title": ref("Source filing 2"),
                "date": ref("04/07/2025"),
                "security": ref("Public"),
                "extension": ref(".pdf"),
            }
            envelope["output"][0]["content"][0]["text"] = json.dumps(action)
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(skip)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS, 1),
            tmp_path,
            base_url=redesigned_site[0],
            timeout_ms=200,
            recovery=BrowserRecovery(client, "test-key", "test-model"),
        )
    assert result.status == "failed"
    assert result.downloaded_count == 0


@pytest.mark.parametrize("scenario", ["normal", "delayededitor"])
async def test_matter_entry_activates_filemaker_click_to_edit_field(
    fixture_url: str, scenario: str
) -> None:
    from playwright.async_api import async_playwright

    from senpilot.recovery_browser import RecoveryBrowser

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.goto(fixture_url + "?case=" + scenario)
        driver = RecoveryBrowser(page, fixture_url, 500)
        observation = await driver.observe()
        target = next(
            n["id"] for n in observation.nodes if n["kind"] == "control" and n["tag"] == "div"
        )
        await driver.fill_matter(observation, target, "M12205")
        assert await page.locator("[contenteditable=true]").inner_text() == "M12205"
        await browser.close()


async def test_recovery_preserves_completed_files_and_resumes_remaining_entries(
    redesigned_site: tuple[str, list[str]],
    tmp_path: Path,
) -> None:
    from playwright.async_api import async_playwright

    from senpilot.archive import validate_file
    from senpilot.models import DownloadedFile, SourceDocument
    from senpilot.recovery import Collection
    from tests.conftest import PDF

    original = tmp_path / "200000-1_filing.pdf"
    original.write_bytes(PDF + b"0")
    size, digest = validate_file(original, ".pdf")
    document = SourceDocument(
        "200000", "Source filing 1", "2025-04-07", "Public", ".pdf", ("200000", "Source filing 1")
    )
    seed = Collection(
        files=[DownloadedFile(document, "filing.pdf", original.name, size, digest)],
        examined=[document],
    )
    before = len(redesigned_site[1])
    async with (
        async_playwright() as pw,
        httpx.AsyncClient(transport=httpx.MockTransport(scripted_model)) as client,
    ):
        browser = await pw.chromium.launch()
        page = await browser.new_page(accept_downloads=True)
        result = await BrowserRecovery(client, "test-key", "test-model").run(
            page,
            DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS),
            tmp_path,
            redesigned_site[0],
            1000,
            seed,
        )
        await browser.close()
    assert result.error is None
    assert len(result.files) == 2
    assert original.read_bytes() == PDF + b"0"
    assert [p for p in redesigned_site[1][before:] if p.startswith("/file")] == ["/file?index=1"]


async def test_recovery_cannot_download_the_same_attachment_twice(redesigned_site, tmp_path):
    def repeat(request):
        envelope = scripted_model(request).json()
        action = json.loads(envelope["output"][0]["content"][0]["text"])
        if action["action"] == "close_document":
            nodes = json.loads(json.loads(request.content)["input"][0]["content"][0]["text"])[
                "nodes"
            ]
            action.update(
                action="download", target=next(n["id"] for n in nodes if n["text"] == "Save PDF")
            )
            envelope["output"][0]["content"][0]["text"] = json.dumps(action)
        return httpx.Response(200, json=envelope)

    before = len(redesigned_site[1])
    async with httpx.AsyncClient(transport=httpx.MockTransport(repeat)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS, 2),
            tmp_path,
            base_url=redesigned_site[0],
            timeout_ms=200,
            recovery=BrowserRecovery(client, "test-key", "test-model"),
        )
    assert result.downloaded_count == 1
    assert result.status == "partial"
    assert [p for p in redesigned_site[1][before:] if p.startswith("/file")] == ["/file?index=0"]


async def test_recovery_cannot_scroll_past_visible_unexamined_entries(redesigned_site, tmp_path):
    def scroll(request):
        data = json.loads(json.loads(request.content)["input"][0]["content"][0]["text"])
        if data["state"]["verified_matter"]:
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(
                                        {
                                            "action": "scroll",
                                            "target": None,
                                            "matter": None,
                                            "document": None,
                                        }
                                    ),
                                }
                            ],
                        }
                    ],
                },
            )
        return scripted_model(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(scroll)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS),
            tmp_path,
            base_url=redesigned_site[0],
            timeout_ms=200,
            recovery=BrowserRecovery(client, "test-key", "test-model", max_steps=6),
        )
    manifest = json.loads(result.manifest_path.read_text())
    assert result.status == "failed"
    assert "unexamined" in manifest["error"]


async def test_recovery_export_sanitizes_filename_and_retains_source_name(fixture_url, tmp_path):
    from playwright.async_api import async_playwright

    from senpilot.recovery_browser import RecoveryBrowser
    from senpilot.uarb import UarbSite

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(accept_downloads=True)
        site = UarbSite(page, 1000, 1000)
        base = fixture_url + "?case=export"
        matter = await site.open_matter(base, "M12205")
        document = await anext(
            site.documents(DocumentType.TRANSCRIPTS, matter.category_counts["Transcripts"])
        )
        await site._row(document).get_by_role("button", name="GO GET IT").click()
        driver = RecoveryBrowser(page, base, 1000)
        observation = await driver.observe()
        ok = next(
            n["id"] for n in observation.nodes if n["kind"] == "control" and n["text"] == "OK"
        )
        await driver.click(observation, ok)
        observation = await driver.observe()
        download = next(
            n["id"]
            for n in observation.nodes
            if n["kind"] == "control" and n["text"].endswith(".pdf")
        )
        assert "," not in observation.text(download)
        file = await driver.transfer(observation, download, document, tmp_path, 1)
        assert file.original_filename == "transcript, session.pdf"
        assert "," not in file.filename
        assert file.size_bytes > 0
        await browser.close()


async def test_recovery_uses_final_api_answer_and_ignores_commentary(redesigned_site, tmp_path):
    def phased(request):
        envelope = scripted_model(request).json()
        envelope["output"][0]["phase"] = "final_answer"
        envelope["output"].insert(
            0,
            {
                "type": "message",
                "phase": "commentary",
                "content": [
                    {"type": "output_text", "text": "Inspecting the source before deciding."}
                ],
            },
        )
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(phased)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS, 1),
            tmp_path,
            base_url=redesigned_site[0],
            timeout_ms=200,
            recovery=BrowserRecovery(client, "test-key", "test-model"),
        )
    assert result.status == "complete"
    assert result.downloaded_count == 1


@pytest.mark.parametrize("action", ["finish", "close_document"])
async def test_recovery_cannot_complete_an_open_entry_without_retrieving_attachments(
    redesigned_site,
    tmp_path,
    action,
):
    def premature(request):
        data = json.loads(json.loads(request.content)["input"][0]["content"][0]["text"])
        if data["state"]["active_document"]:
            target = (
                next(n["id"] for n in data["nodes"] if n["text"] == "Done")
                if action == "close_document"
                else None
            )
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(
                                        {
                                            "action": action,
                                            "target": target,
                                            "matter": None,
                                            "document": None,
                                        }
                                    ),
                                }
                            ],
                        }
                    ],
                },
            )
        return scripted_model(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(premature)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS, 1),
            tmp_path,
            base_url=redesigned_site[0],
            timeout_ms=200,
            recovery=BrowserRecovery(client, "test-key", "test-model"),
        )
    assert result.status == "failed"
    assert result.downloaded_count == 0
    assert "attachment" in json.loads(result.manifest_path.read_text())["error"]


async def test_recovery_cannot_discard_a_remaining_visible_attachment(redesigned_site, tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(scripted_model)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.EXHIBITS),
            tmp_path,
            base_url=redesigned_site[0] + "?case=multiple",
            timeout_ms=200,
            recovery=BrowserRecovery(client, "test-key", "test-model"),
        )
    assert result.status == "partial"
    assert result.downloaded_count == 1
    assert "attachment" in json.loads(result.manifest_path.read_text())["error"]


async def test_distinct_source_identifiers_can_share_titles_and_filing_dates(
    redesigned_site, tmp_path
):
    async with httpx.AsyncClient(transport=httpx.MockTransport(scripted_model)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.OTHER_DOCUMENTS, 3),
            tmp_path,
            base_url=redesigned_site[0] + "?case=same_titles",
            timeout_ms=200,
            recovery=BrowserRecovery(client, "test-key", "test-model"),
        )
    assert result.status == "complete"
    manifest = json.loads(result.manifest_path.read_text())
    assert [f["document"]["identifier"] for f in manifest["files"]] == [
        "200000",
        "200001",
        "200002",
    ]


async def test_ai_recovery_excludes_html_disguised_as_a_pdf(redesigned_site, tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(scripted_model)) as client:
        result = await download_matter(
            DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS),
            tmp_path,
            base_url=redesigned_site[0] + "?case=corrupt",
            timeout_ms=200,
            recovery=BrowserRecovery(client, "test-key", "test-model"),
        )
    manifest = json.loads(result.manifest_path.read_text())
    assert result.status == "partial"
    assert result.downloaded_count == 1
    assert manifest["failures"][0]["document"]["identifier"] == "200000"
    assert "HTML" in manifest["failures"][0]["error"]
