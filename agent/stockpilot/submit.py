"""On-chain submission to the StockPilotLedger contract (Base Sepolia).

Reads the agent's private key ONLY from the ``DEPLOYER_PRIVATE_KEY``
environment variable or from a ``.env`` file whose path is given (default
``../../.env.testnet`` relative to ``agent/`` — OUTSIDE the repo).
The key is never logged, printed, or written to any file in the repo.

Contract address comes from ``agent/contract-address.txt``. If that file
does not exist (contract not deployed yet), submission refuses to run with
a clear error; ``--dry-run`` still works without a contract.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from web3 import Web3

log = logging.getLogger(__name__)

SEPOLIA_RPC_URL = "https://sepolia.base.org"
SEPOLIA_CHAIN_ID = 84532
RECEIPT_TIMEOUT_SECONDS = 120
GAS_LIMIT_FALLBACK = 500_000

# agent/ is parents[1] of this file (stockpilot/ package -> agent/ -> repo).
AGENT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = AGENT_DIR.parent.parent / ".env.testnet"
DEFAULT_CONTRACT_ADDRESS_PATH = AGENT_DIR / "contract-address.txt"

# Minimal StockPilotLedger ABI (only the functions this agent uses).
LEDGER_ABI = [
    {
        "inputs": [
            {"internalType": "bytes32", "name": "navSnapshotHash", "type": "bytes32"},
            {"internalType": "bytes32", "name": "rationaleHash", "type": "bytes32"},
            {"internalType": "string", "name": "tickersCsv", "type": "string"},
            {"internalType": "uint256[]", "name": "weightsBps", "type": "uint256[]"},
        ],
        "name": "recordRebalance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        # getLatest returns the Rebalance STRUCT (one tuple output), not
        # seven separate outputs. Components follow the struct field order.
        "inputs": [],
        "name": "getLatest",
        "outputs": [
            {
                "components": [
                    {"internalType": "uint256", "name": "id", "type": "uint256"},
                    {"internalType": "uint256", "name": "timestamp", "type": "uint256"},
                    {"internalType": "bytes32", "name": "navSnapshotHash", "type": "bytes32"},
                    {"internalType": "bytes32", "name": "rationaleHash", "type": "bytes32"},
                    {"internalType": "string", "name": "tickersCsv", "type": "string"},
                    {"internalType": "uint256[]", "name": "weightsBps", "type": "uint256[]"},
                    {"internalType": "address", "name": "agent", "type": "address"},
                ],
                "internalType": "struct StockPilotLedger.Rebalance",
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "rebalanceCount",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file (comments and blanks ignored)."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def load_private_key(env_path: str | Path | None = None) -> str:
    """Load DEPLOYER_PRIVATE_KEY from env, else from the .env file.

    The returned value must never be logged or printed by any caller.
    """
    key = os.environ.get("DEPLOYER_PRIVATE_KEY")
    if key:
        return key.strip()
    path = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    if not path.exists():
        raise RuntimeError(
            f"DEPLOYER_PRIVATE_KEY is not set and no .env file was found at {path}. "
            "Set the env var or pass --env-file."
        )
    values = _parse_env_file(path)
    key = values.get("DEPLOYER_PRIVATE_KEY")
    if not key:
        raise RuntimeError(f"DEPLOYER_PRIVATE_KEY not found in {path}")
    return key.strip()


def read_contract_address(path: str | Path | None = None) -> str:
    """Read the ledger address from contract-address.txt (checksummed).

    Raises FileNotFoundError with a clear message when the contract has
    not been deployed yet (file absent).
    """
    path = Path(path) if path is not None else DEFAULT_CONTRACT_ADDRESS_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"contract address file not found: {path} — the StockPilotLedger "
            "contract has not been deployed yet. Run with --dry-run, or deploy "
            "the contract and write its address to that file."
        )
    address = path.read_text(encoding="utf-8").strip()
    if not _ADDRESS_RE.match(address):
        raise ValueError(f"invalid contract address in {path}: {address!r}")
    return Web3.to_checksum_address(address)


def get_w3(rpc_url: str = SEPOLIA_RPC_URL) -> Web3:
    """Connect to Base Sepolia; raises if the RPC is unreachable."""
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise ConnectionError(f"cannot connect to Base Sepolia RPC {rpc_url}")
    return w3


def get_contract(w3: Web3, contract_address: str) -> Any:
    return w3.eth.contract(
        address=w3.to_checksum_address(contract_address), abi=LEDGER_ABI
    )


def read_latest(w3: Web3, contract_address: str) -> dict[str, Any] | None:
    """Read the latest on-chain rebalance record, or None if there is none."""
    contract = get_contract(w3, contract_address)
    count = contract.functions.rebalanceCount().call()
    if count == 0:
        return None
    record = contract.functions.getLatest().call()
    # getLatest returns a single Rebalance struct; web3.py may hand it back
    # as a flat 7-tuple or nested inside a 1-tuple — unwrap if nested.
    if len(record) == 1 and isinstance(record[0], (tuple, list)):
        record = record[0]
    # Struct field order: id, timestamp, navHash, rationaleHash, tickers, weights, agent
    return {
        "id": int(record[0]),
        "timestamp": int(record[1]),
        "nav_snapshot_hash": "0x" + record[2].hex(),
        "rationale_hash": "0x" + record[3].hex(),
        "tickers_csv": record[4],
        "weights_bps": [int(w) for w in record[5]],
        "agent": record[6],
    }


def submit_rebalance(
    w3: Web3,
    contract_address: str,
    nav_snapshot_hash: str,
    rationale_hash: str,
    tickers_csv: str,
    weights_bps: list[int],
    private_key: str,
    timeout: int = RECEIPT_TIMEOUT_SECONDS,
) -> str:
    """Send recordRebalance, wait for the receipt, return the tx hash hex.

    ``private_key`` is used only to sign; it is never logged.
    """
    if sum(weights_bps) != 10000:
        raise ValueError(f"weights must sum to 10000 bps, got {sum(weights_bps)}")
    account = w3.eth.account.from_key(private_key)
    contract = get_contract(w3, contract_address)
    nonce = w3.eth.get_transaction_count(account.address)
    tx = contract.functions.recordRebalance(
        nav_snapshot_hash, rationale_hash, tickers_csv, weights_bps
    ).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "chainId": SEPOLIA_CHAIN_ID,
            "gasPrice": w3.eth.gas_price,
        }
    )
    try:
        estimated = w3.eth.estimate_gas(tx)
        tx["gas"] = int(estimated * 1.2)  # 20% buffer
    except Exception as exc:  # noqa: BLE001 - fall back to a safe limit
        log.warning("gas estimation failed (%s); using fallback limit", exc)
        tx["gas"] = GAS_LIMIT_FALLBACK
    log.info(
        "submitting recordRebalance from %s (nonce %d, gas %d)",
        account.address,
        nonce,
        tx["gas"],
    )
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hash_hex = "0x" + tx_hash.hex()
    log.info("tx sent: %s — waiting for receipt (timeout %ds)", tx_hash_hex, timeout)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    if receipt["status"] != 1:
        raise RuntimeError(f"recordRebalance tx reverted: {tx_hash_hex}")
    log.info("tx confirmed in block %d: %s", receipt["blockNumber"], tx_hash_hex)
    return tx_hash_hex
