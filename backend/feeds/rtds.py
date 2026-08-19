"""Polymarket RTDS feed: Chainlink reference price + 30s TWAP (READ-ONLY).

One WebSocket to ``wss://ws-live-data.polymarket.com``, both topics for a
slash-lowercase symbol (e.g. ``"xrp/usd"``):

- ``crypto_prices_chainlink`` — the EXACT price Polymarket displays and
  whose rolling-lookback TWAP resolves the up/down markets,
- ``crypto_prices_twap_thirty`` — rolling 30s Chainlink TWAP (display parity).

Protocol (verified live):
- subscribe: {"action":"subscribe","subscriptions":[{"topic":...,
  "type":"update","filters":"{\"symbol\":\"xrp/usd\"}"}]} — filters is a
  COMPACT double-encoded JSON string (a space breaks live updates!).
- backfill: {"topic":"crypto_prices","type":"subscribe","payload":
  {"symbol":..., "data":[{"timestamp":ms,"value":f}, ...]}} (~50 points).
- updates ~1/s: {"topic":"crypto_prices_chainlink","type":"update",
  "payload":{"symbol":...,"timestamp":ms,"value":f}}.
- keepalive: send text "PING" every ~10s, server answers "PONG".
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator

import websockets

from backend.config import CONFIG
from backend.feeds.binance import backoff_delay_ms
from backend.feeds.clock import OffsetTracker

logger = logging.getLogger(__name__)

# RTDS pushes chainlink updates ~1/s. A socket that answers PONG but
# stops sending updates is a SILENT stall (observed live) — reconnect
# when nothing parseable arrived for this long.
STALL_TIMEOUT_S = 30.0

TOPIC_CHAINLINK = "crypto_prices_chainlink"
TOPIC_TWAP30 = "crypto_prices_twap_thirty"


def subscribe_frame(symbol: str) -> str:
    filters = json.dumps({"symbol": symbol}, separators=(",", ":"))
    return json.dumps({
        "action": "subscribe",
        "subscriptions": [
            {"topic": TOPIC_CHAINLINK, "type": "update", "filters": filters},
            {"topic": TOPIC_TWAP30, "type": "update", "filters": filters},
        ],
    })


def _parse_point(entry) -> tuple[float, float] | None:
    if not isinstance(entry, dict):
        return None
    try:
        ts_ms = float(entry["timestamp"])
        value = float(entry["value"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return ts_ms, value


def parse_frame(raw) -> tuple[str, str, list[tuple[float, float]]] | None:
    """-> ("chainlink"|"twap30"|"backfill", symbol, [(ts_ms, value), ...])."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not text or text == "PONG":
            return None
        try:
            msg = json.loads(text)
        except ValueError:
            return None
        if not isinstance(msg, dict):
            return None
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return None
        symbol = payload.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            return None
        frame_type = msg.get("type")
        if frame_type == "subscribe":
            data = payload.get("data")
            if not isinstance(data, list):
                return None
            points = [p for p in (_parse_point(e) for e in data) if p is not None]
            return "backfill", symbol, points
        if frame_type == "update":
            topic = msg.get("topic")
            if topic == TOPIC_CHAINLINK:
                kind = "chainlink"
            elif topic == TOPIC_TWAP30:
                kind = "twap30"
            else:
                return None
            point = _parse_point(payload)
            if point is None:
                return None
            return kind, symbol, [point]
        return None
    except Exception:
        return None


class RTDSFeed:
    """Yield ("chainlink"|"twap30"|"backfill", receive_ms, points) forever."""

    def __init__(self, symbol: str) -> None:
        self._symbol = symbol.strip().lower()
        self.connected = False
        self._offset = OffsetTracker()

    @property
    def clock_offset_ms(self) -> float | None:
        """Robust (receive - payload) offset; None until warm."""
        return self._offset.value

    async def stream(self) -> AsyncIterator[tuple[str, float, list[tuple[float, float]]]]:
        attempt = 0
        while True:
            try:
                async with websockets.connect(
                    CONFIG.rtds_ws_url,
                    ping_interval=None,  # app-level "PING" per RTDS protocol
                    open_timeout=15,
                    close_timeout=5,
                ) as ws:
                    await ws.send(subscribe_frame(self._symbol))
                    parsed_any = False
                    next_ping = time.monotonic() + CONFIG.ping_interval_s
                    last_data = time.monotonic()
                    while True:
                        now = time.monotonic()
                        if now - last_data > STALL_TIMEOUT_S:
                            raise ConnectionError("rtds silent stall (no updates)")
                        if now >= next_ping:
                            await ws.send("PING")
                            next_ping = now + CONFIG.ping_interval_s
                        timeout = max(0.05, min(1.0, next_ping - now))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            continue
                        receive_ms = time.time() * 1000.0
                        parsed = parse_frame(raw)
                        if parsed is None:
                            continue
                        last_data = time.monotonic()
                        if not parsed_any:
                            parsed_any = True
                            self.connected = True
                            if attempt:
                                logger.info("rtds %s reconnected", self._symbol)
                            attempt = 0
                        kind, symbol, points = parsed
                        if symbol.strip().lower() != self._symbol:
                            continue
                        if kind != "backfill" and points:
                            self._offset.add(receive_ms, points[0][0])
                        yield kind, receive_ms, points
                raise ConnectionError("server closed the connection")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                attempt += 1
                delay = backoff_delay_ms(attempt)
                if attempt == 1:
                    logger.warning(
                        "rtds %s feed lost (%s: %s); retry in %d ms",
                        self._symbol, type(exc).__name__, exc, delay,
                    )
                await asyncio.sleep(delay / 1000.0)
