# Senpilot

Download public Nova Scotia regulatory filings by matter number and document category.
Each run produces verified source files, a ZIP, and a JSON manifest with matter metadata and download outcomes.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```sh
uv sync
uv run playwright install chromium
```

Python 3.12 or newer is required.
On Linux, install browser system dependencies with `uv run playwright install --with-deps chromium`.

## Download files

```sh
uv run senpilot download M12205 --type "Other Documents"
```

The supported categories are `Exhibits`, `Key Documents`, `Other Documents`, `Transcripts`, and `Recordings`.
The default limit is ten source entries and at most ten files.
Use `--limit` for a smaller batch, `--output` to choose the parent directory, and `--headed` to watch the browser.

```sh
uv run senpilot download M12205 --type "Key Documents" --limit 3 --output ./downloads
```

The CLI writes progress to stderr and a JSON result to stdout.
`uv run python -m senpilot` provides the same interface.
Use `--help` or `download --help` for available options.

## Output

Each invocation creates its own directory under `downloads/` by default:

```text
downloads/M12205-other-documents-<run-id>/
  files/
    102674_102674.pdf
    ...
  M12205-other-documents.zip
  manifest.json
```

The ZIP contains only successfully downloaded source files.
The manifest remains outside the ZIP so the ZIP's file count matches its downloaded source-file count.
Files retain their source bytes and receive a source identifier prefix to prevent ordinary filename collisions.
Runs never overwrite an earlier result.

The manifest records:

- Matter number, title, type, category, status, outcome, and source date labels.
- Counts for all five document categories as reported by the source website.
- Source identifiers or exhibit numbers, titles, filing dates, security labels, and extensions.
- Original and saved filenames, byte lengths, and SHA-256 hashes.
- Request details, UTC timestamps, selection order, failures, and archive name.

`category_entry_count` is the sum of category memberships, not a unique-document count.
Key Documents also appears in Other Documents, so those counts overlap.
Source dates retain their labels; a decision date is not interpreted as the most recent filing date.
Counts and metadata reflect the live source at the start of the run and can change.
The source can associate identical file contents with different document entries; those entries are preserved separately.

## Selection and failures

Documents are selected in the website's displayed order, including rows reached by scrolling its virtualized list.
Transcripts and Recordings receive an additional export confirmation.
The manifest preserves the proposed source filename, and commas in the export filename become underscores because the source's unquoted download header is rejected by Chromium when it contains commas.
The pipeline examines at most `--limit` source entries and downloads at most `--limit` files.
When an entry has several attached files, the remaining file limit applies to those attachments.
Confidential entries are recorded as unavailable without requesting their files.
Failed or inaccessible entries are reported rather than silently replaced by later entries.

Each downloaded file must finish within the completion timeout, be nonempty, and pass the format checks.
The current checks reject HTML responses masquerading as files and require a PDF signature for PDFs.
Large recordings are supported, with hashing and ZIP creation streaming from disk.
The output directory needs space for both the downloaded files and their ZIP.
These checks detect common download failures; they do not constitute full document-format validation or malware scanning.
The final ZIP is checked for CRC integrity.

| Status | Meaning | Exit code |
| --- | --- | --- |
| `complete` | Selected files downloaded and verified | `0` |
| `empty` | The requested category has zero entries | `0` |
| `partial` | Some verified files are available, with failures reported | `1` |
| `failed` | No deliverable ZIP is available or the workflow failed | `1` |

Invalid arguments exit with code `2`.
Empty and failed runs have a manifest but no ZIP.
A navigation failure also saves `error.png` when the browser remains available.

Browser operations default to a 30-second timeout.
Download-event and transfer-completion waits each default to 120 seconds.
Configure them with `--timeout SECONDS` and `--download-timeout SECONDS`.
Failed downloads are not automatically retried.

## Python integration

```python
import asyncio
from pathlib import Path

from senpilot.models import DocumentType, DownloadRequest
from senpilot.pipeline import download_matter

request = DownloadRequest("M12205", DocumentType.OTHER_DOCUMENTS, limit=10)
result = asyncio.run(download_matter(request, Path("downloads")))
print(result.status, result.zip_path, result.manifest_path)
```

In an async application, call `await download_matter(...)` directly.
The CLI and reusable function use the same browser adapter and verification code.
Email intake, email delivery, durable job scheduling, OCR, embeddings, and search are outside this download milestone.

## Development

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

The automated suite runs against a local HTTP fixture with real Chromium downloads.
It covers the click-activated editor, virtualized scrolling, source metadata, category limits, multiple attachments, duplicate filenames, restricted entries, corrupt responses, missing downloads, and failure exit codes.
Routine tests do not contact the public website.

## Source

- [Public documents database](https://uarb.novascotia.ca/fmi/webd/UARB15)
- [Official database guide](https://www.nserbt.ca/nsrab/matters-evidence/how-use-database)

The browser adapter depends on the public FileMaker layout and reports unrecognized metadata layouts as errors.
