// Bot detail page: live market charts + bot brain + equity curve + trades.

import { bus, connect } from "./bus.js";
import { makeChart, renderTwapChart, renderQuoteChart, setQuoteMarkers } from "./chart.js";
import { fmtCountdown } from "./format.js";
import { bindLot, setLotUI } from "./lot.js";

const $ = (id) => document.getElementById(id);
const botId = location.pathname.split("/").filter(Boolean).pop();

let bot = null;
let equitySeries = [];   // [t, balance]

function fmtUsd(v) {
  if (v == null) return "—";
  return (v < 0 ? "−" : "") + "$" + Math.abs(v).toFixed(2);
}
function pnlClass(v) { return v > 0 ? "up" : v < 0 ? "down" : "flat"; }
function fmtTime(ms) { return new Date(ms).toLocaleTimeString("en-US", { hour12: false }); }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------------- bot data ----------------

async function refreshBot() {
  let d;
  try {
    const resp = await fetch(`/api/bots/${botId}`);
    if (!resp.ok) { $("bd-name").textContent = "bot not found"; return; }
    d = await resp.json();
  } catch (_e) { return; }
  // Auto-reload when the server redeploys (new code = new boot stamp).
  if (d.boot) {
    if (!window.__boot) window.__boot = d.boot;
    else if (window.__boot !== d.boot) { location.reload(); return; }
  }
  const firstLoad = bot === null;
  bot = d;

  if (firstLoad) {
    document.title = `${d.name} · Twap Hunter`;
    if (d.asset === "ALL") {
      // Copy bots follow their leader across every market — market
      // charts are meaningless here; show only the bot's own activity.
      document.querySelector(".bd-grid").classList.add("no-market");
      $("bd-twap-card").style.display = "none";
      $("bd-quote-card").style.display = "none";
    } else {
      bus.asset = d.asset;   // subscribe this page's WS to the bot's market
      connect();
    }
    refreshEquity();
  }

  $("bd-name").textContent = d.name;
  $("bd-asset").textContent = d.asset;
  $("bd-strategy").textContent = d.strategy_name;
  const running = d.status === "running";
  $("bd-status").className = "bc-status " + (running ? "run" : "stop");
  $("bd-toggle").textContent = running ? "STOP" : "START";
  setLotUI($("bd-lot"), d);
  $("bd-equity").textContent = fmtUsd(d.equity);
  const pnlEl = $("bd-pnl");
  pnlEl.textContent = `${d.pnl > 0 ? "+" : ""}${fmtUsd(d.pnl)} (${d.pnl_pct}%)`;
  pnlEl.className = pnlClass(d.pnl);

  // brain
  if (d.brain) {
    $("bd-brain-summary").textContent = d.brain.summary || "—";
    let html = "";
    for (const c of d.brain.conditions || []) {
      html += `<div class="brain-cond ${c.ok ? "ok" : "no"}">` +
        `<i>${c.ok ? "✓" : "✗"}</i><span>${escapeHtml(c.label)}</span>` +
        `<b>${escapeHtml(String(c.value ?? "—"))}</b></div>`;
    }
    $("bd-brain-conditions").innerHTML = html;
  }

  // position
  if (d.position) {
    const p = d.position;
    const cls = p.side === "UP" ? "up" : "down";
    const wait = p.waiting_official ? " · <span class='muted'>waiting for official resolution</span>" : "";
    const assetTag = d.asset === "ALL" && p.asset ? ` on <b>${p.asset}</b>` : "";
    $("bd-position").innerHTML =
      `<b class="${cls}">${p.side}</b> ${p.shares} sh @ ${p.entry_cents.toFixed(0)}¢${assetTag}` +
      (p.mark_cents != null ? ` → ${p.mark_cents.toFixed(0)}¢ now` : "") +
      `<div class="muted">round ${escapeHtml(p.round_label || "")}${wait}</div>`;
  } else if (d.book) {
    const bk = d.book;
    $("bd-position").innerHTML =
      `<b class="up">UP ${bk.up_shares}</b> / <b class="down">DOWN ${bk.down_shares}</b>` +
      ` — ${bk.pairs} pairs, cost $${bk.cost}` +
      `<div class="muted">pairs pay $1 each at settlement ` +
      `(${bk.pair_edge >= 0 ? "+" : ""}$${bk.pair_edge} locked) · round ${escapeHtml(bk.round_label || "")}</div>`;
  } else {
    $("bd-position").innerHTML = "<span class='muted'>flat</span>";
  }

  // copied portfolio (copy bots on arbitrary markets)
  const pfWrap = $("bd-portfolio-wrap");
  if (d.portfolio && d.portfolio.length) {
    pfWrap.style.display = "block";
    let ph = "";
    for (const p of d.portfolio) {
      ph += `<div class="pf-row">
        <span class="pf-title" title="${escapeHtml(p.title)}">${escapeHtml(p.title.slice(0, 44))}</span>
        <span class="pf-outcome">${escapeHtml(p.outcome)}</span>
        <span>${p.entry_cents.toFixed(0)}¢→${p.mark_cents.toFixed(0)}¢</span>
        <b class="${pnlClass(p.upnl)}">${p.upnl >= 0 ? "+" : "−"}$${Math.abs(p.upnl).toFixed(2)}</b>
      </div>`;
    }
    $("bd-portfolio").innerHTML = ph;
  } else {
    pfWrap.style.display = "none";
  }

  // stats
  const s = d.stats;
  const wr = s.wins + s.losses > 0 ? ` (${Math.round(100 * s.wins / (s.wins + s.losses))}%)` : "";
  $("bd-record").textContent = `${s.wins}W / ${s.losses}L${wr}`;
  $("bd-rounds").textContent = s.rounds;
  $("bd-fees").textContent = fmtUsd(s.fees);
  $("bd-balance").textContent = fmtUsd(d.balance);

  // params
  let ph = "";
  for (const [k, v] of Object.entries(d.params || {})) {
    ph += `<div class="kv"><span>${escapeHtml(k)}</span><b>${v}</b></div>`;
  }
  $("bd-params").innerHTML = ph;

  // trades table
  let th = "";
  for (const t of d.trades || []) {
    const cls = t.action === "BUY" ? "flat" : pnlClass(t.pnl);
    const side = t.side ? `<b class="${t.side === "UP" ? "up" : "down"}">${t.side}</b>` : "";
    // Settlements are redemptions ($1/share for winners), not fills.
    let price = t.price_cents != null ? t.price_cents.toFixed(0) + "¢" : "";
    if (t.action === "SETTLE" || t.action === "ADJUST") {
      price = t.price_cents >= 100
        ? "<b class='up'>WIN $1/sh</b>" : "<b class='down'>LOSS $0</b>";
    }
    th += `<tr><td>${fmtTime(t.t)}</td>` +
      `<td class="ta-${(t.action || "").toLowerCase()}">${t.action}</td><td>${side}</td>` +
      `<td>${price}</td>` +
      `<td class="${cls}">${t.pnl != null && t.action !== "BUY" ? fmtUsd(t.pnl) : ""}</td>` +
      `<td class="muted">${escapeHtml(t.round_label || "")}</td>` +
      `<td class="muted">${escapeHtml(t.reason || "")}</td></tr>`;
  }
  $("bd-trades").innerHTML = th || "<tr><td class='muted'>no trades yet</td></tr>";

  // markers for the quote chart (current + recent trades)
  setQuoteMarkers((d.trades || [])
    .filter((t) => ["BUY", "SELL", "SETTLE"].includes(t.action) && t.price_cents != null)
    .map((t) => ({ t: t.t, price_cents: t.price_cents, action: t.action, side: t.side })));
}

