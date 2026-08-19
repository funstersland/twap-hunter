// Minimal ms-resolution canvas chart engine with pan/zoom.
//
// Time model: "now" is anchored at NOW_FRAC of the width (a bit right of
// center) with empty space to its right. Dragging pans back in time
// (chart leaves follow mode), the mouse wheel zooms the span, and
// double-click / the LIVE button snaps back to following.

import { bus } from "./bus.js";
import { fmtPrice } from "./format.js";

const COL = {
  grid: "#1a2030",
  axis: "#2a3145",
  text: "#6b7488",
  nowLine: "#3a4360",
  chainlink: "#4fa3ff",
  twap: "#f5b942",
  twap30: "#b07cff",
  ptb: "#f5b942",
  price: "#9aa7c4",
  buy: "#22c77a",
  sell: "#f0475c",
  bid: "#22c77a",
  ask: "#f0475c",
  lastTrade: "#e8ce6f",
  pred: "#35d0d0",
  predHit: "#22c77a",
  predMiss: "#f0475c",
};

const PRED_HIT_BPS = 1.0; // matches backend prediction_hit_bps

const NOW_FRAC = 0.7;          // "now" sits at 70% of the width
const MAX_VISIBLE_DOTS = 5000; // skip per-tick dots beyond this

// Optional trade markers drawn on the quote chart (bot detail page).
let quoteMarkers = [];
export function setQuoteMarkers(markers) { quoteMarkers = markers || []; }

export function makeChart(canvas, renderFn, opts = {}) {
  const ctx = canvas.getContext("2d");
  let w = 0, h = 0, dpr = 1;

  const view = {
    pannable: !!opts.pan,
    spanMs: opts.defaultSpan || 60_000,
    minSpan: opts.minSpan || 15_000,
    maxSpan: opts.maxSpan || 600_000,
    maxPanMs: opts.maxPanMs || 600_000,
    panMs: 0,               // 0 = live/following
  };

  function resize() {
    dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    w = rect.width; h = rect.height;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  new ResizeObserver(resize).observe(canvas);
  resize();

  if (view.pannable) {
    canvas.classList.add("pannable");
    let dragging = false;
    let lastX = 0;

    canvas.addEventListener("mousedown", (e) => {
      dragging = true;
      lastX = e.clientX;
      canvas.classList.add("panning");
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const dx = e.clientX - lastX;
      lastX = e.clientX;
      view.panMs = Math.min(
        view.maxPanMs,
        Math.max(0, view.panMs + (dx / Math.max(1, w)) * view.spanMs),
      );
    });
    window.addEventListener("mouseup", () => {
      dragging = false;
      canvas.classList.remove("panning");
    });
    canvas.addEventListener("dblclick", (e) => {
      view.panMs = 0;
      e.preventDefault();
    });
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const factor = e.deltaY > 0 ? 1.2 : 1 / 1.2;
      const now = bus.smoothNowMs();
      const t1Old = now - view.panMs + view.spanMs * (1 - NOW_FRAC);
      view.spanMs = Math.min(view.maxSpan, Math.max(view.minSpan, view.spanMs * factor));
      if (view.panMs > 0) {
        // Keep the right-edge time stable while zooming a panned view.
        view.panMs = Math.max(0, now + view.spanMs * (1 - NOW_FRAC) - t1Old);
      }
    }, { passive: false });
  }

  return {
    view,
    goLive() { view.panMs = 0; },
    render() {
      if (w === 0 || h === 0) { resize(); return; }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      renderFn(ctx, w, h, view);
    },
  };
}

// ---------- shared helpers ----------

function timeDomain(view) {
  const now = bus.smoothNowMs();
  const anchor = now - view.panMs;
  const t1 = anchor + view.spanMs * (1 - NOW_FRAC);
  return [t1 - view.spanMs, t1, now];
}

function timeTicks(t0, t1) {
  const span = t1 - t0;
  const steps = [1e3, 5e3, 10e3, 15e3, 30e3, 60e3, 120e3, 300e3, 600e3];
  let step = steps[steps.length - 1];
  for (const s of steps) { if (span / s <= 8) { step = s; break; } }
  const ticks = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) ticks.push(t);
  return ticks;
}

