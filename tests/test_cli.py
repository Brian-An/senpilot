import json
from pathlib import Path

import pytest

from senpilot import cli


def test_cli_downloads_and_reports_usable_output_paths(
    fixture_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "SOURCE_URL", fixture_url)
    status = cli.main(
        [
            "download",
            "M12205",
            "--type",
            "Other Documents",
            "--limit",
            "2",
            "--output",
            str(tmp_path),
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert status == 0
    assert result["status"] == "complete"
    assert result["downloaded_count"] == 2
    assert Path(result["manifest_path"]).exists()
    assert Path(result["zip_path"]).exists()


@pytest.mark.parametrize(
    "args",
    [
        ["download", "M1234", "--type", "Exhibits"],
        ["download", "M12205", "--type", "Everything"],
        ["download", "M12205", "--type", "Exhibits", "--limit", "11"],
        ["download", "M12205", "--type", "Exhibits", "--timeout", "0"],
        ["download", "M12205", "--type", "Exhibits", "--timeout", "nan"],
    ],
)
def test_invalid_cli_input_exits_before_browser_navigation(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


def test_cli_partial_result_exits_nonzero(
    fixture_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "SOURCE_URL", f"{fixture_url}?case=partial")
    status = cli.main(
        [
            "download",
            "M12205",
            "--type",
            "Other Documents",
            "--limit",
            "3",
            "--output",
            str(tmp_path),
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert status == 1
    assert result["status"] == "partial"
    assert Path(result["zip_path"]).exists()