async function refreshEquity() {
  try {
    const d = await fetch(`/api/bots/${botId}/trades?limit=2000`).then((r) => r.json());
    const rows = (d.trades || []).filter((t) => t.balance != null).reverse(); // oldest first
    equitySeries = rows.map((t) => [t.t, t.balance]);
    $("bd-equity-info").textContent = `${equitySeries.length} points · cash balance after each action`;
  } catch (_e) { /* retry next cycle */ }
}

// ---------------- equity chart ----------------

const equityChart = makeChart($("chart-equity"), (ctx, w, h) => {
  if (equitySeries.length < 2) {
    ctx.fillStyle = "#6b7488";
    ctx.font = "11px monospace";
    ctx.textAlign = "center";
    ctx.fillText("waiting for trades…", w / 2, h / 2);
    return;
  }
  const start = bot ? bot.start_balance : equitySeries[0][1];
  let lo = Math.min(start, ...equitySeries.map((p) => p[1]));
  let hi = Math.max(start, ...equitySeries.map((p) => p[1]));
  const pad = Math.max((hi - lo) * 0.15, 1);
  lo -= pad; hi += pad;
  const t0 = equitySeries[0][0];
  const t1 = equitySeries[equitySeries.length - 1][0] || t0 + 1;
  const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * (w - 60) + 4;
  const y = (v) => (h - 16) - ((v - lo) / (hi - lo)) * (h - 24);

  // start-balance reference line
  ctx.strokeStyle = "#3a4360";
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(0, y(start)); ctx.lineTo(w, y(start)); ctx.stroke();
  ctx.setLineDash([]);

  const last = equitySeries[equitySeries.length - 1][1];
  ctx.strokeStyle = last >= start ? "#22c77a" : "#f0475c";
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  equitySeries.forEach(([t, v], i) => {
    if (i === 0) ctx.moveTo(x(t), y(v)); else ctx.lineTo(x(t), y(v));
  });
  ctx.stroke();

  ctx.fillStyle = "#d6dbe8";
  ctx.font = "10px monospace";
  ctx.textAlign = "right";
  ctx.fillText("$" + last.toFixed(2), w - 4, y(last) - 4);
});

// ---------------- market charts (reused renderers) ----------------

const twapChart = makeChart($("chart-twap"), renderTwapChart, {
  pan: true, defaultSpan: 150_000, minSpan: 30_000, maxSpan: 600_000, maxPanMs: 11 * 60_000,
});
const quoteChart = makeChart($("chart-quotes"), renderQuoteChart);

function frame() {
  twapChart.render();
  quoteChart.render();
  equityChart.render();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

setInterval(() => {
  if (bot && bot.asset === "ALL") return;  // no market view on copy bots
  const snap = bus.snapshot;
  if (snap && snap.window) {
    const remaining = snap.window.end_ms - bus.smoothNowMs();
    $("bd-round").textContent = `· round ends in ${fmtCountdown(remaining)}`;
  }
}, 200);

$("bd-toggle").onclick = async () => {
  if (!bot) return;
  const action = bot.status === "running" ? "stop" : "start";
  await fetch(`/api/bots/${botId}/${action}`, { method: "POST" });
  refreshBot();
};

bindLot(document.getElementById("bd-lot"), () => bot, `/api/bots/${botId}/update`, () => refreshBot());

refreshBot();
setInterval(refreshBot, 1000);
setInterval(refreshEquity, 10_000);
