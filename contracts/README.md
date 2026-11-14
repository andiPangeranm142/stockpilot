# StockPilot Contracts

Foundry project for the **StockPilotLedger** contract — the on-chain ledger of
the StockPilot agent (Base Stocks Maxxing Builder Quest, track Agentic Stock
Finance).

## What it is

`StockPilotLedger` (`src/StockPilotLedger.sol`) records snapshots of the
agent's **simulated (paper) portfolio** rebalances on Base Sepolia. Each
rebalance stores:

- `navSnapshotHash` — keccak256 hash of the off-chain NAV snapshot JSON
- `rationaleHash` — keccak256 hash of the off-chain rationale text
- `tickersCsv` — comma-separated tickers, e.g. `"NVDAc,AAPLc,TSLA"`
- `weightsBps` — target weights in basis points (must sum to 10000 = 100%)

**Simulation only:** the contract holds no real funds, performs no trading,
and moves no assets. Only the owner (the deployer / agent operator) can record
rebalances. Holdings: min 1, max 20, each weight > 0.

## Setup

Requires [Foundry](https://book.getfoundry.sh/getting-started/installation)
(`forge`, `cast`). `contracts/lib/` is gitignored, so fetch the test library
first on a fresh clone:

```bash
cd contracts
forge install foundry-rs/forge-std
```

## Test

```bash
forge test
```

11 tests cover the happy path (first + second record, event, struct contents,
`getRebalance`/`getLatest`), access control (only owner), and every validation
revert (weights ≠ 10000, ticker/weight count mismatch, 0 holdings, > 20
holdings, zero weight, getters on an empty ledger).

## Deploy (Base Sepolia, chain ID 84532)

Prerequisites: the deployer wallet must hold Sepolia ETH from a faucet.
Set `DEPLOYER_PRIVATE_KEY` in your environment. (The operator keeps the key
in a mode-600 `.env` file OUTSIDE this repo — never copy it into the repo or
print it. From this directory that file is `../../.env.testnet`.)

```bash
cd contracts
set -a && source ../../.env.testnet && set +a
forge script script/Deploy.s.sol:Deploy \
  --rpc-url https://sepolia.base.org \
  --broadcast -vvv
```

The script logs the deployed contract address. The deploy history is saved
under `broadcast/` (kept in the repo on purpose).

## Verify on Basescan (optional)

Source verification needs a Basescan API key (free at
https://basescan.org/myapikey — do not commit it anywhere):

```bash
forge verify-contract <CONTRACT_ADDRESS> \
  src/StockPilotLedger.sol:StockPilotLedger \
  --chain 84532 \
  --etherscan-api-key "$BASESCAN_API_KEY"
```

Then check the contract at
`https://sepolia.basescan.org/address/<CONTRACT_ADDRESS>`.
