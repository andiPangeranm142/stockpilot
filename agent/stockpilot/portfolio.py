"""Portfolio construction and drift detection for StockPilot.

Target weights from research scores:
- Top ``MAX_HOLDINGS`` (8) tokens by score.
- Weights proportional to score, with iterative capping at ``MAX_WEIGHT_BPS``
  (30%) and redistribution of the excess to uncapped holdings.
- Every included holding gets at least ``MIN_WEIGHT_BPS`` (5%); the top-up
  is funded proportionally from holdings above the minimum.
- Weights are normalized to exactly ``TOTAL_BPS`` (10000). Integer rounding
  is deterministic: each weight is floored to whole bps and ALL remaining
  bps go to the highest-scored holding. (Rounding can therefore push the top
  holding up to ``MAX_HOLDINGS - 1`` bps above the 30% cap — at most 0.07%.)
- If fewer than 4 holdings qualify, the cap is relaxed to
  ``ceil(10000 / n)`` so the weights can still sum to 10000.

Drift: the maximum absolute per-symbol weight difference (in bps) between
the current on-chain target and the new target. Rebalance when drift
exceeds ``DRIFT_THRESHOLD_BPS`` (500 bps = 5pp).
"""

from __future__ import annotations

import math

TOTAL_BPS = 10000
MAX_HOLDINGS = 8
MAX_WEIGHT_BPS = 3000  # 30% cap per holding
MIN_WEIGHT_BPS = 500  # 5% floor per included holding
DRIFT_THRESHOLD_BPS = 500  # 5pp
REBALANCE_MAX_AGE_DAYS = 7.0


def build_target_weights(scores: dict[str, float | None]) -> dict[str, int]:
    """Build integer-bps target weights from symbol -> score (0-100).

    ``None`` scores (excluded tokens) are ignored. Returns {} for an empty
    universe. Deterministic: ties broken by symbol ascending.
    """
    usable = {s: sc for s, sc in scores.items() if sc is not None}
    if not usable:
        return {}
    ranked = sorted(usable.items(), key=lambda kv: (-kv[1], kv[0]))
    top = ranked[:MAX_HOLDINGS]
    n = len(top)
    # The 30% cap only works with >= 4 holdings; relax it when fewer qualify.
    cap = max(MAX_WEIGHT_BPS, math.ceil(TOTAL_BPS / n))

    # --- proportional allocation with iterative capping (water-filling) ---
    alloc: dict[str, float] = {}
    active = dict(top)
    remaining = float(TOTAL_BPS)
    total_score = sum(active.values())
    if total_score <= 0:
        # All-zero scores: fall back to equal weights.
        share = TOTAL_BPS / n
        alloc = {symbol: share for symbol, _ in top}
    else:
        while active:
            denom = sum(active.values())
            shares = {s: remaining * sc / denom for s, sc in active.items()}
            over = {s: sh for s, sh in shares.items() if sh > cap}
            if not over:
                alloc.update(shares)
                break
            for symbol in over:
                alloc[symbol] = float(cap)
                remaining -= cap
                del active[symbol]

    # --- enforce the 5% minimum, funded from holdings above the minimum ---
    while True:
        below = {s: w for s, w in alloc.items() if w < MIN_WEIGHT_BPS}
        if not below:
            break
        need = sum(MIN_WEIGHT_BPS - w for w in below.values())
        donors = {s: w for s, w in alloc.items() if w > MIN_WEIGHT_BPS}
        donor_excess = sum(w - MIN_WEIGHT_BPS for w in donors.values())
        if donor_excess <= 0:
            break  # cannot satisfy the minimum; keep current allocation
        for symbol in below:
            alloc[symbol] = float(MIN_WEIGHT_BPS)
        for symbol, weight in donors.items():
            alloc[symbol] = weight - need * (weight - MIN_WEIGHT_BPS) / donor_excess

    # --- deterministic integer rounding: floor, remainder to top holding ---
    bps = {symbol: int(weight) for symbol, weight in alloc.items()}
    remainder = TOTAL_BPS - sum(bps.values())
    top_symbol = top[0][0]
    bps[top_symbol] += remainder
    return bps


def ordered_weights(weights: dict[str, int]) -> list[tuple[str, int]]:
    """Weights as a (symbol, bps) list ordered by weight desc, symbol asc.

    This ordering is what gets submitted on-chain (tickersCsv + weightsBps
    must use the same order).
    """
    return sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))


def tickers_csv(weights: dict[str, int]) -> str:
    """Comma-separated tickers in on-chain order, e.g. 'NVDAc,AAPLc,TSLA'."""
    return ",".join(symbol for symbol, _ in ordered_weights(weights))


def weights_bps_list(weights: dict[str, int]) -> list[int]:
    """Weights in the same order as :func:`tickers_csv`."""
    return [bps for _, bps in ordered_weights(weights)]


def detect_drift(
    current_weights: dict[str, int], target_weights: dict[str, int]
) -> tuple[int, bool]:
    """Return (max_drift_bps, needs_rebalance) for a 500 bps threshold."""
    symbols = set(current_weights) | set(target_weights)
    max_drift = 0
    for symbol in symbols:
        drift = abs(current_weights.get(symbol, 0) - target_weights.get(symbol, 0))
        max_drift = max(max_drift, drift)
    return max_drift, max_drift > DRIFT_THRESHOLD_BPS


def decide_rebalance(
    *,
    first_run: bool,
    drift_bps: int,
    universe_changed: bool,
    days_since_last_rebalance: float | None,
    force: bool = False,
) -> tuple[bool, list[str]]:
    """Decide whether to rebalance; returns (needs_rebalance, reasons).

    Triggers: first run, drift > 500 bps, universe changed vs the on-chain
    target, more than 7 days since the last rebalance, or --force.
    """
    reasons: list[str] = []
    if force:
        reasons.append("forced via --force")
    if first_run:
        reasons.append("first run — no prior on-chain record")
    if drift_bps > DRIFT_THRESHOLD_BPS:
        reasons.append(
            f"drift {drift_bps} bps exceeds {DRIFT_THRESHOLD_BPS} bps threshold"
        )
    if universe_changed:
        reasons.append("universe changed vs on-chain target")
    if (
        days_since_last_rebalance is not None
        and days_since_last_rebalance > REBALANCE_MAX_AGE_DAYS
    ):
        reasons.append(
            f"{days_since_last_rebalance:.1f} days since last rebalance "
            f"(> {REBALANCE_MAX_AGE_DAYS:.0f} days)"
        )
    return bool(reasons), reasons
