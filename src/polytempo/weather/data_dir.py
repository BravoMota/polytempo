"""Repository-root weather calibration data directory."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WEATHER_DATA_DIR = REPO_ROOT / "data" / "weather"
