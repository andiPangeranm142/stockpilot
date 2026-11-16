"""StockPilot agent CLI: fetch -> score -> target weights -> (maybe) submit.

Usage (from ``agent/``):
    python -m stockpilot.run --dry-run      # full pipeline, no transaction
    python -m stockpilot.run                # live: submits if a rebalance is due
    python -m stockpilot.run --force        # live: rebalance even if no trigger

Flow:
1. Fetch NAV snapshots (Tokenized Stocks API, Chainlink fallback).
2. Append today's snapshots to ``data/price-history.jsonl`` (dedup/day).
3. Score tokens with the rule-based research model.
4. Build target weights (max 8 holdings, 30% cap, 5% min, 10000 bps).
5. Read the latest on-chain target via ``getLatest`` (if the contract is
   deployed); otherwise this is treated as a first run.
6. Decide whether to rebalance (first run, drift > 500 bps, universe
   changed, or > 7 days since the last rebalance).
7. Write the decision JSON to ``data/decisions/<UTC timestamp>.json`` and
   copy the latest decision to ``docs/data/latest-decision.json``.
8. If a rebalance is due, not a dry run, and the contract is available:
   submit ``recordRebalance`` and record the tx hash in the decision.
9. Publish ``docs/data/history.json`` — NAV price + score series built
   from the already recorded history, for the dashboard charts.

Paper-portfolio simulation only. Not investment advice.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import api_client
from .api_client import DataUnavailableError, TokenSnapshot
from .hashing import canonical_snapshot_hash, rationale_hash
from .history import write_history_json
from .portfolio import (
    build_target_weights,
    decide_rebalance,
    detect_drift,
    ordered_weights,
    tickers_csv,
    weights_bps_list,
)
from .research import append_history, load_history, score_tokens
from .submit import (
    SEPOLIA_RPC_URL,
    get_w3,
    load_private_key,
    read_contract_address,
    read_latest,
    submit_rebalance,
)

log = logging.getLogger("stockpilot")

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENT_DIR.parent
DATA_DIR = AGENT_DIR / "data"
HISTORY_FILE = DATA_DIR / "price-history.jsonl"
DECISIONS_DIR = DATA_DIR / "decisions"
LATEST_DECISION_PATH = REPO_ROOT / "docs" / "data" / "latest-decision.json"
HISTORY_JSON_PATH = REPO_ROOT / "docs" / "data" / "history.json"

DISCLAIMER = (
    "Paper-portfolio simulation for the StockPilot hackathon project. "
    "Not investment advice. No real assets are bought or sold; NAV values "
    "are Chainlink reference values, not live bid/ask prices."
)


def _snapshot_to_dict(snap: TokenSnapshot) -> dict:
    return {
        "symbol": snap.symbol,
        "nav_price": str(snap.nav_price) if snap.nav_price is not None else None,
        "nav_updated_at": snap.nav_updated_at.isoformat() if snap.nav_updated_at else None,
        "multiplier": str(snap.multiplier),
        "total_supply": str(snap.total_supply) if snap.total_supply is not None else None,
        "source": snap.source,
        "contract_address": snap.contract_address,
    }


def build_rationale(
    *,
    now: datetime,
    universe_size: int,
    tokens_seen: int,
    excluded_count: int,
    top_scores: list[tuple[str, float]],
    target_weights: dict[str, int],
    drift_bps: int,
    needs_rebalance: bool,
    reasons: list[str],
) -> str:
    """Human-readable English rationale for the decision (hashed on-chain)."""
    lines = [
        f"StockPilot rebalance decision — {now.strftime('%Y-%m-%d %H:%M UTC')}.",
        f"Universe: {universe_size} tokenized stocks with a fresh Chainlink NAV "
        f"out of {tokens_seen} listed tokens ({excluded_count} excluded: no NAV "
        "or stale NAV — never priced from invented data).",
    ]
    if top_scores:
        top_text = ", ".join(f"{sym} {score:.1f}" for sym, score in top_scores[:3])
        lines.append(
            "Top research scores (0-100; momentum 40%, dividend multiplier 20%, "
            f"adoption 20%, freshness 20%): {top_text}."
        )
    if target_weights:
        weights_text = ", ".join(
            f"{sym} {bps / 100:.1f}%" for sym, bps in ordered_weights(target_weights)
        )
        lines.append(
            f"Target portfolio ({len(target_weights)} holdings, 30% cap, 5% min): "
            f"{weights_text}."
        )
    else:
        lines.append("Target portfolio: empty (no investable tokens this run).")
    lines.append(f"Drift vs on-chain target: {drift_bps} bps (threshold 500 bps).")
    if needs_rebalance:
        lines.append("Action: REBALANCE. Reasons: " + "; ".join(reasons) + ".")
    else:
        lines.append("Action: HOLD. No rebalance trigger fired.")
    lines.append(DISCLAIMER)
    return " ".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="StockPilot rebalancing agent")
    parser.add_argument("--dry-run", action="store_true", help="do not send any transaction")
    parser.add_argument("--force", action="store_true", help="force a rebalance")
    parser.add_argument("--env-file", default=None, help="path to .env file with DEPLOYER_PRIVATE_KEY")
    parser.add_argument("--data-dir", default=None, help="override agent/data directory")
    parser.add_argument("--history-json", default=None,
                        help="override output path for docs/data/history.json "
                             "(default: docs/data/history.json in the repo)")
    parser.add_argument("--api-url", default=api_client.API_BASE_URL)
    parser.add_argument("--chainlink-rpc", default=api_client.CHAINLINK_RPC_URL)
    parser.add_argument("--sepolia-rpc", default=SEPOLIA_RPC_URL)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    now = datetime.now(timezone.utc)
    data_dir = Path(args.data_dir) if args.data_dir else DATA_DIR
    history_file = data_dir / "price-history.jsonl"
    decisions_dir = data_dir / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)

    tag = "[DRY RUN] " if args.dry_run else ""
    log.info("%sStockPilot run starting at %s", tag, now.isoformat())
    if args.dry_run:
        log.info("[DRY RUN] no transaction will be sent")

    # 1. Fetch snapshots (API -> Chainlink fallback; hard stop if both fail).
    try:
        snapshots = api_client.fetch_snapshots(
            now=now, api_base_url=args.api_url, chainlink_rpc_url=args.chainlink_rpc
        )
    except DataUnavailableError as exc:
        log.error("DATA UNAVAILABLE: %s — stopping, no prices invented.", exc)
        return 1
    included = [s for s in snapshots if s.included]
    excluded = [s for s in snapshots if not s.included]
    sources = {"api": 0, "chainlink": 0}
    for snap in included:
        sources[snap.source] = sources.get(snap.source, 0) + 1
    log.info(
        "%sFetched %d tokens: %d in universe (%s), %d excluded",
        tag,
        len(snapshots),
        len(included),
        ", ".join(f"{k}={v}" for k, v in sources.items() if v),
        len(excluded),
    )

    # 2. Append today's snapshots to local history (dedup per day+symbol).
    run_date = now.strftime("%Y-%m-%d")
    appended = append_history(history_file, included, run_date)
    log.info("history: %d new record(s) appended to %s", appended, history_file)

    # 3. Score tokens.
    history = load_history(history_file)
    scores = score_tokens(included, history, now)
    scored = {s: sc.total for s, sc in scores.items() if sc.total is not None}
    log.info("scored %d tokens", len(scored))
    for symbol, total in sorted(scored.items(), key=lambda kv: -kv[1])[:5]:
        log.info("  score %-6s %.2f", symbol, total)

    # 4. Target weights.
    target_weights = build_target_weights(scored)
    log.info(
        "target weights (%d holdings, sum %d bps): %s",
        len(target_weights),
        sum(target_weights.values()),
        ", ".join(f"{s}={b}" for s, b in ordered_weights(target_weights)),
    )

    # 5. Read the latest on-chain target (if the contract is deployed).
    onchain: dict | None = None
    contract_address: str | None = None
    contract_error: str | None = None
    try:
        contract_address = read_contract_address()
    except (FileNotFoundError, ValueError) as exc:
        contract_error = str(exc)
        log.warning("%s", contract_error)
    if contract_address is not None:
        try:
            w3 = get_w3(args.sepolia_rpc)
            onchain = read_latest(w3, contract_address)
            if onchain is None:
                log.info("on-chain: contract deployed, no rebalance record yet")
            else:
                log.info(
                    "on-chain: latest record #%d at %s: %s",
                    onchain["id"],
                    datetime.fromtimestamp(onchain["timestamp"], tz=timezone.utc).isoformat(),
                    onchain["tickers_csv"],
                )
        except Exception as exc:  # noqa: BLE001 - on-chain read is best-effort
            contract_error = f"on-chain read failed: {exc}"
            log.warning("%s", contract_error)
            onchain = None

    current_weights: dict[str, int] = {}
    first_run = True
    days_since_last: float | None = None
    if onchain is not None:
        first_run = False
        tickers = [t for t in onchain["tickers_csv"].split(",") if t]
        current_weights = dict(zip(tickers, onchain["weights_bps"]))
        days_since_last = (
            now.timestamp() - onchain["timestamp"]
        ) / 86400.0

    # 6. Decide whether to rebalance.
    drift_bps, _drift_flag = detect_drift(current_weights, target_weights)
    universe_changed = set(target_weights) != set(current_weights) if not first_run else True
    needs_rebalance, reasons = decide_rebalance(
        first_run=first_run,
        drift_bps=drift_bps,
        universe_changed=universe_changed,
        days_since_last_rebalance=days_since_last,
        force=args.force,
    )
    if not target_weights:
        needs_rebalance, reasons = False, ["empty universe — nothing to rebalance into"]
    log.info(
        "%sdrift %d bps; first_run=%s; universe_changed=%s; needs_rebalance=%s (%s)",
        tag,
        drift_bps,
        first_run,
        universe_changed,
        needs_rebalance,
        "; ".join(reasons) if reasons else "no trigger",
    )

    # 7. Build the decision record (hashes over canonical snapshot + rationale).
    top_scores = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    rationale = build_rationale(
        now=now,
        universe_size=len(included),
        tokens_seen=len(snapshots),
        excluded_count=len(excluded),
        top_scores=top_scores,
        target_weights=target_weights,
        drift_bps=drift_bps,
        needs_rebalance=needs_rebalance,
        reasons=reasons,
    )
    snapshot_entries = {
        snap.symbol: {
            "nav_price": str(snap.nav_price),
            "updated_at": snap.nav_updated_at.isoformat() if snap.nav_updated_at else None,
        }
        for snap in included
    }
    nav_hash = canonical_snapshot_hash(snapshot_entries)
    rat_hash = rationale_hash(rationale)

    decision: dict = {
        "timestamp": now.isoformat(),
        "disclaimer": DISCLAIMER,
        "dry_run": args.dry_run,
        "data": {
            "tokens_seen": len(snapshots),
            "universe_size": len(included),
            "excluded_count": len(excluded),
            "sources": sources,
            "snapshot": [_snapshot_to_dict(s) for s in included],
            "excluded_tokens": [
                {"symbol": s.symbol, "reason": s.excluded_reason} for s in excluded
            ],
        },
        "scores": {symbol: sc.to_dict() for symbol, sc in scores.items()},
        "target_weights_bps": target_weights,
        "tickers_csv": tickers_csv(target_weights),
        "weights_bps": weights_bps_list(target_weights),
        "onchain": onchain,
        "contract_address": contract_address,
        "contract_error": contract_error,
        "drift_bps": drift_bps,
        "first_run": first_run,
        "universe_changed": universe_changed,
        "days_since_last_rebalance": days_since_last,
        "needs_rebalance": needs_rebalance,
        "rebalance_reasons": reasons,
        "rationale": rationale,
        "nav_snapshot_hash": nav_hash,
        "rationale_hash": rat_hash,
        "tx_hash": None,
        "tx_status": "not needed",
    }

    # 8. Submit if due (live mode only, contract required).
    exit_code = 0
    if needs_rebalance and args.dry_run:
        decision["tx_status"] = "dry-run: not submitted"
        log.info("[DRY RUN] rebalance due but no transaction sent")
    elif needs_rebalance:
        if contract_address is None:
            decision["tx_status"] = "not submitted: contract address unavailable"
            log.error(
                "REBALANCE DUE but the ledger contract is not deployed yet "
                "(agent/contract-address.txt missing). Decision recorded; "
                "no transaction sent."
            )
            exit_code = 1
        else:
            try:
                private_key = load_private_key(args.env_file)
                w3 = get_w3(args.sepolia_rpc)
                tx_hash = submit_rebalance(
                    w3,
                    contract_address,
                    nav_hash,
                    rat_hash,
                    tickers_csv(target_weights),
                    weights_bps_list(target_weights),
                    private_key,
                )
                decision["tx_hash"] = tx_hash
                decision["tx_status"] = "confirmed"
            except Exception as exc:  # noqa: BLE001 - record the failure
                decision["tx_status"] = f"submission failed: {exc}"
                log.error("submission failed: %s", exc)
                exit_code = 1

    # 9. Write the decision JSON + latest-decision copy for the dashboard.
    stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")
    decision_path = decisions_dir / f"{stamp}.json"
    decision_path.write_text(
        json.dumps(decision, indent=2, sort_keys=True), encoding="utf-8"
    )
    LATEST_DECISION_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(decision_path, LATEST_DECISION_PATH)
    log.info("%sdecision written: %s", tag, decision_path)
    log.info("%slatest decision copied to %s", tag, LATEST_DECISION_PATH)

    # 10. Publish the history file for the dashboard charts. This is
    # best-effort: a failure here must not undo a recorded decision or
    # a submitted rebalance, so it only logs a warning.
    history_json_path = (
        Path(args.history_json) if args.history_json else HISTORY_JSON_PATH
    )
    try:
        write_history_json(history_file, decisions_dir, history_json_path)
    except Exception as exc:  # noqa: BLE001 - history publish is best-effort
        log.warning("history.json NOT published (%s): %s", history_json_path, exc)
    log.info(
        "%sDONE: universe=%d holdings=%d rebalance=%s tx=%s",
        tag,
        len(included),
        len(target_weights),
        needs_rebalance,
        decision["tx_hash"] or decision["tx_status"],
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
