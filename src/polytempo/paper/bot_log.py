"""Pretty console output for the paper trading bot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from polytempo.analysis import AnalysisResult
from polytempo.paper.run import ProfileRunResult, RunSummary


def _ts_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PreviewDateSection:
    target_date: date
    lead_hours: float | None
    summary: RunSummary | None = None
    missing_reason: str | None = None


def format_calibration_lead(analysis: AnalysisResult | None) -> str:
    """Calibration CSV lead row used for sigma (best_historical), or fallback note."""
    if analysis is None:
        return "—"
    if analysis.calibration_row is not None:
        row = analysis.calibration_row
        model = analysis.selected_model or row.model
        return f"{row.lead_hours:g}h/{model}"
    if analysis.fallback_reason:
        return f"fallback:{analysis.fallback_reason}"
    return "—"


def _gate_label(profile_id: str, profiles_by_id: dict[str, object]) -> str:
    profile = profiles_by_id.get(profile_id)
    if profile is None:
        return "—"
    gate = profile.entry_gate  # type: ignore[attr-defined]
    return f"{gate.target_lead_hours:.0f}h"


def format_tick_box(
    *,
    now: datetime,
    settle_count: int,
    profile_lines: list[str],
    next_settle_wake: datetime | None,
    next_gate_wake: datetime | None,
    next_gate_label: str | None,
) -> str:
    """Render one bot tick as a boxed summary."""
    width = 100
    title = f" paper bot {_ts_z(now)} "
    bar = "─" * max(4, width - len(title) - 2)
    lines = [
        f"┌{title}{bar}┐",
        f"│ settle sweep: done ({settle_count} settled)"[: width - 1].ljust(width - 1) + "│",
    ]
    if profile_lines:
        for pl in profile_lines:
            text = f"│ {pl}"[: width - 1].ljust(width - 1) + "│"
            lines.append(text)
    else:
        idle = "│ (no gate activity this tick)"[: width - 1].ljust(width - 1) + "│"
        lines.append(idle)

    parts: list[str] = []
    if next_settle_wake is not None:
        parts.append(f"settle {_ts_z(next_settle_wake)}")
    if next_gate_wake is not None and next_gate_label:
        parts.append(f"{next_gate_label} @ {_ts_z(next_gate_wake)}")
    wake = " · ".join(parts) if parts else "idle"
    wake_line = f"│ next wake: {wake}"[: width - 1].ljust(width - 1) + "│"
    lines.append(wake_line)
    lines.append(f"└{'─' * (width - 2)}┘")
    return "\n".join(lines)


def _preview_decision(result: ProfileRunResult) -> tuple[str, str, str]:
    if result.analysis is None:
        return ("—", "—", "—")
    buys = [row for row in result.analysis.rows if row.action.startswith("BUY")]
    if not buys:
        return ("SKIP", "—", "—")
    row = max(buys, key=lambda r: r.stake_usd or 0.0)
    stake = f"${row.stake_usd:.2f}" if row.stake_usd is not None else "—"
    return (row.action, row.label, stake)


def format_preview_report(
    *,
    now: datetime,
    sections: list[PreviewDateSection],
    profiles_by_id: dict[str, object] | None = None,
) -> str:
    """Render dry-run preview: one table per settlement date, no ledger side effects."""
    profiles_by_id = profiles_by_id or {}
    width = 100
    title = f" paper preview (dry-run) {_ts_z(now)} "
    bar = "─" * max(4, width - len(title) - 2)
    lines = [
        f"┌{title}{bar}┐",
        f"│ no DB writes — decisions at current lead only, not at profile gate times"[
            : width - 1
        ].ljust(width - 1)
        + "│",
    ]

    for section in sections:
        lines.append(f"│{'─' * (width - 2)}│")
        lead_s = (
            f"lead={section.lead_hours:.1f}h"
            if section.lead_hours is not None
            else "lead=?"
        )
        header = f"│ {section.target_date.isoformat()}  {lead_s}"
        if section.summary is not None:
            header += f"  {section.summary.event_title[: width - len(header) - 3]}"
        lines.append(header[: width - 1].ljust(width - 1) + "│")

        if section.missing_reason:
            msg = f"│   no Polymarket event ({section.missing_reason})"
            lines.append(msg[: width - 1].ljust(width - 1) + "│")
            continue

        if section.summary is None:
            continue

        col = (
            f"│   {'profile':<22} {'action':<8} {'bucket':<8} {'stake':<8} "
            f"{'gate':<5} {'cal_lead':<24}"
        )
        lines.append(col[: width - 1].ljust(width - 1) + "│")
        for profile in section.summary.profiles:
            action, bucket, stake = _preview_decision(profile)
            gate = _gate_label(profile.profile_id, profiles_by_id)
            cal_lead = format_calibration_lead(profile.analysis)
            row = (
                f"│   {profile.profile_id:<22} {action:<8} {bucket:<8} {stake:<8} "
                f"{gate:<5} {cal_lead:<24}"
            )
            lines.append(row[: width - 1].ljust(width - 1) + "│")

    lines.append(f"└{'─' * (width - 2)}┘")
    return "\n".join(lines)


def format_profile_line(
    profile_id: str,
    result: ProfileRunResult | None,
    *,
    lead_hours: float | None,
    gate_label: str | None = None,
) -> str:
    lead_s = f"lead={lead_hours:.1f}h" if lead_hours is not None else "lead=?"
    if result is None:
        gate = f" ({gate_label})" if gate_label else ""
        return f"{profile_id:<22} WAIT  {lead_s}{gate}"
    action = result.action
    cal_s = ""
    if result.analysis is not None:
        cal = format_calibration_lead(result.analysis)
        if cal != "—":
            cal_s = f" cal={cal}"
    extra = ""
    if action == "GATE_SKIP":
        extra = f" (gate {result.lead_hours and ''})"
    elif result.opened:
        t = result.opened[0]
        extra = f" → {t.bucket_label} ${t.stake_usd:.2f}"
    return f"{profile_id:<22} {action:<6} {lead_s}{cal_s}{extra}"


def format_run_summary(summary: RunSummary) -> str:
    lines: list[str] = []
    lines.append(f"event: {summary.event_title} ({summary.event_id})")
    if summary.target_date:
        lines.append(f"target_date: {summary.target_date}")
    if summary.resolved:
        winner = summary.winning_label or "(no winner)"
        lines.append(f"status: RESOLVED winner={winner}")
    else:
        lines.append("status: OPEN (unresolved)")
    lines.append("")
    for p in summary.profiles:
        lh = f" lead={p.lead_hours:.1f}h" if p.lead_hours is not None else ""
        lines.append(f"[{p.profile_id}] {p.action}{lh}")
        if p.analysis is not None:
            cal = format_calibration_lead(p.analysis)
            if cal != "—":
                lines.append(f"  calibration_stats lead: {cal}")
            action, bucket, stake = _preview_decision(p)
            if action != "SKIP":
                lines.append(f"  → {action} {bucket} stake={stake}")
        for t in p.opened:
            entry = t.entry_price if t.entry_price is not None else t.yes_ask
            lines.append(
                f"  + OPEN  {t.bucket_label:<14} side={t.side} "
                f"entry={entry:.2f} stake=${t.stake_usd:.2f} "
                f"shares={t.shares:.2f} edge={t.edge_pp:.2f}pp"
            )
        for t in p.settled:
            lines.append(
                f"  - SETTLE {t.bucket_label:<14} side={t.side} "
                f"stake=${t.stake_usd:.2f} shares={t.shares:.2f}"
            )
    return "\n".join(lines)
