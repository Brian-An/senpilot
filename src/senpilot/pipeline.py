"""Reusable matter-to-ZIP workflow with an auditable manifest."""

import json
import logging
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from senpilot.archive import create_archive
from senpilot.models import (
    DownloadedFile,
    DownloadFailure,
    DownloadRequest,
    DownloadResult,
    MatterMetadata,
    ResultStatus,
)
from senpilot.uarb import SOURCE_URL, SiteError, UarbSite

logger = logging.getLogger(__name__)


async def download_matter(
    request: DownloadRequest,
    output_root: Path,
    *,
    headless: bool = True,
    base_url: str = SOURCE_URL,
    timeout_ms: int = 30_000,
    download_timeout_ms: int = 120_000,
) -> DownloadResult:
    if timeout_ms <= 0 or download_timeout_ms <= 0:
        raise ValueError("timeouts must be positive")
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    slug = request.document_type.value.lower().replace(" ", "-")
    run_dir = Path(tempfile.mkdtemp(prefix=f"{request.matter_number}-{slug}-", dir=output_root))
    files_dir = run_dir / "files"
    files_dir.mkdir()
    started = datetime.now(UTC).isoformat()
    matter: MatterMetadata | None = None
    files: list[DownloadedFile] = []
    failures: list[DownloadFailure] = []
    error: str | None = None
    source_entries_examined = 0
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=headless)
            try:
                context = await browser.new_context(
                    accept_downloads=True, viewport={"width": 1280, "height": 720}
                )
                page = await context.new_page()
                try:
                    site = UarbSite(page, timeout_ms, download_timeout_ms)
                    matter = await site.open_matter(base_url, request.matter_number)
                    count = matter.category_counts[request.document_type.value]
                    if count:
                        async for document in site.documents(request.document_type, count):
                            source_entries_examined += 1
                            batch = await site.download_document(
                                document, files_dir, request.limit - len(files)
                            )
                            files.extend(batch.files)
                            failures.extend(batch.failures)
                            if batch.navigation_error:
                                raise SiteError(batch.navigation_error)
                            if (
                                source_entries_examined >= request.limit
                                or len(files) >= request.limit
                            ):
                                break
                except (BrowserError, SiteError, OSError) as exc:
                    error = str(exc)
                    try:
                        await page.screenshot(path=run_dir / "error.png", timeout=5000)
                    except BrowserError:
                        logger.debug("Browser screenshot is unavailable", exc_info=True)
            finally:
                await browser.close()
    except BrowserError as exc:
        error = str(exc)
    zip_path: Path | None = None
    if files:
        zip_path = run_dir / f"{request.matter_number}-{slug}.zip"
        try:
            create_archive([files_dir / file.filename for file in files], zip_path)
        except (OSError, ValueError) as exc:
            error = str(exc)
            zip_path = None
    if error:
        status = ResultStatus.PARTIAL if files and zip_path else ResultStatus.FAILED
    elif failures:
        status = ResultStatus.PARTIAL if files else ResultStatus.FAILED
    elif not files:
        status = ResultStatus.EMPTY
    else:
        status = ResultStatus.COMPLETE
    metadata = asdict(matter) if matter else None
    if metadata is not None and matter is not None:
        # Category totals count memberships; Key Documents also appears in Other Documents.
        metadata["category_entry_count"] = sum(matter.category_counts.values())
    manifest = {
        "schema_version": 1,
        "request": asdict(request),
        "source_url": base_url,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "status": status.value,
        "matter": metadata,
        "selection_order": "website displayed order",
        "source_entries_examined": source_entries_examined,
        "downloaded_count": len(files),
        "files": [asdict(file) for file in files],
        "failures": [asdict(failure) for failure in failures],
        "error": error,
        "zip_filename": zip_path.name if zip_path else None,
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return DownloadResult(status, manifest_path, zip_path, len(files), error)
