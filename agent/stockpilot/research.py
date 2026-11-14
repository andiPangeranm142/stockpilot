"""Rule-based, fully transparent research model for StockPilot.

Each investable token gets a total score of 0-100 from four components:

1. Momentum (weight 40%)
   ``return = (latest_nav - earliest_nav) / earliest_nav`` where earliest/latest
   come from the local price history (``agent/data/price-history.jsonl``),
   which the agent appends to on every run.
   ``momentum = clamp(50 + return * 500, 0, 100)``
   so a +10% move scores 100 and a -10% move scores 0. If fewer than 2 history
   points exist, or the history span is under 24 hours, the component is
   neutral (50) and the reason is recorded — no momentum is invented.

2. Dividend / multiplier accrual (weight 20%)
   Coinbase reinvests distributions by raising the token's ``multiplier``
   (shares per token) above 1.0.
   ``multiplier_score = clamp(50 + min(50, (multiplier - 1) * 5000), 0, 100)``
   A multiplier of exactly 1.0 scores 50 (neutral); 1.01 scores 100.

3. Adoption (weight 20%)
   Relative rank of ``log10(total_supply)`` within the current universe,
   min-max scaled to 0-100. Tokens without supply data score a neutral 50.
   If all supplies are equal (or there is only one token), everyone scores 50.

4. Data freshness (weight 20%)
   ``100`` if the NAV was updated less than 24h ago, declining linearly to
   ``0`` at 7 days (the staleness limit; older NAVs are excluded outright).

``total = 0.40*momentum + 0.20*multiplier + 0.20*adoption + 0.20*freshness``

Tokens without a usable NAV are excluded (score ``None``) — never scored on
invented data.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .api_client import STALENESS_LIMIT, TokenSnapshot

log = logging.getLogger(__name__)

W_MOMENTUM = 0.40
W_MULTIPLIER = 0.20
W_ADOPTION = 0.20
W_FRESHNESS = 0.20

MIN_HISTORY_POINTS = 2
MIN_HISTORY_SPAN = timedelta(hours=24)
FRESHNESS_FULL_HOURS = 24.0
MOMENTUM_SCALE = 500.0  # +/-10% NAV move maps to the full 0-100 range
MULTIPLIER_SCALE = 5000.0  # multiplier 1.01 -> +50 points


@dataclass
class TokenScore:
    symbol: str
    total: float | None
    momentum: float | None = None
    multiplier_score: float | None = None
    adoption: float | None = None
    freshness: float | None = None
    notes: list[str] = field(default_factory=list)
    excluded_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "momentum": self.momentum,
            "multiplier": self.multiplier_score,
            "adoption": self.adoption,
            "freshness": self.freshness,
            "notes": self.notes,
            "excluded_reason": self.excluded_reason,
        }


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Price history (local JSONL, one record per day+symbol)
# ---------------------------------------------------------------------------

HistoryPoint = tuple[datetime, Decimal]  # (nav timestamp, nav price)


def load_history(path: str | Path) -> dict[str, list[HistoryPoint]]:
    """Read price history: symbol -> time-sorted [(timestamp, nav_price)]."""
    history: dict[str, list[HistoryPoint]] = {}
    path = Path(path)
    if not path.exists():
        return history
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            symbol = record["symbol"]
            price = Decimal(str(record["nav_price"]))
            ts_raw = record.get("nav_updated_at") or record.get("date")
            if ts_raw is None:
                continue
            if len(str(ts_raw)) == 10:  # plain YYYY-MM-DD date
                ts = datetime.fromisoformat(str(ts_raw)).replace(tzinfo=timezone.utc)
            else:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            history.setdefault(symbol, []).append((ts, price))
        except (KeyError, ValueError, TypeError):
            continue
    for points in history.values():
        points.sort(key=lambda p: p[0])
    return history


def append_history(
    path: str | Path, snapshots: list[TokenSnapshot], run_date: str
) -> int:
    """Append today's included snapshots, deduped per (day, symbol).

    Returns the number of records actually appended.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[tuple[str, str]] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                existing.add((record["date"], record["symbol"]))
            except (KeyError, ValueError, TypeError):
                continue
    appended = 0
    with path.open("a", encoding="utf-8") as fh:
        for snap in snapshots:
            if not snap.included or snap.nav_price is None:
                continue
            key = (run_date, snap.symbol)
            if key in existing:
                continue
            record = {
                "date": run_date,
                "symbol": snap.symbol,
                "nav_price": str(snap.nav_price),
                "nav_updated_at": (
                    snap.nav_updated_at.isoformat() if snap.nav_updated_at else None
                ),
                "source": snap.source,
            }
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            existing.add(key)
            appended += 1
    return appended


