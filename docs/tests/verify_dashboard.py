#!/usr/bin/env python3
"""Verify the StockPilot dashboard end to end with Playwright.

Loads the dashboard (locally from ``docs/`` by default, or the live
GitHub Pages site with ``--live``), waits for the on-chain data, and
checks that every number on the page matches its real source:

- portfolio prices <- ``docs/data/latest-decision.json`` (``data.snapshot``)
- portfolio weights <- on-chain ``getLatest()`` via ``cast``
  (skipped with a warning when ``cast`` is not installed)
- chart points <- ``docs/data/history.json``

Also checks: no console/page errors, no horizontal overflow, all
sections render (donut, score bars, charts, timeline), desktop + mobile.

Requires Playwright + Chromium (``pip install playwright`` then
``playwright install chromium``). In the operator's workspace the venv
at ``../../.venv`` already has Playwright::

    ../../.venv/bin/python docs/tests/verify_dashboard.py          # local
    ../../.venv/bin/python docs/tests/verify_dashboard.py --live   # live site

``--proxy`` is only needed in sandboxed environments where Chromium
cannot reach the internet directly (the sandbox egress proxy is
IPv6-only; run ``localproxy.py`` from the operator's video-build
capture tooling on 127.0.0.1:8888 and pass ``--proxy
http://127.0.0.1:8888``). On a normal machine omit it.

Screenshots go to ``--screenshots`` (default: a temp directory).
Exit code 0 = all checks PASS.
"""
import argparse
import functools
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.dirname(HERE)
REPO = os.path.dirname(DOCS)
CONTRACT = "0x13612a2571C091625db489458298a31c507F54a1"
LIVE_URL = "https://andipangeranm142.github.io/stockpilot/"


