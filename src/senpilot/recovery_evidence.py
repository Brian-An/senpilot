"""Validate browser evidence before it becomes regulatory matter or document metadata."""

import hashlib
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from senpilot.models import DocumentType, DownloadRequest, MatterMetadata, SourceDocument
from senpilot.recovery_browser import Observation
from senpilot.uarb import SiteError, source_date


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CategoryEvidence(EvidenceModel):
    category: DocumentType
    control: int
    count: int


class DateEvidence(EvidenceModel):
    label: int
    value: int


class MatterEvidence(EvidenceModel):
    matter_number: int
    title: int
    matter_type: int
    category: int
    status: int | None
    outcome: int | None
    dates: list[DateEvidence]
    categories: list[CategoryEvidence]


class DocumentEvidence(EvidenceModel):
    row: int
    identifier: int | None
    title: int
    date: int | None
    security: int | None
    extension: int | None


class BrowserAction(EvidenceModel):
    action: Literal[
        "click",
        "fill_matter",
        "scroll",
        "wait",
        "read_matter",
        "open_document",
        "download",
        "close_document",
        "finish",
        "stop",
    ]
    target: int | None
    matter: MatterEvidence | None
    document: DocumentEvidence | None


def evidence_date(value: str) -> str | None:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value).isoformat()
    return source_date(value)


def read_matter(
    observation: Observation, evidence: MatterEvidence, request: DownloadRequest
) -> MatterMetadata:
    number = observation.text(evidence.matter_number)
    numbers = {n for text in observation.values() for n in re.findall(r"\bM[0-9]{5}\b", text)}
    if number != request.matter_number or numbers != {request.matter_number}:
        raise SiteError("recovery could not verify the requested matter")
    if observation.node(evidence.matter_number)["editable"]:
        raise SiteError("a search input is not matter identity evidence")
    counts: dict[str, int] = {}
    controls = set()
    for category in evidence.categories:
        label = observation.text(category.control)
        if category.category.value.casefold() not in label.casefold():
            raise SiteError("category control does not identify the requested source category")
        if observation.node(category.control)["kind"] != "control":
            raise SiteError("category evidence is not a source control")
        count_text = observation.text(category.count)
        digits = re.findall(r"\b[0-9][0-9,]*\b", count_text)
        if len(digits) != 1 or category.category.value in counts:
            raise SiteError("source category count is missing or ambiguous")
        if not observation.contains(category.control, category.count):
            raise SiteError("category count is not associated with its control")
        counts[category.category.value] = int(digits[0].replace(",", ""))
        controls.add(category.control)
    if set(counts) != {c.value for c in DocumentType} or len(controls) != 5:
        raise SiteError("recovery requires five distinct source category counts")
    dates = {}
    for item in evidence.dates:
        label = observation.text(item.label)
        if label in dates:
            raise SiteError("source date labels are ambiguous")
        dates[label] = evidence_date(observation.text(item.value))
    if not dates:
        raise SiteError("recovery requires source-labelled matter dates")
    values = [
        observation.text(ref) for ref in [evidence.title, evidence.matter_type, evidence.category]
    ]
    if not all(values):
        raise SiteError("recovery requires matter title, type and category evidence")
    return MatterMetadata(
        number,
        *values,
        observation.text(evidence.status) if evidence.status is not None else "not provided",
        observation.text(evidence.outcome) if evidence.outcome is not None else "not provided",
        dates,
        counts,
    )


def read_document(
    observation: Observation, evidence: DocumentEvidence, target: int
) -> SourceDocument:
    fields = [
        evidence.title,
        evidence.identifier,
        evidence.date,
        evidence.security,
        evidence.extension,
        target,
    ]
    if any(ref is not None and not observation.contains(evidence.row, ref) for ref in fields):
        raise SiteError("document evidence and retrieval control must belong to one source entry")
    row_text = observation.text(evidence.row)
    title = observation.text(evidence.title)
    if not title:
        raise SiteError("document title evidence is empty")
    raw = observation.values(evidence.row)
    identifier = (
        observation.text(evidence.identifier)
        if evidence.identifier is not None
        else (hashlib.sha256("\x1f".join(raw).encode()).hexdigest()[:16])
    )
    security = observation.text(evidence.security) if evidence.security is not None else None
    if re.search(r"\b(confidential|restricted|private|not public)\b", row_text, re.I):
        security = "Confidential"
    extension = observation.text(evidence.extension) if evidence.extension is not None else ""
    if extension and not re.fullmatch(r"\.[A-Za-z0-9]+", extension):
        raise SiteError("source extension evidence is invalid")
    filed = evidence_date(observation.text(evidence.date)) if evidence.date is not None else None
    return SourceDocument(
        identifier, title, filed, security, extension, raw, evidence.identifier is not None
    )
