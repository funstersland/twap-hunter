// DOM panel + timeline updates (event-driven, not per-frame).

import { bus, setAsset } from "./bus.js";
import {
  fmtPrice, fmtSigned, fmtQty, fmtUsd, fmtBps, fmtCents,
  fmtClock, fmtCountdown, deltaClass, windowName,
} from "./format.js";

const $ = (id) => document.getElementById(id);

function setDelta(el, text, v) {
  el.textContent = text;
  el.classList.remove("up", "down", "flat");
  el.classList.add(deltaClass(v));
}

// ---------- asset tabs ----------

export function renderTabs() {
  const nav = $("asset-tabs");
  nav.innerHTML = "";
  const active = bus.pendingAsset || bus.asset;
  for (const key of bus.assets) {
    // Real links: ctrl/cmd/middle-click opens the market in a NEW
    // browser tab; a plain click switches this tab in place.
    const a = document.createElement("a");
    a.textContent = key;
    a.href = `/?asset=${key}`;
    a.target = "_blank";
    a.rel = "noopener";
    a.title = `${key} — click to switch, Ctrl/middle-click for new tab`;
    if (key === active) a.classList.add("active");
    if (key === bus.pendingAsset) a.classList.add("pending");
    a.onclick = (e) => {
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) return; // let the browser open a tab
      e.preventDefault();
      if (key !== bus.asset && key !== bus.pendingAsset) {
        setAsset(key);
        renderTabs();  // instant highlight — don't wait for the server
      }
    };
    nav.appendChild(a);
  }
}

// ---------- snapshot-driven panels ----------

export function renderPanels() {
  const snap = bus.snapshot;
  if (!snap) return;
  const d = snap.decimals;

  // prices
  $("p-chainlink").textContent = fmtPrice(snap.prices.chainlink, d);
  $("p-last").textContent = fmtPrice(snap.prices.last_trade, d);
  $("p-twap").textContent = fmtPrice(snap.prices.twap, d);
  $("p-twap30").textContent = fmtPrice(snap.prices.twap30, d);
  $("p-ptb").textContent = fmtPrice(snap.window.ptb, d);
  $("p-ptb-src").textContent = snap.window.ptb_source || "—";
  $("p-lookback").textContent = snap.prices.lookback_s ? `(${snap.prices.lookback_s}s)` : "";
  $("twap-lookback").textContent = snap.prices.lookback_s ? `· ${snap.prices.lookback_s}s lookback` : "";
  $("tick-symbol").textContent = `· ${snap.asset}/USDT`;

  // deltas
  setDelta($("d-twap"), fmtSigned(snap.deltas.twap, d + 1), snap.deltas.twap);
  setDelta($("d-twap-bps"), fmtBps(snap.deltas.twap_bps), snap.deltas.twap_bps);
  setDelta($("d-price"), fmtSigned(snap.deltas.price, d + 1), snap.deltas.price);
  setDelta($("d-price-bps"), fmtBps(snap.deltas.price_bps), snap.deltas.price_bps);

  // volume
  const v = snap.volume;
  $("v-buy").textContent = `${fmtQty(v.buy_qty)} · ${fmtUsd(v.buy_usd)}`;
  $("v-sell").textContent = `${fmtQty(v.sell_qty)} · ${fmtUsd(v.sell_usd)}`;
  setDelta($("v-net"), `${fmtQty(v.net_qty)} · ${fmtUsd(v.net_usd)}`, v.net_qty);
  $("v-trades").textContent = v.trades != null ? String(v.trades) : "—";
  const imb = v.imbalance;
  setDelta($("v-imb"), imb == null ? "—" : (imb > 0 ? "+" : "") + (imb * 100).toFixed(1) + "%", imb);
  fillBar($("v-imb-fill"), imb);
  renderImbWindows(v.windows);

  // pressure
  const score = snap.pressure ? snap.pressure.score : null;
  const prEl = $("pr-score");
  setDelta(prEl, score == null ? "—" : (score > 0 ? "+" : "") + score.toFixed(0), score);
  fillBar($("pr-fill"), score == null ? null : score / 100);
  renderPressureComponents(snap.pressure);

  // momentum
  renderMomentum(snap.momentum, d);
  const vel = snap.velocity;
  $("m-velocity").textContent = vel.tps_1s != null
    ? `${vel.tps_1s} t/s ${vel.ratio != null ? `(x${vel.ratio})` : ""}`
    : "—";
  const ur = snap.direction.up_ratio;
  setDelta($("m-direction"), ur == null ? "—" : (ur * 100).toFixed(0) + "% up", ur == null ? null : ur - 0.5);

  // prediction
  renderPrediction(snap, d);

  // market
  $("mk-question").textContent = snap.market.question || "waiting for market discovery…";
  renderQuote(snap.market.quote);

  // health dots
  setHealth("health-binance", snap.health.binance);
  setHealth("health-rtds", snap.health.rtds);
  setHealth("health-clob", snap.health.clob);
  setHealth("health-ws", bus.wsConnected ? "live" : "down");
}

