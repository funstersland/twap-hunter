// Twap Hunter — bootstrap.

import { bus, connect } from "./bus.js";
import { makeChart, renderTwapChart, renderTickChart, renderQuoteChart } from "./chart.js";
import { renderTabs, renderPanels, renderCountdown, appendEvents, resetTimeline } from "./panels.js";

const twapChart = makeChart(document.getElementById("chart-twap"), renderTwapChart, {
  pan: true,
  defaultSpan: 150_000,
  minSpan: 30_000,
  maxSpan: 600_000,
  maxPanMs: 11 * 60_000,
});
const tickChart = makeChart(document.getElementById("chart-ticks"), renderTickChart, {
  pan: true,
  defaultSpan: 60_000,
  minSpan: 10_000,
  maxSpan: 180_000,
  maxPanMs: 5 * 60_000,
});
const quoteChart = makeChart(document.getElementById("chart-quotes"), renderQuoteChart);

// LIVE buttons: visible while a chart is panned away from now.
function attachLiveButton(chart, canvasId) {
  const card = document.getElementById(canvasId).parentElement;
  const btn = document.createElement("button");
  btn.className = "live-btn";
  btn.textContent = "▶ LIVE";
  btn.onclick = () => chart.goLive();
  card.appendChild(btn);
  return () => btn.classList.toggle("visible", chart.view.panMs > 0);
}
const liveButtons = [
  attachLiveButton(twapChart, "chart-twap"),
  attachLiveButton(tickChart, "chart-ticks"),
];

bus.on("init", () => {
  renderTabs();
  renderPanels();
  resetTimeline();
});

bus.on("batch", () => {
  renderPanels();
});

bus.on("events", (events) => {
  appendEvents(events);
});

// Charts redraw on their own rAF loop — decoupled from batch cadence.
function renderCharts() {
  twapChart.render();
  tickChart.render();
  quoteChart.render();
  for (const update of liveButtons) update();
}
function frame() {
  renderCharts();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// Debug handle (also lets tooling force a paint while backgrounded).
window.__twapHunter = { bus, renderCharts, twapChart, tickChart, quoteChart };

// Countdown + round progress at 10 Hz.
setInterval(renderCountdown, 100);

connect();
