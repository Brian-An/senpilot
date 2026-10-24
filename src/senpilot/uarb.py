"""Browser integration for the public UARB FileMaker database."""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast

from playwright.async_api import Download, Locator, Page
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from senpilot.archive import safe_filename, validate_file
from senpilot.models import (
    DocumentDownload,
    DocumentType,
    DownloadedFile,
    DownloadFailure,
    MatterMetadata,
    SourceDocument,
)

SOURCE_URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"
logger = logging.getLogger(__name__)


class SiteError(RuntimeError):
    pass


class HeaderSnapshot(TypedDict):
    fields: list[str]
    title: str
    date_labels: list[str]


HEADER_SCRIPT = """() => {
    const fields = Array.from(document.querySelectorAll('.fm-textarea .text'))
        .filter(el => !el.closest('tr')).map(el => el.textContent.trim());
    const boxes = Array.from(document.querySelectorAll('.iwps_text_box'));
    const label = boxes.find(el => el.textContent.trim() === 'Title - Description');
    const rect = label?.getBoundingClientRect();
    const title = rect ? boxes.filter(el => {
        const r = el.getBoundingClientRect();
        return r.top > rect.top && Math.abs(r.left - rect.left) < 20;
    }).sort((a,b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)
        [0]?.textContent.trim() : '';
    const date_labels = boxes.map(el => el.textContent.trim().replace(/\\s+/g,' '))
        .filter(text => /^(Date Received|Decision Date|Date Final)/.test(text));
    return {fields, title: title || '', date_labels};
}"""

ROWS_SCRIPT = """() => Array.from(document.querySelectorAll('tr[role="row"]'))
    .filter(row => row.querySelector('.fm-textarea'))
    .sort((a,b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)
    .map(row => Array.from(row.querySelectorAll('.fm-textarea .text'))
        .map(el => el.textContent.trim()))"""


def source_date(value: str) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except ValueError as exc:
        raise SiteError(f"unrecognized source date: {value!r}") from exc


def parse_document(fields: list[str], category: DocumentType) -> SourceDocument:
    if len(fields) < 2:
        raise SiteError("document row is missing its source fields")
    date = next((v for v in fields if re.fullmatch(r"\d{2}/\d{2}/\d{4}", v)), "")
    security = next((v for v in fields if v.lower() in {"public", "confidential"}), None)
    extension = next((v for v in reversed(fields) if re.fullmatch(r"\.[A-Za-z0-9]+", v)), "")
    if category in {DocumentType.EXHIBITS, DocumentType.KEY_DOCUMENTS}:
        title = fields[0]
        identifier = next((v for v in fields[1:] if v not in {date, security, extension}), "")
        if len(fields) >= 5:
            security = fields[2] or None
            identifier = fields[3]
    elif fields[0].isdigit():
        identifier, title = fields[:2]
        if len(fields) >= 5:
            security = fields[3] or None
    else:
        identifier = ""
        title = next((v for v in fields if v not in {date, security, extension}), category.value)
    if not identifier:
        identifier = hashlib.sha256("\x1f".join(fields).encode()).hexdigest()[:16]
    return SourceDocument(identifier, title, source_date(date), security, extension, tuple(fields))


