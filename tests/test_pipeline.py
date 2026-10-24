import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from senpilot.models import DocumentType, DownloadRequest
from senpilot.pipeline import download_matter


async def test_downloads_ten_documents_across_virtual_rows(
    fixture_url: str, tmp_path: Path
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.OTHER_DOCUMENTS), tmp_path, base_url=fixture_url
    )
    assert result.status == "complete"
    assert result.downloaded_count == 10
    assert result.zip_path is not None
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["matter"]["category_counts"]["Other Documents"] == 12
    assert manifest["matter"]["category_entry_count"] == 18
    assert manifest["matter"]["dates"] == {
        "Date Received": "2025-04-07",
        "Decision Date": "2025-10-23",
    }
    assert manifest["matter"]["title"].startswith("Halifax Regional Water Commission")
    assert manifest["selection_order"] == "website displayed order"
    assert manifest["files"][-1]["document"]["identifier"] == "100009"
    with zipfile.ZipFile(result.zip_path) as package:
        assert len(package.namelist()) == 10
        assert package.testzip() is None
        for file in manifest["files"]:
            content = package.read(file["filename"])
            assert file["sha256"] == hashlib.sha256(content).hexdigest()
            assert file["size_bytes"] == len(content)


async def test_empty_category_produces_manifest_without_zip(
    fixture_url: str, tmp_path: Path
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.TRANSCRIPTS), tmp_path, base_url=fixture_url
    )
    assert result.status == "empty"
    assert result.downloaded_count == 0
    assert result.zip_path is None
    assert json.loads(result.manifest_path.read_text())["failures"] == []


@pytest.mark.parametrize("scenario", ["unknown", "wrong"])
async def test_never_downloads_from_another_matter(
    fixture_url: str, tmp_path: Path, scenario: str
) -> None:
    matter = "M99999" if scenario == "unknown" else "M12205"
    result = await download_matter(
        DownloadRequest(matter, DocumentType.OTHER_DOCUMENTS),
        tmp_path,
        base_url=f"{fixture_url}?case={scenario}",
        timeout_ms=1500,
    )
    assert result.status == "failed"
    assert result.downloaded_count == 0
    assert result.zip_path is None
    assert "matter" in json.loads(result.manifest_path.read_text())["error"].lower()


async def test_corrupt_file_is_excluded_and_reported(fixture_url: str, tmp_path: Path) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.OTHER_DOCUMENTS, 3),
        tmp_path,
        base_url=f"{fixture_url}?case=partial",
    )
    assert result.status == "partial"
    assert result.downloaded_count == 2
    manifest = json.loads(result.manifest_path.read_text())
    assert len(manifest["failures"]) == 1
    assert "HTML" in manifest["failures"][0]["error"]
    assert result.zip_path is not None
    with zipfile.ZipFile(result.zip_path) as package:
        assert len(package.namelist()) == 2
    assert len(list((result.manifest_path.parent / "files").iterdir())) == 2


async def test_duplicate_suggested_names_do_not_overwrite_files(
    fixture_url: str, tmp_path: Path
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.OTHER_DOCUMENTS, 3),
        tmp_path,
        base_url=f"{fixture_url}?case=duplicate",
    )
    manifest = json.loads(result.manifest_path.read_text())
    assert result.downloaded_count == 3
    assert len({file["filename"] for file in manifest["files"]}) == 3
    assert len({file["sha256"] for file in manifest["files"]}) == 3


async def test_exhibits_preserve_exhibit_numbers_and_skip_confidential_rows(
    fixture_url: str, tmp_path: Path
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.EXHIBITS), tmp_path, base_url=fixture_url
    )
    manifest = json.loads(result.manifest_path.read_text())
    assert result.status == "partial"
    assert [file["document"]["identifier"] for file in manifest["files"]] == ["H-1", "H-3"]
    assert manifest["failures"][0]["document"]["security"] == "Confidential"


async def test_recordings_can_have_no_document_number_or_security_field(
    fixture_url: str, tmp_path: Path
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.RECORDINGS), tmp_path, base_url=fixture_url
    )
    assert result.status == "complete"
    assert result.downloaded_count == 1
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["files"][0]["original_filename"] == "hearing.mp3"


async def test_multiple_attachments_cannot_exceed_file_limit(
    fixture_url: str, tmp_path: Path
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.OTHER_DOCUMENTS, 3),
        tmp_path,
        base_url=f"{fixture_url}?case=multiple",
    )
    assert result.status == "complete"
    assert result.downloaded_count == 3


