// Formatting helpers.

export function fmtPrice(v, decimals) {
  if (v == null || !isFinite(v)) return "—";
  return v.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function fmtSigned(v, decimals) {
  if (v == null || !isFinite(v)) return "—";
  const s = fmtPrice(Math.abs(v), decimals);
  return (v > 0 ? "+" : v < 0 ? "−" : "") + s;
}

export function fmtQty(v) {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return v.toFixed(a >= 10 ? 1 : 2);
}

export function fmtUsd(v) {
  if (v == null || !isFinite(v)) return "—";
  const sign = v < 0 ? "−" : "";
  return sign + "$" + fmtQty(Math.abs(v));
}

export function fmtBps(v) {
  if (v == null || !isFinite(v)) return "—";
  return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(1) + " bps";
}

export function fmtCents(v) {
  if (v == null || !isFinite(v)) return "—";
  return v.toFixed(1) + "¢";
}

export function fmtClock(ms) {
  const d = new Date(ms);
  return d.toLocaleTimeString("en-US", { hour12: false });
}

export function fmtCountdown(ms) {
  if (ms == null || ms < 0) ms = 0;
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

export function deltaClass(v) {
  if (v == null || !isFinite(v) || v === 0) return "flat";
  return v > 0 ? "up" : "down";
}

export function windowName(ms) {
  if (ms >= 60000) return (ms / 60000) + "m";
  if (ms >= 1000) return (ms / 1000) + "s";
  return ms + "ms";
}
