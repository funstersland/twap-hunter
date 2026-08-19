// Real bot detail page — same layout as paper bot, live account data + charts.

import { bus, connect } from "./bus.js";
import { makeChart, renderTwapChart, renderQuoteChart, setQuoteMarkers } from "./chart.js";
import { fmtCountdown } from "./format.js";
import { bindLot, setLotUI } from "./lot.js";

const $ = (id) => document.getElementById(id);
const botId = location.pathname.split("/").filter(Boolean).pop();

let bot = null;
let equitySeries = [];

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

async function refreshBot() {
  let d;
  try {
    const resp = await fetch(`/api/real/bots/${botId}`);
    if (!resp.ok) { $("bd-name").textContent = "bot not found"; return; }
    d = await resp.json();
  } catch (_e) { return; }

  if (d.boot) {
    if (!window.__boot) window.__boot = d.boot;
    else if (window.__boot !== d.boot) { location.reload(); return; }
  }

  const firstLoad = bot === null;
  bot = d;

  if (firstLoad) {
    document.title = `${d.name} · Real · Twap Hunter`;
    if (d.asset === "ALL") {
      document.querySelector(".bd-grid").classList.add("no-market");
      $("bd-twap-card").style.display = "none";
      $("bd-quote-card").style.display = "none";
    } else {
      bus.asset = d.asset;
      connect();
    }
    refreshEquity(d.trades || []);
  }

  $("bd-name").textContent = d.name;
  $("bd-asset").textContent = d.asset;
  $("bd-strategy").textContent = d.strategy_name;
  const running = d.status === "running";
  $("bd-status").className = "bc-status " + (running ? "run" : "stop");
  $("bd-toggle").textContent = running ? "STOP" : "START";
  setLotUI($("bd-lot"), d);
  $("bd-cash-header").textContent = fmtUsd(d.balance ?? d.cash_balance);
  $("bd-equity").textContent = fmtUsd(d.equity);
  const pnlEl = $("bd-pnl");
  const openPnl = d.pnl ?? d.account_cash_pnl ?? 0;
  pnlEl.textContent = `${openPnl > 0 ? "+" : ""}${fmtUsd(openPnl)}`;
  pnlEl.className = pnlClass(openPnl);

  $("bd-acc-label").textContent = d.account_label || "—";
  $("bd-acc-addr").textContent = d.account_address || "—";
  $("bd-profile-name").textContent = d.profile_name || "—";
  if ($("bd-cash")) $("bd-cash").textContent = fmtUsd(d.balance ?? d.cash_balance);
  if ($("bd-pos-value")) $("bd-pos-value").textContent = fmtUsd(d.positions_value);
  if ($("bd-orders")) $("bd-orders").textContent = String(d.orders_placed ?? 0);

  const errEl = $("bd-exec-error");
  if (errEl) {
    if (d.last_exec_error) {
      errEl.style.display = "block";
      errEl.textContent = d.last_exec_error;
    } else if (d.status === "running" && (d.balance ?? 0) <= 0) {
      errEl.style.display = "block";
      errEl.textContent = "Running but CLOB balance is $0 — deposit USDC on polymarket.com to place trades.";
    } else {
      errEl.style.display = "none";
      errEl.textContent = "";
    }
  }

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

  if (d.position) {
    const p = d.position;
    const cls = p.side === "UP" ? "up" : "down";
    const assetTag = d.asset === "ALL" && p.asset ? ` on <b>${p.asset}</b>` : "";
    $("bd-position").innerHTML =
      `<b class="${cls}">${p.side}</b> ${p.shares} sh @ ${p.entry_cents.toFixed(0)}¢${assetTag}` +
      (p.mark_cents != null ? ` → ${p.mark_cents.toFixed(0)}¢ now` : "") +
      (p.title ? `<div class="muted">${escapeHtml(p.title)}</div>` : "") +
      `<div class="muted">round ${escapeHtml(p.round_label || "")}</div>`;
  } else {
    $("bd-position").innerHTML = "<span class='muted'>flat</span>";
  }

  const pfWrap = $("bd-portfolio-wrap");
  const positions = d.portfolio && d.portfolio.length ? d.portfolio : (d.positions || []).map((p) => ({
    title: p.title,
    outcome: p.outcome,
    entry_cents: p.avg_price_cents,
    mark_cents: p.cur_price_cents,
    upnl: p.cash_pnl,
  }));
  if (positions.length > 1 || (d.asset === "ALL" && positions.length)) {
    pfWrap.style.display = "block";
    $("bd-portfolio").innerHTML = positions.map((p) =>
      `<div class="pf-row">
        <span class="pf-title" title="${escapeHtml(p.title || "")}">${escapeHtml((p.title || "—").slice(0, 44))}</span>
        <span class="pf-outcome">${escapeHtml(p.outcome || "")}</span>
        <span>${(p.entry_cents || 0).toFixed(0)}¢→${(p.mark_cents || 0).toFixed(0)}¢</span>
        <b class="${pnlClass(p.upnl)}">${p.upnl >= 0 ? "+" : "−"}$${Math.abs(p.upnl || 0).toFixed(2)}</b>
      </div>`
    ).join("");
  } else {
    pfWrap.style.display = "none";
  }

  const s = d.stats || { wins: 0, losses: 0, rounds: 0, fees: 0 };
  const wr = s.wins + s.losses > 0 ? ` (${Math.round(100 * s.wins / (s.wins + s.losses))}%)` : "";
  $("bd-record").textContent = `${s.wins}W / ${s.losses}L${wr}`;
  $("bd-rounds").textContent = s.rounds;
  $("bd-fees").textContent = fmtUsd(s.fees);
  $("bd-balance").textContent = fmtUsd(d.balance ?? d.cash_balance);

  $("bd-params").innerHTML = Object.entries(d.params || {})
    .map(([k, v]) => `<div class="kv"><span>${escapeHtml(k)}</span><b>${v}</b></div>`)
    .join("") || "<span class='muted'>default</span>";

  let ah = "";
  for (const e of d.activity_log || []) {
    const cls = e.level === "error" ? "down" : e.level === "order" ? "up" : e.level === "skip" ? "flat" : "";
    ah += `<tr class="log-${e.level}"><td>${fmtTime(e.t)}</td>` +
      `<td class="${cls}">${escapeHtml(e.level)}</td>` +
      `<td class="muted log-msg">${escapeHtml(e.msg)}</td></tr>`;
  }
  $("bd-activity").innerHTML = ah || `<tr><td colspan="3" class="muted">no activity yet</td></tr>`;

  let th = "";
  for (const t of d.trades || []) {
    const action = t.action || t.side;
    const cls = action === "BUY" ? "flat" : pnlClass(t.pnl ?? t.cash_pnl);
    const outcome = t.outcome || (t.side === "UP" || t.side === "DOWN" ? t.side : "");
    const side = outcome ? `<b class="${outcome === "UP" ? "up" : "down"}">${outcome}</b>` : "";
    const sh = t.shares ?? t.size;
    const usd = t.usd != null ? fmtUsd(t.usd) : (sh != null && t.price_cents != null
      ? fmtUsd((t.price_cents / 100) * sh) : "");
    const px = t.price_cents != null ? Number(t.price_cents).toFixed(1) + "¢" : "";
    const pnl = t.cash_pnl != null ? fmtUsd(t.cash_pnl) : (t.pnl != null && action !== "BUY" ? fmtUsd(t.pnl) : "");
    const tx = t.tx ? String(t.tx).slice(0, 10) + "…" : "";
    th += `<tr><td>${fmtTime(t.t)}</td>` +
      `<td class="ta-${(action || "").toLowerCase()}">${action}</td><td>${side}</td>` +
      `<td>${px}</td>` +
      `<td>${sh != null ? Number(sh).toFixed(4) + " sh" : ""}</td>` +
      `<td>${usd}</td>` +
      `<td class="${cls}">${pnl}</td>` +
      `<td class="muted">${escapeHtml(t.round_label || t.event_slug || "")}</td>` +
      `<td class="muted">${escapeHtml(tx || t.title || t.reason || "")}</td></tr>`;
  }
  $("bd-trades").innerHTML = th || "<tr><td class='muted'>no Polymarket trades yet for this market</td></tr>";

  setQuoteMarkers((d.trades || [])
    .filter((t) => t.price_cents != null)
    .map((t) => ({
      t: t.t,
      price_cents: t.price_cents,
      action: t.action,
      side: t.side,
    })));

  refreshEquity(d.trades || []);
}

