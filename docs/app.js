// StockPilot dashboard — read-only view over the StockPilotLedger contract
// on Base Sepolia plus the agent's latest decision JSON.
// No wallet, no transactions. All numbers come from the contract, the
// decision file, or the RPC; missing data is shown as "not available".

import { ethers } from "https://cdn.jsdelivr.net/npm/ethers@6/+esm";

const CONFIG = window.CONFIG;
const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000";

// Minimal ABI — read-only functions only. The Rebalance struct return is a
// tuple with fields in contract order:
// (id, timestamp, navSnapshotHash, rationaleHash, tickersCsv, weightsBps, agent)
const REBALANCE_TUPLE = {
  name: "",
  type: "tuple",
  components: [
    { name: "id", type: "uint256" },
    { name: "timestamp", type: "uint256" },
    { name: "navSnapshotHash", type: "bytes32" },
    { name: "rationaleHash", type: "bytes32" },
    { name: "tickersCsv", type: "string" },
    { name: "weightsBps", type: "uint256[]" },
    { name: "agent", type: "address" }
  ]
};

const LEDGER_ABI = [
  {
    type: "function",
    name: "rebalanceCount",
    inputs: [],
    outputs: [{ name: "", type: "uint256" }],
    stateMutability: "view"
  },
  {
    type: "function",
    name: "getRebalance",
    inputs: [{ name: "id", type: "uint256" }],
    outputs: [REBALANCE_TUPLE],
    stateMutability: "view"
  },
  {
    type: "function",
    name: "getLatest",
    inputs: [],
    outputs: [REBALANCE_TUPLE],
    stateMutability: "view"
  }
];

// Safety cap so a very long history does not flood the page / RPC.
const MAX_HISTORY = 50;

// Palette for the allocation donut, legend, and history stacked bars.
// Colors are purely presentational; the numbers always come from the data.
const PALETTE = ["#0052FF", "#2E9BFF", "#00C2FF", "#7C6CFF", "#00D1A9", "#FFB020", "#FF6B8A", "#8B9BB4"];
const DONUT_R = 80;
const DONUT_C = 2 * Math.PI * DONUT_R;

const $ = (id) => document.getElementById(id);

function shortHash(hash) {
  if (!hash || typeof hash !== "string") return "not available";
  return hash.length > 18 ? `${hash.slice(0, 10)}…${hash.slice(-8)}` : hash;
}

function fmtUtc(unixSeconds) {
  const n = Number(unixSeconds);
  if (!Number.isFinite(n) || n <= 0) return "not available";
  return new Date(n * 1000).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
}

function bpsToPct(bps) {
  const n = Number(bps);
  if (!Number.isFinite(n)) return "not available";
  return `${(n / 100).toFixed(2)}%`;
}

function parseWeights(record) {
  const tickers = (record.tickersCsv || "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  return tickers.map((ticker, i) => ({
    ticker,
    bps: record.weightsBps && record.weightsBps[i] !== undefined
      ? record.weightsBps[i]
      : null
  }));
}

function showBanner(message) {
  const el = $("contract-banner");
  el.textContent = message;
  el.classList.remove("hidden");
}

async function loadDecision() {
  try {
    const res = await fetch("data/latest-decision.json", { cache: "no-store" });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null; // Decision file missing or unreadable — dashboard still works.
  }
}

