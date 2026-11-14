"""Live data client for StockPilot.

Primary source: the Coinbase Tokenized Stocks API
(https://api.coinbase.com/v1/tokenized-stocks) — read-only, no API key.
It returns Chainlink-based NAV/reference values (``nav_price``), which are
NOT live bid/ask prices; feeds hold the last close outside market hours.

Fallback source: Chainlink NAV feeds on Base mainnet, read directly via
``latestRoundData()`` eth_call (8 decimals).

Data-integrity rules (never fabricate a price):
- Tokens without ``nav_price`` are returned with ``excluded_reason`` set.
- Tokens whose NAV is older than 7 days are excluded as stale.
- Tokens with a NAV but no ``nav_price_updated_at`` are excluded, because
  freshness cannot be verified.
- If the API fails entirely, the Chainlink fallback is tried. If both fail,
  :class:`DataUnavailableError` is raised and the agent stops.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

log = logging.getLogger(__name__)

API_BASE_URL = "https://api.coinbase.com/v1/tokenized-stocks"
CHAINLINK_RPC_URL = "https://mainnet.base.org"
STALENESS_LIMIT = timedelta(days=7)
REQUEST_TIMEOUT = 30
MAX_RETRIES = 2  # retries after the first attempt
MAX_PAGES = 100  # safety guard against a runaway pagination loop

# Verified Chainlink NAV feed proxies on Base mainnet (docs.base.org).
# Each feed returns 8 decimals via latestRoundData(). Token symbol is the
# ticker plus "c" (e.g. NVDA -> NVDAc). Note: CRCL and INTC have no feed in
# this list, so the fallback universe is at most these 10 tokens.
CHAINLINK_FEEDS: dict[str, str] = {
    "AAPL": "0x787f13dEa48Db0897CbCDD985de77809D837F988",
    "AMZN": "0x06A8E4b3aBB3B7543d8396FB2B763d22820cB295",
    "GOOGL": "0x5bF49E0ffA937CE2FfF033c739aD7C634c4D34F2",
    "META": "0x6526aE6797A76123638b863AeE4dD27Ba4E4b27D",
    "MSFT": "0xeB10A6c9aa7E537aEd766C08c35Dae35B321b18c",
    "MSTR": "0xB3cE282CD188b35DA0E38D8Bc7d58e33173D202a",
    "NVDA": "0x04689a41629776563E6822F76f2e57D148d28513",
    "SNDK": "0x388b0dC46C0Fb05A74BeE0994fa5b02c6Fcca2eA",
    "SPCX": "0x6A634B235903C4ad6376892180d6fF8612e3Fa68",
    "TSLA": "0xFaf869185383a24F8cb00e27BdA6b63B9905DCb4",
}

AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

CHAINLINK_DECIMALS = 8


class ApiError(Exception):
    """The Tokenized Stocks API could not be read."""


class ChainlinkError(Exception):
    """The Chainlink fallback could not be read."""


class DataUnavailableError(Exception):
    """Neither the API nor the Chainlink fallback returned usable data."""


@dataclass
class TokenSnapshot:
    """One token's NAV snapshot. ``excluded_reason`` is set (and the token
    is dropped from the investable universe) whenever no trustworthy NAV
    exists — never fill in a made-up price."""

    symbol: str
    contract_address: str | None
    nav_price: Decimal | None
    nav_updated_at: datetime | None
    multiplier: Decimal = Decimal("1")
    total_supply: Decimal | None = None
    source: str = "api"  # "api" | "chainlink"
    excluded_reason: str | None = None

    @property
    def included(self) -> bool:
        return self.excluded_reason is None


def _to_decimal(value: Any) -> Decimal | None:
    """Parse a JSON number-or-string into Decimal, preserving exact digits."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_rfc3339(value: Any) -> datetime | None:
    """Parse an RFC3339 timestamp (e.g. '2026-11-13T22:15:52Z')."""
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _staleness_reason(updated_at: datetime | None, now: datetime) -> str | None:
    """Return an exclusion reason if the NAV timestamp is missing or stale."""
    if updated_at is None:
        return "missing nav_price_updated_at (freshness unverifiable)"
    if now - updated_at > STALENESS_LIMIT:
        age_days = (now - updated_at).total_seconds() / 86400
        return f"stale nav: last updated {age_days:.1f} days ago (> 7 days)"
    return None


