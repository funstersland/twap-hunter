// /real page: Polymarket accounts + real bots with live account data.

import { lotHtml, bindLot } from "./lot.js";

const $ = (id) => document.getElementById(id);

let paperBots = [];
let realBots = [];
let accounts = [];
let selectedPaperBotId = null;
const expanded = new Set();

function fmtAddr(addr) {
  if (!addr || addr.length < 12) return addr || "—";
  return addr.slice(0, 6) + "…" + addr.slice(-4);
}

function fmtUsd(v) {
  if (v == null) return "—";
  const sign = v < 0 ? "−" : "";
  return sign + "$" + Math.abs(v).toFixed(2);
}

function pnlClass(v) { return v > 0 ? "up" : v < 0 ? "down" : "flat"; }

function fmtTime(ms) {
  return new Date(ms).toLocaleTimeString("en-US", { hour12: false });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------------- refresh ----------------

async function refresh() {
  try {
    const [accData, realData] = await Promise.all([
      fetch("/api/accounts").then((r) => r.json()),
      fetch("/api/real/bots").then((r) => r.json()),
    ]);
    if (accData.boot) checkBoot(accData.boot);
    accounts = accData.accounts || [];
    realBots = realData.bots || [];
  } catch (_e) {
    return;
  }
  renderAccounts();
  renderRealBots();
}

function checkBoot(boot) {
  if (!window.__boot) window.__boot = boot;
  else if (window.__boot !== boot) { location.reload(); return true; }
  return false;
}

// ---------------- accounts ----------------

function renderAccounts() {
  const grid = $("accounts-grid");
  const empty = $("accounts-empty");
  empty.style.display = accounts.length ? "none" : "block";
  grid.innerHTML = "";
  for (const acc of accounts) grid.appendChild(accountCard(acc));
}

function accountCard(acc) {
  const card = document.createElement("div");
  card.className = "account-card account-card-live";
  const mismatch = acc.address_mismatch
    ? `<span class="acc-warn" title="Address differs from key-derived EOA">proxy</span>`
    : "";
  const posHtml = (acc.positions || []).slice(0, 3).map((p) =>
    `<div class="acc-pos-line">
      <span class="${p.outcome === "UP" || p.outcome === "Yes" ? "up" : "down"}">${escapeHtml(p.outcome)}</span>
      ${p.size} sh @ ${p.avg_price_cents}¢
      <span class="${pnlClass(p.cash_pnl)}">${fmtUsd(p.cash_pnl)}</span>
    </div>`
  ).join("") || `<div class="muted" style="font-size:10px">no open positions</div>`;

  card.innerHTML = `
    <div class="acc-head">
      <span class="acc-label">${escapeHtml(acc.label)}</span>
      ${mismatch}
      <button class="mini-btn danger acc-delete" data-id="${acc.id}">✕</button>
    </div>
    <div class="acc-addr" title="${escapeHtml(acc.address)}">${fmtAddr(acc.address)}</div>
    <div class="acc-stats">
      <div><span class="muted">Cash</span> <b>${fmtUsd(acc.cash_balance)}</b></div>
      <div><span class="muted">Positions</span> <b>${fmtUsd(acc.positions_value)}</b></div>
      <div><span class="muted">Total</span> <b>${fmtUsd(acc.total_equity ?? acc.portfolio_value)}</b></div>
      <div><span class="muted">Open PnL</span> <b class="${pnlClass(acc.cash_pnl)}">${fmtUsd(acc.cash_pnl)}</b></div>
    </div>
    ${acc.profile_name ? `<div class="acc-profile muted">${escapeHtml(acc.profile_name)} on Polymarket</div>` : ""}
    <div class="acc-positions">${posHtml}</div>
    ${acc.fetch_error ? `<div class="acc-error muted">${escapeHtml(acc.fetch_error)}</div>` : ""}
  `;
  card.querySelector(".acc-delete").onclick = () => deleteAccount(acc.id, acc.label);
  return card;
}

async function deleteAccount(id, label) {
  if (!confirm(`Remove account "${label}"?`)) return;
  const resp = await fetch(`/api/accounts/${id}`, { method: "DELETE" });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    alert(err.error || "Could not delete account");
    return;
  }
  refresh();
}

