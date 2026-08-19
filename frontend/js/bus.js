// Data bus: WebSocket client + ring buffers kept OUTSIDE any framework.
// Charts read these arrays directly on their own rAF loop.

const KEEP_TICKS_MS = 6 * 60 * 1000;
const KEEP_REFS_MS = 12 * 60 * 1000;
const KEEP_QUOTES_MS = 12 * 60 * 1000;
const MAX_EVENTS = 150;

export const bus = {
  ticks: [],    // [t, price, qty, side(1|-1)]
  refs: [],     // [t, chainlink]
  twaps: [],    // [t, rolling, twap30]
  quotes: [],   // quote objects
  preds: [],    // {t: target_ms, v: predicted, c: created_ms, k: "h"|"end"}
  events: [],   // newest LAST here; timeline renders reversed
  snapshot: null,
  assets: [],
  asset: null,
  pendingAsset: null,  // optimistic tab highlight while the backend switches
  lastEventId: 0,      // dedupe: init history + first batch can overlap
  serverOffsetMs: 0,   // server_now - client_now
  wsConnected: false,
  listeners: { batch: [], init: [], events: [] },

  on(name, fn) { this.listeners[name].push(fn); },
  _fire(name, arg) { for (const fn of this.listeners[name]) fn(arg); },

  nowServerMs() { return Date.now() + this.serverOffsetMs; },

  // Smooth, monotonic display clock: charts must NEVER jump backward or
  // forward when the measured offset wobbles. The clock advances in real
  // time, slewed 0.5x–1.5x toward the target; it only snaps on a huge
  // discontinuity (server restart / asset switch).
  _clock: { value: null, perf: 0 },
  smoothNowMs() {
    const target = this.nowServerMs();
    const p = performance.now();
    const c = this._clock;
    if (c.value == null || Math.abs(target - c.value) > 5000) {
      c.value = target;
      c.perf = p;
      return c.value;
    }
    const dt = Math.max(0, p - c.perf);
    c.perf = p;
    const err = target - c.value;
    const speed = Math.min(1.5, Math.max(0.5, 1 + err / 2000));
    c.value += dt * speed;
    return c.value;
  },
};

function trim(arr, keepMs, now) {
  const cutoff = now - keepMs;
  let drop = 0;
  while (drop < arr.length && (arr[drop][0] ?? arr[drop].t) < cutoff) drop++;
  if (drop > 0) arr.splice(0, drop);
}

function applySnapshot(snap, snapOffset = false) {
  bus.snapshot = snap;
  // The raw offset wobbles with feed jitter — smooth it heavily; only
  // snap on init or a real discontinuity.
  const raw = snap.server_now_ms - Date.now();
  if (snapOffset || bus.serverOffsetMs == null || Math.abs(raw - bus.serverOffsetMs) > 3000) {
    bus.serverOffsetMs = raw;
  } else {
    bus.serverOffsetMs += 0.05 * (raw - bus.serverOffsetMs);
  }
}

function handleInit(msg) {
  // Auto-reload when the server redeploys (new code = new boot stamp).
  if (msg.boot) {
    if (!window.__boot) window.__boot = msg.boot;
    else if (window.__boot !== msg.boot) { location.reload(); return; }
  }
  bus.asset = msg.asset;
  bus.pendingAsset = null;
  bus.assets = msg.assets;
  // Keep the URL + title in sync so refresh/bookmark/new-tab all stick.
  try {
    const url = new URL(location.href);
    url.searchParams.set("asset", msg.asset);
    history.replaceState(null, "", url);
    document.title = `${msg.asset} · Twap Hunter`;
  } catch (_e) { /* ignore */ }
  bus.ticks = msg.history.ticks || [];
  bus.refs = msg.history.ref || [];
  bus.twaps = msg.history.twap || [];
  bus.quotes = msg.history.quotes || [];
  bus.preds = msg.history.preds || [];
  bus.events = msg.history.events || [];
  bus.lastEventId = bus.events.reduce((m, e) => Math.max(m, e.id || 0), 0);
  applySnapshot(msg.snapshot, true);
  bus._fire("init", msg);
  bus._fire("events", bus.events);
}

function handleBatch(msg) {
  const now = msg.snapshot.server_now_ms;
  if (msg.ticks.length) { bus.ticks.push(...msg.ticks); trim(bus.ticks, KEEP_TICKS_MS, now); }
  if (msg.ref.length) { bus.refs.push(...msg.ref); trim(bus.refs, KEEP_REFS_MS, now); }
  if (msg.twap.length) { bus.twaps.push(...msg.twap); trim(bus.twaps, KEEP_REFS_MS, now); }
  if (msg.quotes.length) { bus.quotes.push(...msg.quotes); trim(bus.quotes, KEEP_QUOTES_MS, now); }
  if (msg.preds && msg.preds.length) {
    bus.preds.push(...msg.preds);
    // Trim by CREATION time (targets sit in the future).
    const cutoff = now - KEEP_REFS_MS;
    while (bus.preds.length && bus.preds[0].c < cutoff) bus.preds.shift();
  }
  if (msg.events.length) {
    const fresh = msg.events.filter((e) => (e.id || 0) > bus.lastEventId);
    if (fresh.length) {
      bus.lastEventId = fresh.reduce((m, e) => Math.max(m, e.id || 0), bus.lastEventId);
      bus.events.push(...fresh);
      if (bus.events.length > MAX_EVENTS) bus.events.splice(0, bus.events.length - MAX_EVENTS);
      bus._fire("events", fresh);
    }
  }
  applySnapshot(msg.snapshot);
  bus._fire("batch", msg);
}

let ws = null;

export function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  // This tab's market: current asset (reconnects) or the ?asset= URL
  // param (fresh tab) — each browser tab subscribes independently.
  const urlAsset = new URLSearchParams(location.search).get("asset");
  const asset = bus.asset || (urlAsset ? urlAsset.toUpperCase() : null);
  const q = asset ? `?asset=${encodeURIComponent(asset)}` : "";
  ws = new WebSocket(`${proto}://${location.host}/ws${q}`);
  ws.onopen = () => { bus.wsConnected = true; };
  ws.onclose = () => {
    bus.wsConnected = false;
    setTimeout(connect, 1000);
  };
  ws.onerror = () => { try { ws.close(); } catch (_e) {} };
  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch (_e) { return; }
    if (msg.type === "init") handleInit(msg);
    else if (msg.type === "batch") handleBatch(msg);
  };
}

export function setAsset(key) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    bus.pendingAsset = key;
    ws.send(JSON.stringify({ type: "set_asset", asset: key }));
  }
}
