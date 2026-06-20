"""Run reporting.

Captures every ``httpx.get`` call made inside a ``with RunReporter():`` block
plus user-supplied markdown sections, and writes a single markdown file with the
full inputs, intermediate state, raw API responses, and final analysis result.

The reporter monkey-patches ``httpx.get`` at the module level for the duration
of the block, so any code path that calls ``httpx.get(...)`` (forecast or
markets ingestion) is recorded automatically without changes to those modules.
"""

from __future__ import annotations

import json
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx


@dataclass
class HttpCall:
    """One captured outbound HTTP call."""

    method: str
    url: str
    params: dict[str, Any] | None
    status_code: int | None
    duration_ms: float
    response_json: Any | None
    response_text: str | None
    error: str | None


@dataclass
class RunReporter(AbstractContextManager["RunReporter"]):
    """Markdown run reporter with automatic httpx.get capture."""

    output_dir: str | Path = "reports/live"
    filename: str | None = None
    title: str = "PolyTempo live run"

    _started_at: datetime = field(init=False)
    _sections: list[tuple[str, str]] = field(init=False, default_factory=list)
    _http_calls: list[HttpCall] = field(init=False, default_factory=list)
    _original_get: Any = field(init=False, default=None)
    _error: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._started_at = datetime.now(timezone.utc)

    def __enter__(self) -> "RunReporter":
        self._original_get = httpx.get
        httpx.get = self._patched_get  # type: ignore[assignment]
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if self._original_get is not None:
            httpx.get = self._original_get  # type: ignore[assignment]
            self._original_get = None
        if exc is not None and exc_type is not None:
            self._error = f"{exc_type.__name__}: {exc}"
        return False

    def section(self, title: str, body: str) -> None:
        """Append a markdown section to the report."""
        self._sections.append((title, body.rstrip()))

    @property
    def http_calls(self) -> list[HttpCall]:
        return list(self._http_calls)

    def write(self) -> Path:
        """Render the report to disk and return the file path."""
        directory = Path(self.output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (self.filename or self._default_filename())
        path.write_text(self._render(), encoding="utf-8")
        return path

    def _patched_get(self, url: Any, **kwargs: Any) -> httpx.Response:
        params = kwargs.get("params")
        call = HttpCall(
            method="GET",
            url=str(url),
            params=dict(params) if params else None,
            status_code=None,
            duration_ms=0.0,
            response_json=None,
            response_text=None,
            error=None,
        )
        started = time.perf_counter()
        try:
            response = self._original_get(url, **kwargs)
        except Exception as exc:
            call.duration_ms = (time.perf_counter() - started) * 1000.0
            call.error = f"{type(exc).__name__}: {exc}"
            self._http_calls.append(call)
            raise

        call.duration_ms = (time.perf_counter() - started) * 1000.0
        call.status_code = response.status_code
        try:
            call.response_json = response.json()
        except Exception:
            call.response_text = response.text
        self._http_calls.append(call)
        return response

    def _default_filename(self) -> str:
        stamp = self._started_at.strftime("%Y%m%dT%H%M%SZ")
        return f"live_{stamp}.md"

    def _render(self) -> str:
        lines: list[str] = []
        lines.append(f"# {self.title} — {self._started_at.isoformat()}")
        lines.append("")
        if self._error is not None:
            lines.append(f"**Run ended with error:** `{self._error}`")
            lines.append("")
        for title, body in self._sections:
            lines.append(f"## {title}")
            lines.append("")
            lines.append(body)
            lines.append("")
        lines.append("## HTTP calls")
        lines.append("")
        if not self._http_calls:
            lines.append("_No HTTP calls captured._")
            lines.append("")
        for index, call in enumerate(self._http_calls, start=1):
            lines.append(f"### {index}. {call.method} {call.url}")
            lines.append("")
            lines.append(
                f"- status: {call.status_code if call.status_code is not None else 'n/a'}"
            )
            lines.append(f"- duration_ms: {call.duration_ms:.1f}")
            if call.params:
                lines.append("- params:")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(call.params, indent=2, default=str))
                lines.append("```")
            if call.error:
                lines.append(f"- error: `{call.error}`")
            response_lines = _format_response_body(call)
            if response_lines:
                lines.extend(response_lines)
            elif call.response_text is not None:
                lines.append("- response (text):")
                lines.append("")
                lines.append("```")
                lines.append(call.response_text)
                lines.append("```")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _is_gamma_events_list(url: str) -> bool:
    """True for GET /events (search/list), not GET /events/{id}."""
    path = url.split("?", 1)[0].rstrip("/")
    if not path.endswith("/events"):
        return False
    suffix = path.rsplit("/events", 1)[-1]
    return suffix == "" or suffix == "/"


def _summarize_gamma_events_list(payload: Any) -> str:
    if not isinstance(payload, list):
        return "not a JSON array"
    count = len(payload)
    lines = [f"**{count}** events returned"]
    for item in payload[:5]:
        if isinstance(item, dict):
            title = item.get("title") or item.get("slug") or "(no title)"
            event_id = item.get("id", "?")
            lines.append(f"- `{event_id}`: {title}")
    if count > 5:
        lines.append(f"- … and {count - 5} more")
    return "\n".join(lines)


def _is_open_meteo_forecast(url: str) -> bool:
    return "api.open-meteo.com" in url and "/forecast" in url


def _is_open_meteo_meta(url: str) -> bool:
    return "openmeteo.s3.amazonaws.com" in url and url.rstrip("/").endswith("meta.json")


def _summarize_open_meteo_forecast(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "not a JSON object"
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return "missing daily block"
    times = daily.get("time") or []
    lines = [
        f"returned lat/lon: {payload.get('latitude')}, {payload.get('longitude')}",
        f"daily dates: {', '.join(str(t) for t in times)}",
    ]
    for key, values in sorted(daily.items()):
        if key == "time" or not key.startswith("temperature_2m_max"):
            continue
        model = key.removeprefix("temperature_2m_max_") or "(default)"
        sample = values[0] if isinstance(values, list) and values else "—"
        lines.append(f"- `{model}`: {sample}°C")
    return "\n".join(lines)


def _summarize_open_meteo_meta(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "not a JSON object"
    keys = (
        "last_run_initialisation_time",
        "last_run_availability_time",
        "last_run_modification_time",
        "update_interval_seconds",
        "temporal_resolution_seconds",
    )
    lines: list[str] = []
    for key in keys:
        if key in payload:
            lines.append(f"- {key}: `{payload[key]}`")
    if "data_end_time" in payload:
        lines.append(f"- data_end_time: `{payload['data_end_time']}`")
    return "\n".join(lines) if lines else "empty meta object"


def _format_response_body(call: HttpCall) -> list[str]:
    if call.response_json is None:
        return []
    if _is_gamma_events_list(call.url):
        lines = ["- response (summary):"]
        lines.append("")
        lines.append(_summarize_gamma_events_list(call.response_json))
        return lines
    if _is_open_meteo_forecast(call.url):
        lines = ["- response (summary):"]
        lines.append("")
        lines.append(_summarize_open_meteo_forecast(call.response_json))
        return lines
    if _is_open_meteo_meta(call.url):
        lines = ["- response (summary):"]
        lines.append("")
        lines.append(_summarize_open_meteo_meta(call.response_json))
        return lines
    lines = ["- response (JSON):", ""]
    lines.append("```json")
    lines.append(json.dumps(call.response_json, indent=2, default=str))
    lines.append("```")
    return lines