function openAccountModal() {
  $("account-label").value = "";
  $("account-address").value = "";
  $("account-key").value = "";
  $("account-derived").style.display = "none";
  $("account-backdrop").style.display = "flex";
}

function closeAccountModal() {
  $("account-backdrop").style.display = "none";
}

async function saveAccount() {
  const body = {
    label: $("account-label").value.trim(),
    address: $("account-address").value.trim(),
    private_key: $("account-key").value.trim(),
  };
  const resp = await fetch("/api/accounts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    alert(data.error || "Could not save account");
    return;
  }
  if (data.warning) alert(data.warning);
  closeAccountModal();
  refresh();
}

// ---------------- real bots ----------------

function renderRealBots() {
  const grid = $("real-bots-grid");
  const empty = $("real-bots-empty");
  empty.style.display = realBots.length ? "none" : "block";
  grid.innerHTML = "";
  const sorted = [...realBots].sort((a, b) => b.created_ms - a.created_ms);
  for (const bot of sorted) grid.appendChild(realBotCard(bot));
}

function positionHtml(bot) {
  const positions = bot.positions || [];
  if (!positions.length) return "<span class='muted'>flat</span>";
  return positions.map((p) => {
    const cls = p.outcome === "UP" || p.outcome === "Yes" ? "up" : "down";
    return `<b class="${cls}">${escapeHtml(p.outcome)}</b> ${p.size} sh @ ${p.avg_price_cents}¢` +
      ` <span class="${pnlClass(p.cash_pnl)}">(${fmtUsd(p.cash_pnl)})</span>`;
  }).join(" · ");
}

function tradesTable(trades) {
  if (!trades || !trades.length) {
    return "<div class='muted' style='padding:6px'>no Polymarket trades yet for this market</div>";
  }
  let html = "<table class='trades-table'><tbody>";
  for (const t of trades) {
    const action = t.action || t.side;
    const cls = action === "BUY" ? "flat" : pnlClass(t.cash_pnl ?? t.pnl);
    const outcome = t.outcome || "";
    const side = outcome ? `<b class="${outcome === "UP" ? "up" : "down"}">${outcome}</b>` : "";
    const sh = t.shares ?? t.size;
    const px = t.price_cents != null ? Number(t.price_cents).toFixed(1) + "¢" : "—";
    const usd = t.usd != null ? fmtUsd(t.usd) : "";
    const pnl = t.cash_pnl != null ? fmtUsd(t.cash_pnl) : (t.pnl != null ? fmtUsd(t.pnl) : "");
    html += `<tr>
      <td>${fmtTime(t.t)}</td>
      <td class="ta-${(action || "").toLowerCase()}">${action}</td>
      <td>${side}</td>
      <td>${px}${sh != null ? " · " + Number(sh).toFixed(4) + " sh" : ""}</td>
      <td class="${cls}">${pnl || usd}</td>
      <td class="muted">${escapeHtml(t.title || t.event_slug || t.round_label || "")}</td>
    </tr>`;
  }
  return html + "</tbody></table>";
}

function activityTable(entries) {
  if (!entries || !entries.length) {
    return "<div class='muted' style='padding:6px'>no activity yet — start the bot and wait for signals</div>";
  }
  let html = "<table class='trades-table activity-table'><tbody>";
  for (const e of entries) {
    const cls = e.level === "error" ? "down" : e.level === "order" ? "up" : e.level === "skip" ? "flat" : "";
    html += `<tr class="log-${e.level}">
      <td>${fmtTime(e.t)}</td>
      <td class="${cls}">${escapeHtml(e.level)}</td>
      <td class="muted log-msg">${escapeHtml(e.msg)}</td>
    </tr>`;
  }
  return html + "</tbody></table>";
}

