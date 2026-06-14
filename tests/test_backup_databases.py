"""Tests for scripts/backup_databases.py."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "backup_databases.py"


def _load_backup_module():
    spec = importlib.util.spec_from_file_location("backup_databases", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["backup_databases"] = module
    spec.loader.exec_module(module)
    return module


backup = _load_backup_module()


def test_resolve_database_url_weather_prefers_polytempo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYTEMPO_DATABASE_URL", "postgresql://host/polytempo")
    monkeypatch.setenv("DATABASE_URL", "postgresql://host/other")
    assert backup.resolve_database_url("POLYTEMPO_DATABASE_URL") == "postgresql://host/polytempo"


def test_resolve_database_url_weather_falls_back_to_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POLYTEMPO_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://host/polytempo")
    assert backup.resolve_database_url("POLYTEMPO_DATABASE_URL") == "postgresql://host/polytempo"


def test_resolve_database_url_paper_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLYTEMPO_PAPER_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="POLYTEMPO_PAPER_DATABASE_URL"):
        backup.resolve_database_url("POLYTEMPO_PAPER_DATABASE_URL")


def test_resolve_output_dir_cli_override() -> None:
    assert backup.resolve_output_dir(override=Path("/tmp/custom")) == Path("/tmp/custom")


def test_resolve_output_dir_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYTEMPO_BACKUP_DIR", "/tmp/from-env")
    assert backup.resolve_output_dir(override=None) == Path("/tmp/from-env")


def test_dump_path_uses_date_folder_and_custom_extension() -> None:
    fixed = datetime(2026, 6, 14, 3, 0, 12, tzinfo=timezone.utc)
    path = backup.dump_path(Path("/backups"), "polytempo", now=fixed)
    assert path == Path("/backups/2026-06-14/polytempo_20260614T030012Z.dump")


def test_selected_databases_all_by_default() -> None:
    assert len(backup.selected_databases(None)) == 4


def test_selected_databases_subset() -> None:
    selected = backup.selected_databases(["weather", "paper"])
    assert selected == [
        ("weather", "POLYTEMPO_DATABASE_URL"),
        ("paper", "POLYTEMPO_PAPER_DATABASE_URL"),
    ]


def test_selected_databases_rejects_unknown() -> None:
    with pytest.raises(RuntimeError, match="unknown database name"):
        backup.selected_databases(["weather", "nope"])


def test_prune_old_backups_removes_expired_dirs(tmp_path: Path) -> None:
    old = tmp_path / "2026-05-01"
    keep = tmp_path / "2026-06-10"
    old.mkdir()
    keep.mkdir()
    (old / "polytempo.dump").write_text("x")
    (keep / "polytempo.dump").write_text("x")

    removed = backup.prune_old_backups(
        tmp_path,
        retention_days=14,
        today=date(2026, 6, 14),
    )

    assert removed == [old]
    assert not old.exists()
    assert keep.exists()


def test_prune_old_backups_skips_non_date_dirs(tmp_path: Path) -> None:
    misc = tmp_path / "notes"
    misc.mkdir()
    (misc / "readme.txt").write_text("keep")

    removed = backup.prune_old_backups(
        tmp_path,
        retention_days=1,
        today=date(2026, 6, 14),
    )

    assert removed == []
    assert misc.exists()


def test_prune_old_backups_dry_run_does_not_delete(tmp_path: Path) -> None:
    old = tmp_path / "2026-05-01"
    old.mkdir()

    removed = backup.prune_old_backups(
        tmp_path,
        retention_days=14,
        today=date(2026, 6, 14),
        dry_run=True,
    )

    assert removed == [old]
    assert old.exists()


def test_pg_dump_dry_run_does_not_invoke_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = {"run": False}

    def fake_run(*_args, **_kwargs):
        called["run"] = True

    monkeypatch.setattr(subprocess, "run", fake_run)
    out_path = tmp_path / "2026-06-14" / "polytempo.dump"
    backup.pg_dump("postgresql://host/polytempo", out_path, dry_run=True)

    assert called["run"] is False
    assert "would dump polytempo" in capsys.readouterr().out


def test_run_backup_dry_run_all_databases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("POLYTEMPO_DATABASE_URL", "postgresql://host/polytempo")
    monkeypatch.setenv("POLYTEMPO_TEST_DATABASE_URL", "postgresql://host/polytempo_test")
    monkeypatch.setenv("POLYTEMPO_PAPER_DATABASE_URL", "postgresql://host/polytempo_paper")
    monkeypatch.setenv(
        "POLYTEMPO_PAPER_TEST_DATABASE_URL",
        "postgresql://host/polytempo_paper_test",
    )
    fixed = datetime(2026, 6, 14, 3, 0, 0, tzinfo=timezone.utc)

    backup.run_backup(
        output_dir=tmp_path,
        dry_run=True,
        now=fixed,
    )

    out = capsys.readouterr().out
    assert out.count("would dump") == 4
    assert (tmp_path / "2026-06-14").exists() is False


def test_run_backup_skip_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("POLYTEMPO_DATABASE_URL", "postgresql://host/polytempo")
    monkeypatch.delenv("POLYTEMPO_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("POLYTEMPO_PAPER_DATABASE_URL", raising=False)
    monkeypatch.delenv("POLYTEMPO_PAPER_TEST_DATABASE_URL", raising=False)

    backup.run_backup(
        output_dir=tmp_path,
        only=["weather", "weather_test"],
        skip_missing=True,
        dry_run=True,
    )

    out = capsys.readouterr().out
    assert "skip weather_test" in out
    assert "would dump polytempo" in out


def test_main_dry_run_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("POLYTEMPO_DATABASE_URL", "postgresql://host/polytempo")
    monkeypatch.setenv("POLYTEMPO_TEST_DATABASE_URL", "postgresql://host/polytempo_test")
    monkeypatch.setenv("POLYTEMPO_PAPER_DATABASE_URL", "postgresql://host/polytempo_paper")
    monkeypatch.setenv(
        "POLYTEMPO_PAPER_TEST_DATABASE_URL",
        "postgresql://host/polytempo_paper_test",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backup_databases.py",
            "--once",
            "--dry-run",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert backup.main() == 0
    assert "would dump" in capsys.readouterr().out


def test_main_rejects_dry_run_without_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["backup_databases.py", "--dry-run"])
    with pytest.raises(SystemExit):
        backup.main()
    assert "requires --once" in capsys.readouterr().err


def test_run_daemon_runs_backup_when_due(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = {"count": 0}

    def fake_run_once(**_kwargs):
        calls["count"] += 1
        backup._stop = True
        return 0

    monkeypatch.setattr(backup, "_run_once", fake_run_once)
    monkeypatch.setattr(backup.time, "sleep", lambda _s: None)

    assert backup._run_daemon(
        output_dir=tmp_path,
        only=None,
        retention_days=14,
        skip_missing=False,
        anchor_time_utc="03:00",
    ) == 0
    assert calls["count"] == 1
    backup._stop = False
