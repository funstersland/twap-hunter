"""Binance trade feed (READ-ONLY public market data).

Wire shape (verified live): ``{"e":"trade","E":ms,"s":"XRPUSDT","t":id,
"p":"3.0701","q":"812","T":ms,"m":bool}``. ``m`` is buyer-is-maker, so
the AGGRESSOR side is "sell" when true, "buy" when false.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

import websockets

from backend.config import CONFIG, Asset
from backend.feeds.clock import OffsetTracker

logger = logging.getLogger(__name__)


def backoff_delay_ms(attempt: int) -> int:
    schedule = CONFIG.reconnect_backoff_ms
    index = min(max(attempt, 1) - 1, len(schedule) - 1)
    return schedule[index]


def parse_trade(msg: dict) -> tuple[float, float, float, str] | None:
    """-> (exchange_ts_ms, price, qty, aggressor_side) or None."""
    if not isinstance(msg, dict) or msg.get("e") != "trade":
        return None
    try:
        price = float(msg["p"])
        qty = float(msg["q"])
        ts_ms = float(msg["T"])
        buyer_is_maker = bool(msg["m"])
    except (KeyError, TypeError, ValueError):
        return None
    if price <= 0 or qty < 0:
        return None
    return ts_ms, price, qty, ("sell" if buyer_is_maker else "buy")


class BinanceTradeFeed:
    """Reconnect-forever trade stream for one symbol."""

    def __init__(self, asset: Asset) -> None:
        self._asset = asset
        base = (
            CONFIG.binance_futures_base
            if asset.binance_venue == "futures"
            else CONFIG.binance_spot_base
        )
        self._url = f"{base}/ws/{asset.binance_symbol}@trade"
        self.connected = False
        self._offset = OffsetTracker()

    @property
    def clock_offset_ms(self) -> float | None:
        """Robust (receive - exchange) offset; None until warm."""
        return self._offset.value

    async def stream(self) -> AsyncIterator[tuple[float, float, float, float, str]]:
        """Yield (receive_ms, exchange_ms, price, qty, side) forever."""
        attempt = 0
        while True:
            try:
                async with websockets.connect(self._url, open_timeout=15, close_timeout=5) as ws:
                    parsed_any = False
                    async for raw in ws:
                        receive_ms = time.time() * 1000.0
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        parsed = parse_trade(msg)
                        if parsed is None:
                            continue
                        if not parsed_any:
                            parsed_any = True
                            self.connected = True
                            if attempt:
                                logger.info("binance %s reconnected", self._asset.key)
                            attempt = 0
                        ts_ms, price, qty, side = parsed
                        self._offset.add(receive_ms, ts_ms)
                        yield receive_ms, ts_ms, price, qty, side
                raise ConnectionError("server closed the connection")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                attempt += 1
                delay = backoff_delay_ms(attempt)
                if attempt == 1:
                    logger.warning(
                        "binance %s feed lost (%s: %s); retry in %d ms",
                        self._asset.key, type(exc).__name__, exc, delay,
                    )
                await asyncio.sleep(delay / 1000.0)