function realBotCard(bot) {
  const card = document.createElement("div");
  card.className = "bot-card real-bot-card";
  const armed = bot.status === "running";
  const paramsHtml = Object.entries(bot.params || {})
    .map(([k, v]) => `<span class="param-tag">${escapeHtml(k)}=${escapeHtml(String(v))}</span>`)
    .join(" ");

  const errHtml = bot.last_exec_error
    ? `<div class="bc-exec-error">${escapeHtml(bot.last_exec_error)}</div>` : "";

  card.innerHTML = `
    <div class="bc-head">
      <span class="bc-status ${armed ? "run" : "stop"}"></span>
      <a class="bc-name bc-link" href="/real/${bot.id}">${escapeHtml(bot.name)} ↗</a>
      <span class="bc-asset">${bot.asset}</span>
      <span class="bc-strategy">${bot.strategy_name}</span>
      <span class="status-badge ${armed ? "armed-badge" : "disarmed-badge"}">${armed ? "RUNNING" : "STOPPED"}</span>
      <span class="bc-actions">
        ${lotHtml(bot)}
        <a class="mini-btn open-btn" href="/real/${bot.id}">OPEN ↗</a>
        <button class="mini-btn" data-act="toggle">${armed ? "STOP" : "START"}</button>
        <button class="mini-btn danger" data-act="delete">✕</button>
      </span>
    </div>
    <div class="bc-stats">
      <div class="bc-stat"><span>Account</span><b>${escapeHtml(bot.account_label || "—")} <i class="muted">${fmtAddr(bot.account_address)}</i></b></div>
      <div class="bc-stat"><span>Cash</span><b>${fmtUsd(bot.cash_balance ?? bot.balance)}</b></div>
      <div class="bc-stat"><span>Positions</span><b>${fmtUsd(bot.positions_value)}</b></div>
      <div class="bc-stat"><span>Equity</span><b>${fmtUsd(bot.equity ?? bot.portfolio_value)}</b></div>
      <div class="bc-stat"><span>Orders</span><b>${bot.orders_placed ?? 0}</b></div>
      <div class="bc-stat"><span>Open PnL</span><b class="${pnlClass(bot.account_cash_pnl)}">${fmtUsd(bot.account_cash_pnl)}</b></div>
      <div class="bc-stat wide"><span>Params</span><b>${paramsHtml || "<span class='muted'>default</span>"}</b></div>
    </div>
    ${errHtml}
    <div class="bc-trades${expanded.has(bot.id) ? " open" : ""}">
      <div class="log-section-title">Activity log</div>
      ${activityTable(bot.activity_log)}
      <div class="log-section-title">Trades</div>
      ${tradesTable(bot.trades)}
    </div>
    <button class="bc-expand" data-act="expand">${expanded.has(bot.id) ? "▲ hide logs" : "▼ logs & trades"}</button>
  `;

  card.querySelector('[data-act="delete"]').onclick = async () => {
    if (!confirm(`Delete real bot "${bot.name}"?`)) return;
    await fetch(`/api/real/bots/${bot.id}`, { method: "DELETE" });
    refresh();
  };
  card.querySelector('[data-act="toggle"]').onclick = async () => {
    const action = bot.status === "running" ? "stop" : "start";
    await fetch(`/api/real/bots/${bot.id}/${action}`, { method: "POST" });
    refresh();
  };
  card.querySelector('[data-act="expand"]').onclick = () => {
    if (expanded.has(bot.id)) expanded.delete(bot.id); else expanded.add(bot.id);
    renderRealBots();
  };
  bindLot(card, () => bot, `/api/real/bots/${bot.id}/update`, () => refresh());
  return card;
}

// ---------------- import modal ----------------

async function loadPaperBots() {
  try {
    const data = await fetch("/api/bots").then((r) => r.json());
    paperBots = data.bots || [];
  } catch (_e) {
    paperBots = [];
  }
}

