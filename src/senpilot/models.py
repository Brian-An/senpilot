"""Validated download requests and source document metadata."""

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class DocumentType(StrEnum):
    EXHIBITS = "Exhibits"
    KEY_DOCUMENTS = "Key Documents"
    OTHER_DOCUMENTS = "Other Documents"
    TRANSCRIPTS = "Transcripts"
    RECORDINGS = "Recordings"


@dataclass(frozen=True)
class DownloadRequest:
    matter_number: str
    document_type: DocumentType
    limit: int = 10

    def __post_init__(self) -> None:
        if not re.fullmatch(r"M[0-9]{5}", self.matter_number):
            raise ValueError("matter number must be M followed by exactly five digits")
        try:
            object.__setattr__(self, "document_type", DocumentType(self.document_type))
        except ValueError as exc:
            raise ValueError("unknown document type") from exc
        if type(self.limit) is not int or not 1 <= self.limit <= 10:
            raise ValueError("limit must be an integer from 1 through 10")


@dataclass(frozen=True)
class MatterMetadata:
    matter_number: str
    title: str
    matter_type: str
    category: str
    status: str
    outcome: str
    dates: dict[str, str | None]
    category_counts: dict[str, int]


@dataclass(frozen=True)
class SourceDocument:
    identifier: str
    title: str
    date_filed: str | None
    security: str | None
    extension: str
    source_fields: tuple[str, ...]


@dataclass(frozen=True)
class DownloadedFile:
    document: SourceDocument
    original_filename: str
    filename: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class DownloadFailure:
    document: SourceDocument
    error: str
    original_filename: str | None = None


@dataclass(frozen=True)
class DocumentDownload:
    files: list[DownloadedFile]
    failures: list[DownloadFailure]
    navigation_error: str | None = None


class ResultStatus(StrEnum):
    COMPLETE = "complete"
    EMPTY = "empty"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class DownloadResult:
    status: ResultStatus
    manifest_path: Path
    zip_path: Path | None
    downloaded_count: int
    error: str | None = None