def parse_token_record(record: dict[str, Any], now: datetime) -> TokenSnapshot:
    """Parse one Tokenized Stocks API token record into a snapshot.

    The API omits default-valued fields, so every field is treated as
    optional. ``nav_price``/``total_supply``/``multiplier`` arrive as JSON
    numbers in the live API (verified 2026-11-14); strings are accepted too.
    ``total_supply`` is already in normalized (human) units — no decimals
    scaling is applied.
    """
    symbol = record.get("symbol") or ""
    if not symbol:
        return TokenSnapshot(
            symbol="",
            contract_address=record.get("contract_address"),
            nav_price=None,
            nav_updated_at=None,
            excluded_reason="missing symbol",
        )

    nav_price = _to_decimal(record.get("nav_price"))
    if nav_price is None:
        return TokenSnapshot(
            symbol=symbol,
            contract_address=record.get("contract_address"),
            nav_price=None,
            nav_updated_at=_parse_rfc3339(record.get("nav_price_updated_at")),
            multiplier=_to_decimal(record.get("multiplier")) or Decimal("1"),
            total_supply=_to_decimal(record.get("total_supply")),
            excluded_reason="missing nav_price (no Chainlink NAV published)",
        )
    if nav_price <= 0:
        return TokenSnapshot(
            symbol=symbol,
            contract_address=record.get("contract_address"),
            nav_price=None,
            nav_updated_at=_parse_rfc3339(record.get("nav_price_updated_at")),
            excluded_reason=f"invalid nav_price ({nav_price})",
        )

    updated_at = _parse_rfc3339(record.get("nav_price_updated_at"))
    stale_reason = _staleness_reason(updated_at, now)
    if stale_reason:
        return TokenSnapshot(
            symbol=symbol,
            contract_address=record.get("contract_address"),
            nav_price=nav_price,
            nav_updated_at=updated_at,
            multiplier=_to_decimal(record.get("multiplier")) or Decimal("1"),
            total_supply=_to_decimal(record.get("total_supply")),
            excluded_reason=stale_reason,
        )

    return TokenSnapshot(
        symbol=symbol,
        contract_address=record.get("contract_address"),
        nav_price=nav_price,
        nav_updated_at=updated_at,
        multiplier=_to_decimal(record.get("multiplier")) or Decimal("1"),
        total_supply=_to_decimal(record.get("total_supply")),
        source="api",
    )


def _get_page(
    session: Any,
    base_url: str,
    page_size: int,
    page_token: str | None,
    timeout: int,
    max_retries: int,
) -> dict[str, Any]:
    """GET one API page with retries (``max_retries`` after the first try)."""
    params: dict[str, Any] = {"page_size": page_size}
    if page_token:
        params["page_token"] = page_token
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = session.get(base_url, params=params, timeout=timeout)
            if resp.status_code != 200:
                raise ApiError(f"API returned HTTP {resp.status_code}")
            # parse_float=Decimal keeps NAV/multiplier digits exact.
            return json.loads(resp.text, parse_float=Decimal)
        except (requests.RequestException, ApiError, ValueError) as exc:
            last_error = exc
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
    raise ApiError(f"failed to fetch page (page_token={page_token!r}): {last_error}")


def fetch_from_api(
    session: Any = None,
    base_url: str = API_BASE_URL,
    page_size: int = 100,
    now: datetime | None = None,
    timeout: int = REQUEST_TIMEOUT,
    max_retries: int = MAX_RETRIES,
) -> list[TokenSnapshot]:
    """Fetch ALL tokenized stocks from the API, following pagination."""
    now = now or datetime.now(timezone.utc)
    session = session if session is not None else requests
    snapshots: list[TokenSnapshot] = []
    page_token: str | None = None
    pages = 0
    while True:
        pages += 1
        if pages > MAX_PAGES:
            raise ApiError(f"pagination exceeded {MAX_PAGES} pages; aborting")
        data = _get_page(session, base_url, page_size, page_token, timeout, max_retries)
        records = data.get("tokens") or []
        for record in records:
            snapshots.append(parse_token_record(record, now))
        page_token = data.get("next_page_token") or None
        if not page_token:
            break
    if not snapshots:
        raise ApiError("API returned zero tokens")
    log.info("API: %d tokens across %d page(s)", len(snapshots), pages)
    return snapshots