function fmtTick(t) {
  const d = new Date(t);
  return String(d.getMinutes()).padStart(2, "0") + ":" + String(d.getSeconds()).padStart(2, "0");
}

function drawTimeAxis(ctx, w, h, t0, t1, plotH) {
  ctx.strokeStyle = COL.grid;
  ctx.fillStyle = COL.text;
  ctx.font = "9px monospace";
  ctx.textAlign = "center";
  ctx.lineWidth = 1;
  for (const t of timeTicks(t0, t1)) {
    const x = ((t - t0) / (t1 - t0)) * w;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, plotH);
    ctx.stroke();
    ctx.fillText(fmtTick(t), x, h - 3);
  }
}

function drawNowLine(ctx, w, plotH, t0, t1, now) {
  if (now < t0 || now > t1) return null;
  const x = ((now - t0) / (t1 - t0)) * w;
  ctx.strokeStyle = COL.nowLine;
  ctx.setLineDash([2, 3]);
  ctx.beginPath();
  ctx.moveTo(x, 0);
  ctx.lineTo(x, plotH);
  ctx.stroke();
  ctx.setLineDash([]);
  return x;
}

function yTicks(lo, hi) {
  const span = hi - lo;
  if (span <= 0) return [];
  const raw = span / 4;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  let step = mag;
  for (const m of [1, 2, 2.5, 5, 10]) { if (raw / (mag * m) <= 1) { step = mag * m; break; } }
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(v);
  return ticks;
}

function drawYGrid(ctx, w, plotH, lo, hi, decimals) {
  ctx.strokeStyle = COL.grid;
  ctx.fillStyle = COL.text;
  ctx.font = "9px monospace";
  ctx.textAlign = "right";
  ctx.lineWidth = 1;
  for (const v of yTicks(lo, hi)) {
    const y = plotH - ((v - lo) / (hi - lo)) * plotH;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
    ctx.fillText(fmtPrice(v, decimals), w - 4, y - 2);
  }
}

function visibleWindow(series, t0) {
  let lo = 0, hi = series.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (series[mid][0] < t0) lo = mid + 1; else hi = mid;
  }
  return Math.max(0, lo - 1);
}

function drawLine(ctx, series, col, t0, t1, lo, hi, w, plotH, width = 1.5, colIdx = 1, step = false, extendToX = null) {
  if (hi - lo <= 0) return;
  const start = visibleWindow(series, t0);
  ctx.strokeStyle = col;
  ctx.lineWidth = width;
  ctx.beginPath();
  let started = false;
  let prevY = null;
  let prevX = null;
  for (let i = start; i < series.length; i++) {
    const p = series[i];
    if (p[0] > t1) {
      // First point beyond the right edge: draw the step/segment into it
      // and stop (panned views must not pull in the whole future).
      if (started && prevY != null) {
        const x = ((p[0] - t0) / (t1 - t0)) * w;
        const v = p[colIdx];
        if (v != null) {
          const y = plotH - ((v - lo) / (hi - lo)) * plotH;
          if (step) { ctx.lineTo(x, prevY); ctx.lineTo(x, y); }
          else ctx.lineTo(x, y);
        }
      }
      prevX = w + 1;
      break;
    }
    const v = p[colIdx];
    if (v == null) continue;
    const x = ((p[0] - t0) / (t1 - t0)) * w;
    const y = plotH - ((v - lo) / (hi - lo)) * plotH;
    if (!started) { ctx.moveTo(x, y); started = true; }
    else if (step && prevY != null) { ctx.lineTo(x, prevY); ctx.lineTo(x, y); }
    else ctx.lineTo(x, y);
    prevY = y;
    prevX = x;
  }
  // Hold the last value flat up to "now" (a price stays valid until the
  // next sample) — a paused feed must not blank the chart.
  if (started && extendToX != null && prevX != null && prevX < extendToX) {
    ctx.lineTo(extendToX, prevY);
  }
  ctx.stroke();
}