function fillBar(el, frac) {
  // frac in -1..+1; bar grows from center.
  if (frac == null || !isFinite(frac)) { el.style.width = "0"; return; }
  const clamped = Math.max(-1, Math.min(1, frac));
  const pct = Math.abs(clamped) * 50;
  el.style.width = pct + "%";
  el.style.background = clamped >= 0 ? "var(--up)" : "var(--down)";
  if (clamped >= 0) { el.style.left = "50%"; el.style.right = "auto"; }
  else { el.style.left = (50 - pct) + "%"; el.style.right = "auto"; }
}

function renderImbWindows(windows) {
  const host = $("v-imb-windows");
  if (!windows) { host.innerHTML = ""; return; }
  let html = "";
  for (const [ms, val] of Object.entries(windows)) {
    const cls = deltaClass(val);
    const text = val == null ? "—" : (val > 0 ? "+" : "") + (val * 100).toFixed(0) + "%";
    html += `<div class="mini-imb">${windowName(Number(ms))}<b class="${cls}">${text}</b></div>`;
  }
  host.innerHTML = html;
}

const PR_NAMES = {
  volume_imbalance: "vol imbalance",
  tick_direction: "tick direction",
  momentum: "momentum",
  tick_velocity: "tick velocity",
};

function renderPressureComponents(pressure) {
  const host = $("pr-components");
  if (!pressure || !pressure.components) { host.innerHTML = ""; return; }
  let html = "";
  for (const [key, val] of Object.entries(pressure.components)) {
    const cls = deltaClass(val);
    html += `<div class="pr-comp"><span>${PR_NAMES[key] || key}</span><b class="${cls}">${val == null ? "—" : val.toFixed(2)}</b></div>`;
  }
  host.innerHTML = html;
}

function renderMomentum(momentum, decimals) {
  const body = $("mom-body");
  if (!momentum) { body.innerHTML = ""; return; }
  let html = "";
  for (const [ms, m] of Object.entries(momentum)) {
    const cls = deltaClass(m.bps);
    html += `<tr><td>${windowName(Number(ms))}</td>` +
      `<td class="${cls}">${fmtSigned(m.abs, decimals + 1)}</td>` +
      `<td class="${cls}">${fmtBps(m.bps)}</td></tr>`;
  }
  body.innerHTML = html;
}

function renderPrediction(snap, d) {
  const pred = snap.prediction;
  if (!pred) return;
  $("pred-horizon").textContent = pred.horizon_s ? `(+${pred.horizon_s}s ahead, every 5s)` : "";

  const ptb = snap.window ? snap.window.ptb : null;
  const h30 = pred.next_h30;
  if (h30 && h30.v != null) {
    const delta = ptb != null ? h30.v - ptb : null;
    setDelta($("pred-h30"), fmtPrice(h30.v, d), delta);
  } else {
    $("pred-h30").textContent = "—";
  }
  const fin = pred.final;
  let ourDir = null;
  if (fin && fin.v != null) {
    const delta = fin.ptb != null ? fin.v - fin.ptb : null;
    setDelta($("pred-final"), fmtPrice(fin.v, d), delta);
    if (delta != null) ourDir = delta > 0 ? "UP" : "DOWN";
  } else {
    $("pred-final").textContent = "—";
  }
  setDirChip($("pred-our-dir"), ourDir);

  const q = snap.market.quote;
  let mktDir = null;
  let mktMid = null;
  if (q && q.up_bid != null && q.up_ask != null) {
    mktMid = (q.up_bid + q.up_ask) / 2;
    mktDir = mktMid > 50 ? "UP" : mktMid < 50 ? "DOWN" : null;
  }
  setDirChip($("pred-mkt-dir"), mktDir, mktMid);

  const h = pred.h30;
  $("pred-acc").textContent = h && h.n
    ? `±${h.mae_bps} bps avg · ${h.hit_pct}% ≤1bp (n=${h.n})`
    : "warming up…";
  const e = pred.end;
  $("pred-end-acc").textContent = e && e.n ? `${e.dir_hit_pct}% (n=${e.n})` : "—";
  $("pred-mkt-acc").textContent = e && e.mkt_n ? `${e.mkt_hit_pct}% (n=${e.mkt_n})` : "—";
}

