"""Tests for the StockPilot agent.

Fixtures mirror the REAL Tokenized Stocks API schema (verified live
2026-11-14): snake_case fields, JSON numbers for nav_price/multiplier/
total_supply, RFC3339 nav_price_updated_at, fields omitted when unset.
A few fixture values reuse the verified 13 Nov 2026 Friday-close NAVs.
No test touches the live API or any chain — Chainlink/API behaviour is
tested with fakes.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from stockpilot.api_client import (
    TokenSnapshot,
    fetch_from_api,
    fetch_from_chainlink,
    parse_token_record,
)
from stockpilot.hashing import canonical_snapshot_hash, rationale_hash
from stockpilot.submit import (
    load_private_key,
    read_contract_address,
    submit_rebalance,
)
from stockpilot.portfolio import (
    build_target_weights,
    decide_rebalance,
    detect_drift,
)
from stockpilot.research import (
    adoption_scores,
    append_history,
    freshness_score,
    load_history,
    momentum_score,
    multiplier_score,
    score_tokens,
)

NOW = datetime(2026, 11, 14, 7, 0, tzinfo=timezone.utc)
FRIDAY_CLOSE = "2026-11-13T21:32:29Z"  # ~9.5h before NOW: fresh, < 24h


def api_record(symbol, nav_price=None, updated_at=FRIDAY_CLOSE, multiplier=1,
               total_supply=None, contract_address="0xB200000000000000000000000000000000000001"):
    record = {
        "contract_address": contract_address,
        "symbol": symbol,
        "name": f"{symbol} Inc.",
        "decimals": 8,
        "isin": "US0000000000",
        "multiplier": multiplier,
    }
    if nav_price is not None:
        record["nav_price"] = nav_price
    if updated_at is not None:
        record["nav_price_updated_at"] = updated_at
    if total_supply is not None:
        record["total_supply"] = total_supply
    return record


# --- API parsing -----------------------------------------------------------

def test_parse_full_api_record():
    """A complete record (real schema) parses with exact Decimal NAV."""
    record = api_record("NVDAc", nav_price=160.76543559,
                        multiplier=1.0005379395763694, total_supply=12345.678)
    snap = parse_token_record(record, NOW)
    assert snap.included
    assert snap.symbol == "NVDAc"
    assert snap.nav_price == Decimal("160.76543559")
    assert snap.multiplier == Decimal("1.0005379395763694")
    assert snap.total_supply == Decimal("12345.678")
    assert snap.nav_updated_at == datetime(2026, 11, 13, 21, 32, 29, tzinfo=timezone.utc)
    assert snap.source == "api"


def test_parse_missing_nav_price_excluded():
    """A token without nav_price is excluded, never priced from thin air."""
    snap = parse_token_record(api_record("RKTc"), NOW)
    assert not snap.included
    assert snap.nav_price is None
    assert "nav_price" in snap.excluded_reason


def test_parse_missing_updated_at_excluded():
    """NAV present but no timestamp -> excluded (freshness unverifiable)."""
    snap = parse_token_record(api_record("TSLAc", nav_price=387.793, updated_at=None), NOW)
    assert not snap.included
    assert "updated_at" in snap.excluded_reason


def test_parse_staleness_exclusion():
    """NAV older than 7 days is excluded; 6 days old is still usable."""
    old = api_record("AAPLc", nav_price=355.585, updated_at="2026-11-06T07:00:00Z")
    snap = parse_token_record(old, NOW)
    assert not snap.included
    assert "stale" in snap.excluded_reason

    six_days = api_record("AAPLc", nav_price=355.585, updated_at="2026-11-08T07:00:01Z")
    snap = parse_token_record(six_days, NOW)
    assert snap.included


def test_parse_string_numbers_accepted():
    """String-typed numbers (per the brief's schema) also parse."""
    record = api_record("MSFTc", nav_price="522.405", multiplier="1.0",
                        total_supply="9876.5")
    snap = parse_token_record(record, NOW)
    assert snap.included
    assert snap.nav_price == Decimal("522.405")
    assert snap.total_supply == Decimal("9876.5")


class FakeResponse:
    def __init__(self, payload, status_code=200):
        import json
        self.text = json.dumps(payload)
        self.status_code = status_code


class FakeSession:
    """Returns pre-canned pages; records how many requests were made."""

    def __init__(self, pages):
        self.pages = pages  # list of (page_token_in, payload)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        token = (params or {}).get("page_token")
        for expected_token, payload in self.pages:
            if token == expected_token:
                return FakeResponse(payload)
        raise AssertionError(f"unexpected page_token {token!r}")


def test_api_pagination_follows_next_page_token():
    session = FakeSession([
        (None, {"tokens": [api_record("NVDAc", 160.76, total_supply=1000.0)],
                "next_page_token": "tok2"}),
        ("tok2", {"tokens": [api_record("AAPLc", 355.58, total_supply=2000.0)]}),
    ])
    snaps = fetch_from_api(session=session, now=NOW)
    assert session.calls == 2
    assert [s.symbol for s in snaps] == ["NVDAc", "AAPLc"]
    assert all(s.included for s in snaps)


# --- Chainlink fallback (mocked web3) --------------------------------------

class FakeFeedFunction:
    def __init__(self, answer, updated_at):
        self.answer = answer
        self.updated_at = updated_at

    def call(self):
        return (1, self.answer, 0, self.updated_at, 1)


class FakeContract:
    def __init__(self, answer, updated_at):
        self.functions = type(
            "F", (), {"latestRoundData": lambda self: FakeFeedFunction(answer, updated_at)}
        )()


class FakeEth:
    def __init__(self, answers):
        self.answers = answers  # address -> (answer, updated_at)

    def contract(self, address, abi):
        answer, updated_at = self.answers[address]
        return FakeContract(answer, updated_at)


class FakeW3:
    def __init__(self, answers):
        self.eth = FakeEth(answers)

    def is_connected(self):
        return True

    def to_checksum_address(self, address):
        return address


def test_chainlink_fallback_mock():
    """latestRoundData answers (8 decimals) become correct NAV snapshots."""
    updated = int((NOW - timedelta(hours=2)).timestamp())
    w3 = FakeW3({
        "0xfeedNVDA": (16076543559, updated),  # 160.76543559 USD
        "0xfeedAAPL": (35558500000, updated),
    })
    snaps = fetch_from_chainlink(
        w3=w3, feeds={"NVDA": "0xfeedNVDA", "AAPL": "0xfeedAAPL"}, now=NOW
    )
    by_symbol = {s.symbol: s for s in snaps}
    assert by_symbol["NVDAc"].nav_price == Decimal("160.76543559")
    assert by_symbol["AAPLc"].nav_price == Decimal("355.585")
    assert all(s.source == "chainlink" and s.included for s in snaps)
    assert by_symbol["NVDAc"].multiplier == Decimal("1")  # neutral default


def test_chainlink_stale_feed_excluded():
    updated = int((NOW - timedelta(days=8)).timestamp())
    w3 = FakeW3({"0xfeed": (10000000000, updated)})
    snaps = fetch_from_chainlink(w3=w3, feeds={"TSLA": "0xfeed"}, now=NOW)
    assert not snaps[0].included
    assert "stale" in snaps[0].excluded_reason


# --- Research model --------------------------------------------------------

def test_momentum_neutral_empty_history():
    score, note = momentum_score([])
    assert score == 50.0
    assert "fewer than 2" in note


def test_momentum_neutral_short_span():
    points = [
        (NOW - timedelta(hours=23), Decimal("100")),
        (NOW, Decimal("110")),
    ]
    score, note = momentum_score(points)
    assert score == 50.0
    assert "under 24h" in note


def test_momentum_positive_move():
    points = [
        (NOW - timedelta(days=3), Decimal("100")),
        (NOW, Decimal("105")),
    ]
    score, _ = momentum_score(points)
    assert score == 75.0  # 50 + 0.05*500


def test_multiplier_score():
    assert multiplier_score(Decimal("1"))[0] == 50.0
    # Real NVDAc multiplier from the live API: 1.0005379... -> ~52.69
    score, note = multiplier_score(Decimal("1.0005379395763694"))
    assert score == pytest.approx(52.69, abs=0.01)
    assert "reinvested" in note
    assert multiplier_score(Decimal("1.01"))[0] == 100.0


def test_freshness_score():
    assert freshness_score(NOW - timedelta(hours=1), NOW)[0] == 100.0
    # 4 days old: 100 * (1 - (96-24)/144) = 50
    assert freshness_score(NOW - timedelta(days=4), NOW)[0] == 50.0


def test_adoption_ranking():
    scores = adoption_scores({
        "BIGc": Decimal("1000000"),
        "MIDc": Decimal("1000"),
        "SMALLc": Decimal("1"),
        "NODATAc": None,
    })
    assert scores["BIGc"][0] == 100.0
    assert scores["SMALLc"][0] == 0.0
    assert scores["MIDc"][0] == 50.0  # log midpoint
    assert scores["NODATAc"][0] == 50.0  # neutral without data


def test_score_tokens_excluded_have_no_score():
    snaps = [
        TokenSnapshot("NVDAc", "0x1", Decimal("160.76"), NOW - timedelta(hours=2),
                      multiplier=Decimal("1"), total_supply=Decimal("1000")),
        TokenSnapshot("RKTc", "0x2", None, None, excluded_reason="missing nav_price"),
    ]
    scores = score_tokens(snaps, {}, NOW)
    assert scores["NVDAc"].total is not None
    assert scores["RKTc"].total is None
    assert scores["RKTc"].excluded_reason == "missing nav_price"


def test_history_append_dedups_per_day(tmp_path):
    path = tmp_path / "price-history.jsonl"
    snaps = [
        TokenSnapshot("NVDAc", "0x1", Decimal("160.76"), NOW, total_supply=Decimal("1")),
        TokenSnapshot("AAPLc", "0x2", Decimal("355.58"), NOW, total_supply=Decimal("2")),
    ]
    assert append_history(path, snaps, "2026-11-14") == 2
    assert append_history(path, snaps, "2026-11-14") == 0  # same day: dedup
    assert append_history(path, snaps, "2026-11-15") == 2  # new day: append
    history = load_history(path)
    assert len(history["NVDAc"]) == 2
    assert history["NVDAc"][0][1] == Decimal("160.76")


# --- Portfolio -------------------------------------------------------------

def test_weights_sum_10000_cap_min_max_holdings():
    scores = {f"T{i:02d}c": 90.0 - i for i in range(12)}  # 12 tokens
    weights = build_target_weights(scores)
    assert len(weights) == 8  # max 8 holdings
    assert sum(weights.values()) == 10000
    assert max(weights.values()) <= 3007  # 30% cap (+ rounding remainder)
    assert min(weights.values()) >= 500  # 5% minimum


def test_weights_cap_binds_on_dominant_score():
    scores = {"DOMc": 100.0, **{f"T{i}c": 5.0 for i in range(7)}}
    weights = build_target_weights(scores)
    assert weights["DOMc"] <= 3007
    assert sum(weights.values()) == 10000
    # The dominant token still gets the largest weight.
    assert weights["DOMc"] == max(weights.values())


def test_weights_empty_universe():
    assert build_target_weights({}) == {}
    assert build_target_weights({"A": None}) == {}


def test_drift_below_threshold():
    current = {"NVDAc": 3000, "AAPLc": 3000, "TSLAc": 4000}
    target = {"NVDAc": 3200, "AAPLc": 3000, "TSLAc": 3800}
    drift, needs = detect_drift(current, target)
    assert drift == 200
    assert needs is False


def test_drift_above_threshold():
    current = {"NVDAc": 3000, "AAPLc": 7000}
    target = {"NVDAc": 2000, "AAPLc": 8000}
    drift, needs = detect_drift(current, target)
    assert drift == 1000
    assert needs is True


def test_drift_counts_new_and_dropped_holdings():
    current = {"NVDAc": 10000}
    target = {"AAPLc": 10000}
    drift, needs = detect_drift(current, target)
    assert drift == 10000
    assert needs is True


# --- Rebalance decision ----------------------------------------------------

def test_decide_rebalance_first_run():
    needs, reasons = decide_rebalance(
        first_run=True, drift_bps=0, universe_changed=False,
        days_since_last_rebalance=None,
    )
    assert needs is True
    assert any("first run" in r for r in reasons)


def test_decide_rebalance_small_drift_no():
    needs, reasons = decide_rebalance(
        first_run=False, drift_bps=100, universe_changed=False,
        days_since_last_rebalance=1.0,
    )
    assert needs is False
    assert reasons == []


def test_decide_rebalance_stale_record():
    needs, reasons = decide_rebalance(
        first_run=False, drift_bps=0, universe_changed=False,
        days_since_last_rebalance=8.0,
    )
    assert needs is True
    assert any("days since" in r for r in reasons)


def test_decide_rebalance_force():
    needs, _ = decide_rebalance(
        first_run=False, drift_bps=0, universe_changed=False,
        days_since_last_rebalance=1.0, force=True,
    )
    assert needs is True


# --- Hashing ---------------------------------------------------------------

def test_hash_deterministic_and_order_independent():
    entries_a = {
        "NVDAc": {"nav_price": "160.76543559", "updated_at": "2026-11-13T21:32:29+00:00"},
        "AAPLc": {"nav_price": "355.585", "updated_at": "2026-11-13T21:32:29+00:00"},
    }
    entries_b = dict(reversed(list(entries_a.items())))  # different key order
    assert canonical_snapshot_hash(entries_a) == canonical_snapshot_hash(entries_b)
    assert canonical_snapshot_hash(entries_a).startswith("0x")
    assert len(canonical_snapshot_hash(entries_a)) == 66


def test_hash_changes_when_input_changes():
    base = {"NVDAc": {"nav_price": "160.76", "updated_at": "2026-11-13T00:00:00+00:00"}}
    changed = {"NVDAc": {"nav_price": "160.77", "updated_at": "2026-11-13T00:00:00+00:00"}}
    assert canonical_snapshot_hash(base) != canonical_snapshot_hash(changed)
    assert rationale_hash("hello") != rationale_hash("hello!")
    assert rationale_hash("same") == rationale_hash("same")


# --- Submission guards -----------------------------------------------------

def test_read_contract_address_missing_file(tmp_path):
    """No contract-address.txt -> clear FileNotFoundError (not deployed yet)."""
    with pytest.raises(FileNotFoundError, match="not been deployed"):
        read_contract_address(tmp_path / "contract-address.txt")


def test_read_contract_address_valid(tmp_path):
    path = tmp_path / "contract-address.txt"
    path.write_text("0x0000000000000000000000000000000000000001\n")
    assert read_contract_address(path) == "0x0000000000000000000000000000000000000001"


def test_load_private_key_missing(monkeypatch, tmp_path):
    """No env var and no .env file -> clear error, never a guessed key."""
    monkeypatch.delenv("DEPLOYER_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEPLOYER_PRIVATE_KEY"):
        load_private_key(tmp_path / ".env.testnet")


def test_load_private_key_from_env_file(tmp_path, monkeypatch):
    """Key loads from a .env file when the env var is unset."""
    monkeypatch.delenv("DEPLOYER_PRIVATE_KEY", raising=False)
    path = tmp_path / ".env.testnet"
    path.write_text("# comment\nDEPLOYER_PRIVATE_KEY=0xTESTKEY\n")
    assert load_private_key(path) == "0xTESTKEY"


def test_submit_rejects_weights_not_summing_to_10000():
    """Weights must sum to exactly 10000 bps or submission refuses."""
    with pytest.raises(ValueError, match="10000"):
        submit_rebalance(
            w3=None,  # never reached: the sum check runs first
            contract_address="0x0000000000000000000000000000000000000001",
            nav_snapshot_hash="0x" + "00" * 32,
            rationale_hash="0x" + "00" * 32,
            tickers_csv="NVDAc",
            weights_bps=[9999],
            private_key="0x00",
        )


# --- Ledger ABI cross-check vs the compiled contract -----------------------

def test_agent_abi_matches_compiled_ledger():
    """Our hand-written ABI must match the compiled StockPilotLedger.

    Compares 4-byte selectors for the three functions the agent uses.
    Skipped when the contracts build output is absent (contracts are built
    by another workstream).
    """
    import json

    from eth_utils import function_signature_to_4byte_selector

    from stockpilot.submit import LEDGER_ABI

    artifact = (
        Path(__file__).resolve().parents[2]
        / "contracts" / "out" / "StockPilotLedger.sol" / "StockPilotLedger.json"
    )
    if not artifact.exists():
        pytest.skip("compiled ledger artifact not present")
    compiled_abi = json.loads(artifact.read_text())["abi"]

    def selectors(abi):
        out = {}
        for item in abi:
            if item.get("type") != "function":
                continue
            sig = f"{item['name']}({','.join(i['type'] for i in item['inputs'])})"
            out[item["name"]] = function_signature_to_4byte_selector(sig).hex()
        return out

    mine, theirs = selectors(LEDGER_ABI), selectors(compiled_abi)
    for name in ("recordRebalance", "getLatest", "rebalanceCount"):
        assert mine[name] == theirs[name], f"selector mismatch for {name}"


class FakeLedgerCall:
    def __init__(self, value):
        self.value = value

    def call(self):
        return self.value


class FakeLedgerFunctions:
    def __init__(self, count, latest):
        self._count = count
        self._latest = latest

    def rebalanceCount(self):
        return FakeLedgerCall(self._count)

    def getLatest(self):
        return FakeLedgerCall(self._latest)


class FakeLedgerContract:
    def __init__(self, count, latest):
        self.functions = FakeLedgerFunctions(count, latest)


class FakeLedgerEth:
    def __init__(self, contract):
        self._contract = contract

    def contract(self, address, abi):
        return self._contract


class FakeLedgerW3:
    def __init__(self, contract):
        self.eth = FakeLedgerEth(contract)

    def to_checksum_address(self, address):
        return address


def test_read_latest_decodes_struct_tuple():
    """read_latest handles getLatest's struct return (nested or flat)."""
    from stockpilot.submit import read_latest

    struct = (
        3,
        1794614000,
        b"\x11" * 32,
        b"\x22" * 32,
        "NVDAc,AAPLc",
        [6000, 4000],
        "0x0000000000000000000000000000000000000001",
    )
    for latest_value in ((struct,), struct):  # nested 1-tuple and flat 7-tuple
        w3 = FakeLedgerW3(FakeLedgerContract(4, latest_value))
        record = read_latest(w3, "0x0000000000000000000000000000000000000001")
        assert record["id"] == 3
        assert record["timestamp"] == 1794614000
        assert record["nav_snapshot_hash"] == "0x" + "11" * 32
        assert record["rationale_hash"] == "0x" + "22" * 32
        assert record["tickers_csv"] == "NVDAc,AAPLc"
        assert record["weights_bps"] == [6000, 4000]


def test_read_latest_no_records_returns_none():
    from stockpilot.submit import read_latest

    w3 = FakeLedgerW3(FakeLedgerContract(0, None))
    assert read_latest(w3, "0x0000000000000000000000000000000000000001") is None
