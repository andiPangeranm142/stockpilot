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
fetches `data/latest-decision.json`, which the agent publishes on each run,
for the rationale, per-token score breakdown, drift, and transaction hash.
If either source is missing, the dashboard shows "not available" (or an
explicit empty state) instead of inventing numbers.

## Before the contract is deployed

`config.js` ships with `CONTRACT_ADDRESS` set to the zero address. Until the
coordinator fills in the real deployed address, the dashboard shows a
"Contract not deployed yet" banner and only the RPC status (and any decision
file, if present) is live. After deploy, edit `config.js` and set
`CONTRACT_ADDRESS` to the deployed `StockPilotLedger` address on Base Sepolia
(chain ID 84532) — no other change is needed.

Simulated paper portfolio — not investment advice.