class UarbSite:
    def __init__(self, page: Page, timeout_ms: int, download_timeout_ms: int) -> None:
        self.page = page
        self.timeout_ms = timeout_ms
        self.download_timeout_ms = download_timeout_ms
        page.set_default_timeout(timeout_ms)

    async def open_matter(self, url: str, matter_number: str) -> MatterMetadata:
        await self.page.goto(url, wait_until="domcontentloaded")
        await (
            self.page.locator(".fm-textarea")
            .filter(has=self.page.get_by_text("eg M01234", exact=True))
            .locator(".text")
            .click()
        )
        editor = self.page.locator('[contenteditable="true"]')
        # FileMaker commits edits through keyboard and blur events.
        await editor.press_sequentially(matter_number, delay=35)
        await editor.press("Tab")
        await self.page.get_by_role("button", name="Search", exact=True).last.click()
        await self.page.wait_for_function(
            """() => Array.from(document.querySelectorAll('button')).some(el =>
                /^Exhibits\\s*-\\s*\\d+/.test(el.textContent.trim())) ||
                /No records matched|No Records Found/i.test(document.body.innerText)"""
        )
        if await self.page.get_by_text(re.compile("No records matched", re.I)).count():
            raise SiteError(f"matter {matter_number} was not found")
        # Playwright's evaluate result crosses an untyped DOM boundary.
        snapshot = cast(HeaderSnapshot, await self.page.evaluate(HEADER_SCRIPT))
        fields = snapshot["fields"]
        if len(fields) != 7 or fields[0] != matter_number:
            raise SiteError(f"source did not return the requested matter {matter_number}")
        if not snapshot["title"] or len(snapshot["date_labels"]) != 2:
            raise SiteError("matter metadata layout is not recognized")
        counts: dict[str, int] = {}
        for category in DocumentType:
            button = self.page.get_by_role(
                "button", name=re.compile(rf"^{re.escape(category.value)}\s*-\s*[\d,]+$")
            )
            text = await button.inner_text()
            counts[category.value] = int(text.rsplit("-", 1)[1].strip().replace(",", ""))
        return MatterMetadata(
            matter_number=fields[0],
            title=snapshot["title"],
            matter_type=fields[1],
            status=fields[2],
            outcome=fields[5],
            category=fields[6],
            dates=dict(zip(snapshot["date_labels"], map(source_date, fields[3:5]), strict=True)),
            category_counts=counts,
        )

    async def documents(self, category: DocumentType, count: int) -> AsyncIterator[SourceDocument]:
        await self.page.get_by_role(
            "button", name=re.compile(rf"^{re.escape(category.value)}\s*-\s*[\d,]+$")
        ).click()
        await self.page.locator('tr[role="row"] .fm-textarea').first.wait_for(state="visible")
        seen: set[tuple[str, ...]] = set()
        stalled = 0
        while len(seen) < count:
            rows = cast(list[list[str]], await self.page.evaluate(ROWS_SCRIPT))
            fresh = next((fields for fields in rows if tuple(fields) not in seen), None)
            if fresh is not None:
                seen.add(tuple(fresh))
                stalled = 0
                yield parse_document(fresh, category)
                continue
            grid = self.page.locator(".v-grid").first
            bounds = await grid.bounding_box()
            if bounds is None:
                raise SiteError("document list has no visible scroll container")
            await grid.hover()
            await self.page.mouse.wheel(0, max(60, bounds["height"] * 0.65))
            try:
                await self.page.wait_for_function(
                    """seen => Array.from(document.querySelectorAll('tr[role="row"]'))
                        .some(row => {
                            const fields = Array.from(row.querySelectorAll('.fm-textarea .text'))
                                .map(el => el.textContent.trim());
                            return fields.length && !seen.includes(JSON.stringify(fields));
                        })""",
                    arg=[json.dumps(list(fields), separators=(",", ":")) for fields in seen],
                    timeout=min(1500, self.timeout_ms),
                )
            except BrowserTimeout as exc:
                stalled += 1
                if stalled >= 3:
                    raise SiteError(
                        f"document list stopped after {len(seen)} of {count} entries"
                    ) from exc

    def _row(self, document: SourceDocument) -> Locator:
        row = self.page.get_by_role("row")
        for field in document.source_fields:
            if field:
                row = row.filter(has=self.page.get_by_text(field, exact=True))
        return row.first

    async def download_document(
        self, document: SourceDocument, destination: Path, remaining: int
    ) -> DocumentDownload:
        files: list[DownloadedFile] = []
        failures: list[DownloadFailure] = []
        navigation_error: str | None = None
        if document.security and document.security.lower() != "public":
            return DocumentDownload(
                [],
                [DownloadFailure(document, "source marks this entry as unavailable to the public")],
            )
        dialog = self.page.get_by_role("dialog").filter(
            has_text="Your files are ready for download"
        )
        export_dialog = self.page.get_by_role("dialog").filter(
            has_text="Specify a filename to export."
        )
        try:
            await self._row(document).get_by_role("button", name="GO GET IT", exact=True).click()
            await self.page.get_by_role("dialog").last.wait_for(state="visible")
            if await export_dialog.is_visible():
                # Transcripts and recordings retain the source's proposed export filename.
                await export_dialog.get_by_role("button", name="OK", exact=True).click()
            await dialog.wait_for(state="visible")
            buttons = dialog.locator(".fm-download-button")
            total = await buttons.count()
            if not total:
                raise SiteError("download dialog contains no file buttons")
            for index in range(min(total, remaining)):
                original = (await buttons.nth(index).inner_text()).strip()
                download: Download | None = None
                path: Path | None = None
                try:
                    async with self.page.expect_download(timeout=self.download_timeout_ms) as event:
                        await buttons.nth(index).click()
                    download = await event.value
                    original = download.suggested_filename
                    prefix = (
                        document.identifier if total == 1 else f"{document.identifier}-{index + 1}"
                    )
                    candidate = destination / safe_filename(prefix, original)
                    if candidate.exists():
                        raise ValueError("download filename conflicts with an existing source file")
                    path = candidate
                    await asyncio.wait_for(
                        download.save_as(path), timeout=self.download_timeout_ms / 1000
                    )
                    size, digest = validate_file(path, document.extension or path.suffix)
                    files.append(DownloadedFile(document, original, path.name, size, digest))
                    logger.info("Downloaded %s (%s bytes)", path.name, size)
                except (BrowserError, TimeoutError, ValueError, OSError) as exc:
                    if download is not None:
                        await download.cancel()
                    if path is not None and not any(file.filename == path.name for file in files):
                        path.unlink(missing_ok=True)
                    message = str(exc) or "download exceeded its completion timeout"
                    failures.append(DownloadFailure(document, message, original))
        except (BrowserError, SiteError) as exc:
            failures.append(DownloadFailure(document, str(exc)))
        finally:
            try:
                if await dialog.is_visible():
                    await dialog.get_by_role("button", name="Close", exact=True).click()
                    await dialog.wait_for(state="hidden")
                if await export_dialog.is_visible():
                    await export_dialog.get_by_role("button", name="Cancel", exact=True).click()
                    await export_dialog.wait_for(state="hidden")
                if await self.page.get_by_role("dialog").count():
                    navigation_error = "an unexpected source dialog prevents further navigation"
            except BrowserError as exc:
                navigation_error = f"download dialog could not be closed: {exc}"
        return DocumentDownload(files, failures, navigation_error)