function renderDecision(decision) {
  if (!decision) return; // Keep the "not available" placeholder visible.
  $("decision-empty").classList.add("hidden");
  $("decision-body").classList.remove("hidden");

  $("decision-time").textContent = decision.timestamp
    ? `Published ${fmtUtc(new Date(decision.timestamp).getTime() / 1000)}`
    : "Timestamp not available";
  $("decision-rationale").textContent = decision.rationale || "Rationale not available.";
  $("decision-drift").textContent =
    decision.drift_bps !== undefined && decision.drift_bps !== null
      ? `${Number(decision.drift_bps)} bps`
      : "not available";
  $("decision-needs").textContent =
    decision.needs_rebalance === undefined || decision.needs_rebalance === null
      ? "not available"
      : decision.needs_rebalance ? "Yes" : "No";
  $("decision-nav-hash").textContent = decision.nav_snapshot_hash || "not available";
  $("decision-rationale-hash").textContent = decision.rationale_hash || "not available";

  const txEl = $("decision-tx");
  if (decision.tx_hash) {
    const a = document.createElement("a");
    a.href = `${CONFIG.EXPLORER}/tx/${decision.tx_hash}`;
    a.textContent = shortHash(decision.tx_hash);
    a.className = "mono";
    txEl.replaceChildren(a);
  } else {
    txEl.textContent = "not available";
  }

  // Stat strip: universe size from the decision file (same source as the
  // rationale text). Action chip mirrors needs_rebalance (HOLD / REBALANCE).
  const universe = decision.data && typeof decision.data.universe_size === "number"
    ? decision.data.universe_size
    : (typeof decision.universe_size === "number" ? decision.universe_size : null);
  $("stat-universe").textContent = universe === null ? "—" : String(universe);

  const chip = $("decision-action");
  if (decision.needs_rebalance === true) {
    chip.textContent = "REBALANCE";
    chip.className = "action-chip rebalance";
  } else if (decision.needs_rebalance === false) {
    chip.textContent = "HOLD";
    chip.className = "action-chip hold";
  } else {
    chip.classList.add("hidden");
  }

  const scores = decision.scores || {};
  const body = $("scores-body");
  body.replaceChildren();
  const symbols = Object.keys(scores);
  if (symbols.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "muted";
    cell.textContent = "No scores published.";
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  for (const sym of symbols) {
    const s = scores[sym] || {};
    const row = document.createElement("tr");
    const tokenCell = document.createElement("td");
    tokenCell.textContent = sym;
    row.appendChild(tokenCell);
    // Scores are 0–100, so the bar width maps directly to the value.
    for (const key of ["total", "momentum", "multiplier", "adoption", "freshness"]) {
      const v = s[key];
      const cell = document.createElement("td");
      cell.className = "num score-cell";
      const num = document.createElement("div");
      num.className = "score-num";
      num.textContent = (v === undefined || v === null) ? "—" : String(v);
      cell.appendChild(num);
      const n = Number(v);
      if (Number.isFinite(n)) {
        const bar = document.createElement("div");
        bar.className = "score-bar";
        const fill = document.createElement("span");
        fill.style.width = `${Math.max(0, Math.min(100, n))}%`;
        bar.appendChild(fill);
        cell.appendChild(bar);
      }
      row.appendChild(cell);
    }
    body.appendChild(row);
  }
}

function fmtNavPrice(nav) {
  if (!nav || nav.nav_price === undefined || nav.nav_price === null) return "not available";
  const n = Number(nav.nav_price);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : String(nav.nav_price);
}

// Donut chart of the on-chain target weights (SVG, no chart library).
// Segment angles come from the same bps values shown in the table.
function renderDonut(weights) {
  const svg = $("donut-svg");
  svg.replaceChildren();
  const NS = "http://www.w3.org/2000/svg";

  const track = document.createElementNS(NS, "circle");
  track.setAttribute("cx", "100");
  track.setAttribute("cy", "100");
  track.setAttribute("r", String(DONUT_R));
  track.setAttribute("fill", "none");
  track.setAttribute("stroke", "rgba(255,255,255,0.07)");
  track.setAttribute("stroke-width", "32");
  svg.appendChild(track);

  let acc = 0;
  weights.forEach(({ ticker, bps }, i) => {
    const n = Number(bps);
    if (!Number.isFinite(n) || n <= 0) return;
    const frac = n / 10000;
    const len = Math.max(0, frac * DONUT_C - 2); // small visual gap between segments
    const seg = document.createElementNS(NS, "circle");
    seg.setAttribute("cx", "100");
    seg.setAttribute("cy", "100");
    seg.setAttribute("r", String(DONUT_R));
    seg.setAttribute("fill", "none");
    seg.setAttribute("stroke", PALETTE[i % PALETTE.length]);
    seg.setAttribute("stroke-width", "32");
    seg.setAttribute("stroke-dasharray", `${len} ${DONUT_C - len}`);
    seg.setAttribute("stroke-dashoffset", String(-acc));
    const title = document.createElementNS(NS, "title");
    title.textContent = `${ticker} ${bpsToPct(bps)}`;
    seg.appendChild(title);
    svg.appendChild(seg);
    acc += frac * DONUT_C;
  });

  $("donut-holdings").textContent = String(weights.length);

  const legend = $("donut-legend");
  legend.replaceChildren();
  weights.forEach(({ ticker, bps }, i) => {
    const li = document.createElement("li");
    const swatch = document.createElement("span");
    swatch.className = "legend-swatch";
    swatch.style.background = PALETTE[i % PALETTE.length];
    const name = document.createElement("span");
    name.className = "legend-ticker";
    name.textContent = ticker;
    const pct = document.createElement("span");
    pct.className = "legend-pct";
    pct.textContent = bps === null ? "not available" : bpsToPct(bps);
    li.append(swatch, name, pct);
    legend.appendChild(li);
  });
  $("portfolio-viz").classList.remove("hidden");
}

function renderPortfolio(record, decision) {
  if (!record) {
    $("portfolio-empty").classList.remove("hidden");
    $("stat-holdings").textContent = "—";
    return;
  }
  const weights = parseWeights(record);
  $("stat-holdings").textContent = String(weights.length);
  renderDonut(weights);
  const navBySymbol = {};
  // The decision file nests the price snapshot at data.snapshot
  // (top-level `snapshot` kept as a fallback for older files).
  const snapshot = decision && (decision.snapshot || (decision.data && decision.data.snapshot));
  if (Array.isArray(snapshot)) {
    for (const entry of snapshot) {
      if (entry && entry.symbol) navBySymbol[entry.symbol] = entry;
    }
  }
  const maxBps = Math.max(1, ...weights.map((w) => Number(w.bps) || 0));
  const body = $("portfolio-body");
  body.replaceChildren();
  weights.forEach(({ ticker, bps }, i) => {
    const nav = navBySymbol[ticker];
    const row = document.createElement("tr");

    const tickerCell = document.createElement("td");
    const tickerWrap = document.createElement("span");
    tickerWrap.className = "ticker-cell";
    const dot = document.createElement("span");
    dot.className = "legend-swatch";
    dot.style.background = PALETTE[i % PALETTE.length];
    tickerWrap.append(dot, document.createTextNode(ticker));
    tickerCell.appendChild(tickerWrap);

    const weightCell = document.createElement("td");
    weightCell.className = "num";
    weightCell.appendChild(document.createTextNode(bps === null ? "not available" : bpsToPct(bps)));
    if (bps !== null && Number.isFinite(Number(bps))) {
      const bar = document.createElement("div");
      bar.className = "weight-bar";
      const fill = document.createElement("span");
      fill.style.width = `${(Number(bps) / maxBps) * 100}%`;
      fill.style.background = PALETTE[i % PALETTE.length];
      bar.appendChild(fill);
      weightCell.appendChild(bar);
    }

    const priceCell = document.createElement("td");
    priceCell.className = "num";
    priceCell.textContent = fmtNavPrice(nav);

    const sourceCell = document.createElement("td");
    sourceCell.className = "num";
    sourceCell.textContent = nav && nav.source ? nav.source : "not available";

    row.append(tickerCell, weightCell, priceCell, sourceCell);
    body.appendChild(row);
  });
  $("portfolio-table").classList.remove("hidden");
  $("portfolio-meta").textContent =
    `From on-chain record #${record.id} · ${fmtUtc(record.timestamp)} · recorded by ${record.agent}`;
}

function renderHistory(records) {
  const list = $("history-list");
  list.replaceChildren();
  if (records.length === 0) {
    $("history-empty").classList.remove("hidden");
    return;
  }
  for (const record of records) {
    const item = document.createElement("li");

    const head = document.createElement("div");
    head.className = "hist-head";
    const idEl = document.createElement("span");
    idEl.className = "hist-id";
    idEl.textContent = `Rebalance #${record.id}`;
    const timeEl = document.createElement("span");
    timeEl.className = "hist-time";
    timeEl.textContent = fmtUtc(record.timestamp);
    head.append(idEl, timeEl);

    const weightsEl = document.createElement("div");
    weightsEl.className = "hist-weights";
    weightsEl.textContent = parseWeights(record)
      .map(({ ticker, bps }) => `${ticker} ${bps === null ? "n/a" : bpsToPct(bps)}`)
      .join(" · ") || "not available";

    // Stacked allocation bar — segment widths are the same on-chain bps.
    const barEl = document.createElement("div");
    barEl.className = "stacked-bar";
    parseWeights(record).forEach(({ bps }, i) => {
      const n = Number(bps);
      if (!Number.isFinite(n) || n <= 0) return;
      const span = document.createElement("span");
      span.style.width = `${n / 100}%`;
      span.style.background = PALETTE[i % PALETTE.length];
      barEl.appendChild(span);
    });

    const hashesEl = document.createElement("div");
    hashesEl.className = "hash";
    hashesEl.textContent =
      `nav ${shortHash(record.navSnapshotHash)} · rationale ${shortHash(record.rationaleHash)} · agent ${shortHash(record.agent)}`;

    item.append(head, weightsEl, barEl, hashesEl);
    list.appendChild(item);
  }
}

async function main() {
  $("rpc-url").textContent = CONFIG.RPC_URL;
  $("footer-repo").href = CONFIG.REPO_URL;

  const deployed = CONFIG.CONTRACT_ADDRESS &&
    CONFIG.CONTRACT_ADDRESS.toLowerCase() !== ZERO_ADDRESS;
  if (!deployed) {
    showBanner("Contract not deployed yet — on-chain data will appear once the StockPilotLedger contract is deployed and its address is set in config.js.");
    $("contract-address").textContent = "not deployed yet";
    $("footer-contract").href = CONFIG.EXPLORER;
  } else {
    $("contract-address").textContent = CONFIG.CONTRACT_ADDRESS;
    $("footer-contract").href = `${CONFIG.EXPLORER}/address/${CONFIG.CONTRACT_ADDRESS}`;
  }

  // RPC status: current block number (works even before deploy).
  let provider = null;
  try {
    provider = new ethers.JsonRpcProvider(CONFIG.RPC_URL, CONFIG.CHAIN_ID);
    const blockNumber = await provider.getBlockNumber();
    $("rpc-status").textContent = "Connected to Base Sepolia.";
    $("block-number").textContent = String(blockNumber);
  } catch (err) {
    $("rpc-status").textContent =
      "Could not reach the Base Sepolia RPC — on-chain data is unavailable right now. Please try again later.";
    $("block-number").textContent = "not available";
    console.error("RPC error:", err);
  }

  // Agent decision file (may not exist yet).
  const decision = await loadDecision();
  renderDecision(decision);

  if (!deployed || !provider) {
    // No contract to read: show the empty states, not errors.
    renderPortfolio(null, decision);
    renderHistory([]);
    if (!deployed) $("rebalance-count").textContent = "not available";
    return;
  }

  const ledger = new ethers.Contract(CONFIG.CONTRACT_ADDRESS, LEDGER_ABI, provider);

  let count = 0;
  try {
    count = Number(await ledger.rebalanceCount());
    $("rebalance-count").textContent = String(count);
  } catch (err) {
    $("rebalance-count").textContent = "not available";
    console.error("rebalanceCount failed:", err);
    renderPortfolio(null, decision);
    renderHistory([]);
    return;
  }

  if (count === 0) {
    renderPortfolio(null, decision);
    renderHistory([]);
    return;
  }

  // Latest record drives the portfolio table. getLatest() reverts when the
  // ledger is empty; count > 0 here, but guard anyway.
  try {
    const latest = await ledger.getLatest();
    renderPortfolio(latest, decision);
  } catch (err) {
    console.error("getLatest failed:", err);
    renderPortfolio(null, decision);
  }

  // Full history, newest first. Reads are sequential to stay gentle on the
  // public RPC; capped at MAX_HISTORY most recent records.
  const start = Math.max(0, count - MAX_HISTORY);
  const records = [];
  for (let i = count - 1; i >= start; i--) {
    try {
      records.push(await ledger.getRebalance(i));
    } catch (err) {
      console.error(`getRebalance(${i}) failed:`, err);
    }
  }
  renderHistory(records);
}

main().catch((err) => {
  console.error("Dashboard failed to initialize:", err);
  const status = $("rpc-status");
  if (status) status.textContent = "Dashboard failed to load — please refresh the page.";
});
