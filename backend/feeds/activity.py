"""Polymarket platform activity feed (READ-ONLY) — powers copy trading.

One RTDS websocket subscribed to the public ``activity`` topic streams
EVERY trade on the platform in real time (~100-500ms after the fill):
taker wallet, side, outcome, price, size, event slug. We keep only the
5-minute up/down crypto series and expose per-wallet queries.

Verified live (2026-08-17): frames are
``{"topic":"activity","type":"trades","payload":{"proxyWallet":...,
"side":"BUY","outcome":"Down","price":0.83,"size":464,
"eventSlug":"btc-updown-5m-1786987800","timestamp":...,...}}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque

import websockets

from backend.config import CONFIG

logger = logging.getLogger(__name__)

STALL_TIMEOUT_S = 45.0   # platform-wide trades flow constantly
KEEP_TRADES = 8000       # ALL platform trades now (copy bots follow anywhere)


def parse_activity_frame(raw) -> dict | None:
    """One frame -> normalized trade dict, or None. Never raises."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        text = raw.strip() if isinstance(raw, str) else ""
        if not text or text == "PONG":
            return None
        msg = json.loads(text)
        payload = msg.get("payload") if isinstance(msg, dict) else None
        if not isinstance(payload, dict):
            return None
        slug = str(payload.get("eventSlug") or "")
        wallet = str(payload.get("proxyWallet") or payload.get("wallet") or "").lower()
        if not wallet.startswith("0x"):
            return None
        outcome_raw = str(payload.get("outcome") or "")
        if not outcome_raw:
            return None
        side = str(payload.get("side") or "BUY").upper()
        if side not in ("BUY", "SELL"):
            return None
        price = float(payload.get("price"))
        size = float(payload.get("size"))
        ts = payload.get("timestamp")
        ts_ms = None
        if ts is not None:
            ts_f = float(ts)
            ts_ms = ts_f * 1000.0 if ts_f < 1e12 else ts_f
        try:
            outcome_index = int(payload.get("outcomeIndex"))
        except (TypeError, ValueError):
            outcome_index = None
        return {
            "t_ms": ts_ms if ts_ms is not None else time.time() * 1000.0,
            "recv_ms": time.time() * 1000.0,
            "wallet": wallet,
            "side": side,
            # UP/DOWN for the crypto series; arbitrary label otherwise
            "outcome": outcome_raw.upper() if outcome_raw.upper() in ("UP", "DOWN") else outcome_raw,
            "outcome_index": outcome_index,
            "price_cents": round(price * 100.0, 2),
            "size": size,
            "event_slug": slug,
            "condition_id": str(payload.get("conditionId") or ""),
            "token_id": str(payload.get("asset") or ""),
            "title": str(payload.get("title") or payload.get("question") or ""),
            "name": str(payload.get("name") or payload.get("pseudonym") or ""),
        }
    except Exception:
        return None


class ActivityFeed:
    """Reconnect-forever activity stream shared by all copy bots."""

    def __init__(self) -> None:
        self.trades: deque[dict] = deque(maxlen=KEEP_TRADES)
        self.connected = False
        self.last_frame_ms = 0.0
        self._task: asyncio.Task | None = None
        self._seq = 0   # monotonically increasing id for cheap "since" queries

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="activity-feed")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def since(self, seq: int, wallet: str | None = None,
              slug: str | None = None) -> tuple[int, list[dict]]:
        """Trades newer than ``seq`` (optionally filtered). Returns
        (latest_seq, trades oldest-first).

        Walks BACKWARDS from the newest trade and stops at ``seq`` —
        O(new trades) per call, which is what lets ~100 copy bots poll
        this at 4 Hz without scanning the whole buffer each time.
        """
        out = []
        w = wallet.lower() if wallet else None
        for tr in reversed(self.trades):
            if tr["_seq"] <= seq:
                break
            if w is not None and tr["wallet"] != w:
                continue
            if slug is not None and tr["event_slug"] != slug:
                continue
            out.append(tr)
        out.reverse()
        return max(seq, self._seq), out

    @property
    def latest_seq(self) -> int:
        return self._seq

    async def _run(self) -> None:
        attempt = 0
        sub = json.dumps({
            "action": "subscribe",
            "subscriptions": [{"topic": "activity", "type": "trades", "filters": ""}],
        })
        while True:
            try:
                async with websockets.connect(
                    CONFIG.rtds_ws_url,
                    ping_interval=None,
                    open_timeout=15,
                    close_timeout=5,
                ) as ws:
                    await ws.send(sub)
                    last_data = time.monotonic()
                    next_ping = time.monotonic() + CONFIG.ping_interval_s
                    while True:
                        now = time.monotonic()
                        if now - last_data > STALL_TIMEOUT_S:
                            raise ConnectionError("activity feed silent stall")
                        if now >= next_ping:
                            await ws.send("PING")
                            next_ping = now + CONFIG.ping_interval_s
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=max(0.05, min(1.0, next_ping - now)))
                        except asyncio.TimeoutError:
                            continue
                        trade = parse_activity_frame(raw)
                        self.last_frame_ms = time.time() * 1000.0
                        last_data = time.monotonic()
                        if not self.connected:
                            self.connected = True
                            if attempt:
                                logger.info("activity feed reconnected")
                            attempt = 0
                        if trade is None:
                            continue
                        self._seq += 1
                        trade["_seq"] = self._seq
                        self.trades.append(trade)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                attempt += 1
                delay = CONFIG.reconnect_backoff_ms[
                    min(attempt - 1, len(CONFIG.reconnect_backoff_ms) - 1)]
                if attempt == 1:
                    logger.warning(
                        "activity feed lost (%s: %s); retry in %d ms",
                        type(exc).__name__, exc, delay)
                await asyncio.sleep(delay / 1000.0)