function rangeOf(seriesList, t0, t1, extra) {
  let lo = Infinity, hi = -Infinity;
  for (const { series, colIdx } of seriesList) {
    const start = visibleWindow(series, t0);
    for (let i = start; i < series.length; i++) {
      if (series[i][0] > t1 && i > start) break;
      const v = series[i][colIdx];
      if (v == null) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  for (const v of extra) {
    if (v == null) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (!isFinite(lo) || !isFinite(hi)) return null;
  if (lo === hi) { lo -= Math.abs(lo) * 1e-4 || 1; hi += Math.abs(hi) * 1e-4 || 1; }
  const pad = (hi - lo) * 0.1;
  return [lo - pad, hi + pad];
}

function actualTwapAt(target, tolMs = 3000) {
  // Nearest recorded rolling-TWAP value to `target` (binary search).
  const s = bus.twaps;
  if (!s.length) return null;
  let lo = 0, hi = s.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (s[mid][0] < target) lo = mid + 1; else hi = mid;
  }
  let best = null, bestDt = tolMs + 1;
  for (const i of [lo - 1, lo]) {
    if (i < 0 || i >= s.length) continue;
    const dt = Math.abs(s[i][0] - target);
    if (dt < bestDt) { bestDt = dt; best = s[i][1]; }
  }
  return best;
}

function drawPredDots(ctx, w, plotH, t0, t1, lo, hi, now) {
  for (const p of bus.preds) {
    if (p.t < t0 || p.t > t1 || p.v == null) continue;
    if (p.v < lo || p.v > hi) continue;
    const x = ((p.t - t0) / (t1 - t0)) * w;
    const y = plotH - ((p.v - lo) / (hi - lo)) * plotH;
    const isEnd = p.k === "end";
    const r = isEnd ? 4 : 2.5;
    if (p.t > now) {
      // Future: hollow cyan (end-of-round prediction = larger diamond)
      ctx.strokeStyle = COL.pred;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      if (isEnd) {
        ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y);
        ctx.closePath();
      } else {
        ctx.arc(x, y, r, 0, Math.PI * 2);
      }
      ctx.stroke();
    } else {
      // Past: filled, colored by how close the actual TWAP came.
      const actual = actualTwapAt(p.t);
      let col = COL.pred;
      if (actual != null && actual > 0) {
        const errBps = Math.abs((p.v - actual) / actual * 10_000);
        col = errBps <= PRED_HIT_BPS ? COL.predHit : COL.predMiss;
      }
      ctx.fillStyle = col;
      ctx.globalAlpha = 0.85;
      ctx.beginPath();
      if (isEnd) {
        ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y);
        ctx.closePath();
      } else {
        ctx.arc(x, y, r, 0, Math.PI * 2);
      }
      ctx.fill();
      ctx.globalAlpha = 1;
    }
  }
}

function priceTag(ctx, w, plotH, lo, hi, value, col, decimals) {
  if (value == null || value < lo || value > hi) return;
  const y = plotH - ((value - lo) / (hi - lo)) * plotH;
  const label = fmtPrice(value, decimals);
  ctx.font = "10px monospace";
  const tw = ctx.measureText(label).width + 8;
  ctx.fillStyle = col;
  ctx.fillRect(w - tw, y - 7, tw, 14);
  ctx.fillStyle = "#0b0e14";
  ctx.textAlign = "right";
  ctx.fillText(label, w - 4, y + 3);
}

// ---------- chart renderers ----------

