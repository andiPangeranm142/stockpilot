# StockPilot

**Research-to-portfolio agent for Coinbase tokenized stocks on Base.**
Built for the [Base Stocks Maxxing Builder Quest](https://base.org/stocksmaxxing)
(Coinbase/Base), **Agentic Stock Finance** track (Base prize pool $20,000).

StockPilot is an autonomous agent that researches Coinbase-issued tokenized
stocks, builds a target paper portfolio from a transparent rule-based model,
and records every rebalance decision on-chain — with the reasoning hashed and
anchored in a smart contract on Base Sepolia.

> **Simulation only — not investment advice.** StockPilot runs a *paper*
> portfolio. It holds no real funds, buys or sells nothing, and never enables
> trading for anyone. NAV values are Chainlink reference values, not live
> bid/ask prices. Nothing here is investment, legal, or tax advice.

## How it works

```
Tokenized Stocks API (api.coinbase.com) ──► fetch NAV snapshots (no API key)
        │  fallback: Chainlink NAV feeds on Base mainnet (on-chain reads)
        ▼
Rule-based research model (transparent, explainable)
  momentum 40% · dividend/multiplier accrual 20% · adoption 20% · freshness 20%
        ▼
Target portfolio (max 8 holdings, 30% cap, 5% min, 10000 bps)
        ▼
Rebalance? first run · drift > 500 bps · universe changed · > 7 days
        ▼
StockPilotLedger.recordRebalance(...) on Base Sepolia
  (NAV snapshot hash + rationale hash + tickers + weights, all public)
        ▼
Static dashboard (GitHub Pages) reads the ledger live
```

**Data integrity rules the agent follows:**

- Only real data is used: the Tokenized Stocks API (read-only, no key) with
  an on-chain Chainlink fallback. The API lists 358 tokens, but only the
  ~12 with a fresh Chainlink NAV enter the universe; the rest are excluded
  with a recorded reason. **No price is ever invented** — if both sources
  fail, the agent stops with an error.
- NAV staleness is enforced: tokens whose NAV is older than 7 days are
  excluded (feeds run 24/5 and hold Friday's close over the weekend).
- Momentum comes from the agent's own local price history (one snapshot per
  day). Until at least two snapshots ≥ 24h apart exist, the momentum
  component is neutral (50) and the rationale says so.

## Repo layout

| Path | What it is |
| --- | --- |
| `agent/` | Python agent: API client, research model, portfolio builder, on-chain submitter, CLI, pytest suite |
| `contracts/` | Foundry project: `StockPilotLedger.sol`, 11 tests, deploy script |
| `docs/` | Static dashboard (GitHub Pages): reads the ledger live via Base Sepolia RPC |
| `LICENSE` | Apache-2.0 |

## Quickstart

### Agent (dry run — no transaction)

```bash
cd agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m stockpilot.run --dry-run
```

The run writes a decision JSON to `agent/data/decisions/` and copies the
latest one to `docs/data/latest-decision.json` for the dashboard.
See [agent/README.md](agent/README.md) for the research model, rebalance
policy, and live-run instructions.

### Contracts

```bash
cd contracts
forge install foundry-rs/forge-std   # first time on a fresh clone
forge test                            # 11 tests
```

### Dashboard

```bash
cd docs
python3 -m http.server 8000
# open http://localhost:8000
```

Live dashboard: <https://andipangeranm142.github.io/stockpilot/>
(served from `docs/` via GitHub Pages).

## Deployment status

| Item | Status |
| --- | --- |
| `StockPilotLedger` on Base Sepolia | **Pending** — deployer wallet awaits testnet ETH from a faucet |
| Deployer / agent wallet (testnet only) | `0xde897528DEC3E6de4CF52616F7b88c72FA8403eC` |
| Contract address | TBD after deploy (then recorded in `agent/contract-address.txt` and `docs/config.js`) |
| First on-chain rebalance | TBD after deploy |

Deploy once the wallet is funded (key in a mode-600 `.env` file outside
this repo — never committed):

```bash
cd contracts
set -a && source ../../.env.testnet && set +a
forge script script/Deploy.s.sol:Deploy --rpc-url https://sepolia.base.org --broadcast -vvv
```

Then run the agent live once to record the first rebalance:

```bash
cd agent
python -m stockpilot.run
```

End-to-end flow (deploy → record → read back) is already verified on a
local Anvil chain, and the dashboard logic is tested against both the
not-deployed and deployed states.

## Test results

- Contracts: `forge test` — **11 passed, 0 failed**
- Agent: `pytest -q` — **36 passed, 0 failed**
- Dashboard: `node --check` clean; logic tests 14/14 (not-deployed state)
  and 19/19 (deployed state with sample data)
- Live dry run (14 Nov 2026): 358 tokens fetched, 12-token universe,
  8 holdings, weights sum 10000 bps; Chainlink fallback cross-checked
  10/10 feeds, prices identical to the API

## Links

- Quest: <https://base.org/stocksmaxxing> (submit by 5 Dec 2026, 23:59 UTC)
- Tokenized Stocks API docs: <https://docs.base.org/sdks/tokenized-stocks/overview>
- Base Sepolia explorer: <https://sepolia.basescan.org>

## Disclaimer

StockPilot is a hackathon project. It is not affiliated with, endorsed by,
or sponsored by Coinbase, Base, or any issuer of tokenized stocks. It is a
research and simulation tool: it does not trade, does not hold assets, and
its output is not investment advice.