async function openImportModal() {
  if (!accounts.length) {
    alert("Add a Polymarket account first.");
    openAccountModal();
    return;
  }
  await loadPaperBots();
  selectedPaperBotId = paperBots[0]?.id || null;
  renderPaperBotPick();
  renderImportAccounts();
  renderImportPreview();
  $("import-name").value = "";
  $("import-backdrop").style.display = "flex";
}

function closeImportModal() {
  $("import-backdrop").style.display = "none";
}

function renderPaperBotPick() {
  const host = $("paper-bot-pick");
  host.innerHTML = "";
  if (!paperBots.length) {
    host.innerHTML = `<div class="muted">No paper bots — create one on the <a href="/bots">bots page</a> first.</div>`;
    return;
  }
  for (const bot of paperBots) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "paper-bot-row" + (bot.id === selectedPaperBotId ? " active" : "");
    row.innerHTML =
      `<span class="pbr-name">${escapeHtml(bot.name)}</span>` +
      `<span class="pbr-meta">${bot.asset} · ${escapeHtml(bot.strategy_name)}</span>` +
      `<span class="pbr-pnl muted">strategy only</span>`;
    row.onclick = () => {
      selectedPaperBotId = bot.id;
      renderPaperBotPick();
      renderImportPreview();
      const picked = paperBots.find((b) => b.id === selectedPaperBotId);
      if (picked) $("import-name").placeholder = picked.name;
    };
    host.appendChild(row);
  }
}

function renderImportAccounts() {
  const sel = $("import-account");
  sel.innerHTML = "";
  for (const acc of accounts) {
    const opt = document.createElement("option");
    opt.value = acc.id;
    opt.textContent = `${acc.label} (${fmtAddr(acc.address)})`;
    sel.appendChild(opt);
  }
}

function renderImportPreview() {
  const host = $("import-preview");
  const bot = paperBots.find((b) => b.id === selectedPaperBotId);
  if (!bot) {
    host.innerHTML = "";
    return;
  }
  const params = Object.entries(bot.params || {})
    .map(([k, v]) => `<li><b>${escapeHtml(k)}</b> = ${escapeHtml(String(v))}</li>`)
    .join("");
  host.innerHTML =
    `<div class="preview-title">Copies strategy config only — live data comes from your Polymarket account:</div>` +
    `<ul class="preview-list">` +
    `<li><b>Market</b> ${bot.asset}</li>` +
    `<li><b>Strategy</b> ${escapeHtml(bot.strategy_name)}</li>` +
    params +
    `</ul>` +
    `<div class="preview-note muted">Bot starts DISARMED. Trades and PnL shown are from the linked account, not paper.</div>`;
}

async function importBot() {
  if (!selectedPaperBotId) {
    alert("Select a paper bot to import.");
    return;
  }
  const accountId = $("import-account").value;
  if (!accountId) {
    alert("Select an account.");
    return;
  }
  const body = {
    paper_bot_id: selectedPaperBotId,
    account_id: accountId,
    name: $("import-name").value.trim() || undefined,
  };
  const resp = await fetch("/api/real/bots/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    alert(data.error || "Could not import bot");
    return;
  }
  closeImportModal();
  refresh();
}

// ---------------- boot ----------------

$("add-account-btn").onclick = openAccountModal;
$("import-bot-btn").onclick = openImportModal;
$("account-close").onclick = closeAccountModal;
$("account-backdrop").onclick = (e) => { if (e.target === $("account-backdrop")) closeAccountModal(); };
$("account-save").onclick = saveAccount;
$("import-close").onclick = closeImportModal;
$("import-backdrop").onclick = (e) => { if (e.target === $("import-backdrop")) closeImportModal(); };
$("import-save").onclick = importBot;

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  closeAccountModal();
  closeImportModal();
});

refresh();
setInterval(refresh, 5000);
