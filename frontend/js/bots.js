// /bots page: manage paper-trading bots (poll-based, no framework).

import { lotHtml, bindLot } from "./lot.js";

const $ = (id) => document.getElementById(id);

let CATALOG = { strategies: [], assets: [] };
let selectedStrategy = null;
let selectedAsset = null;
const expanded = new Set();
let lastBots = [];

// Market filter — restored from the URL so filtered views are bookmarkable.
let filterAsset = (new URLSearchParams(location.search).get("market") || "ALL").toUpperCase();

function fmtUsd(v) {
  if (v == null) return "—";
  const sign = v < 0 ? "−" : "";
  return sign + "$" + Math.abs(v).toFixed(2);
}
function pnlClass(v) { return v > 0 ? "up" : v < 0 ? "down" : "flat"; }
function fmtTime(ms) {
  return new Date(ms).toLocaleTimeString("en-US", { hour12: false });
}

// ---------------- add-bot modal ----------------

async function loadCatalog() {
  CATALOG = await fetch("/api/strategies").then((r) => r.json());
  selectedStrategy = CATALOG.strategies[0]?.id || null;
  selectedAsset = CATALOG.assets[0] || null;
  renderAssetPick();
  renderStrategyPick();
  renderParamForm();
}

function renderAssetPick() {
  const host = $("asset-pick");
  host.innerHTML = "";
  if (selectedStrategy === "copy_trader") {
    const b = document.createElement("button");
    b.textContent = "ALL MARKETS";
    b.className = "active";
    b.disabled = true;
    b.title = "Copy bots follow their leader on every market automatically";
    host.appendChild(b);
    const note = document.createElement("span");
    note.className = "muted";
    note.style.cssText = "font-size:10px;align-self:center;margin-left:6px";
    note.textContent = "copies the leader wherever they trade";
    host.appendChild(note);
    return;
  }
  for (const key of CATALOG.assets) {
    const b = document.createElement("button");
    b.textContent = key;
    b.className = key === selectedAsset ? "active" : "";
    b.onclick = () => { selectedAsset = key; renderAssetPick(); };
    host.appendChild(b);
  }
}

function renderStrategyPick() {
  const host = $("strategy-pick");
  host.innerHTML = "";
  for (const s of CATALOG.strategies) {
    const card = document.createElement("div");
    card.className = "strategy-card" + (s.id === selectedStrategy ? " active" : "");
    card.innerHTML =
      `<div class="sc-name">${s.name}</div>` +
      `<div class="sc-tag">${s.tagline}</div>` +
      `<div class="sc-desc">${s.description}</div>`;
    card.onclick = () => {
      selectedStrategy = s.id;
      renderStrategyPick();
      renderAssetPick();   // copy bots lock the market picker to ALL
      renderParamForm();
    };
    host.appendChild(card);
  }
}

function renderParamForm() {
  const host = $("param-form");
  host.innerHTML = "";
  const strat = CATALOG.strategies.find((s) => s.id === selectedStrategy);
  if (!strat) return;
  for (const p of strat.params) {
    host.appendChild(paramRow(p, p.default));
  }
}

function paramRow(p, value) {
  const row = document.createElement("div");
  row.className = "param-row" + (p.type === "text" ? " wide" : "");
  const label = document.createElement("span");
  label.textContent = p.label;
  const input = document.createElement("input");
  if (p.type === "text") {
    input.type = "text";
    input.value = value != null ? value : "";
    input.dataset.type = "text";
    input.placeholder = "0x…";
  } else {
    input.type = "number";
    input.value = value != null ? value : p.default;
    if (p.min != null) input.min = p.min;
    if (p.max != null) input.max = p.max;
    input.step = p.step || 1;
  }
  input.dataset.key = p.key;
  row.append(label, input);
  return row;
}

function collectParams(host) {
  const params = {};
  for (const input of host.querySelectorAll("input")) {
    params[input.dataset.key] = input.dataset.type === "text"
      ? input.value.trim()
      : parseFloat(input.value);
  }
  return params;
}

function openModal() { $("modal-backdrop").style.display = "flex"; }
function closeModal() { $("modal-backdrop").style.display = "none"; }

