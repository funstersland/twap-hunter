"""Twap Hunter configuration: asset registry + tunables.

All wire protocols were verified live against Polymarket / Binance
(2026-08-17, see the PolyTwap research project this evolved from):

- Binance trade stream: ``wss://stream.binance.com:9443/ws/{sym}@trade``
- Polymarket Gamma discovery: ``GET /events?series_slug=...``
- Polymarket CLOB market WS: ``wss://ws-subscriptions-clob.polymarket.com/ws/market``
- Polymarket RTDS (Chainlink reference + TWAP): ``wss://ws-live-data.polymarket.com``
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Asset:
    key: str                 # UI key, e.g. "XRP"
    binance_symbol: str      # e.g. "xrpusdt"
    binance_venue: str       # "spot" | "futures"
    rtds_symbol: str         # e.g. "xrp/usd"
    series_slug: str         # Gamma series slug, e.g. "xrp-up-or-down-5m"
    decimals: int            # price display decimals


ASSETS: dict[str, Asset] = {
    "XRP": Asset("XRP", "xrpusdt", "spot", "xrp/usd", "xrp-up-or-down-5m", 4),
    "BTC": Asset("BTC", "btcusdt", "spot", "btc/usd", "btc-up-or-down-5m", 2),
    "ETH": Asset("ETH", "ethusdt", "spot", "eth/usd", "eth-up-or-down-5m", 2),
    "SOL": Asset("SOL", "solusdt", "spot", "sol/usd", "sol-up-or-down-5m", 3),
    "DOGE": Asset("DOGE", "dogeusdt", "spot", "doge/usd", "doge-up-or-down-5m", 5),
}

DEFAULT_ASSET = "XRP"


@dataclass(frozen=True)
class Config:
    # --- endpoints -----------------------------------------------------
    binance_spot_base: str = "wss://stream.binance.com:9443"
    binance_futures_base: str = "wss://fstream.binance.com"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    rtds_ws_url: str = "wss://ws-live-data.polymarket.com"

    # --- timing --------------------------------------------------------
    window_ms: int = 300_000          # 5-minute rounds
    broadcast_interval_ms: int = 50   # WS batch cadence to the UI
    ping_interval_s: float = 10.0     # app-level PING for Polymarket sockets
    discovery_interval_ms: int = 15_000
    resolution_poll_interval_ms: int = 4_000
    # Official resolutions can land minutes after round end — keep
    # polling long enough for the parity study + settlement reconciliation.
    resolution_give_up_ms: int = 300_000
    reconnect_backoff_ms: tuple[int, ...] = (500, 1000, 2000, 5000)

    # --- TWAP ----------------------------------------------------------
    twap_lookback_s: int = 60         # overridden live by cryptoMarketConfig

    # --- TWAP prediction ----------------------------------------------
    # Every interval, project the rolling TWAP forward by holding the
    # current Chainlink price (the rolling integral makes the near
    # future largely deterministic) and score against actuals later.
    prediction_interval_ms: int = 5_000
    prediction_horizon_ms: int = 30_000
    prediction_hit_bps: float = 1.0   # |error| <= this counts as a value hit

    # --- metrics -------------------------------------------------------
    momentum_windows_ms: tuple[int, ...] = (250, 500, 1000, 2000, 5000)
    imbalance_windows_ms: tuple[int, ...] = (1000, 5000, 30000)
    direction_lookback: int = 25
    velocity_baseline_ms: int = 30_000
    pressure_weights: dict[str, float] = field(default_factory=lambda: {
        "volume_imbalance": 0.40,
        "tick_direction": 0.25,
        "momentum": 0.25,
        "tick_velocity": 0.10,
    })
    pressure_imbalance_window_ms: int = 1000
    pressure_momentum_window_ms: int = 1000
    pressure_momentum_scale_bps: float = 4.0   # tanh scale, asset-agnostic
    pressure_strong_threshold: float = 60.0

    # --- event detection ----------------------------------------------
    momentum_event_bps: float = 6.0       # |1s move| in bps -> MOMENTUM event
    volume_spike_ratio: float = 3.0       # 1s vol vs 30s baseline -> VOL_SPIKE
    quote_swing_cents: float = 5.0        # UP bid move within window -> QUOTE_SWING
    quote_swing_window_ms: int = 2000
    target_cross_min_delta: float = 0.0   # any sign flip counts

    # --- buffers -------------------------------------------------------
    ticks_max: int = 50_000
    ref_max: int = 4_000
    quotes_max: int = 4_000
    events_max: int = 300
    init_ticks_seconds: int = 360   # panning back needs history on load


CONFIG = Config()
