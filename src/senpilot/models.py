"""Validated download requests and source document metadata."""

import re
from dataclasses import dataclass
from enum import StrEnum


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