async function createBot() {
  const params = collectParams($("param-form"));
  const body = {
    name: $("bot-name").value.trim(),
    asset: selectedAsset,
    strategy: selectedStrategy,
    params,
    start_balance: parseFloat($("bot-balance").value) || 1000,
    start: $("start-now").checked,
  };
  const resp = await fetch("/api/bots", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (resp.ok) {
    closeModal();
    $("bot-name").value = "";
    await refresh();
  } else {
    const err = await resp.json().catch(() => ({}));
    alert("Could not create bot: " + (err.error || resp.status));
  }
}

// ---------------- bot cards ----------------

async function refresh() {
  let data;
  try {
    data = await fetch("/api/bots").then((r) => r.json());
  } catch (_e) {
    return;
  }
  // Auto-reload when the server redeploys (new code = new boot stamp).
  if (data.boot) {
    if (!window.__boot) window.__boot = data.boot;
    else if (window.__boot !== data.boot) { location.reload(); return; }
  }
  lastBots = data.bots || [];
  renderFilter();
  let bots;
  if (filterAsset === "ALL") bots = lastBots;
  else if (filterAsset === "COPY") bots = lastBots.filter((b) => b.strategy === "copy_trader");
  else bots = lastBots.filter((b) => b.asset === filterAsset);  // copy bots live under COPY only
  renderLeaderboard(bots);
  $("bots-empty").style.display = bots.length ? "none" : "block";
  const grid = $("bots-grid");
  grid.innerHTML = "";
  bots.sort((a, b) => b.created_ms - a.created_ms);
  for (const bot of bots) grid.appendChild(botCard(bot));
}

const lbExpanded = { profit: false, loss: false };

function renderLeaderboard(bots) {
  const winPct = (b) => {
    const total = b.stats.wins + b.stats.losses;
    return total > 0 ? Math.round((b.stats.wins / total) * 100) + "%" : "—";
  };
  const row = (b, rank) =>
    `<tr class="lb-row" data-id="${b.id}">
      <td class="lb-rank">#${rank}</td>
      <td class="lb-name">${escapeHtml(b.name)}${filterAsset === "ALL" ? ` <i class="lb-asset">${b.asset}</i>` : ""}</td>
      <td class="${pnlClass(b.pnl)}">${b.pnl >= 0 ? "+" : "−"}$${Math.abs(b.pnl).toFixed(2)}</td>
      <td>${winPct(b)}</td>
    </tr>`;
  const empty = (msg) => `<tr><td colspan="4" class="lb-empty">${msg}</td></tr>`;

  const allProfit = [...bots].filter((b) => b.pnl > 0).sort((a, b) => b.pnl - a.pnl);
  const allLoss = [...bots].filter((b) => b.pnl < 0).sort((a, b) => a.pnl - b.pnl);
  const profit = lbExpanded.profit ? allProfit : allProfit.slice(0, 5);
  const loss = lbExpanded.loss ? allLoss : allLoss.slice(0, 5);
  const expandRow = (key, total, shown) => total > shown || lbExpanded[key]
    ? `<tr><td colspan="4"><button class="lb-expand" data-lb="${key}">` +
      (lbExpanded[key] ? "▲ show top 5" : `▼ show all ${total}`) + `</button></td></tr>`
    : "";
  $("lb-profit").innerHTML = (profit.length
    ? profit.map((b, i) => row(b, i + 1)).join("")
    : empty("no profitable bots yet")) + expandRow("profit", allProfit.length, profit.length);
  $("lb-loss").innerHTML = (loss.length
    ? loss.map((b, i) => row(b, i + 1)).join("")
    : empty("no losing bots 🎉")) + expandRow("loss", allLoss.length, loss.length);
  for (const tr of document.querySelectorAll(".lb-row")) {
    tr.onclick = () => { location.href = `/bots/${tr.dataset.id}`; };
  }
  for (const btn of document.querySelectorAll(".lb-expand")) {
    btn.onclick = (e) => {
      e.stopPropagation();
      lbExpanded[btn.dataset.lb] = !lbExpanded[btn.dataset.lb];
      refresh();
    };
  }
}

function renderFilter() {
  const host = $("bots-filter");
  if (!host) return;
  const markets = ["ALL", ...(CATALOG.assets || []), "COPY"];
  host.innerHTML = "";
  for (const m of markets) {
    const group = m === "ALL" ? lastBots
      : m === "COPY" ? lastBots.filter((b) => b.strategy === "copy_trader")
      : lastBots.filter((b) => b.asset === m);
    const running = group.filter((b) => b.status === "running").length;
    const pnl = group.reduce((s, b) => s + (b.pnl || 0), 0);
    const chip = document.createElement("button");
    chip.className = "filter-chip" + (m === filterAsset ? " active" : "")
      + (m === "COPY" ? " copy-chip" : "");
    chip.innerHTML =
      `<b>${m}</b><span>${running}/${group.length} live</span>` +
      `<span class="${pnl > 0 ? "up" : pnl < 0 ? "down" : "flat"}">` +
      `${pnl >= 0 ? "+" : "−"}$${Math.abs(pnl).toFixed(2)}</span>`;
    chip.onclick = () => {
      filterAsset = m;
      const url = new URL(location.href);
      if (m === "ALL") url.searchParams.delete("market");
      else url.searchParams.set("market", m);
      history.replaceState(null, "", url);
      refresh();
    };
    host.appendChild(chip);
  }
}

function botCard(bot) {
  const card = document.createElement("div");
  card.className = "bot-card";
  const running = bot.status === "running";
  const winrate = bot.stats.wins + bot.stats.losses > 0
    ? Math.round((bot.stats.wins / (bot.stats.wins + bot.stats.losses)) * 100)
    : null;

  let posHtml = "<span class='muted'>flat</span>";
  if (bot.position) {
    const p = bot.position;
    const cls = p.side === "UP" ? "up" : "down";
    const mark = p.mark_cents != null ? ` @ ${p.mark_cents.toFixed(0)}¢ now` : "";
    posHtml = `<b class="${cls}">${p.side}</b> ${p.shares} sh from ${p.entry_cents.toFixed(0)}¢${mark}`;
  } else if (bot.book) {
    const bk = bot.book;
    posHtml = `<b class="up">U${bk.up_shares}</b>/<b class="down">D${bk.down_shares}</b>` +
      ` · ${bk.pairs} pairs (${bk.pair_edge >= 0 ? "+" : ""}$${bk.pair_edge} locked)`;
  }
  if (bot.position && bot.position.asset && bot.asset === "ALL") {
    posHtml += ` <i class="lb-asset">${bot.position.asset}</i>`;
  }
  if (bot.portfolio && bot.portfolio.length) {
    const upnl = bot.portfolio.reduce((s2, p) => s2 + p.upnl, 0);
    posHtml += `${bot.position ? " · " : ""}${bot.portfolio.length} copies open ` +
      `<b class="${pnlClass(upnl)}">(${upnl >= 0 ? "+" : "−"}$${Math.abs(upnl).toFixed(2)})</b>`;
  }

  card.innerHTML = `
    <div class="bc-head">
      <span class="bc-status ${running ? "run" : "stop"}"></span>
      <a class="bc-name bc-link" href="/bots/${bot.id}">${escapeHtml(bot.name)} ↗</a>
      <span class="bc-asset">${bot.asset}</span>
      <span class="bc-strategy">${bot.strategy_name}</span>
      <span class="bc-actions">
        ${lotHtml(bot)}
        <a class="mini-btn open-btn" href="/bots/${bot.id}">OPEN ↗</a>
        <button class="mini-btn" data-act="settings" title="settings">⚙</button>
        <button class="mini-btn" data-act="toggle">${running ? "STOP" : "START"}</button>
        <button class="mini-btn danger" data-act="delete">✕</button>
      </span>
    </div>
    <div class="bc-stats">
      <div class="bc-stat"><span>Equity</span><b>${fmtUsd(bot.equity)}</b></div>
      <div class="bc-stat"><span>PnL</span><b class="${pnlClass(bot.pnl)}">${fmtUsd(bot.pnl)} (${bot.pnl_pct > 0 ? "+" : ""}${bot.pnl_pct}%)</b></div>
      <div class="bc-stat"><span>W/L</span><b>${bot.stats.wins}/${bot.stats.losses}${winrate != null ? ` (${winrate}%)` : ""}</b></div>
      <div class="bc-stat"><span>Rounds</span><b>${bot.stats.rounds}</b></div>
      <div class="bc-stat"><span>Fees</span><b>${fmtUsd(bot.stats.fees)}</b></div>
      <div class="bc-stat"><span>Position</span><b>${posHtml}</b></div>
    </div>
    <div class="bc-trades${expanded.has(bot.id) ? " open" : ""}">
      ${tradesTable(bot.trades)}
    </div>
    <button class="bc-expand" data-act="expand">${expanded.has(bot.id) ? "▲ hide log" : "▼ trade log"}</button>
  `;

  card.querySelector('[data-act="settings"]').onclick = () => openSettings(bot);
  bindLot(card, () => bot, `/api/bots/${bot.id}/update`, () => refresh());
  card.querySelector('[data-act="toggle"]').onclick = async () => {
    await fetch(`/api/bots/${bot.id}/${running ? "stop" : "start"}`, { method: "POST" });
    refresh();
  };
  card.querySelector('[data-act="delete"]').onclick = async () => {
    if (!confirm(`Delete bot "${bot.name}"?`)) return;
    await fetch(`/api/bots/${bot.id}`, { method: "DELETE" });
    refresh();
  };
  card.querySelector('[data-act="expand"]').onclick = () => {
    if (expanded.has(bot.id)) expanded.delete(bot.id); else expanded.add(bot.id);
    refresh();
  };
  return card;
}

function tradesTable(trades) {
  if (!trades || !trades.length) return "<div class='muted' style='padding:6px'>no trades yet</div>";
  let html = "<table class='trades-table'><tbody>";
  for (const t of trades) {
    const cls = t.action === "BUY" ? "flat" : pnlClass(t.pnl);
    const side = t.side ? `<b class="${t.side === "UP" ? "up" : "down"}">${t.side}</b>` : "";
    // Settlements are REDEMPTIONS, not market fills: winners pay $1/share.
    let price = t.price_cents != null ? t.price_cents.toFixed(0) + "¢" : "";
    if (t.action === "SETTLE" || t.action === "ADJUST") {
      price = t.price_cents >= 100
        ? "<b class='up'>WIN $1/sh</b>" : "<b class='down'>LOSS $0</b>";
    }
    const pnl = t.pnl != null && t.action !== "BUY" ? fmtUsd(t.pnl) : "";
    html += `<tr>
      <td>${fmtTime(t.t)}</td>
      <td class="ta-${t.action.toLowerCase()}">${t.action}</td>
      <td>${side}</td>
      <td>${price}</td>
      <td class="${cls}">${pnl}</td>
      <td class="muted">${escapeHtml(t.reason || "")}</td>
    </tr>`;
  }
  return html + "</tbody></table>";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------------- per-bot settings modal ----------------

let settingsBot = null;

function openSettings(bot) {
  settingsBot = bot;
  $("settings-title").textContent = `SETTINGS — ${bot.name}`;
  $("settings-name").value = bot.name;
  const host = $("settings-params");
  host.innerHTML = "";
  const strat = CATALOG.strategies.find((s) => s.id === bot.strategy);
  const schema = strat ? strat.params : [];
  for (const p of schema) {
    host.appendChild(paramRow(p, bot.params[p.key]));
  }
  $("settings-backdrop").style.display = "flex";
}

function closeSettings() {
  $("settings-backdrop").style.display = "none";
  settingsBot = null;
}

async function saveSettings() {
  if (!settingsBot) return;
  const params = collectParams($("settings-params"));
  await fetch(`/api/bots/${settingsBot.id}/update`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: $("settings-name").value.trim(), params }),
  });
  closeSettings();
  refresh();
}

async function resetBot() {
  if (!settingsBot) return;
  if (!confirm(`Reset "${settingsBot.name}" to a fresh balance? This clears its trade history.`)) return;
  await fetch(`/api/bots/${settingsBot.id}/reset`, { method: "POST" });
  closeSettings();
  refresh();
}

$("settings-close").onclick = closeSettings;
$("settings-backdrop").onclick = (e) => { if (e.target === $("settings-backdrop")) closeSettings(); };
$("settings-save").onclick = saveSettings;
$("settings-reset").onclick = resetBot;

// ---------------- boot ----------------

$("add-bot-btn").onclick = openModal;
$("modal-close").onclick = closeModal;
$("modal-backdrop").onclick = (e) => { if (e.target === $("modal-backdrop")) closeModal(); };
$("create-bot").onclick = createBot;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

loadCatalog();
refresh();
setInterval(refresh, 2000);
