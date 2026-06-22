"""Tests for scripts/view_performance.py export helper."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "view_performance.py"


def _load_view_module():
    spec = importlib.util.spec_from_file_location("view_performance", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    source = source.rsplit("\nmain()\n", 1)[0]
    exec(compile(source, str(SCRIPT_PATH), "exec"), module.__dict__)
    sys.modules["view_performance"] = module
    return module


view = _load_view_module()


def test_export_command_args() -> None:
    csv_path = REPO_ROOT / "reports/performance/daily.csv"
    cmd = view._export_command(csv_path)
    assert cmd[0].endswith("run-with-env.sh")
    assert cmd[1].endswith("report_performance.py")
    assert "--all" in cmd
    assert "--csv" in cmd
    assert cmd[-1] == str(csv_path)


def test_run_export_success(tmp_path: Path) -> None:
    csv_path = tmp_path / "daily.csv"
    mock_result = MagicMock(returncode=0, stdout="wrote daily.csv (10 rows)\n", stderr="")
    with patch.object(view.subprocess, "run", return_value=mock_result) as run:
        ok, msg = view._run_export(csv_path)
    assert ok is True
    assert "10 rows" in msg
    run.assert_called_once()
    assert run.call_args.kwargs["cwd"] == view.REPO_ROOT
