"""Server-side visualizer prefs (JSON file on the Streamlit host).

Stores last-used filter knobs, distribution overlays, and CSV path presets so a
restart does not reopen every wallet / every strategy. The performance page
still *opens* on the default CSV; presets are only a picker.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from polytempo.analysis import MODEL_STRATEGIES
from polytempo.visualizer.paths import DEFAULT_PREFS, REPO_ROOT

_MAX_CSV_PRESETS = 20

# First visit (no saved overlays): one strategy, not the full MODEL_STRATEGIES set.
DEFAULT_ENABLED_STRATS: tuple[str, ...] = ("weighted_historical_updated",)

_PREFERRED_MODELS: tuple[str, ...] = (
    "weighted_historical_updated",
    "best_historical_updated",
    "best_historical",
)


@dataclass
class FilterPrefs:
    """Last-used performance-page knob filters. Empty list = no filter (show all)."""

    models: list[str] = field(default_factory=list)
    trades: list[str] = field(default_factory=list)
    lead_lo: int | None = None
    lead_hi: int | None = None
    exits: list[str] = field(default_factory=list)
    budgets: list[str] = field(default_factory=list)


@dataclass
class OverlayPrefs:
    show_forecasts: bool = True
    show_market: bool = True
    show_resolved: bool = True
    enabled_strats: list[str] = field(default_factory=lambda: list(DEFAULT_ENABLED_STRATS))


@dataclass
class VisualizerPrefs:
    csv_presets: list[str] = field(default_factory=list)
    filters: FilterPrefs | None = None
    overlays: OverlayPrefs | None = None


def knob_options(values: Iterable[object]) -> list[str]:
    """Distinct non-empty knob labels from a CSV column, sorted."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text or text.lower() == "nan":
            continue
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return sorted(out)


def default_models(available: list[str]) -> list[str]:
    """Light first-run model filter: one preferred model that exists in the CSV."""
    wanted = set(available)
    for model in _PREFERRED_MODELS:
        if model in wanted:
            return [model]
    return [available[0]] if available else []


def intersect_saved(saved: list[str], available: list[str]) -> list[str]:
    """Keep saved values that still exist, preserving saved order."""
    allowed = set(available)
    return [item for item in saved if item in allowed]


def resolve_models(saved: list[str] | None, available: list[str]) -> list[str]:
    """Restore last models, or a single default on first visit / empty intersection."""
    if saved is None:
        return default_models(available)
    found = intersect_saved(saved, available)
    if found:
        return found
    if saved:
        return default_models(available)
    return []


def resolve_leads(
    lead_lo: int | None,
    lead_hi: int | None,
    lead_values: list[int],
) -> tuple[int, int] | None:
    """Clamp a saved lead range onto the leads present in this CSV."""
    if not lead_values:
        return None
    if lead_lo is None or lead_hi is None:
        return lead_values[0], lead_values[-1]
    lo, hi = (lead_lo, lead_hi) if lead_lo <= lead_hi else (lead_hi, lead_lo)
    in_range = [value for value in lead_values if lo <= value <= hi]
    if in_range:
        return in_range[0], in_range[-1]
    return lead_values[0], lead_values[-1]


def enabled_strats_from_prefs(overlays: OverlayPrefs | None) -> frozenset[str]:
    """Strategies that start enabled. Unknown names are dropped."""
    known = set(MODEL_STRATEGIES)
    if overlays is None:
        return frozenset(s for s in DEFAULT_ENABLED_STRATS if s in known)
    return frozenset(s for s in overlays.enabled_strats if s in known)


def normalize_csv_preset(path: Path, *, repo_root: Path = REPO_ROOT) -> str:
    """Repo-relative posix path when possible; otherwise an absolute path."""
    resolved = path.expanduser().resolve()
    root = repo_root.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return str(resolved)


def resolve_csv_preset(preset: str, *, repo_root: Path = REPO_ROOT) -> Path:
    raw = Path(preset).expanduser()
    if raw.is_absolute():
        return raw
    return (repo_root / raw).resolve()


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip() != ""]


def _as_optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_filters(raw: object) -> FilterPrefs | None:
    if not isinstance(raw, dict):
        return None
    return FilterPrefs(
        models=_as_str_list(raw.get("models")),
        trades=_as_str_list(raw.get("trades")),
        lead_lo=_as_optional_int(raw.get("lead_lo")),
        lead_hi=_as_optional_int(raw.get("lead_hi")),
        exits=_as_str_list(raw.get("exits")),
        budgets=_as_str_list(raw.get("budgets")),
    )


def _parse_overlays(raw: object) -> OverlayPrefs | None:
    if not isinstance(raw, dict):
        return None
    enabled = raw.get("enabled_strats")
    if enabled is None:
        enabled_strats = list(DEFAULT_ENABLED_STRATS)
    else:
        enabled_strats = _as_str_list(enabled)
    return OverlayPrefs(
        show_forecasts=bool(raw.get("show_forecasts", True)),
        show_market=bool(raw.get("show_market", True)),
        show_resolved=bool(raw.get("show_resolved", True)),
        enabled_strats=enabled_strats,
    )


def prefs_from_dict(raw: object) -> VisualizerPrefs:
    if not isinstance(raw, dict):
        return VisualizerPrefs()
    return VisualizerPrefs(
        csv_presets=_as_str_list(raw.get("csv_presets")),
        filters=_parse_filters(raw.get("filters")),
        overlays=_parse_overlays(raw.get("overlays")),
    )


def load_prefs(path: Path | None = None) -> VisualizerPrefs:
    target = path or DEFAULT_PREFS
    if not target.is_file():
        return VisualizerPrefs()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return VisualizerPrefs()
    return prefs_from_dict(raw)


def save_prefs(prefs: VisualizerPrefs, path: Path | None = None) -> None:
    target = path or DEFAULT_PREFS
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "csv_presets": prefs.csv_presets,
        "filters": asdict(prefs.filters) if prefs.filters is not None else None,
        "overlays": asdict(prefs.overlays) if prefs.overlays is not None else None,
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)


def add_csv_preset(
    path: Path,
    *,
    prefs_path: Path | None = None,
    default_csv: Path | None = None,
    repo_root: Path = REPO_ROOT,
) -> VisualizerPrefs:
    """Remember a used CSV path. Skips the default daily file and missing paths."""
    prefs = load_prefs(prefs_path)
    if not path.is_file():
        return prefs
    normalized = normalize_csv_preset(path, repo_root=repo_root)
    if default_csv is not None:
        if normalize_csv_preset(default_csv, repo_root=repo_root) == normalized:
            return prefs
    presets = [item for item in prefs.csv_presets if item != normalized]
    prefs.csv_presets = [normalized, *presets][:_MAX_CSV_PRESETS]
    save_prefs(prefs, prefs_path)
    return prefs


def save_filters(filters: FilterPrefs, *, prefs_path: Path | None = None) -> VisualizerPrefs:
    prefs = load_prefs(prefs_path)
    prefs.filters = filters
    save_prefs(prefs, prefs_path)
    return prefs


def save_overlays(overlays: OverlayPrefs, *, prefs_path: Path | None = None) -> VisualizerPrefs:
    prefs = load_prefs(prefs_path)
    prefs.overlays = overlays
    save_prefs(prefs, prefs_path)
    return prefs
