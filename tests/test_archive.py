import hashlib
import zipfile
from pathlib import Path

import pytest

from senpilot.archive import create_archive, safe_filename, validate_file
from senpilot.models import DocumentType, DownloadRequest


@pytest.mark.parametrize("matter", ["M12205", "M00001"])
def test_accepts_exact_matter_number(matter: str) -> None:
    assert DownloadRequest(matter, DocumentType.OTHER_DOCUMENTS).matter_number == matter


@pytest.mark.parametrize("matter", ["", "M1234", "M123456", "*12205", "../M12205", "m12205"])
def test_rejects_inexact_matter_numbers(matter: str) -> None:
    with pytest.raises(ValueError, match="matter"):
        DownloadRequest(matter, DocumentType.EXHIBITS)


@pytest.mark.parametrize("limit", [0, -1, 11, True, 1.5])
def test_rejects_limits_outside_one_to_ten(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        DownloadRequest("M12205", DocumentType.EXHIBITS, limit)


def test_rejects_unknown_document_type() -> None:
    with pytest.raises(ValueError, match="document type"):
        DownloadRequest("M12205", "Everything")


def test_validates_pdf_and_hash(tmp_path: Path) -> None:
    content = b"%PDF-1.7\nregulatory evidence\n%%EOF\n"
    path = tmp_path / "evidence.pdf"
    path.write_bytes(content)
    size, digest = validate_file(path, ".pdf")
    assert size == len(content)
    assert digest == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize("content", [b"", b"<html>Server error</html>", b"not a PDF"])
def test_rejects_invalid_pdf_download(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "evidence.pdf"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        validate_file(path, ".pdf")


def test_rejects_html_error_under_word_extension(tmp_path: Path) -> None:
    path = tmp_path / "evidence.docx"
    path.write_text("  <!DOCTYPE html><html>Session expired</html>")
    with pytest.raises(ValueError, match="HTML"):
        validate_file(path, ".docx")


@pytest.mark.parametrize(
    "filename", ["../../escape.pdf", r"C:\secret\evidence.pdf", "bad\x00name.pdf"]
)
def test_names_cannot_escape_output_directory(filename: str) -> None:
    result = safe_filename("123456", filename)
    assert result.startswith("123456_")
    assert "/" not in result and "\\" not in result and "\x00" not in result
    assert len(result.encode()) <= 220


def test_limits_long_filenames() -> None:
    result = safe_filename("123456", "é" * 300 + ".pdf")
    assert len(result.encode()) <= 220
    assert result.endswith(".pdf")


def test_zip_contains_only_requested_files(tmp_path: Path) -> None:
    source = tmp_path / "files"
    source.mkdir()
    first = source / "123_a.pdf"
    second = source / "456_b.pdf"
    first.write_bytes(b"%PDF-1.7\nfirst")
    second.write_bytes(b"%PDF-1.7\nsecond")
    (source / "unselected.txt").write_text("not selected")
    archive = tmp_path / "matter.zip"
    create_archive([first, second], archive)
    with zipfile.ZipFile(archive) as package:
        assert package.namelist() == [first.name, second.name]
        assert package.testzip() is None
        assert package.read(first.name) == first.read_bytes()
