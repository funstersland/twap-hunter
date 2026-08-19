"""Pure, tick-level metric engines. No I/O, no network — O(1)-ish updates.

All timestamps are epoch milliseconds (float ok). Sides are "buy"/"sell"
meaning the AGGRESSOR (taker) side.
"""

from __future__ import annotations

import math
from collections import deque


# ---------------------------------------------------------------------------
# Rolling TWAP (time-weighted, price held until next sample)
# ---------------------------------------------------------------------------

def rolling_twap(samples, now_ms: float, lookback_s: float) -> float | None:
    """Time-weighted mean of held prices over ``[now - lookback, now]``.

    ``samples``: ascending ``(ts_ms, value)`` pairs. Each price is held
    from its sample time until the next sample (last until ``now_ms``);
    the sample straddling the window start contributes its in-window
    portion only. Returns None when the window has no coverage.
    """
    try:
        if not samples or not math.isfinite(now_ms) or lookback_s <= 0:
            return None
        window_start = float(now_ms) - float(lookback_s) * 1000.0
        integral = 0.0
        weight = 0.0
        count = len(samples)
        for i in range(count):
            ts, value = samples[i]
            if not math.isfinite(value):
                continue
            seg_end = float(samples[i + 1][0]) if i + 1 < count else float(now_ms)
            seg_start = max(float(ts), window_start)
            seg_end = min(seg_end, float(now_ms))
            if seg_end <= seg_start:
                continue
            integral += value * (seg_end - seg_start)
            weight += seg_end - seg_start
        if weight <= 0.0:
            return None
        return integral / weight
    except Exception:
        return None


class RollingTwap:
    """Holds an ascending sample deque trimmed to lookback + margin."""

    MARGIN_S = 5

    def __init__(self, lookback_s: int) -> None:
        self.lookback_s = int(lookback_s)
        self._samples: deque[tuple[float, float]] = deque()

    def set_lookback(self, lookback_s: int) -> None:
        if isinstance(lookback_s, int) and lookback_s > 0:
            self.lookback_s = lookback_s
            self._trim()

    def add(self, ts_ms: float, value: float) -> None:
        if self._samples and ts_ms <= self._samples[-1][0]:
            return
        self._samples.append((ts_ms, value))
        self._trim()

    def _trim(self) -> None:
        s = self._samples
        if not s:
            return
        cutoff = s[-1][0] - (self.lookback_s + self.MARGIN_S) * 1000.0
        while len(s) >= 2 and s[1][0] <= cutoff:
            s.popleft()

    @property
    def value(self) -> float | None:
        s = self._samples
        if not s:
            return None
        return rolling_twap(s, s[-1][0], self.lookback_s)

    def value_at(self, now_ms: float) -> float | None:
        """Rolling TWAP evaluated at ``now_ms``.

        For a FUTURE ``now_ms`` this doubles as the price-hold
        projection: the last sample is held until ``now_ms``, exactly
        the "price stays here" assumption.
        """
        return rolling_twap(self._samples, now_ms, self.lookback_s)

    @property
    def last_sample_ms(self) -> float | None:
        return self._samples[-1][0] if self._samples else None


# ---------------------------------------------------------------------------
# Session (per-round) volume + rolling imbalance
# ---------------------------------------------------------------------------

class VolumeEngine:
    """Buy/sell session totals (reset each round) + rolling imbalance."""

    def __init__(self, rolling_keep_ms: int = 60_000) -> None:
        self._keep_ms = rolling_keep_ms
        self._trades: deque[tuple[float, float, float]] = deque()  # ts, qty, signed
        self.buy_qty = 0.0
        self.sell_qty = 0.0
        self.buy_usd = 0.0
        self.sell_usd = 0.0
        self.trade_count = 0

    def reset_session(self) -> None:
        self.buy_qty = self.sell_qty = 0.0
        self.buy_usd = self.sell_usd = 0.0
        self.trade_count = 0

    def add(self, ts_ms: float, price: float, qty: float, side: str) -> None:
        usd = price * qty
        if side == "buy":
            self.buy_qty += qty
            self.buy_usd += usd
            signed = qty
        else:
            self.sell_qty += qty
            self.sell_usd += usd
            signed = -qty
        self.trade_count += 1
        self._trades.append((ts_ms, qty, signed))
        cutoff = ts_ms - self._keep_ms
        t = self._trades
        while t and t[0][0] < cutoff:
            t.popleft()

    @property
    def net_qty(self) -> float:
        return self.buy_qty - self.sell_qty

    @property
    def net_usd(self) -> float:
        return self.buy_usd - self.sell_usd

    @property
    def session_imbalance(self) -> float | None:
        total = self.buy_qty + self.sell_qty
        if total <= 0:
            return None
        return (self.buy_qty - self.sell_qty) / total

    def imbalance(self, window_ms: int, now_ms: float) -> float | None:
        cutoff = now_ms - window_ms
        total = 0.0
        signed = 0.0
        for ts, qty, sq in reversed(self._trades):
            if ts < cutoff:
                break
            total += qty
            signed += sq
        if total <= 0:
            return None
        return signed / total

    def volume_in(self, window_ms: int, now_ms: float) -> float:
        cutoff = now_ms - window_ms
        total = 0.0
        for ts, qty, _sq in reversed(self._trades):
            if ts < cutoff:
                break
            total += qty
        return total