function refreshEquity(trades) {
  const sorted = [...trades].sort((a, b) => a.t - b.t);
  if (!sorted.length) {
    equitySeries = bot && bot.equity != null ? [[Date.now(), bot.equity]] : [];
    $("bd-equity-info").textContent = "waiting for Polymarket trades…";
    return;
  }
  let cash = 0;
  const points = sorted.map((t) => {
    const sh = t.shares ?? t.size ?? 0;
    const usd = t.usd != null ? Number(t.usd) : ((t.price_cents || 0) / 100) * sh;
    const action = t.action || t.side;
    if (action === "BUY") cash -= usd;
    else if (action === "SELL") cash += usd;
    return [t.t, cash];
  });
  const last = points[points.length - 1][1];
  const anchor = bot && bot.equity != null ? bot.equity : last;
  const shift = anchor - last;
  equitySeries = points.map(([t, v]) => [t, v + shift]);
  equitySeries.push([Date.now(), anchor]);
  $("bd-equity-info").textContent = `${equitySeries.length} points · Polymarket fills`;
}

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
  if (bot && bot.asset === "ALL") return;
  const snap = bus.snapshot;
  if (snap && snap.window) {
    const remaining = snap.window.end_ms - bus.smoothNowMs();
    $("bd-round").textContent = `· round ends in ${fmtCountdown(remaining)}`;
  }
}, 200);

$("bd-toggle").onclick = async () => {
  if (!bot) return;
  const action = bot.status === "running" ? "stop" : "start";
  await fetch(`/api/real/bots/${botId}/${action}`, { method: "POST" });
  refreshBot();
};

bindLot(document.getElementById("bd-lot"), () => bot, `/api/real/bots/${botId}/update`, () => refreshBot());

refreshBot();
setInterval(refreshBot, 1000);
