"""Robust feed clock-offset estimation.

offset = (local receive time) - (source timestamp) = clock skew + network
latency. Latency is always >= 0 and spiky (a delayed burst of frames can
poison a mean/EWMA for minutes), so the rolling MIN of recent samples is
the robust estimator: it converges to skew + minimal latency and ignores
delay outliers entirely.
"""

from __future__ import annotations

from collections import deque


class OffsetTracker:
    def __init__(self, window: int = 240) -> None:
        self._samples: deque[float] = deque(maxlen=window)

    def add(self, receive_ms: float, source_ms: float) -> None:
        self._samples.append(receive_ms - source_ms)

    @property
    def value(self) -> float | None:
        if not self._samples:
            return None
        return min(self._samples)
