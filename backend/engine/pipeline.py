"""Per-asset pipeline: feeds -> metric engines -> batched wire payloads.

Hot path per tick: append buffers, O(small-window) metric updates,
event checks — no JSON, no blocking IO. The WebSocket layer drains a
pending batch every ~50 ms.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from collections import deque

import httpx

from backend.config import ASSETS, CONFIG, Asset
from backend.engine import metrics
from backend.feeds.binance import BinanceTradeFeed
from backend.feeds.polymarket import PolymarketFeed
from backend.feeds.rtds import RTDSFeed
from backend.storage import storage

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # tzdata missing — fall back to UTC labels
    _ET = datetime.timezone.utc


def round_label_et(window_id: int, window_ms: int = 0) -> str:
    """Polymarket-style round label, e.g. '10:35–10:40AM ET'."""
    window_ms = window_ms or CONFIG.window_ms
    start = datetime.datetime.fromtimestamp(window_id * window_ms / 1000.0, tz=_ET)
    end = datetime.datetime.fromtimestamp((window_id + 1) * window_ms / 1000.0, tz=_ET)
    fmt = lambda d: d.strftime("%I:%M").lstrip("0")  # noqa: E731
    suffix = "ET" if _ET is not datetime.timezone.utc else "UTC"
    return f"{fmt(start)}–{fmt(end)}{end.strftime('%p')} {suffix}"


def now_ms() -> float:
    return time.time() * 1000.0


# The local clock can be seconds off. Two corrected clocks matter:
# - "poly time" (RTDS payload timestamps) drives round boundaries, the
#   PTB latch and the countdown — parity with Polymarket's own clock.
# - "exchange time" (Binance trade timestamps) drives the rolling metric
#   windows, whose data is stamped in that timebase.


class Pipeline:
    """Owns the three feeds and all derived state for one asset."""

    def __init__(self, asset: Asset) -> None:
        self.asset = asset
        self.binance = BinanceTradeFeed(asset)
        self.rtds = RTDSFeed(asset.rtds_symbol)
        self.polymarket = PolymarketFeed(asset)
        self.polymarket.on_new_market = self._on_new_market

        # Ring buffers (history for late-joining clients).
        self.ticks: deque[list] = deque(maxlen=CONFIG.ticks_max)    # [t, price, qty, side]
        self.refs: deque[list] = deque(maxlen=CONFIG.ref_max)       # [t, chainlink]
        self.twaps: deque[list] = deque(maxlen=CONFIG.ref_max)      # [t, rolling, twap30]
        self.quotes: deque[dict] = deque(maxlen=CONFIG.quotes_max)
        self.events: deque[dict] = deque(maxlen=CONFIG.events_max)
        self.preds: deque[dict] = deque(maxlen=600)
        # Ended rounds: window_id -> {"outcome","final_twap","ptb"} (for
        # paper settlement; outcome None = void, e.g. unknown PTB).
        self.round_results: dict[int, dict] = {}

        # Pending batch for the broadcaster.
        self._pending_ticks: list[list] = []
        self._pending_refs: list[list] = []
        self._pending_twaps: list[list] = []
        self._pending_quotes: list[dict] = []
        self._pending_events: list[dict] = []
        self._pending_preds: list[dict] = []

        # Engines.
        self.volume = metrics.VolumeEngine()
        self.tick_engine = metrics.TickEngine()
        self.rolling = metrics.RollingTwap(CONFIG.twap_lookback_s)

        # State.
        self.chainlink: float | None = None
        self.twap30: float | None = None
        self.last_trade: float | None = None
        self.window_id: int | None = None
        self.window_start_ms: float = 0.0
        self.window_end_ms: float = 0.0
        self.ptb: float | None = None
        self.ptb_source: str | None = None
        # Parity study: the OTHER price-to-beat candidate — the raw
        # chainlink price at round start (official outcomes suggest this
        # may be what Polymarket actually latches).
        self.ptb_price: float | None = None
        self.last_quote: dict | None = None
        self._last_tick_ms: float = 0.0
        self._last_ref_ms: float = 0.0
        self._last_quote_ms: float = 0.0
        self._event_id = 0

        # Event-detection state.
        self._twap_delta_sign = 0
        self._pressure_armed = True
        self._momentum_next_ok = 0.0
        self._volspike_next_ok = 0.0
        self._quote_swing_next_ok = 0.0
        self._pressure_next_ms = 0.0
        self._up_mids: deque[tuple[float, float]] = deque()
        self._last_pressure: dict = {"score": None, "components": {}}
        # Tick-signal events stay muted until the baseline windows are
        # warm — otherwise the first seconds produce fake "spikes".
        self._events_warm_ms = now_ms() + 30_000

        # Prediction state: creation throttle + accuracy aggregates.
        self._pred_next_ms = 0.0
        self._pred_stats = {
            "h": {"n": 0, "abs_bps_sum": 0.0, "hit": 0},
            "end": {"n": 0, "dir_hit": 0, "mkt_n": 0, "mkt_hit": 0},
        }

        # PTB recovery + feed-health transition tracking.
        self._http: httpx.AsyncClient | None = None
        self._ptb_next_try_ms = 0.0
        self._ptb_kline_attempts = 0
        self._health_prev: dict[str, str] = {}

        self._tasks: list[asyncio.Task] = []

    def poly_now_ms(self) -> float:
        return now_ms() - (self.rtds.clock_offset_ms or 0.0)

    def _tick_to_poly(self, exchange_ts_ms: float) -> float:
        """Binance trade timestamp -> poly timebase (via both offsets)."""
        off_b = self.binance.clock_offset_ms
        off_r = self.rtds.clock_offset_ms
        if off_b is None or off_r is None:
            return exchange_ts_ms
        return exchange_ts_ms + off_b - off_r

    def _local_to_poly(self, local_ms: float) -> float:
        return local_ms - (self.rtds.clock_offset_ms or 0.0)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=10.0)
        self._roll_window(self.poly_now_ms(), initial=True)
        await self.polymarket.start()
        self._tasks = [
            asyncio.create_task(self._run_binance(), name=f"{self.asset.key}-binance"),
            asyncio.create_task(self._run_rtds(), name=f"{self.asset.key}-rtds"),
            asyncio.create_task(self._run_quotes(), name=f"{self.asset.key}-quotes"),
            asyncio.create_task(self._run_clock(), name=f"{self.asset.key}-clock"),
        ]
        self._emit("SYSTEM", f"{self.asset.key} pipeline started", None)

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self.polymarket.stop()
        http, self._http = self._http, None
        if http is not None:
            try:
                await http.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Feed tasks
    # ------------------------------------------------------------------

    async def _run_binance(self) -> None:
        async for receive_ms, ts_ms, price, qty, side in self.binance.stream():
            self._last_tick_ms = receive_ms
            self.last_trade = price
            # ONE timebase everywhere: rebase the exchange stamp onto the
            # poly clock so charts/windows never straddle skewed feeds.
            ts = self._tick_to_poly(ts_ms)
            side_num = 1 if side == "buy" else -1
            row = [round(ts), price, qty, side_num]
            self.ticks.append(row)
            self._pending_ticks.append(row)
            self.volume.add(ts, price, qty, side)
            self.tick_engine.add(ts, price)
            self._check_tick_events(ts)

    async def _run_rtds(self) -> None:
        async for kind, receive_ms, points in self.rtds.stream():
            if kind == "backfill":
                self._verify_backfill(points)
                for ts, value in points:
                    self.rolling.add(ts, value)
                    self.chainlink = value
                    self.refs.append([round(ts), value])
                    rolled = self.rolling.value
                    if rolled is not None:
                        self.twaps.append([round(ts), rolled, self.twap30])
                # Cold start mid-round: the backfill (~1 min of 1/s
                # points) may reach back past the round boundary — latch
                # the rolling TWAP AT the boundary as the Price To Beat
                # (approximate when coverage is partial, but far closer
                # to Polymarket's true PTB than a late first sample).
                if (
                    self.ptb is None
                    and points
                    and points[0][0] <= self.window_start_ms <= points[-1][0]
                ):
                    latched = self.rolling.value_at(self.window_start_ms)
                    if latched is not None:
                        self._set_ptb(latched, "twap_at_start_backfill")
                logger.info(
                    "rtds backfill: %d points for %s", len(points), self.asset.key
                )
                continue
            if not points:
                continue
            ts, value = points[0]
            self._last_ref_ms = receive_ms
            if kind == "twap30":
                self.twap30 = value
                continue
            # chainlink reference update (~1/s)
            self.chainlink = value
            self.rolling.add(ts, value)
            row = [round(ts), value]
            self.refs.append(row)
            self._pending_refs.append(row)
            rolled = self.rolling.value
            if rolled is not None:
                trow = [round(ts), rolled, self.twap30]
                self.twaps.append(trow)
                self._pending_twaps.append(trow)
            # Lazy PTB: first reference just after the window start when
            # the boundary latch had nothing to grab. Only within 10s of
            # the boundary — a later sample is a "now" price, not a PTB;
            # better to show none until the next round latches cleanly.
            if (
                self.ptb is None
                and self.window_start_ms <= ts <= self.window_start_ms + 10_000
            ):
                self._set_ptb(value, "first_reference")
            if self.ptb_price is None and ts <= self.window_start_ms + 10_000:
                self.ptb_price = value
            self._check_target_cross()

    async def _run_quotes(self) -> None:
        async for quote in self.polymarket.stream_quotes():
            self._last_quote_ms = quote["t"]
            quote["t"] = self._local_to_poly(quote["t"])
            self.last_quote = quote
            self.quotes.append(quote)
            self._pending_quotes.append(quote)
            self._check_quote_swing(quote)

    async def _run_clock(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            t = self.poly_now_ms()
            if t >= self.window_end_ms:
                self._roll_window(t)
            if t >= self._pred_next_ms:
                self._pred_next_ms = t + CONFIG.prediction_interval_ms
                self._make_predictions(t)
            self._score_predictions(t)
            await self._ptb_recovery(t)
            self._check_feed_transitions()
            res = self.polymarket.resolution
            if res is not None:
                self.polymarket.resolution = None
                self._apply_official_resolution(res)

    # ------------------------------------------------------------------
    # TWAP prediction (price-hold projection of the rolling integral)
    # ------------------------------------------------------------------

    def _up_mid_cents(self) -> float | None:
        q = self.last_quote
        if not q or q.get("up_bid") is None or q.get("up_ask") is None:
            return None
        return (q["up_bid"] + q["up_ask"]) / 2.0

    def _make_predictions(self, t: float) -> None:
        """Project the rolling TWAP forward; dots land in future time.

        ``value_at`` holds the last Chainlink sample until the target —
        the exact "price stays here" assumption. Predictions are only
        made off a FRESH oracle (last sample < 5s old).
        """
        last_ms = self.rolling.last_sample_ms
        if last_ms is None or t - last_ms > 5000:
            return
        up_mid = self._up_mid_cents()
        # Horizon dot (+30s)
        target = t + CONFIG.prediction_horizon_ms
        value = self.rolling.value_at(target)
        if value is not None:
            self._add_pred({
                "t": target, "v": _rnd(value, self.asset.decimals + 3),
                "c": t, "k": "h", "ptb": self.ptb, "m": up_mid, "s": 0,
            })
        # Round-end dot (final TWAP => UP/DOWN vs the Price To Beat)
        if self.ptb is not None and self.window_end_ms > t + 1000:
            final = self.rolling.value_at(self.window_end_ms)
            if final is not None:
                self._add_pred({
                    "t": self.window_end_ms, "v": _rnd(final, self.asset.decimals + 3),
                    "c": t, "k": "end", "ptb": self.ptb, "m": up_mid, "s": 0,
                })

    def _add_pred(self, rec: dict) -> None:
        self.preds.append(rec)
        self._pending_preds.append(rec)

    def _actual_twap_at(self, target: float, tol_ms: float = 3000) -> float | None:
        """Nearest recorded rolling-TWAP value to ``target`` (within tol)."""
        best_dt = tol_ms + 1
        best = None
        for row in reversed(self.twaps):
            dt = abs(row[0] - target)
            if dt < best_dt:
                best_dt = dt
                best = row[1]
            if row[0] < target - tol_ms:
                break
        return best

    def _score_predictions(self, t: float) -> None:
        for rec in self.preds:
            if rec["s"] or rec["t"] > t - 1500:
                continue
            actual = self._actual_twap_at(rec["t"])
            if actual is None or actual <= 0:
                rec["s"] = 3  # unscorable (feed gap)
                continue
            err_bps = (rec["v"] - actual) / actual * 10_000.0
            hit_value = abs(err_bps) <= CONFIG.prediction_hit_bps
            rec["a"] = _rnd(actual, self.asset.decimals + 3)
            rec["e"] = _rnd(err_bps, 2)
            if rec["k"] == "h":
                stats = self._pred_stats["h"]
                stats["n"] += 1
                stats["abs_bps_sum"] += abs(err_bps)
                stats["hit"] += 1 if hit_value else 0
                rec["s"] = 1 if hit_value else 2
            else:
                stats = self._pred_stats["end"]
                ptb = rec.get("ptb")
                if ptb is not None:
                    dir_pred = rec["v"] > ptb
                    dir_actual = actual > ptb
                    dir_hit = dir_pred == dir_actual
                    stats["n"] += 1
                    stats["dir_hit"] += 1 if dir_hit else 0
                    rec["s"] = 1 if dir_hit else 2
                    if rec.get("m") is not None:
                        mkt_hit = (rec["m"] > 50) == dir_actual
                        stats["mkt_n"] += 1
                        stats["mkt_hit"] += 1 if mkt_hit else 0
                else:
                    rec["s"] = 3

    def _prediction_snapshot(self) -> dict:
        h = self._pred_stats["h"]
        end = self._pred_stats["end"]
        next_h = None
        final = None
        for rec in reversed(self.preds):
            if rec["k"] == "h" and next_h is None and not rec["s"]:
                next_h = {"t": rec["t"], "v": rec["v"]}
            if rec["k"] == "end" and final is None and not rec["s"]:
                final = {"t": rec["t"], "v": rec["v"], "ptb": rec.get("ptb")}
            if next_h and final:
                break
        return {
            "h30": {
                "n": h["n"],
                "mae_bps": _rnd(h["abs_bps_sum"] / h["n"], 2) if h["n"] else None,
                "hit_pct": _rnd(100.0 * h["hit"] / h["n"], 1) if h["n"] else None,
            },
            "end": {
                "n": end["n"],
                "dir_hit_pct": _rnd(100.0 * end["dir_hit"] / end["n"], 1) if end["n"] else None,
                "mkt_n": end["mkt_n"],
                "mkt_hit_pct": _rnd(100.0 * end["mkt_hit"] / end["mkt_n"], 1) if end["mkt_n"] else None,
            },
            "next_h30": next_h,
            "final": final,
            "horizon_s": CONFIG.prediction_horizon_ms // 1000,
        }

    def _apply_official_resolution(self, res: dict) -> None:
        """Adopt Polymarket's official outcome as the round's truth.

        Attributed by the slug's epoch suffix (= round start seconds).
        Overrides the provisional local verdict; a flip is called out in
        the timeline — that is the thin-round case local math gets wrong.
        """
        outcome = res.get("outcome")
        slug = str(res.get("slug") or "")
        wid = None
        try:
            wid = int(slug.rsplit("-", 1)[1]) * 1000 // CONFIG.window_ms
        except (ValueError, IndexError):
            pass
        label = round_label_et(wid) if wid is not None else ""
        flipped = False
        if wid is not None:
            rec = self.round_results.get(wid)
            if rec is not None:
                flipped = rec.get("outcome") not in (None, outcome)
                rec["outcome"] = outcome
                rec["source"] = "market"
            else:
                self.round_results[wid] = {
                    "outcome": outcome, "source": "market",
                    "label": label,
                }
            try:
                storage.upsert_round(self.asset.key, wid, outcome_official=outcome)
            except Exception:
                logger.debug("round official upsert failed", exc_info=True)
        if flipped:
            self._emit(
                "RESOLUTION",
                f"OFFICIAL: {label} resolved {outcome} — overrides local math (thin round)",
                outcome,
                kind_class="up" if outcome == "UP" else "down",
            )
        else:
            self._emit(
                "RESOLUTION",
                f"Official: {label} resolved {outcome}",
                outcome,
                kind_class="up" if outcome == "UP" else "down",
            )

    # ------------------------------------------------------------------
    # PTB recovery + reconnect verification + feed transition events
    # ------------------------------------------------------------------

    async def _ptb_recovery(self, t: float) -> None:
        """Recover a missing Price To Beat mid-round.

        Ladder: (1) recompute from stored oracle history when it covers
        the boundary (missed latch), (2) approximate from Binance 1s
        klines over [start-60s, start] — honestly labeled, because the
        oracle's own history only reaches back ~60s. Runs every 5s
        while the round is active and PTB is unknown.
        """
        if (
            self.ptb is not None
            or t >= self.window_end_ms
            or t - self.window_start_ms < 4000   # boundary latch/backfill first
            or t < self._ptb_next_try_ms
        ):
            return
        self._ptb_next_try_ms = t + 5000

        # (1) Stored oracle history covering [start - lookback, start]?
        start = self.window_start_ms
        lookback_ms = self.rolling.lookback_s * 1000.0
        refs = self.refs
        if refs and refs[0][0] <= start - lookback_ms * 0.9:
            pts = [(r[0], r[1]) for r in refs if start - lookback_ms - 5000 <= r[0] <= start + 1000]
            value = metrics.rolling_twap(pts, start, self.rolling.lookback_s)
            if value is not None:
                self._set_ptb(value, "twap_at_start_recovered")
                return

        # (2) Exchange 1s-kline approximation (max 3 tries per round).
        if self._ptb_kline_attempts >= 3 or self._http is None:
            return
        self._ptb_kline_attempts += 1
        try:
            # Convert the poly-time boundary into the exchange timebase.
            off_b = self.binance.clock_offset_ms or 0.0
            off_r = self.rtds.clock_offset_ms or 0.0
            start_ex = start + off_r - off_b
            base = (
                "https://fapi.binance.com/fapi/v1/klines"
                if self.asset.binance_venue == "futures"
                else "https://api.binance.com/api/v3/klines"
            )
            resp = await self._http.get(base, params={
                "symbol": self.asset.binance_symbol.upper(),
                "interval": "1s",
                "startTime": int(start_ex - lookback_ms),
                "endTime": int(start_ex),
                "limit": 120,
            })
            resp.raise_for_status()
            rows = resp.json()
            closes = [float(r[4]) for r in rows if isinstance(r, list) and len(r) > 4]
            if len(closes) < 10:
                return
            value = sum(closes) / len(closes)
            self._set_ptb(value, "exchange_approx")
            self._emit(
                "PTB",
                "PTB approximated from exchange 1s klines "
                "(oracle history unavailable for this boundary)",
                _rnd(value, self.asset.decimals + 2),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("ptb kline recovery failed: %r", exc)

    def _verify_backfill(self, points: list[tuple[float, float]]) -> None:
        """Cross-check a reconnect backfill against data we already hold.

        Overlapping timestamps must carry (nearly) identical values —
        this is the 'is the fetched data real' check after an outage.
        """
        if not self.refs or not points:
            return
        existing = {round(r[0] / 1000): r[1] for r in list(self.refs)[-180:]}
        overlap = 0
        max_diff_bps = 0.0
        for ts, value in points:
            prev = existing.get(round(ts / 1000))
            if prev is None or prev <= 0:
                continue
            overlap += 1
            diff = abs(value - prev) / prev * 10_000.0
            if diff > max_diff_bps:
                max_diff_bps = diff
        if overlap < 3:
            return
        if max_diff_bps <= 2.0:
            self._emit(
                "ORACLE",
                f"Reconnect backfill verified: {overlap} overlapping pts, "
                f"max Δ {max_diff_bps:.2f} bps",
                _rnd(max_diff_bps, 2),
                kind_class="up",
            )
        else:
            self._emit(
                "ORACLE",
                f"Backfill DISAGREES with held data ({overlap} pts, "
                f"max Δ {max_diff_bps:.1f} bps) — treating backfill as truth",
                _rnd(max_diff_bps, 2),
                kind_class="down",
            )

    def _check_feed_transitions(self) -> None:
        """Emit timeline events when a feed changes live/stale/down state.

        Thresholds are much laxer than the health dots: a quiet market
        legitimately gaps between trades — only sustained silence is an
        event worth notifying about.
        """
        tl = now_ms()
        current = {
            "TICKS": _health(self._last_tick_ms, tl, 30000),
            "ORACLE": _health(self._last_ref_ms, tl, 15000),
            "BOOK": _health(self._last_quote_ms, tl, 120000),
        }
        for name, state in current.items():
            prev = self._health_prev.get(name)
            self._health_prev[name] = state
            if prev is None or prev == state:
                continue
            if state == "live":
                label = "online" if prev == "down" else "recovered"
                self._emit("FEED", f"{name} feed {label}", None, kind_class="up")
            elif state == "down":
                self._emit("FEED", f"{name} feed DOWN — reconnecting", None, kind_class="down")
            else:
                self._emit("FEED", f"{name} feed stale — no data", None, kind_class="down")

    # ------------------------------------------------------------------
    # Window / Price To Beat
    # ------------------------------------------------------------------

    def _roll_window(self, t: float, initial: bool = False) -> None:
        window_ms = float(CONFIG.window_ms)
        start = t - (t % window_ms)
        # Settle the ENDING round first. Local math is PROVISIONAL: two
        # of three live rounds disagreed with the official resolution at
        # ~2bps margins, so the official Polymarket outcome (polled a
        # little later) overrides this, and all candidate quantities are
        # recorded for the parity study.
        if not initial and self.window_id is not None:
            final = self.rolling.value_at(self.window_end_ms)
            range_pts = [
                (r[0], r[1]) for r in self.refs
                if self.window_start_ms - 1000 <= r[0] <= self.window_end_ms
            ]
            final_range = metrics.rolling_twap(
                range_pts, self.window_end_ms, (self.window_end_ms - self.window_start_ms) / 1000.0
            )
            old_ptb = self.ptb
            outcome = None
            if final is not None and old_ptb is not None:
                outcome = "UP" if final >= old_ptb else "DOWN"
            d3 = self.asset.decimals + 3
            self.round_results[self.window_id] = {
                "outcome": outcome,
                "source": "local",
                "final_twap": _rnd(final, d3),
                "final_range_twap": _rnd(final_range, d3),
                "ptb": _rnd(old_ptb, d3),
                "ptb_price": _rnd(self.ptb_price, d3),
                "label": round_label_et(self.window_id),
            }
            while len(self.round_results) > 60:
                self.round_results.pop(next(iter(self.round_results)))
            try:
                storage.upsert_round(
                    self.asset.key, self.window_id,
                    start_ms=self.window_start_ms,
                    round_label=round_label_et(self.window_id),
                    ptb_twap=_rnd(old_ptb, d3),
                    ptb_price=_rnd(self.ptb_price, d3),
                    final_twap=_rnd(final, d3),
                    final_range_twap=_rnd(final_range, d3),
                    outcome_local=outcome,
                )
            except Exception:
                logger.debug("round upsert failed", exc_info=True)
        self.window_id = int(start // window_ms)
        self.window_start_ms = start
        self.window_end_ms = start + window_ms
        self.ptb = None
        self.ptb_source = None
        # Chainlink price AT the boundary — the other PTB candidate.
        self.ptb_price = self.chainlink
        self._twap_delta_sign = 0
        self._ptb_next_try_ms = 0.0
        self._ptb_kline_attempts = 0
        self.volume.reset_session()

        # Polymarket parity: the Price To Beat IS the rolling lookback
        # TWAP latched at the round boundary. Fallbacks: last chainlink
        # price, then the first reference sample after start (lazy).
        latched = self.rolling.value_at(start)
        if latched is not None:
            self._set_ptb(latched, "twap_at_start")
        elif self.chainlink is not None:
            self._set_ptb(self.chainlink, "chainlink_last")
        if not initial:
            self._emit("WINDOW", "New 5m round started", None)

    def _set_ptb(self, value: float, source: str) -> None:
        if self.ptb is not None:
            return
        self.ptb = value
        self.ptb_source = source
        self._emit(
            "PTB",
            f"Price to beat {value:.{self.asset.decimals}f} ({source})",
            value,
        )

    def _on_new_market(self, market: dict) -> None:
        lookback = market.get("twap_lookback_s")
        if lookback:
            self.rolling.set_lookback(int(lookback))
        # New market -> new order books; stale mids would read as a swing.
        self._up_mids.clear()
        self._emit("MARKET", market.get("question") or market.get("slug"), None)

    # ------------------------------------------------------------------
    # Event detection
    # ------------------------------------------------------------------

    def _emit(self, kind: str, label: str, value, kind_class: str | None = None) -> None:
        self._event_id += 1
        evt = {
            "id": self._event_id,
            "t": self.poly_now_ms(),
            "kind": kind,
            "label": label,
            "value": value,
            "cls": kind_class or "",
        }
        self.events.append(evt)
        self._pending_events.append(evt)

    def _check_target_cross(self) -> None:
        if self.ptb is None:
            return
        twap = self.rolling.value
        if twap is None:
            return
        delta = twap - self.ptb
        sign = 1 if delta > 0 else (-1 if delta < 0 else 0)
        if sign != 0 and self._twap_delta_sign != 0 and sign != self._twap_delta_sign:
            direction = "UP" if sign > 0 else "DOWN"
            self._emit(
                "TARGET_CROSS",
                f"TWAP crossed price-to-beat -> {direction}",
                round(delta, self.asset.decimals + 2),
                kind_class="up" if sign > 0 else "down",
            )
        if sign != 0:
            self._twap_delta_sign = sign

    def _check_tick_events(self, ts_ms: float) -> None:
        # Throttle: the pressure/velocity math iterates small windows —
        # once per 100 ms is plenty, even at 100+ ticks/s.
        if ts_ms < self._pressure_next_ms:
            return
        self._pressure_next_ms = ts_ms + 100
        pressure = self._compute_pressure(ts_ms)
        if ts_ms < self._events_warm_ms:
            return
        # Momentum spike
        if ts_ms >= self._momentum_next_ok:
            bps = self.tick_engine.momentum_bps(1000, ts_ms)
            if bps is not None and abs(bps) >= CONFIG.momentum_event_bps:
                direction = "up" if bps > 0 else "down"
                self._emit(
                    "MOMENTUM",
                    f"Momentum {bps:+.1f} bps / 1s",
                    round(bps, 1),
                    kind_class=direction,
                )
                self._momentum_next_ok = ts_ms + 2000
        # Volume spike
        if ts_ms >= self._volspike_next_ok:
            vol_1s = self.volume.volume_in(1000, ts_ms)
            base = self.volume.volume_in(30_000, ts_ms) / 30.0
            if base > 0 and vol_1s / base >= CONFIG.volume_spike_ratio and vol_1s > 0:
                imb = self.volume.imbalance(1000, ts_ms)
                side = "buy" if (imb or 0) >= 0 else "sell"
                self._emit(
                    "VOL_SPIKE",
                    f"Volume spike x{vol_1s / base:.1f} ({side}-heavy)",
                    round(vol_1s, 2),
                    kind_class="up" if side == "buy" else "down",
                )
                self._volspike_next_ok = ts_ms + 3000
        # Pressure threshold crossing (with hysteresis re-arm at 40)
        score = pressure.get("score")
        if score is not None:
            if self._pressure_armed and abs(score) >= CONFIG.pressure_strong_threshold:
                direction = "UP" if score > 0 else "DOWN"
                self._emit(
                    "PRESSURE",
                    f"Strong {direction} pressure {score:+.0f}",
                    score,
                    kind_class="up" if score > 0 else "down",
                )
                self._pressure_armed = False
            elif not self._pressure_armed and abs(score) < 40:
                self._pressure_armed = True

    def _check_quote_swing(self, quote: dict) -> None:
        bid, ask = quote.get("up_bid"), quote.get("up_ask")
        if bid is None or ask is None:
            return
        mid = (bid + ask) / 2.0
        t = quote["t"]
        self._up_mids.append((t, mid))
        cutoff = t - CONFIG.quote_swing_window_ms
        while self._up_mids and self._up_mids[0][0] < cutoff:
            self._up_mids.popleft()
        if t < self._events_warm_ms:
            return  # initial book snapshots are not "swings"
        if t < self._quote_swing_next_ok or len(self._up_mids) < 2:
            return
        values = [m for _t, m in self._up_mids]
        move = values[-1] - values[0]
        if abs(move) >= CONFIG.quote_swing_cents:
            self._emit(
                "QUOTE_SWING",
                f"UP quote moved {move:+.0f}c in {CONFIG.quote_swing_window_ms / 1000:.0f}s",
                round(move, 1),
                kind_class="up" if move > 0 else "down",
            )
            self._quote_swing_next_ok = t + 2000

    def _compute_pressure(self, t: float) -> dict:
        self._last_pressure = metrics.pressure_score(
            self.volume,
            self.tick_engine,
            t,
            weights=CONFIG.pressure_weights,
            imbalance_window_ms=CONFIG.pressure_imbalance_window_ms,
            momentum_window_ms=CONFIG.pressure_momentum_window_ms,
            momentum_scale_bps=CONFIG.pressure_momentum_scale_bps,
            direction_lookback=CONFIG.direction_lookback,
            velocity_baseline_ms=CONFIG.velocity_baseline_ms,
        )
        return self._last_pressure

    # ------------------------------------------------------------------
    # Wire payloads
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        t = self.poly_now_ms()   # everything lives on the poly timebase now
        tx = t
        tl = now_ms()            # health ages use the raw local clock
        d = self.asset.decimals
        twap = self.rolling.value
        ptb = self.ptb
        price_delta = twap_delta = None
        price_delta_bps = twap_delta_bps = None
        if ptb is not None and self.chainlink is not None:
            price_delta = self.chainlink - ptb
            price_delta_bps = price_delta / ptb * 10_000.0 if ptb else None
        if ptb is not None and twap is not None:
            twap_delta = twap - ptb
            twap_delta_bps = twap_delta / ptb * 10_000.0 if ptb else None

        momentum = {}
        for w in CONFIG.momentum_windows_ms:
            momentum[str(w)] = {
                "abs": _rnd(self.tick_engine.momentum(w, tx), d + 2),
                "bps": _rnd(self.tick_engine.momentum_bps(w, tx), 2),
            }
        imb = {}
        for w in CONFIG.imbalance_windows_ms:
            imb[str(w)] = _rnd(self.volume.imbalance(w, tx), 3)

        tps_1s = self.tick_engine.velocity_tps(1000, tx)
        tps_base = self.tick_engine.velocity_tps(CONFIG.velocity_baseline_ms, tx)

        market = self.polymarket.market or {}
        return {
            "asset": self.asset.key,
            "decimals": d,
            "server_now_ms": t,
            "window": {
                "id": self.window_id,
                "start_ms": self.window_start_ms,
                "end_ms": self.window_end_ms,
                "time_remaining_ms": max(0.0, self.window_end_ms - t),
                "ptb": _rnd(ptb, d + 2),
                "ptb_source": self.ptb_source,
            },
            "prices": {
                "last_trade": _rnd(self.last_trade, d + 2),
                "chainlink": _rnd(self.chainlink, d + 2),
                "twap": _rnd(twap, d + 2),
                "twap30": _rnd(self.twap30, d + 2),
                "lookback_s": self.rolling.lookback_s,
            },
            "deltas": {
                "price": _rnd(price_delta, d + 3),
                "price_bps": _rnd(price_delta_bps, 2),
                "twap": _rnd(twap_delta, d + 3),
                "twap_bps": _rnd(twap_delta_bps, 2),
            },
            "volume": {
                "buy_qty": _rnd(self.volume.buy_qty, 2),
                "sell_qty": _rnd(self.volume.sell_qty, 2),
                "net_qty": _rnd(self.volume.net_qty, 2),
                "buy_usd": _rnd(self.volume.buy_usd, 0),
                "sell_usd": _rnd(self.volume.sell_usd, 0),
                "net_usd": _rnd(self.volume.net_usd, 0),
                "imbalance": _rnd(self.volume.session_imbalance, 3),
                "trades": self.volume.trade_count,
                "windows": imb,
            },
            "momentum": momentum,
            "velocity": {
                "tps_1s": _rnd(tps_1s, 1),
                "tps_baseline": _rnd(tps_base, 1),
                "ratio": _rnd(tps_1s / tps_base, 2) if tps_base > 0.2 else None,
            },
            "direction": {
                "up_ratio": _rnd(self.tick_engine.up_ratio(CONFIG.direction_lookback), 3),
            },
            "pressure": self._last_pressure,
            "prediction": self._prediction_snapshot(),
            "market": {
                "slug": market.get("slug"),
                "question": market.get("question"),
                "quote": self.last_quote,
            },
            "health": {
                "binance": _health(self._last_tick_ms, tl, 3000),
                "rtds": _health(self._last_ref_ms, tl, 5000),
                # A quiet book is normal late in a decided round — only
                # flag it after a full minute without any change.
                "clob": _health(self._last_quote_ms, tl, 60000),
                "binance_offset_ms": _rnd(self.binance.clock_offset_ms, 1),
                "rtds_offset_ms": _rnd(self.rtds.clock_offset_ms, 1),
            },
        }

    def drain_batch(self) -> dict | None:
        """Pending batch + fresh snapshot; None when nothing new arrived."""
        if not (
            self._pending_ticks
            or self._pending_refs
            or self._pending_twaps
            or self._pending_quotes
            or self._pending_events
            or self._pending_preds
        ):
            return None
        batch = {
            "type": "batch",
            "ticks": self._pending_ticks,
            "ref": self._pending_refs,
            "twap": self._pending_twaps,
            "quotes": self._pending_quotes,
            "events": self._pending_events,
            "preds": self._pending_preds,
            "snapshot": self.snapshot(),
        }
        self._pending_ticks = []
        self._pending_refs = []
        self._pending_twaps = []
        self._pending_quotes = []
        self._pending_events = []
        self._pending_preds = []
        return batch

    def init_payload(self) -> dict:
        t = now_ms()
        cutoff = t - CONFIG.init_ticks_seconds * 1000.0
        return {
            "type": "init",
            "asset": self.asset.key,
            "assets": list(ASSETS.keys()),
            "history": {
                "ticks": [row for row in self.ticks if row[0] >= cutoff],
                "ref": list(self.refs),
                "twap": list(self.twaps),
                "quotes": list(self.quotes)[-300:],
                "events": list(self.events)[-100:],
                "preds": list(self.preds),
            },
            "snapshot": self.snapshot(),
        }


def _rnd(value, digits: int):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _health(last_ms: float, now: float, stale_ms: float) -> str:
    if last_ms <= 0:
        return "down"
    age = now - last_ms
    if age > stale_ms:
        return "stale"
    return "live"
