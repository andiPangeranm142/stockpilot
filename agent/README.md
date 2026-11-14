# StockPilot Agent

Rule-based research + rebalancing agent for Coinbase tokenized stocks on
Base. It reads live NAV reference data, scores the investable universe with
a transparent rule-based model, builds a target portfolio, and records each
rebalance decision on-chain in the `StockPilotLedger` contract (Base
Sepolia) — hash of the NAV snapshot + hash of the human-readable rationale.

> **Disclaimer — simulation, not investment advice.** StockPilot manages a
> **paper portfolio**: no real assets are bought or sold and no user funds
> are involved. NAV values are Chainlink reference values (the Friday close
> outside market hours), not live bid/ask or execution prices. Nothing here
> is investment advice.

## Quick start

```bash
# From the repo root (venv lives OUTSIDE the repo, at ../.venv)
cd stockpilot/agent
python3 -m venv ../../.venv          # only needed once
../../.venv/bin/pip install -r requirements.txt

# Dry run: full pipeline, no transaction sent
../../.venv/bin/python -m stockpilot.run --dry-run

# Live run: submits recordRebalance when a rebalance is due
../../.venv/bin/python -m stockpilot.run

# Force a rebalance even if no trigger fired
../../.venv/bin/python -m stockpilot.run --force

# Tests
../../.venv/bin/python -m pytest -q
```

## Data sources (real data only — nothing is invented)

| Source | What it provides |
|---|---|
| **Tokenized Stocks API** (primary) — `https://api.coinbase.com/v1/tokenized-stocks`, read-only, no API key | All listed tokens, paginated (`page_size`, `next_page_token`). Each token may carry `nav_price` (Chainlink NAV, USD), `nav_price_updated_at`, `multiplier`, `total_supply`. Fields are omitted when unset, so the parser treats every field as optional. |
| **Chainlink NAV feeds on Base mainnet** (fallback) — `latestRoundData()`, 8 decimals | Used only if the API is unreachable. 10 feeds: AAPL, AMZN, GOOGL, META, MSFT, MSTR, NVDA, SNDK, SPCX, TSLA (token symbol = ticker + `c`). Multiplier defaults to 1.0 and supply is unknown in fallback mode. |

Exclusion rules (a token is dropped from the universe, with the reason
recorded, when any of these apply):

- no `nav_price` published (most of the 358 listed tokens have none yet);
- `nav_price_updated_at` missing (freshness cannot be verified);
- NAV older than **7 days** (staleness limit).

If the API **and** the Chainlink fallback both fail, the agent stops with a
clear error instead of using made-up numbers.

Note on market hours: NAV feeds hold the last close on weekends and
holidays, so a Saturday run sees Friday's close — stale but valid, and well
inside the 7-day limit.

## Research model (rule-based, transparent)

Every investable token scores 0–100:

```
total = 0.40*momentum + 0.20*multiplier + 0.20*adoption + 0.20*freshness
```

1. **Momentum (40%)** — NAV change vs the earliest snapshot in the local
   price history (`data/price-history.jsonl`, appended on every run):
   `momentum = clamp(50 + return * 500, 0, 100)`, so +10% scores 100 and
   −10% scores 0. With fewer than 2 history points, or a history span under
   24h, the component is neutral (50) and the reason is recorded.
2. **Dividend / multiplier accrual (20%)** — Coinbase reinvests
   distributions by raising the token's `multiplier` (shares per token)
   above 1.0:
   `multiplier_score = clamp(50 + min(50, (multiplier − 1) * 5000), 0, 100)`.
   Multiplier 1.0 → 50; 1.01 → 100.
3. **Adoption (20%)** — rank of `log10(total_supply)` within the universe,
   min-max scaled to 0–100. Tokens without supply data score a neutral 50.
4. **Data freshness (20%)** — 100 if the NAV is under 24h old, declining
   linearly to 0 at 7 days.

Excluded tokens get no score (`total: null`).

## Portfolio construction

- Top **8** tokens by score.
- Weights proportional to score, **capped at 30%** per holding (excess
  redistributed to uncapped holdings, iteratively).
- **Minimum 5%** per included holding (funded from holdings above the min).
- Normalized to exactly **10000 bps**; integer rounding is deterministic —
  each weight is floored to whole bps and **all remaining bps go to the
  highest-scored holding**. (Rounding can push the top holding up to 7 bps
  above the 30% cap.)
- With fewer than 4 qualifying holdings the cap is relaxed to
  `ceil(10000 / n)` so the weights can still sum to 10000.

## Rebalance policy

A rebalance is due when **any** trigger fires:

- **First run** — no prior on-chain record;
- **Drift > 500 bps** — max absolute per-symbol weight difference between
  the on-chain target and the new target;
- **Universe changed** — the set of target holdings differs from on-chain;
- **> 7 days** since the last on-chain rebalance;
- **`--force`**.

## Decision JSON

Each run writes `data/decisions/<UTC timestamp>.json` and copies the latest
to `../docs/data/latest-decision.json` (for the dashboard). Contents:

| Field | Meaning |
|---|---|
| `timestamp`, `dry_run`, `disclaimer` | run metadata |
| `data.snapshot` | included tokens: symbol, NAV, updated-at, multiplier, supply, source |
| `data.excluded_tokens` | excluded tokens with reasons |
| `scores` | per-token total + 4 component scores + notes |
| `target_weights_bps`, `tickers_csv`, `weights_bps` | target portfolio (CSV + bps arrays share one order) |
| `onchain` | latest ledger record (`null` on first run) |
| `drift_bps`, `needs_rebalance`, `rebalance_reasons` | decision inputs |
| `rationale` | human-readable English rationale |
| `nav_snapshot_hash` | keccak256 of canonical JSON `{symbol: {nav_price, updated_at}}`, keys sorted |
| `rationale_hash` | keccak256 of the rationale text |
| `tx_hash`, `tx_status` | submission outcome (`null` / `dry-run: not submitted` on dry runs) |

Decision files contain only public market data and hashes — never secrets.

## On-chain submission

- Ledger: `StockPilotLedger` on **Base Sepolia** (chain ID 84532,
  RPC `https://sepolia.base.org`), function
  `recordRebalance(navSnapshotHash, rationaleHash, tickersCsv, weightsBps)`.
- Contract address is read from `agent/contract-address.txt`. If the file
  is missing (contract not deployed yet), live submission refuses with a
  clear error; `--dry-run` still works.
- The agent wallet's private key is read from the `DEPLOYER_PRIVATE_KEY`
  environment variable, or from a `.env` file passed via `--env-file`
  (default `../../.env.testnet`, **outside the repo**). It is never logged,
  printed, or written into the repo.
- Submission waits for the receipt (120 s timeout) and records the tx hash
  in the decision JSON.

## Layout

```
agent/
├── stockpilot/
│   ├── __init__.py
│   ├── api_client.py   # Tokenized Stocks API + Chainlink fallback
│   ├── research.py     # rule-based scoring + price history (JSONL)
│   ├── portfolio.py    # target weights, drift, rebalance decision
│   ├── hashing.py      # keccak256 snapshot/rationale hashes
│   ├── submit.py       # ledger contract calls (read + submit)
│   └── run.py          # CLI: python -m stockpilot.run [--dry-run] [--force]
├── tests/
│   └── test_agent.py   # 36 tests (pytest)
├── data/
│   ├── price-history.jsonl
│   └── decisions/
├── conftest.py
├── requirements.txt
└── README.md
```