# ---------------------------------------------------------------------------
# Momentum, tick direction, tick velocity
# ---------------------------------------------------------------------------

class TickEngine:
    """Price momentum over N ms + direction ratio + tick velocity."""

    def __init__(self, keep_ms: int = 60_000, direction_maxlen: int = 200) -> None:
        self._keep_ms = keep_ms
        self._ticks: deque[tuple[float, float]] = deque()      # ts_ms, price
        self._directions: deque[int] = deque(maxlen=direction_maxlen)
        self._last_price: float | None = None

    def add(self, ts_ms: float, price: float) -> None:
        if self._last_price is not None:
            if price > self._last_price:
                self._directions.append(1)
            elif price < self._last_price:
                self._directions.append(-1)
            else:
                self._directions.append(0)
        self._last_price = price
        self._ticks.append((ts_ms, price))
        cutoff = ts_ms - self._keep_ms
        t = self._ticks
        while t and t[0][0] < cutoff:
            t.popleft()

    @property
    def last_price(self) -> float | None:
        return self._last_price

    def momentum(self, window_ms: int, now_ms: float) -> float | None:
        """Price change (last - first) over the window, in price units."""
        if not self._ticks:
            return None
        cutoff = now_ms - window_ms
        oldest: float | None = None
        for ts, price in reversed(self._ticks):
            if ts < cutoff:
                break
            oldest = price
        if oldest is None:
            return None
        return self._ticks[-1][1] - oldest

    def momentum_bps(self, window_ms: int, now_ms: float) -> float | None:
        mom = self.momentum(window_ms, now_ms)
        if mom is None or not self._ticks:
            return None
        price = self._ticks[-1][1]
        if price <= 0:
            return None
        return mom / price * 10_000.0

    def up_ratio(self, lookback: int) -> float | None:
        """Fraction of the last N non-flat directions that were up."""
        if not self._directions:
            return None
        window = list(self._directions)[-lookback:]
        moves = [d for d in window if d != 0]
        if not moves:
            return None
        ups = sum(1 for d in moves if d > 0)
        return ups / len(moves)

    def velocity_tps(self, window_ms: int, now_ms: float) -> float:
        if window_ms <= 0:
            return 0.0
        cutoff = now_ms - window_ms
        count = 0
        for ts, _price in reversed(self._ticks):
            if ts < cutoff:
                break
            count += 1
        return count / (window_ms / 1000.0)


# ---------------------------------------------------------------------------
# Composite pressure score
# ---------------------------------------------------------------------------

_MIN_BASELINE_TPS = 0.2


def pressure_score(
    volume: VolumeEngine,
    ticks: TickEngine,
    now_ms: float,
    *,
    weights: dict[str, float],
    imbalance_window_ms: int,
    momentum_window_ms: int,
    momentum_scale_bps: float,
    direction_lookback: int,
    velocity_baseline_ms: int,
) -> dict:
    """score = 100 * sum(w_i * c_i) / sum(w_i); components in -1..+1.

    - volume_imbalance: rolling (buy-sell)/(buy+sell)
    - tick_direction:   up_ratio * 2 - 1
    - momentum:         tanh(momentum_bps / scale_bps)
    - tick_velocity:    clamp(tps/baseline - 1, -1, 1) SIGNED by momentum
    """
    imb = volume.imbalance(imbalance_window_ms, now_ms)
    c_volume = imb if imb is not None else 0.0

    ratio = ticks.up_ratio(direction_lookback)
    c_direction = (ratio * 2.0 - 1.0) if ratio is not None else 0.0

    mom_bps = ticks.momentum_bps(momentum_window_ms, now_ms)
    c_momentum = math.tanh(mom_bps / momentum_scale_bps) if mom_bps is not None else 0.0

    baseline_tps = ticks.velocity_tps(velocity_baseline_ms, now_ms)
    if baseline_tps > _MIN_BASELINE_TPS:
        current_tps = ticks.velocity_tps(1000, now_ms)
        excess = max(-1.0, min(1.0, current_tps / baseline_tps - 1.0))
    else:
        excess = 0.0
    c_velocity = excess if c_momentum >= 0 else -excess

    pairs = [
        (weights.get("volume_imbalance", 0.0), c_volume),
        (weights.get("tick_direction", 0.0), c_direction),
        (weights.get("momentum", 0.0), c_momentum),
        (weights.get("tick_velocity", 0.0), c_velocity),
    ]
    total_weight = sum(w for w, _ in pairs)
    score = None
    if total_weight > 0:
        raw = 100.0 * sum(w * c for w, c in pairs) / total_weight
        score = round(max(-100.0, min(100.0, raw)), 1)
    return {
        "score": score,
        "components": {
            "volume_imbalance": round(c_volume, 3),
            "tick_direction": round(c_direction, 3),
            "momentum": round(c_momentum, 3),
            "tick_velocity": round(c_velocity, 3),
        },
    }
