# StockPilot dashboard

Static dashboard (no build step) for the StockPilot agent — research-to-portfolio
for Coinbase tokenized stocks on Base Sepolia. This folder is the GitHub Pages
source for the repository.

## Run locally

```sh
cd docs
python3 -m http.server 8000
```

Then open http://localhost:8000 in a browser. The page is plain HTML/CSS/JS
(`index.html`, `styles.css`, `app.js`, `config.js`); the only external
dependency is ethers v6, loaded as an ES module from the jsDelivr CDN.

## How data is loaded

All reads are read-only — the dashboard never connects a wallet or sends a
transaction. `app.js` uses an ethers `JsonRpcProvider` pointed at the public
Base Sepolia RPC to call the `StockPilotLedger` contract: `rebalanceCount()`
and `getRebalance(i)` for the history, `getLatest()` for the current target
portfolio, plus the latest block number for the network status card. It also
fetches two files the agent publishes on each run:

- `data/latest-decision.json` — the rationale, per-token score breakdown,
  drift, and transaction hash of the latest run.
- `data/history.json` — NAV price + research-score series for the
  "Price & score history" charts (see `agent/README.md` for the format).
  Every chart point traces back to a recorded line in
  `agent/data/price-history.jsonl` or a recorded decision file; dots mark
  recorded points and lines only connect them — nothing is interpolated or
  projected. If either source is missing, the dashboard shows "not
  available" (or an explicit empty state) instead of inventing numbers.

## Verify the dashboard

`tests/verify_dashboard.py` checks the dashboard end to end with Playwright
(requires Playwright + Chromium): every displayed price, weight, and score
against the on-chain record and the decision files; every chart point
against `history.json`; no console errors; desktop + mobile screenshots.

```sh
# from the repo root, with the operator venv (has Playwright)
../.venv/bin/python docs/tests/verify_dashboard.py          # local copy
../.venv/bin/python docs/tests/verify_dashboard.py --live   # live site
```

## Before the contract is deployed

`config.js` ships with `CONTRACT_ADDRESS` set to the zero address. Until the
coordinator fills in the real deployed address, the dashboard shows a
"Contract not deployed yet" banner and only the RPC status (and any decision
file, if present) is live. After deploy, edit `config.js` and set
`CONTRACT_ADDRESS` to the deployed `StockPilotLedger` address on Base Sepolia
(chain ID 84532) — no other change is needed.

Simulated paper portfolio — not investment advice.
