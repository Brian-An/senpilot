"""Command-line interface for the regulatory download pipeline."""

import argparse
import asyncio
import json
import logging
import math
from collections.abc import Sequence
from pathlib import Path

from senpilot.models import DocumentType, DownloadRequest, ResultStatus
from senpilot.pipeline import download_matter
from senpilot.uarb import SOURCE_URL


def positive_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0.001:
        raise argparse.ArgumentTypeError(
            "timeout must be a finite number of at least 0.001 seconds"
        )
    return number


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download public regulatory filings into a ZIP.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("migrate", help="Initialize the private PostgreSQL job schema")
    worker = subcommands.add_parser("worker", help="Process queued email requests")
    worker.add_argument("--once", action="store_true", help="Process at most one queued job")
    download = subcommands.add_parser("download", help="Download one matter's document category")
    download.add_argument("matter_number", help="Matter number, for example M12205")
    download.add_argument(
        "--type", required=True, choices=[category.value for category in DocumentType]
    )
    download.add_argument(
        "--limit", type=int, default=10, help="Maximum source entries and files (1-10)"
    )
    download.add_argument("--output", type=Path, default=Path("downloads"))
    download.add_argument("--headed", action="store_true", help="Show the browser")
    download.add_argument("--timeout", type=positive_seconds, default=30.0, metavar="SECONDS")
    download.add_argument(
        "--download-timeout", type=positive_seconds, default=120.0, metavar="SECONDS"
    )
    args = parser.parse_args(argv)
    if args.command in {"worker", "migrate"}:
        from senpilot.config import required
        from senpilot.jobs import migrate
        from senpilot.worker import run_worker

        logging.basicConfig(level=logging.INFO, format="%(message)s")
        try:
            operation = (
                migrate(required("DATABASE_URL"))
                if args.command == "migrate"
                else run_worker(once=args.once)
            )
            asyncio.run(operation)
        except ValueError as exc:
            parser.error(str(exc))
        except KeyboardInterrupt:
            return 130
        return 0
    try:
        request = DownloadRequest(args.matter_number, DocumentType(args.type), args.limit)
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        result = asyncio.run(
            download_matter(
                request,
                args.output,
                headless=not args.headed,
                base_url=SOURCE_URL,
                timeout_ms=int(args.timeout * 1000),
                download_timeout_ms=int(args.download_timeout * 1000),
            )
        )
    except OSError as exc:
        logging.error("Cannot write download output: %s", exc)
        return 1
    except KeyboardInterrupt:
        logging.error("Download cancelled")
        return 130
    print(
        json.dumps(
            {
                "status": result.status.value,
                "downloaded_count": result.downloaded_count,
                "manifest_path": str(result.manifest_path),
                "zip_path": str(result.zip_path) if result.zip_path else None,
                "error": result.error,
            },
            indent=2,
        )
    )
    return 0 if result.status in {ResultStatus.COMPLETE, ResultStatus.EMPTY} else 1
