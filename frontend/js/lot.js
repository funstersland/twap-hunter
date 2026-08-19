// Shared lot-size control ($1 default) for paper and real bot screens.

export const LOT_MIN = 1;
export const LOT_MAX = 500;
export const LOT_PRESETS = [1, 2, 5, 10, 20, 50];

export function capitalUsd(bot) {
  // "capital" for sizing: prefer the amount that bot can actually spend
  // on CLOB right now.
  const v =
    Number(bot?.balance ?? bot?.cash_balance ?? bot?.equity ?? 0);
  return Number.isFinite(v) && v > 0 ? v : 0;
}

export function lotUsd(bot) {
  const v = Number(bot?.params?.order_usd);
  return Number.isFinite(v) && v > 0 ? v : 1;
}

export function formatLot(v) {
  const n = Number(v) || 1;
  return n % 1 === 0 ? "$" + n.toFixed(0) : "$" + n.toFixed(2);
}

export function lotHtml(bot) {
  const v = lotUsd(bot);
  return `<div class="lot-ctrl" title="Lot size — choose fixed $ or % of capital">
    <span class="lot-label">LOT</span>
    <select class="lot-type">
      <option value="usd" selected>USD</option>
      <option value="pct">% capital</option>
    </select>
    <input class="lot-input" type="number" step="0.01" min="0" value="${v}" />
    <button type="button" class="lot-apply">Apply</button>
  </div>`;
}

export function setLotUI(root, bot) {
  if (!root) return;
  const modeEl = root.querySelector(".lot-type");
  const inputEl = root.querySelector(".lot-input");
  if (!modeEl || !inputEl) return; // old button-based UI

  // If the user is actively editing, don't override their current text on
  // the next refresh tick.
  if (document && document.activeElement === inputEl) return;

  const mode = modeEl.value || "usd";
  const orderUsd = lotUsd(bot);
  if (mode === "usd") {
    inputEl.value = Number(orderUsd).toString();
    return;
  }

  const cap = capitalUsd(bot);
  const pct = cap > 0 ? (orderUsd / cap) * 100 : 0;
  // Keep it readable; backend will re-clamp when saving.
  inputEl.value = String(Number.isFinite(pct) ? pct.toFixed(2) : "0");
}

export function nextLot(current, act) {
  let v = Number(current) || 1;
  if (act === "-") return Math.max(LOT_MIN, Math.round(v) - 1);
  if (act === "+") return Math.min(LOT_MAX, Math.round(v) + 1);
  const rounded = Math.round(v);
  const i = LOT_PRESETS.indexOf(rounded);
  if (i >= 0) return LOT_PRESETS[(i + 1) % LOT_PRESETS.length];
  const bigger = LOT_PRESETS.find((p) => p > v);
  return bigger != null ? bigger : LOT_PRESETS[0];
}

export async function setLot(endpoint, usd) {
  const resp = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ params: { order_usd: usd } }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || "could not update lot size");
  }
  return resp.json();
}

export function bindLot(root, getBot, endpoint, onSaved) {
  if (!root) return;

  const el = root.classList?.contains("lot-ctrl") ? root : root.querySelector(".lot-ctrl");
  if (!el) return;

  // New UI: dropdown + input + apply
  const modeEl = el.querySelector(".lot-type");
  const inputEl = el.querySelector(".lot-input");
  const applyEl = el.querySelector(".lot-apply");
  if (modeEl && inputEl) {
    // Initialize the input value from the current bot snapshot.
    setLotUI(el, getBot());

    const apply = async () => {
      const bot = getBot();
      if (!bot) return;

      const mode = modeEl.value || "usd";
      let usd = 0;
      if (mode === "pct") {
        const cap = capitalUsd(bot);
        const pct = Number(inputEl.value);
        const safePct = Number.isFinite(pct) ? Math.max(0, pct) : 0;
        usd = cap * safePct / 100;
      } else {
        usd = Number(inputEl.value);
      }

      // Clamp to the same range as the presets (and keep it positive).
      if (!Number.isFinite(usd)) usd = LOT_MIN;
      usd = Math.max(LOT_MIN, Math.min(LOT_MAX, usd));

      // Persist the computed USD back into the input when switching modes,
      // so the UI doesn't look inconsistent.
      if (mode === "usd") inputEl.value = String(usd);
      else inputEl.value = String(inputEl.value); // keep user's percent

      try {
        await setLot(endpoint, usd);
        if (onSaved) onSaved(usd);
      } catch (err) {
        alert(err.message || err);
      }
    };

    // Apply on button or Enter key.
    if (applyEl) {
      applyEl.onclick = async (e) => {
        e.preventDefault();
        e.stopPropagation();
        await apply();
      };
    }
    inputEl.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      e.stopPropagation();
      await apply();
    });

    // When switching modes, update the displayed value.
    modeEl.onchange = () => {
      setLotUI(el, getBot());
    };
    return;
  }

  // Old UI: cycle presets via −/+ buttons
  el.onclick = async (e) => {
    const btn = e.target.closest("[data-lot-act]");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const bot = getBot();
    if (!bot) return;
    const usd = nextLot(lotUsd(bot), btn.dataset.lotAct);
    const amt = el.querySelector(".lot-amt");
    if (amt) amt.textContent = formatLot(usd);
    try {
      await setLot(endpoint, usd);
      if (onSaved) onSaved(usd);
    } catch (err) {
      alert(err.message || err);
    }
  };
}
