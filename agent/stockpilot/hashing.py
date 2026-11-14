"""Deterministic keccak256 hashing for StockPilot decision records.

- ``nav_snapshot_hash``: keccak256 of the canonical JSON of the NAV snapshot
  (``{symbol: {"nav_price": str, "updated_at": str}}``, keys sorted,
  compact separators). Same snapshot -> same hash, regardless of dict order.
- ``rationale_hash``: keccak256 of the UTF-8 rationale text.
"""

from __future__ import annotations

import json
from typing import Any

from web3 import Web3


def canonical_snapshot_hash(entries: dict[str, dict[str, Any]]) -> str:
    """keccak256 hex (0x-prefixed) of the canonical snapshot JSON."""
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "0x" + Web3.keccak(text=canonical).hex()


def rationale_hash(text: str) -> str:
    """keccak256 hex (0x-prefixed) of the rationale text."""
    return "0x" + Web3.keccak(text=text).hex()