def fetch_from_chainlink(
    w3: Any = None,
    rpc_url: str = CHAINLINK_RPC_URL,
    feeds: dict[str, str] | None = None,
    now: datetime | None = None,
) -> list[TokenSnapshot]:
    """Fallback: read Chainlink NAV feeds on Base mainnet via eth_call.

    ``w3`` may be injected (used by tests); otherwise a Web3 client is built
    from ``rpc_url``. Multiplier and total supply are not available from the
    feeds, so multiplier defaults to 1.0 (neutral) and supply to None.
    """
    from web3 import Web3  # imported lazily so API-only use stays light

    now = now or datetime.now(timezone.utc)
    feeds = feeds if feeds is not None else CHAINLINK_FEEDS
    if w3 is None:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": REQUEST_TIMEOUT}))
    if not w3.is_connected():
        raise ChainlinkError(f"cannot connect to Chainlink RPC {rpc_url}")

    snapshots: list[TokenSnapshot] = []
    failures = 0
    for ticker, feed_address in feeds.items():
        symbol = f"{ticker}c"
        try:
            contract = w3.eth.contract(
                address=w3.to_checksum_address(feed_address), abi=AGGREGATOR_ABI
            )
            _round_id, answer, _started_at, updated_at, _answered = (
                contract.functions.latestRoundData().call()
            )
        except Exception as exc:  # noqa: BLE001 - per-feed failure, keep going
            failures += 1
            snapshots.append(
                TokenSnapshot(
                    symbol=symbol,
                    contract_address=None,
                    nav_price=None,
                    nav_updated_at=None,
                    source="chainlink",
                    excluded_reason=f"chainlink feed call failed: {exc}",
                )
            )
            continue
        nav_price = Decimal(answer) / Decimal(10**CHAINLINK_DECIMALS)
        updated_dt = datetime.fromtimestamp(updated_at, tz=timezone.utc)
        if nav_price <= 0:
            snapshots.append(
                TokenSnapshot(
                    symbol=symbol,
                    contract_address=None,
                    nav_price=None,
                    nav_updated_at=updated_dt,
                    source="chainlink",
                    excluded_reason="chainlink answer <= 0",
                )
            )
            continue
        stale_reason = _staleness_reason(updated_dt, now)
        snapshots.append(
            TokenSnapshot(
                symbol=symbol,
                contract_address=None,
                nav_price=nav_price,
                nav_updated_at=updated_dt,
                multiplier=Decimal("1"),
                total_supply=None,
                source="chainlink",
                excluded_reason=stale_reason,
            )
        )
    if failures == len(feeds):
        raise ChainlinkError("all Chainlink feed calls failed")
    log.info(
        "Chainlink fallback: %d feeds read, %d failed", len(feeds) - failures, failures
    )
    return snapshots


def fetch_snapshots(
    now: datetime | None = None,
    api_base_url: str = API_BASE_URL,
    chainlink_rpc_url: str = CHAINLINK_RPC_URL,
    session: Any = None,
    w3: Any = None,
) -> list[TokenSnapshot]:
    """Fetch snapshots, preferring the API and falling back to Chainlink.

    Raises :class:`DataUnavailableError` if neither source works — the
    caller must stop rather than invent prices.
    """
    now = now or datetime.now(timezone.utc)
    try:
        return fetch_from_api(session=session, base_url=api_base_url, now=now)
    except ApiError as exc:
        log.warning("Tokenized Stocks API unavailable (%s); trying Chainlink fallback", exc)
    try:
        return fetch_from_chainlink(w3=w3, rpc_url=chainlink_rpc_url, now=now)
    except ChainlinkError as exc:
        raise DataUnavailableError(
            f"both data sources failed (API error above; Chainlink: {exc})"
        ) from exc
