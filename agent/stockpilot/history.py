"""Build the dashboard history file (``docs/data/history.json``).

The dashboard is served from ``docs/`` by GitHub Pages, so it cannot read
``agent/data/``. Each agent run therefore publishes a small JSON summary of
the *already recorded* history for the dashboard charts:

- NAV price series per token, from ``agent/data/price-history.jsonl``
  (one record per day+symbol, appended on every run). Each point is keyed
  by the *recording date* (the day the agent observed the value) — the
  NAV's own ``nav_updated_at`` is kept alongside it. Keying by recording
  date matters: NAV feeds hold Friday's close over the weekend, so several
  recorded days can share one ``nav_updated_at``; keying by it would
  collapse distinct observations into a single point.
- Total research-score series per token, from ``agent/data/decisions/*.json``
  (one file per run).

Every point in the output traces back to a recorded line/file — nothing is
interpolated, projected, or estimated. A series with fewer than 2 points is
still published as-is; the dashboard labels the point count honestly and
only draws a chart when at least 2 points exist.

Output format (``docs/data/history.json``)::

    {
      "generated_at": "2026-11-16T09:41:00+00:00",
      "sources": {
        "nav_prices": "agent/data/price-history.jsonl",
        "scores": "agent/data/decisions/*.json"
      },
      "nav_dates": ["2026-11-13", "2026-11-14", ...],
      "score_runs": ["2026-11-13T23:59:31+00:00", ...],
      "nav_prices": {"NVDAc": [{"t": "2026-11-13T00:00:00+00:00",
                                "price": "160.77",
                                "nav_updated_at": "2026-11-13T23:36:23+00:00"}, ...], ...},
      "scores": {"NVDAc": [{"t": "...", "total": 70.54}, ...], ...}
    }

Prices are kept as strings to preserve the exact recorded decimals; score
totals are the numbers recorded in the decision files. For NAV points, ``t``
is the recording date at UTC midnight (the record stores a date, not a
time); ``nav_updated_at`` is the timestamp Chainlink published that NAV
value. For score points, ``t`` is the decision run's timestamp.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _read_nav_series(history_file: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Per-symbol NAV series from price-history.jsonl, keyed by recording date.

    One point per recorded (date, symbol). The point's ``t`` is the
    recording date at UTC midnight; the NAV's own ``nav_updated_at`` is
    preserved alongside it. Malformed lines are skipped, never fatal.
    """
    series: dict[str, dict[str, dict[str, Any]]] = {}
    path = Path(history_file)
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            symbol = record["symbol"]
            date = record["date"]
            price = str(record["nav_price"])
            # Validate the date parses as YYYY-MM-DD.
            datetime.strptime(date, "%Y-%m-%d")
        except (KeyError, ValueError, TypeError):
            continue
        point = {
            "t": f"{date}T00:00:00+00:00",
            "price": price,
            "nav_updated_at": record.get("nav_updated_at"),
        }
        # append_history dedups per (day, symbol); if a duplicate ever
        # appears, the last record wins deterministically.
        series.setdefault(symbol, {})[date] = point
    return {
        symbol: [series[symbol][date] for date in sorted(series[symbol])]
        for symbol in sorted(series)
    }


def _read_decision_scores(decisions_dir: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Per-symbol total-score series from decision files, time-sorted.

    Malformed or unreadable files are skipped (with a warning), never
    fatal: a partial series is still honest as long as every point is real.
    """
    scores: dict[str, list[dict[str, Any]]] = {}
    decisions_dir = Path(decisions_dir)
    if not decisions_dir.is_dir():
        return scores
    entries: list[tuple[str, dict]] = []
    for path in sorted(decisions_dir.glob("*.json")):
        try:
            decision = json.loads(path.read_text(encoding="utf-8"))
            ts = decision.get("timestamp")
            if not isinstance(ts, str) or not ts:
                log.warning("history: skipping %s (no timestamp)", path.name)
                continue
            entries.append((ts, decision))
        except (ValueError, OSError) as exc:
            log.warning("history: skipping unreadable decision file %s: %s", path.name, exc)
    entries.sort(key=lambda item: item[0])
    for ts, decision in entries:
        for symbol, score in (decision.get("scores") or {}).items():
            total = score.get("total") if isinstance(score, dict) else None
            if total is None:
                continue  # excluded token in that run — no point, no invention
            scores.setdefault(symbol, []).append({"t": ts, "total": total})
    return scores


def build_history_payload(
    history_file: str | Path,
    decisions_dir: str | Path,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the history payload from the recorded data files."""
    nav_prices = _read_nav_series(history_file)
    nav_dates = sorted(
        {pt["t"][:10] for series in nav_prices.values() for pt in series}
    )

    scores = _read_decision_scores(decisions_dir)
    score_runs = sorted({pt["t"] for series in scores.values() for pt in series})

    return {
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "sources": {
            "nav_prices": "agent/data/price-history.jsonl",
            "scores": "agent/data/decisions/*.json",
        },
        "nav_dates": sorted(nav_dates),
        "score_runs": score_runs,
        "nav_prices": nav_prices,
        "scores": scores,
    }


def write_history_json(
    history_file: str | Path,
    decisions_dir: str | Path,
    out_path: str | Path,
) -> dict[str, Any]:
    """Build the history payload and write it to ``out_path`` (JSON).

    Returns the payload. The caller decides whether a failure here is
    fatal; this function raises on I/O errors so the caller can log them.
    """
    payload = build_history_payload(history_file, decisions_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    nav_points = sum(len(v) for v in payload["nav_prices"].values())
    score_points = sum(len(v) for v in payload["scores"].values())
    log.info(
        "history.json written: %s (%d NAV points across %d symbols, "
        "%d score points across %d symbols, %d score runs)",
        out_path,
        nav_points,
        len(payload["nav_prices"]),
        score_points,
        len(payload["scores"]),
        len(payload["score_runs"]),
    )
    return payload