export function renderTwapChart(ctx, w, h, view) {
  const snap = bus.snapshot;
  if (!snap) return;
  const decimals = snap.decimals;
  const [t0, t1, now] = timeDomain(view);
  const plotH = h - 14;

  const ptb = snap.window ? snap.window.ptb : null;
  const ptbSource = snap.window ? (snap.window.ptb_source || "") : "";
  // Fallback sources are approximations — not the true Polymarket TWAP at boundary
  const PTB_APPROX_SOURCES = new Set(["chainlink_last", "first_reference", "exchange_approx"]);
  const ptbApprox = PTB_APPROX_SOURCES.has(ptbSource);
  const extra = [ptb];
  for (const p of bus.preds) {
    if (p.t >= t0 && p.t <= t1 && p.v != null) extra.push(p.v);
  }
  const range = rangeOf(
    [
      { series: bus.refs, colIdx: 1 },
      { series: bus.twaps, colIdx: 1 },
      { series: bus.twaps, colIdx: 2 },
    ],
    t0, t1,
    extra,
  );
  if (!range) return;
  const [lo, hi] = range;

  drawTimeAxis(ctx, w, h, t0, t1, plotH);
  drawYGrid(ctx, w, plotH, lo, hi, decimals);

  // Round-boundary vertical lines (all rounds in view)
  if (snap.window) {
    const winMs = snap.window.end_ms - snap.window.start_ms;
    for (let b = Math.ceil(t0 / winMs) * winMs; b <= t1; b += winMs) {
      const x = ((b - t0) / (t1 - t0)) * w;
      ctx.strokeStyle = COL.axis;
      ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, plotH); ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  const nowX = drawNowLine(ctx, w, plotH, t0, t1, now);
  const holdX = nowX != null ? nowX : (now > t1 ? w : null);

  // Price-to-beat dashed line — dimmed when using a fallback source (approx)
  if (ptb != null && ptb >= lo && ptb <= hi) {
    const y = plotH - ((ptb - lo) / (hi - lo)) * plotH;
    ctx.strokeStyle = ptbApprox ? "rgba(245,185,66,0.35)" : COL.ptb;
    ctx.setLineDash(ptbApprox ? [3, 5] : [6, 4]);
    ctx.lineWidth = ptbApprox ? 1.0 : 1.2;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    ctx.setLineDash([]);
  }

  drawLine(ctx, bus.twaps, COL.twap30, t0, t1, lo, hi, w, plotH, 1, 2, false, holdX);
  drawLine(ctx, bus.refs, COL.chainlink, t0, t1, lo, hi, w, plotH, 1.5, 1, false, holdX);
  drawLine(ctx, bus.twaps, COL.twap, t0, t1, lo, hi, w, plotH, 2, 1, false, holdX);

  drawPredDots(ctx, w, plotH, t0, t1, lo, hi, now);

  priceTag(ctx, w, plotH, lo, hi, ptb, ptbApprox ? "rgba(245,185,66,0.45)" : COL.ptb, decimals);
  priceTag(ctx, w, plotH, lo, hi, snap.prices.chainlink, COL.chainlink, decimals);
  priceTag(ctx, w, plotH, lo, hi, snap.prices.twap, COL.twap, decimals);
}

export function renderTickChart(ctx, w, h, view) {
  const snap = bus.snapshot;
  if (!snap) return;
  const decimals = snap.decimals;
  const [t0, t1, now] = timeDomain(view);
  const volH = Math.round(h * 0.24);
  const plotH = h - 14 - volH;

  const range = rangeOf([{ series: bus.ticks, colIdx: 1 }], t0, t1, []);
  if (!range) return;
  const [lo, hi] = range;

  drawTimeAxis(ctx, w, h, t0, t1, plotH + volH);
  drawYGrid(ctx, w, plotH, lo, hi, decimals);
  const nowX = drawNowLine(ctx, w, plotH + volH, t0, t1, now);
  const holdX = nowX != null ? nowX : (now > t1 ? w : null);

  // Price line (held flat to "now" between trades)
  drawLine(ctx, bus.ticks, COL.price, t0, t1, lo, hi, w, plotH, 1, 1, false, holdX);

  // Tick dots sized by qty (skipped when a zoomed-out view has too many)
  const start = visibleWindow(bus.ticks, t0);
  let end = start;
  while (end < bus.ticks.length && bus.ticks[end][0] <= t1) end++;
  if (end - start <= MAX_VISIBLE_DOTS) {
    let maxQty = 0;
    for (let i = start; i < end; i++) {
      const q = bus.ticks[i][2];
      if (q > maxQty) maxQty = q;
    }
    for (let i = start; i < end; i++) {
      const [t, price, qty, side] = bus.ticks[i];
      const x = ((t - t0) / (t1 - t0)) * w;
      const y = plotH - ((price - lo) / (hi - lo)) * plotH;
      const r = maxQty > 0 ? 1 + 2.5 * Math.sqrt(qty / maxQty) : 1.5;
      ctx.fillStyle = side > 0 ? COL.buy : COL.sell;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  // Volume bars: bucket size scales with the span so bars stay readable.
  const bucketMs = Math.max(200, Math.round(view.spanMs / 120 / 100) * 100);
  const buckets = new Map();
  for (let i = start; i < end; i++) {
    const [t, , qty, side] = bus.ticks[i];
    if (t < t0) continue;
    const key = Math.floor(t / bucketMs);
    let b = buckets.get(key);
    if (!b) { b = { buy: 0, sell: 0 }; buckets.set(key, b); }
    if (side > 0) b.buy += qty; else b.sell += qty;
  }
  let maxVol = 0;
  for (const b of buckets.values()) {
    const tot = b.buy + b.sell;
    if (tot > maxVol) maxVol = tot;
  }
  if (maxVol > 0) {
    const barW = Math.max(1, (bucketMs / (t1 - t0)) * w - 1);
    const base = plotH + volH;
    for (const [key, b] of buckets) {
      const t = key * bucketMs;
      const x = ((t - t0) / (t1 - t0)) * w;
      const buyH = (b.buy / maxVol) * (volH - 4);
      const sellH = (b.sell / maxVol) * (volH - 4);
      ctx.fillStyle = COL.buy;
      ctx.fillRect(x, base - buyH, barW, buyH);
      ctx.fillStyle = COL.sell;
      ctx.fillRect(x, base - buyH - sellH, barW, sellH);
    }
  }

  priceTag(ctx, w, plotH, lo, hi, snap.prices.last_trade, COL.price, decimals);
}

export function renderQuoteChart(ctx, w, h) {
  const snap = bus.snapshot;
  if (!snap || !snap.window) return;
  const t0 = snap.window.start_ms;
  const t1 = snap.window.end_ms;
  const plotH = h - 14;
  const lo = 0, hi = 100;

  ctx.strokeStyle = COL.grid;
  ctx.fillStyle = COL.text;
  ctx.font = "9px monospace";
  ctx.textAlign = "right";
  for (const v of [25, 50, 75]) {
    const y = plotH - (v / 100) * plotH;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y);
    ctx.strokeStyle = v === 50 ? COL.axis : COL.grid;
    ctx.stroke();
    ctx.fillText(v + "¢", w - 4, y - 2);
  }
  drawTimeAxis(ctx, w, h, t0, t1, plotH);

  const now = bus.smoothNowMs();
  let nowX = null;
  if (now >= t0 && now <= t1) {
    nowX = ((now - t0) / (t1 - t0)) * w;
    ctx.strokeStyle = COL.nowLine;
    ctx.beginPath(); ctx.moveTo(nowX, 0); ctx.lineTo(nowX, plotH); ctx.stroke();
  }

  const quotes = bus.quotes.filter((q) => q.t >= t0 - 1000);
  if (quotes.length === 0) return;
  const bidSeries = quotes.map((q) => [q.t, q.up_bid]);
  const askSeries = quotes.map((q) => [q.t, q.up_ask]);
  const lastSeries = quotes.map((q) => [q.t, q.last_trade_up]);
  const holdX = nowX != null ? nowX : w;
  drawLine(ctx, askSeries, COL.ask, t0, t1, lo, hi, w, plotH, 1.2, 1, true, holdX);
  drawLine(ctx, bidSeries, COL.bid, t0, t1, lo, hi, w, plotH, 1.2, 1, true, holdX);
  drawLine(ctx, lastSeries, COL.lastTrade, t0, t1, lo, hi, w, plotH, 1, 1, true, holdX);

  // Bot trade markers: ▲ = buy, ▼ = exit/settle, colored by side.
  for (const m of quoteMarkers) {
    if (m.t < t0 || m.t > t1 || m.price_cents == null) continue;
    const x = ((m.t - t0) / (t1 - t0)) * w;
    const y = plotH - ((m.price_cents - lo) / (hi - lo)) * plotH;
    const col = m.side === "UP" ? COL.buy : COL.sell;
    const up = m.action === "BUY";
    ctx.fillStyle = col;
    ctx.strokeStyle = "#0b0e14";
    ctx.lineWidth = 1;
    ctx.beginPath();
    if (up) {
      ctx.moveTo(x, y - 5); ctx.lineTo(x + 4.5, y + 3.5); ctx.lineTo(x - 4.5, y + 3.5);
    } else {
      ctx.moveTo(x, y + 5); ctx.lineTo(x + 4.5, y - 3.5); ctx.lineTo(x - 4.5, y - 3.5);
    }
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  }
}
