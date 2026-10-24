"""Safe output filenames and verification of downloaded files and ZIPs."""

import hashlib
import re
import zipfile
from pathlib import Path


def safe_filename(source_id: str, suggested_name: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9_.()-]", "_", source_id)[:48].strip(".") or "document"
    name = suggested_name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", name).strip(" .") or "file"
    suffix = Path(name).suffix[:20]
    stem = name[: -len(suffix)] if suffix else name
    budget = 220 - len(f"{prefix}_{suffix}".encode())
    stem = stem.encode()[:budget].decode("utf-8", errors="ignore")
    return f"{prefix}_{stem}{suffix}"


def validate_file(path: Path, expected_extension: str) -> tuple[int, str]:
    size = path.stat().st_size
    if size == 0:
        raise ValueError("download is empty")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        header = source.read(1024)
        normalized = header.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        if normalized.startswith((b"<!doctype html", b"<html")):
            raise ValueError("download contains an HTML page instead of a source file")
        if expected_extension.lower() == ".pdf" and b"%PDF-" not in header:
            raise ValueError("download does not contain a PDF signature")
        digest.update(header)
        while block := source.read(1024 * 1024):
            digest.update(block)
    return size, digest.hexdigest()


def create_archive(files: list[Path], destination: Path) -> None:
    if len({file.name for file in files}) != len(files):
        raise ValueError("archive filenames must be unique")
    # Exclusive creation protects a previous result if a caller reuses its path.
    with destination.open("xb") as output:
        try:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as package:
                for file in files:
                    package.write(file, arcname=file.name)
            output.flush()
            with zipfile.ZipFile(destination) as package:
                if corrupt := package.testzip():
                    raise ValueError(f"archive integrity check failed for {corrupt}")
        except Exception:
            destination.unlink(missing_ok=True)
            raise