# ---------------------------------------------------------------------------
# Scoring components
# ---------------------------------------------------------------------------

def momentum_score(points: list[HistoryPoint]) -> tuple[float, str]:
    """Momentum vs the earliest local history point (see module docstring)."""
    if len(points) < MIN_HISTORY_POINTS:
        return 50.0, "neutral: fewer than 2 history points"
    first_ts, first_price = points[0]
    last_ts, last_price = points[-1]
    if last_ts - first_ts < MIN_HISTORY_SPAN:
        return 50.0, "neutral: history span under 24h"
    if first_price <= 0:
        return 50.0, "neutral: earliest price not positive"
    ret = float((last_price - first_price) / first_price)
    score = _clamp(50.0 + ret * MOMENTUM_SCALE)
    return score, f"NAV {ret:+.2%} vs earliest snapshot ({first_ts.date().isoformat()})"


def multiplier_score(multiplier: Decimal) -> tuple[float, str]:
    """Dividend-accrual score from the share-per-token multiplier."""
    mult = float(multiplier)
    score = _clamp(50.0 + min(50.0, (mult - 1.0) * MULTIPLIER_SCALE))
    if mult > 1.0:
        note = f"multiplier {multiplier} (>1.0 = reinvested distributions)"
    elif mult < 1.0:
        note = f"multiplier {multiplier} (<1.0)"
    else:
        note = "multiplier 1.0 (no accrual yet)"
    return score, note


def adoption_scores(supplies: dict[str, Decimal | None]) -> dict[str, tuple[float, str]]:
    """Log-supply rank within the universe, min-max scaled to 0-100."""
    logs: dict[str, float] = {}
    for symbol, supply in supplies.items():
        if supply is not None and supply > 0:
            logs[symbol] = math.log10(float(supply))
    result: dict[str, tuple[float, str]] = {}
    if not logs:
        for symbol in supplies:
            result[symbol] = (50.0, "neutral: no total_supply data in universe")
        return result
    lo, hi = min(logs.values()), max(logs.values())
    for symbol in supplies:
        if symbol not in logs:
            result[symbol] = (50.0, "neutral: no total_supply data")
        elif hi == lo:
            result[symbol] = (50.0, "neutral: identical supply across universe")
        else:
            score = (logs[symbol] - lo) / (hi - lo) * 100.0
            result[symbol] = (round(score, 2), f"log10(total_supply) rank in universe")
    return result


def freshness_score(updated_at: datetime | None, now: datetime) -> tuple[float, str]:
    """100 if NAV <24h old, linear decline to 0 at the 7-day staleness limit."""
    if updated_at is None:
        return 0.0, "no NAV timestamp"
    age_hours = (now - updated_at).total_seconds() / 3600.0
    if age_hours <= FRESHNESS_FULL_HOURS:
        return 100.0, f"NAV updated {age_hours:.1f}h ago"
    limit_hours = STALENESS_LIMIT.total_seconds() / 3600.0
    score = _clamp(100.0 * (1.0 - (age_hours - FRESHNESS_FULL_HOURS) / (limit_hours - FRESHNESS_FULL_HOURS)))
    return round(score, 2), f"NAV updated {age_hours:.1f}h ago"


# ---------------------------------------------------------------------------
# Full scoring pass
# ---------------------------------------------------------------------------

def score_tokens(
    snapshots: list[TokenSnapshot],
    history: dict[str, list[HistoryPoint]],
    now: datetime,
) -> dict[str, TokenScore]:
    """Score every snapshot; excluded tokens get total=None."""
    included = [s for s in snapshots if s.included]
    adoption = adoption_scores({s.symbol: s.total_supply for s in included})
    scores: dict[str, TokenScore] = {}
    for snap in snapshots:
        if not snap.included:
            scores[snap.symbol] = TokenScore(
                symbol=snap.symbol,
                total=None,
                excluded_reason=snap.excluded_reason,
            )
            continue
        mom, mom_note = momentum_score(history.get(snap.symbol, []))
        mult, mult_note = multiplier_score(snap.multiplier)
        adopt, adopt_note = adoption.get(snap.symbol, (50.0, "neutral"))
        fresh, fresh_note = freshness_score(snap.nav_updated_at, now)
        total = round(
            W_MOMENTUM * mom
            + W_MULTIPLIER * mult
            + W_ADOPTION * adopt
            + W_FRESHNESS * fresh,
            2,
        )
        scores[snap.symbol] = TokenScore(
            symbol=snap.symbol,
            total=total,
            momentum=round(mom, 2),
            multiplier_score=round(mult, 2),
            adoption=round(adopt, 2),
            freshness=round(fresh, 2),
            notes=[mom_note, mult_note, adopt_note, fresh_note],
        )
    return scores