async def test_repeated_runs_have_separate_output_directories(
    fixture_url: str, tmp_path: Path
) -> None:
    request = DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS, 1)
    first = await download_matter(request, tmp_path, base_url=fixture_url)
    content = first.manifest_path.read_bytes()
    second = await download_matter(request, tmp_path, base_url=fixture_url)
    assert first.manifest_path.parent != second.manifest_path.parent
    assert first.manifest_path.read_bytes() == content


async def test_missing_document_list_fails_with_bounded_wait(
    fixture_url: str, tmp_path: Path
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.OTHER_DOCUMENTS),
        tmp_path,
        base_url=f"{fixture_url}?case=stalled",
        timeout_ms=700,
    )
    assert result.status == "failed"
    assert result.downloaded_count == 0
    assert result.manifest_path.exists()


async def test_filename_collision_preserves_the_first_download(
    fixture_url: str, tmp_path: Path
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.OTHER_DOCUMENTS, 2),
        tmp_path,
        base_url=f"{fixture_url}?case=idcollision",
    )
    assert result.status == "partial"
    assert result.downloaded_count == 1
    assert result.zip_path is not None
    manifest = json.loads(result.manifest_path.read_text())
    first = result.manifest_path.parent / "files" / manifest["files"][0]["filename"]
    assert first.read_bytes().endswith(b"0")
    with zipfile.ZipFile(result.zip_path) as package:
        assert package.read(first.name) == first.read_bytes()


async def test_missing_download_event_times_out_and_keeps_other_files(
    fixture_url: str, tmp_path: Path
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.OTHER_DOCUMENTS, 3),
        tmp_path,
        base_url=f"{fixture_url}?case=missing",
        download_timeout_ms=250,
    )
    assert result.status == "partial"
    assert result.downloaded_count == 2
    manifest = json.loads(result.manifest_path.read_text())
    assert len(manifest["failures"]) == 1
    assert "Timeout" in manifest["failures"][0]["error"]


async def test_key_documents_preserve_source_document_numbers(
    fixture_url: str, tmp_path: Path
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS, 1),
        tmp_path,
        base_url=fixture_url,
    )
    manifest = json.loads(result.manifest_path.read_text())
    assert result.status == "complete"
    assert manifest["files"][0]["document"]["identifier"] == "100000"


async def test_blocked_dialog_preserves_verified_files_and_stops_navigation(
    fixture_url: str, tmp_path: Path
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.OTHER_DOCUMENTS, 3),
        tmp_path,
        base_url=f"{fixture_url}?case=closeblocked",
        timeout_ms=1000,
    )
    assert result.status == "partial"
    assert result.downloaded_count == 1
    assert result.zip_path is not None
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["source_entries_examined"] == 1
    assert "dialog" in manifest["error"].lower()
    with zipfile.ZipFile(result.zip_path) as package:
        assert len(package.namelist()) == 1
        content = package.read(manifest["files"][0]["filename"])
        assert hashlib.sha256(content).hexdigest() == manifest["files"][0]["sha256"]


@pytest.mark.parametrize(
    "category,filename",
    [
        (DocumentType.TRANSCRIPTS, "transcript, session.pdf"),
        (DocumentType.RECORDINGS, "hearing.mp3"),
    ],
)
async def test_export_prompt_precedes_transcript_and_recording_download(
    fixture_url: str, tmp_path: Path, category: DocumentType, filename: str
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", category, 1),
        tmp_path,
        base_url=f"{fixture_url}?case=export",
        timeout_ms=1000,
        download_timeout_ms=1000,
    )
    assert result.status == "complete"
    manifest = json.loads(result.manifest_path.read_text())
    assert result.downloaded_count == 1
    assert manifest["files"][0]["original_filename"] == filename
    assert "," not in manifest["files"][0]["filename"]


async def test_changed_header_field_order_is_not_silently_mislabelled(
    fixture_url: str, tmp_path: Path
) -> None:
    result = await download_matter(
        DownloadRequest("M12205", DocumentType.KEY_DOCUMENTS, 1),
        tmp_path,
        base_url=fixture_url + "?case=headerorder",
    )
    assert result.status == "failed"
    assert "layout" in result.error
    assert result.downloaded_count == 0