function setDirChip(el, dir, midCents) {
  const b = el.querySelector("b");
  b.classList.remove("up", "down", "flat");
  if (dir === "UP") {
    b.textContent = midCents != null ? `UP ${midCents.toFixed(0)}¢` : "UP";
    b.classList.add("up");
  } else if (dir === "DOWN") {
    b.textContent = midCents != null ? `DOWN ${(100 - midCents).toFixed(0)}¢` : "DOWN";
    b.classList.add("down");
  } else {
    b.textContent = "—";
    b.classList.add("flat");
  }
}

function renderQuote(q) {
  if (!q) return;
  const upMid = q.up_bid != null && q.up_ask != null ? (q.up_bid + q.up_ask) / 2 : null;
  const downMid = q.down_bid != null && q.down_ask != null ? (q.down_bid + q.down_ask) / 2 : null;
  $("q-up").textContent = upMid != null ? fmtCents(upMid) : "—";
  $("q-down").textContent = downMid != null ? fmtCents(downMid) : "—";
  $("q-up-bid").textContent = q.up_bid != null ? fmtCents(q.up_bid) : "—";
  $("q-up-ask").textContent = q.up_ask != null ? fmtCents(q.up_ask) : "—";
  $("q-down-bid").textContent = q.down_bid != null ? fmtCents(q.down_bid) : "—";
  $("q-down-ask").textContent = q.down_ask != null ? fmtCents(q.down_ask) : "—";
  $("q-up-size").textContent =
    `${q.up_bid_size != null ? fmtQty(q.up_bid_size) : "—"} × ${q.up_ask_size != null ? fmtQty(q.up_ask_size) : "—"}`;
  $("q-down-size").textContent =
    `${q.down_bid_size != null ? fmtQty(q.down_bid_size) : "—"} × ${q.down_ask_size != null ? fmtQty(q.down_ask_size) : "—"}`;
  $("q-last").textContent = q.last_trade_up != null ? fmtCents(q.last_trade_up) : "—";
  $("q-spread").textContent = q.up_bid != null && q.up_ask != null
    ? fmtCents(q.up_ask - q.up_bid) : "—";
}

function setHealth(id, status) {
  const dot = $(id).querySelector(".dot");
  dot.classList.remove("live", "stale", "down");
  dot.classList.add(status || "down");
}

// ---------- countdown (10 Hz timer, clock-offset corrected) ----------

export function renderCountdown() {
  const snap = bus.snapshot;
  if (!snap || !snap.window) return;
  const now = bus.smoothNowMs();
  const remaining = snap.window.end_ms - now;
  const el = $("countdown");
  el.textContent = fmtCountdown(remaining);
  el.classList.toggle("hot", remaining < 30_000);
  const total = snap.window.end_ms - snap.window.start_ms;
  const frac = Math.max(0, Math.min(1, (now - snap.window.start_ms) / total));
  $("round-progress-fill").style.width = (frac * 100) + "%";
  const s = new Date(snap.window.start_ms);
  const e = new Date(snap.window.end_ms);
  const fmt = (x) => x.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit" });
  $("round-label").textContent = `${snap.asset} 5m round ${fmt(s)}–${fmt(e)}`;
}

// ---------- event timeline ----------

export function appendEvents(events) {
  const list = $("tl-list");
  for (const evt of events) {
    const item = document.createElement("div");
    item.className = "tl-item " + (evt.cls || "");
    const time = document.createElement("span");
    time.className = "tl-time";
    time.textContent = fmtClock(evt.t);
    const kind = document.createElement("span");
    kind.className = "tl-kind k-" + evt.kind;
    kind.textContent = evt.kind.replace("_", " ");
    const label = document.createElement("span");
    label.className = "tl-label";
    label.textContent = evt.label;
    item.append(time, kind, label);
    list.prepend(item);
  }
  while (list.childElementCount > 150) list.removeChild(list.lastChild);
}

export function resetTimeline() {
  $("tl-list").innerHTML = "";
}
