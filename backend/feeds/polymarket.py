"""Read-only Polymarket feed: Gamma discovery + CLOB market WebSocket.

STRICTLY READ-ONLY market data. No orders, wallets, keys, or auth.

- Discovery: ``GET {gamma}/events?series_slug=...&closed=false&
  end_date_min=<now>&order=endDate&ascending=true`` — first market whose
  round (endDate - 5min .. endDate) contains now is the live one.
  ``clobTokenIds`` index 0 = Up, 1 = Down. ``cryptoMarketConfig.
  twapLookbackSeconds`` tunes the resolution TWAP lookback.
- Quotes: ``wss://ws-subscriptions-clob.polymarket.com/ws/market``,
  subscribe ``{"assets_ids": [...], "type": "market"}``; ``book``
  snapshots + ``price_change`` deltas + ``last_trade_price``; app-level
  PING/PONG every ~10s.
- Resolution: poll ``GET {gamma}/markets?condition_ids=...`` after the
  round ends; ``outcomePrices`` '["1","0"]' (index 0 = Up) decisive.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import httpx
import websockets

from backend.config import CONFIG, Asset
from backend.feeds.binance import backoff_delay_ms

logger = logging.getLogger(__name__)


def _iso_to_ms(value: Any) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000.0
    except (ValueError, OSError, OverflowError):
        return None


def _parse_twap_lookback_s(market: Any) -> int | None:
    try:
        cfg = market.get("cryptoMarketConfig")
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        if not isinstance(cfg, dict):
            return None
        raw = cfg.get("twapLookbackSeconds")
        if isinstance(raw, bool):
            return None
        lookback = int(raw)
        return lookback if lookback > 0 else None
    except (TypeError, ValueError, AttributeError):
        return None


def parse_live_market(events: Any, now_ms: float, window_ms: int) -> dict | None:
    """Pure: pick the live round from a Gamma /events response.

    Round start is derived as ``endDate - window`` (market ``startDate``
    is the CREATION time, ~1 day early — not the round start).
    """
    if not isinstance(events, list):
        return None
    fallback: dict | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            end_ms = _iso_to_ms(market.get("endDate") or event.get("endDate"))
            if end_ms is None or now_ms >= end_ms:
                continue
            token_ids = market.get("clobTokenIds")
            if isinstance(token_ids, str):
                try:
                    token_ids = json.loads(token_ids)
                except ValueError:
                    continue
            if not isinstance(token_ids, list) or len(token_ids) < 2:
                continue
            start_ms = end_ms - window_ms
            info = {
                "slug": str(market.get("slug") or ""),
                "question": str(market.get("question") or ""),
                "condition_id": str(market.get("conditionId") or ""),
                "up_token": str(token_ids[0]),
                "down_token": str(token_ids[1]),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "twap_lookback_s": _parse_twap_lookback_s(market),
            }
            if start_ms <= now_ms:
                return info  # current round
            if fallback is None:
                fallback = info  # next round (pre-open) as a fallback
    return fallback


def parse_resolution(markets: Any) -> str | None:
    """Pure: "UP" | "DOWN" from a Gamma /markets response, else None."""
    if isinstance(markets, dict):
        markets = [markets]
    if not isinstance(markets, list):
        return None
    for market in markets:
        if not isinstance(market, dict):
            continue
        prices = market.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except ValueError:
                prices = None
        values: list[float] = []
        if isinstance(prices, list):
            try:
                values = [float(x) for x in prices]
            except (TypeError, ValueError):
                values = []
        if len(values) < 2:
            continue
        resolved = str(market.get("umaResolutionStatus") or "").lower() == "resolved"
        decisive = max(values[0], values[1]) >= 0.999
        if not (resolved or decisive):
            continue
        if values[0] >= 0.999 and values[1] <= 0.001:
            return "UP"
        if values[1] >= 0.999 and values[0] <= 0.001:
            return "DOWN"
    return None


class TokenBook:
    """Price-level book {cents: shares} for one outcome token. Pure."""

    __slots__ = ("_bids", "_asks")

    def __init__(self) -> None:
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}

    @staticmethod
    def to_cents(price: Any) -> float | None:
        try:
            return round(float(price) * 100.0, 4)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _parse_levels(cls, levels: Any) -> dict[float, float]:
        out: dict[float, float] = {}
        if not isinstance(levels, list):
            return out
        for lvl in levels:
            if not isinstance(lvl, dict):
                continue
            price = cls.to_cents(lvl.get("price"))
            try:
                size = float(lvl.get("size"))
            except (TypeError, ValueError):
                continue
            if price is None or size <= 0:
                continue
            out[price] = size
        return out

    def clear(self) -> None:
        self._bids.clear()
        self._asks.clear()

    def apply_snapshot(self, bids: Any, asks: Any) -> None:
        self._bids = self._parse_levels(bids)
        self._asks = self._parse_levels(asks)

    def apply_delta(self, price: Any, size: Any, side: Any) -> None:
        cents = self.to_cents(price)
        try:
            shares = float(size)
        except (TypeError, ValueError):
            return
        if cents is None:
            return
        book = self._bids if str(side).upper().startswith("B") else self._asks
        if shares <= 0:
            book.pop(cents, None)
        else:
            book[cents] = shares

    def best_bid(self) -> tuple[float, float] | None:
        if not self._bids:
            return None
        price = max(self._bids)
        return (price, self._bids[price])

    def best_ask(self) -> tuple[float, float] | None:
        if not self._asks:
            return None
        price = min(self._asks)
        return (price, self._asks[price])


class PolymarketFeed:
    """Discovery + resolution tasks and the CLOB quote stream."""

    def __init__(self, asset: Asset) -> None:
        self._asset = asset
        self.market: dict | None = None
        self.resolution: dict | None = None   # consumed by the pipeline
        self.connected = False
        self._books = {"UP": TokenBook(), "DOWN": TokenBook()}
        self._token_side: dict[str, str] = {}
        self._last_trade_up_cents: float | None = None
        self._resubscribe = asyncio.Event()
        self._pending: dict | None = None
        self._last_resolved_slug: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task] = []
        self.on_new_market = None            # callback(market) set by pipeline

    async def start(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(timeout=15.0)
        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="pm-discovery"),
            asyncio.create_task(self._resolution_loop(), name="pm-resolution"),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    # -- discovery -------------------------------------------------------

    async def _discovery_loop(self) -> None:
        while True:
            try:
                await self._discover_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("polymarket discovery error: %r", exc)
            # Poll fast near a round boundary so the next market is
            # adopted within ~1s of the roll.
            market = self.market
            now_ms = time.time() * 1000.0
            if market is None or now_ms >= market["end_ms"] - 3000:
                delay = 1.0
            else:
                delay = CONFIG.discovery_interval_ms / 1000.0
                delay = min(delay, max(1.0, (market["end_ms"] - 3000 - now_ms) / 1000.0))
            await asyncio.sleep(delay)

    async def _discover_once(self) -> None:
        client = self._client
        if client is None:
            return
        now = datetime.now(timezone.utc)
        resp = await client.get(
            f"{CONFIG.gamma_base_url}/events",
            params={
                "series_slug": self._asset.series_slug,
                "closed": "false",
                "limit": 10,
                "order": "endDate",
                "ascending": "true",
                "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        resp.raise_for_status()
        market = parse_live_market(resp.json(), now.timestamp() * 1000.0, CONFIG.window_ms)
        if market is None:
            return
        self._install_market(market)

    def _install_market(self, market: dict) -> None:
        current = self.market
        if current is not None and current["slug"] == market["slug"]:
            return
        if (
            current is not None
            and current["slug"] != self._last_resolved_slug
        ):
            self._pending = current
        self.market = market
        self._token_side = {market["up_token"]: "UP", market["down_token"]: "DOWN"}
        for book in self._books.values():
            book.clear()
        self._last_trade_up_cents = None
        self._resubscribe.set()
        logger.info("polymarket live market %s — %s", market["slug"], market["question"])
        if self.on_new_market is not None:
            try:
                self.on_new_market(market)
            except Exception:
                pass

    # -- resolution ------------------------------------------------------

    async def _resolution_loop(self) -> None:
        while True:
            await asyncio.sleep(CONFIG.resolution_poll_interval_ms / 1000.0)
            try:
                await self._poll_resolution_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("polymarket resolution error: %r", exc)

    async def _poll_resolution_once(self) -> None:
        if self.resolution is not None:
            return
        now_ms = time.time() * 1000.0
        target = self._pending
        if target is not None and target.get("slug") == self._last_resolved_slug:
            self._pending = None
            target = None
        if target is None:
            current = self.market
            if (
                current is not None
                and now_ms >= current["end_ms"]
                and current.get("slug") != self._last_resolved_slug
            ):
                target = current
        if target is None or now_ms < target["end_ms"]:
            return
        if now_ms > target["end_ms"] + CONFIG.resolution_give_up_ms:
            if target is self._pending:
                self._pending = None
            return
        client = self._client
        if client is None or not target.get("condition_id"):
            return
        resp = await client.get(
            f"{CONFIG.gamma_base_url}/markets",
            params={"condition_ids": target["condition_id"]},
        )
        resp.raise_for_status()
        outcome = parse_resolution(resp.json())
        if outcome is None:
            return
        self.resolution = {
            "outcome": outcome,
            "slug": target.get("slug"),
            "question": target.get("question"),
            "t_ms": now_ms,
        }
        self._last_resolved_slug = target.get("slug")
        if target is self._pending:
            self._pending = None
        logger.info("polymarket %s resolved %s", target.get("slug"), outcome)

    # -- quotes ----------------------------------------------------------

    async def stream_quotes(self) -> AsyncIterator[dict]:
        """Reconnect forever; yield a quote dict on every top-of-book change."""
        attempt = 0
        while True:
            self._resubscribe.clear()
            market = self.market
            if market is None:
                await asyncio.sleep(0.25)
                continue
            tokens = [market["up_token"], market["down_token"]]
            rolled = False
            try:
                async with websockets.connect(
                    CONFIG.clob_ws_url,
                    ping_interval=None,
                    open_timeout=15,
                    close_timeout=5,
                ) as ws:
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                    got_message = False
                    next_ping = time.monotonic() + CONFIG.ping_interval_s
                    while True:
                        if self._resubscribe.is_set():
                            rolled = True
                            break
                        now = time.monotonic()
                        if now >= next_ping:
                            await ws.send("PING")
                            next_ping = now + CONFIG.ping_interval_s
                        timeout = max(0.05, min(1.0, next_ping - now))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            continue
                        receive_ms = time.time() * 1000.0
                        if not got_message:
                            got_message = True
                            self.connected = True
                            attempt = 0
                        quote = self._process_raw(raw, receive_ms)
                        if quote is not None:
                            yield quote
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                logger.debug("polymarket ws error: %r", exc)
            if rolled:
                continue
            attempt += 1
            await asyncio.sleep(backoff_delay_ms(attempt) / 1000.0)

    def _process_raw(self, raw: Any, receive_ms: float) -> dict | None:
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="replace")
            text = raw.strip() if isinstance(raw, str) else ""
            if not text or text == "PONG":
                return None
            try:
                payload = json.loads(text)
            except ValueError:
                return None
            events = payload if isinstance(payload, list) else [payload]
            before = self._top_snapshot()
            for evt in events:
                if isinstance(evt, dict):
                    self._apply_event(evt)
            if self._top_snapshot() == before:
                return None
            return self._build_quote(receive_ms)
        except Exception:
            return None

    def _apply_event(self, evt: dict) -> None:
        kind = evt.get("event_type")
        if kind == "book":
            side = self._token_side.get(str(evt.get("asset_id")))
            if side is not None:
                self._books[side].apply_snapshot(evt.get("bids"), evt.get("asks"))
                if side == "UP":
                    cents = TokenBook.to_cents(evt.get("last_trade_price"))
                    if cents is not None:
                        self._last_trade_up_cents = cents
        elif kind == "price_change":
            changes = evt.get("price_changes")
            if isinstance(changes, list):
                for pc in changes:
                    if not isinstance(pc, dict):
                        continue
                    side = self._token_side.get(str(pc.get("asset_id")))
                    if side is not None:
                        self._books[side].apply_delta(
                            pc.get("price"), pc.get("size"), pc.get("side")
                        )
            else:
                side = self._token_side.get(str(evt.get("asset_id")))
                if side is not None:
                    for pc in evt.get("changes") or []:
                        if isinstance(pc, dict):
                            self._books[side].apply_delta(
                                pc.get("price"), pc.get("size"), pc.get("side")
                            )
        elif kind == "last_trade_price":
            market = self.market
            if market is not None and str(evt.get("asset_id")) == market["up_token"]:
                cents = TokenBook.to_cents(evt.get("price"))
                if cents is not None:
                    self._last_trade_up_cents = cents

    def _top_snapshot(self):
        up, down = self._books["UP"], self._books["DOWN"]
        return (up.best_bid(), up.best_ask(), down.best_bid(), down.best_ask())

    def _build_quote(self, receive_ms: float) -> dict:
        up_bid, up_ask, down_bid, down_ask = self._top_snapshot()
        return {
            "t": receive_ms,
            "up_bid": up_bid[0] if up_bid else None,
            "up_ask": up_ask[0] if up_ask else None,
            "down_bid": down_bid[0] if down_bid else None,
            "down_ask": down_ask[0] if down_ask else None,
            "up_bid_size": up_bid[1] if up_bid else None,
            "up_ask_size": up_ask[1] if up_ask else None,
            "down_bid_size": down_bid[1] if down_bid else None,
            "down_ask_size": down_ask[1] if down_ask else None,
            "last_trade_up": self._last_trade_up_cents,
        }