def wait_port(host, port, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection((host, port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def expected_values():
    decision = json.load(open(os.path.join(DOCS, "data", "latest-decision.json")))
    prices = {}
    for entry in decision["data"]["snapshot"]:
        prices[entry["symbol"]] = f"${float(entry['nav_price']):.2f}"
    weights = None
    holdings = None
    cast = shutil.which("cast") or os.path.expanduser("~/.foundry/bin/cast")
    if os.path.exists(cast):
        out = subprocess.run(
            [cast, "call", CONTRACT,
             "getLatest()((uint256,uint256,bytes32,bytes32,string,uint256[],address))",
             "--rpc-url", "https://sepolia.base.org"],
            capture_output=True, text=True, timeout=60,
        )
        m = re.search(r'"([^"]+)", \[([0-9, ]+)\]', out.stdout)
        if m:
            holdings = m.group(1).split(",")
            weights = {
                t: f"{int(w) / 100:.2f}%"
                for t, w in zip(holdings, m.group(2).split(","))
            }
    if weights is None:
        print("WARNING: cast unavailable or getLatest failed — "
              "on-chain weight checks will be skipped")
        holdings = sorted(prices)
    # On-chain rebalance count (drives the stat strip + history timeline).
    count = None
    if os.path.exists(cast):
        out = subprocess.run(
            [cast, "call", CONTRACT, "rebalanceCount() returns (uint256)",
             "--rpc-url", "https://sepolia.base.org"],
            capture_output=True, text=True, timeout=60,
        )
        try:
            count = int(out.stdout.strip(), 0)
        except ValueError:
            count = None
    if count is None:
        print("WARNING: rebalanceCount unavailable — "
              "count/history checks will be skipped")
    # Timeline cap, read from the dashboard itself so the two cannot drift.
    app_js = open(os.path.join(DOCS, "app.js")).read()
    m_hist = re.search(r"const MAX_HISTORY = (\d+);", app_js)
    max_history = int(m_hist.group(1)) if m_hist else 50
    history = json.load(open(os.path.join(DOCS, "data", "history.json")))
    return {
        "prices": prices,
        "weights": weights,
        "holdings": holdings,
        "history": history,
        "count": count,
        "universe": decision["data"]["universe_size"],
        "n_scored": len(decision["data"]["snapshot"]),
        "max_history": max_history,
    }


def check(name, cond, failures):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def run_checks(page, exp, errors):
    failures = []
    print("rpc-status:", page.locator("#rpc-status").inner_text())
    print("rebalance-count:", page.locator("#rebalance-count").inner_text())
    print("stat-holdings:", page.locator("#stat-holdings").inner_text())
    print("stat-universe:", page.locator("#stat-universe").inner_text())
    print("action-chip:", page.locator("#decision-action").inner_text())

    check("rpc connected", "Connected" in page.locator("#rpc-status").inner_text(), failures)
    n_hold = len(exp["holdings"])
    if exp["count"] is not None:
        check(f"rebalance count is {exp['count']}",
              page.locator("#rebalance-count").inner_text().strip() == str(exp["count"]), failures)
    check(f"holdings stat is {n_hold}",
          page.locator("#stat-holdings").inner_text().strip() == str(n_hold), failures)
    check(f"universe stat is {exp['universe']}",
          page.locator("#stat-universe").inner_text().strip() == str(exp["universe"]), failures)

    table_text = page.locator("#portfolio-table").inner_text()
    for sym, price in exp["prices"].items():
        if sym in exp["holdings"]:
            check(f"price {sym} {price} in table", price in table_text, failures)
    if exp["weights"]:
        for sym, w in exp["weights"].items():
            check(f"weight {sym} {w} in table", w in table_text, failures)
    check("no 'not available' in portfolio table", "not available" not in table_text, failures)
    check(f"donut has {n_hold + 1} circles", page.locator("#donut-svg circle").count() == n_hold + 1, failures)
    check(f"donut legend has {n_hold} items", page.locator("#donut-legend li").count() == n_hold, failures)
    check(f"{exp['n_scored']} score rows", page.locator("#scores-body tr").count() == exp["n_scored"], failures)
    check(f"{exp['n_scored'] * 5} score bars", page.locator("#scores-body .score-bar").count() == exp["n_scored"] * 5, failures)
    if exp["count"] is not None:
        n_hist = min(exp["count"], exp["max_history"])
        check(f"{n_hist} history items", page.locator("#history-list li").count() == n_hist, failures)
    check("rationale non-empty", page.locator("#decision-rationale").inner_text().strip() != "", failures)

    hist = exp["history"]
    holdings = exp["holdings"]
    nav_points = sum(len(hist["nav_prices"].get(s, [])) for s in holdings)
    score_points = sum(len(hist["scores"].get(s, [])) for s in holdings)
    print("charts-body visible:", page.locator("#charts-body").is_visible())
    print("nav note:", page.locator("#nav-chart-note").inner_text())
    print("score note:", page.locator("#score-chart-note").inner_text())
    check("charts body visible", page.locator("#charts-body").is_visible(), failures)
    check("charts empty message hidden", page.locator("#charts-empty").is_hidden(), failures)
    check(f"nav chart has {n_hold} polylines", page.locator("#nav-chart polyline").count() == n_hold, failures)
    check(f"nav chart has {nav_points} dots",
          page.locator("#nav-chart circle").count() == nav_points, failures)
    check(f"score chart has {n_hold} polylines", page.locator("#score-chart polyline").count() == n_hold, failures)
    check(f"score chart has {score_points} dots",
          page.locator("#score-chart circle").count() == score_points, failures)
    check(f"nav legend has {n_hold} items", page.locator("#nav-legend li").count() == n_hold, failures)
    check(f"score legend has {n_hold} items", page.locator("#score-legend li").count() == n_hold, failures)
    nav_note = page.locator("#nav-chart-note").inner_text()
    check("nav note shows point count",
          f"{len(hist['nav_dates'])} data points" in nav_note, failures)
    score_note = page.locator("#score-chart-note").inner_text()
    check("score note shows run count",
          f"{len(hist['score_runs'])} runs" in score_note, failures)
    # Spot-check dot tooltips against history.json (SVG <title> has no
    # innerText, so read textContent via JS).
    last_nav = hist["nav_prices"]["NVDAc"][-1]
    nav_day = last_nav["t"][8:10].lstrip("0")  # "5 Nov", not "05 Nov"
    expected_tip = f"NVDAc · {nav_day} Nov · ${float(last_nav['price']):.2f}"
    titles = page.evaluate(
        "() => [...document.querySelectorAll('#nav-chart title')].map(t => t.textContent)")
    check(f"NVDAc latest NAV tooltip '{expected_tip}' present", expected_tip in titles, failures)
    last_score = hist["scores"]["NVDAc"][-1]
    score_day = last_score["t"][8:10].lstrip("0")
    score_time = last_score["t"][11:16]
    expected_stip = f"NVDAc · {score_day} Nov, {score_time} UTC · score {last_score['total']}"
    stip_titles = page.evaluate(
        "() => [...document.querySelectorAll('#score-chart title')].map(t => t.textContent)")
    check(f"NVDAc latest score tooltip '{expected_stip}' present", expected_stip in stip_titles, failures)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    check("no horizontal overflow", not overflow, failures)
    if errors:
        print("CONSOLE/PAGE ERRORS:")
        for e in errors:
            print("  -", e)
    check("no console/page errors", len(errors) == 0, failures)
    return failures


def main():
    parser = argparse.ArgumentParser(description="Verify the StockPilot dashboard.")
    parser.add_argument("--live", action="store_true", help="check the live GitHub Pages site")
    parser.add_argument("--proxy", default=None, help="proxy server for Chromium, e.g. http://127.0.0.1:8888")
    parser.add_argument("--port", type=int, default=8123, help="local server port (local mode)")
    parser.add_argument("--screenshots", default=None, help="directory for screenshots (default: temp dir)")
    args = parser.parse_args()

    exp = expected_values()
    print("expected prices:", exp["prices"])
    print("expected weights:", exp["weights"])
    print("nav dates:", exp["history"]["nav_dates"], "| score runs:", len(exp["history"]["score_runs"]))

    shot_dir = args.screenshots or tempfile.mkdtemp(prefix="stockpilot-verify-")
    os.makedirs(shot_dir, exist_ok=True)
    print("screenshots:", shot_dir)

    httpd = None
    if not args.live:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=DOCS)
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        assert wait_port("127.0.0.1", args.port), "http server did not come up"
        base_url = f"http://127.0.0.1:{args.port}/"
    else:
        base_url = LIVE_URL

    all_failures = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            launch_kwargs = {"args": ["--ignore-certificate-errors"]}
            if args.proxy:
                launch_kwargs["proxy"] = {"server": args.proxy, "bypass": "localhost,127.0.0.1"}
            browser = p.chromium.launch(**launch_kwargs)
            errors = []
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base_url, wait_until="networkidle", timeout=90000)
            page.wait_for_selector("#portfolio-table >> text=NVDAc", timeout=60000)
            page.wait_for_selector("#rpc-status >> text=Connected", timeout=60000)
            page.wait_for_selector("#charts-body:not(.hidden)", timeout=60000)
            page.wait_for_timeout(2500)
            print("=== DESKTOP CHECKS ===")
            all_failures += run_checks(page, exp, errors)
            page.screenshot(path=os.path.join(shot_dir, "verify-desktop.png"), full_page=True)
            print("saved verify-desktop.png")

            merrors = []
            mobile = browser.new_page(viewport={"width": 390, "height": 844})
            mobile.on("console", lambda m: merrors.append(m.text) if m.type == "error" else None)
            mobile.on("pageerror", lambda e: merrors.append(str(e)))
            mobile.goto(base_url, wait_until="networkidle", timeout=90000)
            mobile.wait_for_selector("#portfolio-table >> text=NVDAc", timeout=60000)
            mobile.wait_for_selector("#charts-body:not(.hidden)", timeout=60000)
            mobile.wait_for_timeout(2500)
            print("=== MOBILE CHECKS ===")
            mfail = []
            check("mobile: charts body visible", mobile.locator("#charts-body").is_visible(), mfail)
            check(f"mobile: nav chart {len(exp['holdings'])} polylines", mobile.locator("#nav-chart polyline").count() == len(exp["holdings"]), mfail)
            check(f"mobile: score chart {len(exp['holdings'])} polylines", mobile.locator("#score-chart polyline").count() == len(exp["holdings"]), mfail)
            moverflow = mobile.evaluate("document.documentElement.scrollWidth > window.innerWidth")
            check("mobile: no horizontal overflow", not moverflow, mfail)
            if merrors:
                print("MOBILE CONSOLE/PAGE ERRORS:")
                for e in merrors:
                    print("  -", e)
            check("mobile: no console/page errors", len(merrors) == 0, mfail)
            all_failures += mfail
            mobile.screenshot(path=os.path.join(shot_dir, "verify-mobile.png"), full_page=True)
            print("saved verify-mobile.png")
            browser.close()
    finally:
        if httpd is not None:
            httpd.shutdown()

    print("=" * 50)
    if all_failures:
        print(f"RESULT: FAIL ({len(all_failures)} failures)")
        for f in all_failures:
            print("  -", f)
        sys.exit(1)
    print("RESULT: ALL CHECKS PASS")


if __name__ == "__main__":
    main()
