# Twap Hunter

Real-time tick-level research terminal for scalping Polymarket's 5-minute
crypto **Up or Down** markets (XRP, BTC, ETH, SOL, DOGE) using the TWAP lag
edge: the real market moves first, the Polymarket TWAP chart follows.

## What it shows (all live, tick level)

| Panel | Data | Source |
|---|---|---|
| Oracle / TWAP chart | Chainlink reference price, rolling 60s TWAP (the exact quantity the round resolves on), 30s TWAP, **Price To Beat** line, round boundaries | Polymarket RTDS (`wss://ws-live-data.polymarket.com`) |
| Exchange ticks chart | Every trade with aggressor side, dot size ∝ quantity, 500ms buy/sell volume bars | Binance `{sym}@trade` WebSocket |
| UP quote chart | UP bid/ask/last over the whole round (0–100¢) | Polymarket CLOB market WebSocket |
| Prices | Chainlink, Binance last, TWAP 60s, TWAP 30s, Price To Beat + source | |
| Deltas | **TWAP Δ vs PTB** (resolves the round) and oracle price Δ, in $ and bps | |
| Session volume | Buy / Sell / Net volume ($ and units), trade count, session imbalance + rolling 1s/5s/30s imbalance | reset every round |
| Volume pressure | −100..+100 weighted score (imbalance 40%, tick direction 25%, momentum 25%, tick velocity 10%) with component breakdown | |
| Momentum | Price change over 250ms/500ms/1s/2s/5s in $ and bps, tick velocity, up-tick ratio | |
| Polymarket | Round question, UP/DOWN bid/ask + sizes, last trade, spread | |
| TWAP prediction | Every 5s the rolling TWAP is projected forward (+30s dot and a round-end diamond) by holding the current Chainlink price — the rolling integral makes the near future largely deterministic. Dots land in the future space right of the now-line and self-score when time reaches them (green = actual TWAP within 1 bp, red = miss). The panel tracks +30s accuracy and round-end direction hit rate for the model vs the market's own implied prediction (UP mid > 50¢). | |
| Event timeline | Round rolls, PTB latches, TWAP target crosses, momentum spikes, volume spikes, strong pressure, quote swings, market discovery, resolutions | notification box, newest first |

**Price To Beat**: latched as the rolling lookback TWAP at the round
boundary (Polymarket parity — "price at the beginning of the range" IS the
rolling TWAP at range start). On a cold start mid-round it is approximated
from the RTDS backfill when possible, or left blank until the next round.

## Architecture

```
Binance trade WS      Polymarket RTDS WS       Polymarket Gamma REST + CLOB WS
 (ticks, aggressor)    (Chainlink + TWAP30)     (market discovery, books, resolution)
        │                     │                          │
        ▼                     ▼                          ▼
              backend/engine/pipeline.py  (asyncio, one pipeline per asset)
     ring buffers · volume/momentum/pressure engines · 5m window + PTB · events
                          │
                          ▼  batched every 50 ms (no per-tick JSON)
              FastAPI WebSocket  /ws   ─►  frontend (vanilla JS + canvas)
                                            rAF-rendered charts, zero frameworks
```

- **Hot path** per tick: buffer append + O(small-window) metric updates.
  Pressure/velocity math is throttled to 10 Hz.
- The UI receives batches every 50 ms; charts render on their own
  `requestAnimationFrame` loop from ring buffers — a burst of hundreds of
  ticks/second never causes DOM churn.
- The countdown is computed from the server round boundary plus a measured
  clock offset — never a blindly decremented timer.
- All feeds reconnect forever with backoff. Everything is READ-ONLY market
  data; there is no order code, no keys, no wallet anywhere.

## Run

Requirements: Python 3.12+ (no Node needed).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r backend/requirements.txt
.venv/Scripts/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8010
```

Open http://127.0.0.1:8010 — the XRP terminal loads immediately. Switch
assets with the tabs in the top bar (the backend swaps its feeds to the
selected series; all connected browsers follow).

## Layout

- `backend/config.py` — asset registry (Binance symbol / RTDS symbol /
  Gamma series slug per asset) and all tunables.
- `backend/feeds/` — `binance.py`, `rtds.py`, `polymarket.py` (discovery,
  order books, resolution).
- `backend/engine/metrics.py` — pure metric engines (rolling TWAP, volume,
  momentum, pressure).
- `backend/engine/pipeline.py` — wiring, 5m window, PTB, event detection,
  wire payloads.
- `backend/main.py` — FastAPI app + WebSocket hub + asset switching.
- `frontend/` — static, no build step: `js/bus.js` (WS + ring buffers),
  `js/chart.js` (canvas engine), `js/panels.js` (DOM panels + timeline).

## Paper-trading bots (/bots)

The **/bots** page manages automated paper-trading bots per market —
"+ ADD BOT" picks a strategy, market, and parameters. Everything is
simulation: fills execute against the REAL displayed CLOB top-of-book
(price and size), Polymarket's crypto taker fee is modeled
(`7% × shares × min(p, 1−p)`, taker only — it is brutal near 50¢ and this
matters for strategy design), and rounds settle by the real rule
(final TWAP ≥ price-to-beat → UP). No real orders exist anywhere.

Strategies:
- **TWAP Lock-In** — enter in the last N seconds when the projected final
  TWAP clears the PTB by a margin but the side still sells below a max
  price. The verified statistical edge.
- **Pressure Scalp** — enter on strong order-flow pressure, cents-based TP/SL.
- **Momentum Rider** — enter on sharp 1s spot moves before quotes reprice.
- **Cross Hunter** — buy the side the TWAP just crossed to.

Running bots keep their market's data pipeline alive without any browser
tab, persist across restarts (`data/bots.json`), and log every action
with its reason. Real execution, if ever added, will be a separate,
explicitly armed layer.
