"""Repository paths used by the performance viewer."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CSV = REPO_ROOT / "reports/performance/daily.csv"
DEFAULT_PREFS = REPO_ROOT / "reports/visualizer/prefs.json"
BACKTEST_PROFILES = REPO_ROOT / "config/backtest_profiles.yaml"
RUN_WITH_ENV = REPO_ROOT / "deploy/bin/run-with-env.sh"
EXPORT_SCRIPT = REPO_ROOT / "scripts/report_performance.py"

ROW_HEIGHT = 35
HEADER_HEIGHT = 40
