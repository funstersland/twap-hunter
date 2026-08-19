"""Paper-trading bots: strategies + simulated execution + manager.

STRICTLY PAPER: fills are simulated against the REAL displayed CLOB
top-of-book (price + size), Polymarket's crypto taker fee is modeled
(fee = rate * shares * min(p, 1-p), taker only), and settlement uses the
OFFICIAL Polymarket resolution — local TWAP math settles immediately
only when the round wasn't close (>= 5 bps), because live testing showed
local math flips thin rounds. No real order ever leaves this process.

Persistence: SQLite via backend.storage (bots, every trade, rounds).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import httpx

from backend.config import ASSETS, CONFIG
from backend.engine.pipeline import round_label_et
from backend.feeds.activity import ActivityFeed
from backend.feeds.polymarket import parse_resolution
from backend.storage import storage

logger = logging.getLogger(__name__)

LEGACY_JSON = Path(__file__).resolve().parent.parent.parent / "data" / "bots.json"

FEE_RATE = 0.07            # Polymarket crypto_fees_v2: taker only
MIN_ORDER_SHARES = 1.0     # paper: allow $1 lots (real CLOB min is 5 shares)
MAX_TRADES_KEPT = 300      # in-memory per bot; full history lives in SQLite
LOCAL_SETTLE_MARGIN_BPS = 5.0    # local math may settle instantly beyond this
OFFICIAL_WAIT_MS = 90_000        # else wait this long for the official outcome
RECONCILE_TTL_MS = 15 * 60_000   # keep provisional settles correctable this long
VOID_DEADLINE_MS = 10 * 60_000   # only void after every lookup avenue failed
COPY_ALL = "ALL"                 # copy bots follow their leader on EVERY market


def bot_assets(bot: dict) -> list[str]:
    """The pipeline assets a bot needs alive."""
    if bot.get("asset") == COPY_ALL:
        return list(ASSETS.keys())
    return [bot["asset"]]


# Pre-created on fresh installs (e.g. Railway) — matches live BTC lock-in cut bot.
SEED_PAPER_BOTS: list[dict] = [
    {
        "name": "BTC Lock In Small Loss",
        "asset": "BTC",
        "strategy": "twap_lockin_cut",
        "params": {
            "entry_window_s": 90,
            "min_margin_bps": 0.3,
            "max_entry_cents": 95,
            "cut_margin_bps": 0.5,
            "cut_confirm_s": 2,
            "order_usd": 1,
        },
        "status": "running",
        "start_balance": 1000.0,
    },
]

# ---------------------------------------------------------------------------
# Strategy catalog
# ---------------------------------------------------------------------------

STRATEGIES: dict[str, dict] = {
    "twap_lockin": {
        "name": "TWAP Lock-In",
        "tagline": "The math edge: bet late, when the final TWAP is nearly decided.",
        "description": (
            "In the last part of a round the final TWAP is mostly locked in "
            "(it averages prices already printed). This bot projects the final "
            "TWAP; when the projection clears the price-to-beat by a margin but "
            "the market still sells that side below your max price, it buys and "
            "holds to settlement."
        ),
        "params": [
            {"key": "entry_window_s", "label": "Enter in last (s)", "default": 55, "min": 15, "max": 240},
            {"key": "min_margin_bps", "label": "Min projected margin (bps)", "default": 2.5, "min": 0.1, "max": 50, "step": 0.1},
            {"key": "max_entry_cents", "label": "Max entry price (¢)", "default": 88, "min": 50, "max": 95},
            {"key": "order_usd", "label": "Order size ($)", "default": 1, "min": 1, "max": 500},
        ],
    },
    "twap_lockin_cut": {
        "name": "TWAP Lock-In + Cut Loss",
        "tagline": "The lock-in edge, but never eat a full loss — bail when the math flips.",
        "description": (
            "Identical entry to TWAP Lock-In: buy the projected side late in "
            "the round. But instead of blindly holding, it keeps watching the "
            "projected final TWAP — if the resolution flips against the "
            "position (confirmed for a moment), it sells at market for a SMALL "
            "loss rather than riding a wrong side to a total loss."
        ),
        "params": [
            {"key": "entry_window_s", "label": "Enter in last (s)", "default": 90, "min": 15, "max": 240},
            {"key": "min_margin_bps", "label": "Min projected margin (bps)", "default": 1.5, "min": 0.1, "max": 50, "step": 0.1},
            {"key": "max_entry_cents", "label": "Max entry price (¢)", "default": 88, "min": 50, "max": 95},
            {"key": "cut_margin_bps", "label": "Cut when against by (bps)", "default": 0.5, "min": 0.0, "max": 10, "step": 0.1},
            {"key": "cut_confirm_s", "label": "Cut confirmation (s)", "default": 2, "min": 0, "max": 15},
            {"key": "order_usd", "label": "Order size ($)", "default": 1, "min": 1, "max": 500},
        ],
    },
    "winning_hold": {
        "name": "Winning Hold",
        "tagline": "Buy the favorite near even odds, hold — switch sides if the math turns.",
        "description": (
            "Enters early while a side still costs near 50¢, picking the side "
            "the TWAP projection favors. Then it HOLDS. If the projected final "
            "flips against the held side by a margin and stays there for a "
            "confirmation period, it sells the loser and switches to the other "
            "side (up to a max number of switches); otherwise it rides the "
            "position to settlement."
        ),
        "params": [
            {"key": "entry_min_cents", "label": "Min entry (¢)", "default": 40, "min": 20, "max": 60},
            {"key": "entry_max_cents", "label": "Max entry (¢)", "default": 60, "min": 40, "max": 80},
            {"key": "conf_bps", "label": "Entry signal margin (bps)", "default": 0.5, "min": 0.0, "max": 10, "step": 0.1},
            {"key": "switch_margin_bps", "label": "Switch when against by (bps)", "default": 1.2, "min": 0.2, "max": 20, "step": 0.1},
            {"key": "switch_confirm_s", "label": "Confirmation (s)", "default": 4, "min": 1, "max": 30},
            {"key": "switch_max_cents", "label": "Max switch price (¢)", "default": 78, "min": 50, "max": 95},
            {"key": "stop_switch_s", "label": "No switches in last (s)", "default": 20, "min": 5, "max": 120},
            {"key": "max_switches", "label": "Max switches / round", "default": 3, "min": 0, "max": 8},
            {"key": "order_usd", "label": "Order size ($)", "default": 1, "min": 1, "max": 500},
        ],
    },
    "cross_line": {
        "name": "Cross Line",
        "tagline": "Fire the instant the TWAP crosses the line — exit your way.",
        "description": (
            "The moment the rolling TWAP crosses the price-to-beat line by a "
            "real margin, the likely outcome flips — this bot buys the "
            "crossing side immediately. Exits are configurable: a fixed "
            "profit target in cents (5/10/15), or hold until MOMENTUM "
            "REVERSES against the position (sustained counter-move), with a "
            "safety stop always armed. Anything still open settles at round "
            "end."
        ),
        "params": [
            {"key": "min_cross_bps", "label": "Cross must clear PTB by (bps)", "default": 0.5, "min": 0.1, "max": 10, "step": 0.1},
            {"key": "max_entry_cents", "label": "Max entry price (¢)", "default": 78, "min": 5, "max": 99},
            {"key": "tp_cents", "label": "Profit target (¢, 0 = none)", "default": 5, "min": 0, "max": 50},
            {"key": "use_momrev", "label": "Exit on momentum reverse (1=yes)", "default": 0, "min": 0, "max": 1},
            {"key": "rev_bps", "label": "Reverse momentum trigger (bps/1s)", "default": 2.0, "min": 0.3, "max": 20, "step": 0.1},
            {"key": "rev_confirm_s", "label": "Reverse confirmation (s)", "default": 2, "min": 0, "max": 15},
            {"key": "sl_cents", "label": "Safety stop loss (¢)", "default": 12, "min": 2, "max": 50},
            {"key": "order_usd", "label": "Order size ($)", "default": 1, "min": 1, "max": 500},
            {"key": "max_entries", "label": "Max entries / round", "default": 2, "min": 1, "max": 10},
        ],
    },
    "next_round_scalp": {
        "name": "Next Round Scalping",
        "tagline": "Ride last round's dying momentum into the fresh 50¢ open.",
        "description": (
            "In the final seconds of each round it records the momentum and "
            "pressure. If the move was strong, it buys that side in the NEW "
            "round's opening seconds while the quote still sits near 50¢ — "
            "betting the move carries into the fresh books. Pure scalp: takes "
            "a few cents profit, stops out fast, and never holds past its "
            "time limit. The continuous variant keeps re-scalping live "
            "momentum bursts all round."
        ),
        "params": [
            {"key": "signal_bps_min", "label": "Min end-of-round momentum (bps)", "default": 2.0, "min": 0.3, "max": 20, "step": 0.1},
            {"key": "capture_last_s", "label": "Capture signal in last (s)", "default": 8, "min": 3, "max": 30},
            {"key": "entry_window_s", "label": "Enter within first (s)", "default": 20, "min": 5, "max": 60},
            {"key": "entry_min_cents", "label": "Min entry (¢)", "default": 49, "min": 30, "max": 55},
            {"key": "entry_max_cents", "label": "Max entry (¢)", "default": 51, "min": 45, "max": 70},
            {"key": "tp_cents", "label": "Take profit (¢)", "default": 3, "min": 1, "max": 20},
            {"key": "sl_cents", "label": "Stop loss (¢)", "default": 4, "min": 1, "max": 20},
            {"key": "max_hold_s", "label": "Max hold (s)", "default": 45, "min": 10, "max": 240},
            {"key": "order_usd", "label": "Order size ($)", "default": 1, "min": 1, "max": 500},
            {"key": "continuous", "label": "Keep scalping all round (1=yes)", "default": 0, "min": 0, "max": 1},
            {"key": "max_entries", "label": "Max scalps / round (continuous)", "default": 4, "min": 1, "max": 12},
            {"key": "re_entry_min_cents", "label": "Re-scalp min entry (¢)", "default": 40, "min": 10, "max": 60},
            {"key": "re_entry_max_cents", "label": "Re-scalp max entry (¢)", "default": 60, "min": 40, "max": 90},
        ],
    },
    "copy_trader": {
        "name": "Copy Trader",
        "tagline": "Mirror a profitable Polymarket profile's trades in real time.",
        "description": (
            "Watches the platform's live trade feed for one wallet (paste it "
            "from their profile URL) and copies them EVERYWHERE — crypto "
            "rounds, sports, politics, anything. Crypto 5m copies fill "
            "against our live books; other markets fill at the leader's own "
            "price plus a slippage allowance and are held as a portfolio "
            "that redeems when each market resolves. Dust trades and stale "
            "signals are filtered."
        ),
        "params": [
            {"key": "wallet", "label": "Leader wallet (0x…)", "type": "text", "default": ""},
            {"key": "copy_sells", "label": "Copy sells too (1=yes 0=no)", "default": 1, "min": 0, "max": 1},
            {"key": "min_leader_size", "label": "Ignore trades below (shares)", "default": 20, "min": 0, "max": 10000},
            {"key": "max_copy_delay_s", "label": "Skip signals older than (s)", "default": 4, "min": 1, "max": 30},
            {"key": "min_copy_gap_s", "label": "Min gap between entries (s)", "default": 3, "min": 0, "max": 60},
            {"key": "max_entry_cents", "label": "Max entry price (¢)", "default": 95, "min": 5, "max": 99},
            {"key": "order_usd", "label": "Order size ($)", "default": 1, "min": 1, "max": 500},
            {"key": "slippage_cents", "label": "Non-crypto fill slippage (¢)", "default": 1, "min": 0, "max": 10},
            {"key": "max_open", "label": "Max open copies (other markets)", "default": 10, "min": 1, "max": 50},
        ],
    },
    "dip_harvester": {
        "name": "Dip Harvester (Anon-style)",
        "tagline": "Buy BOTH sides' panic dips — pairs lock profit, the skew rides.",
        "description": (
            "Reverse-engineered from a +$146/round BTC profile: 5-minute "
            "quotes whipsaw violently, so this bot buys UP when UP dips hard "
            "below its recent high, and DOWN when DOWN dips — accumulating "
            "BOTH piles at panic prices. Every UP+DOWN pair bought for under "
            "100¢ combined is locked profit at settlement regardless of the "
            "outcome; the unpaired excess rides the dips you caught most of. "
            "Holds everything to settlement."
        ),
        "params": [
            {"key": "dip_cents", "label": "Dip trigger (¢ below recent high)", "default": 12, "min": 3, "max": 40},
            {"key": "lookback_s", "label": "Recent-high lookback (s)", "default": 60, "min": 10, "max": 240},
            {"key": "max_price_cents", "label": "Max buy price (¢)", "default": 80, "min": 10, "max": 95},
            {"key": "order_usd", "label": "$ per dip buy", "default": 1, "min": 1, "max": 100},
            {"key": "per_side_max_usd", "label": "Max $ per side / round", "default": 3, "min": 1, "max": 1000},
            {"key": "min_gap_s", "label": "Min gap between buys (s)", "default": 4, "min": 0, "max": 60},
            {"key": "no_buy_last_s", "label": "No buys in last (s)", "default": 15, "min": 5, "max": 60},
        ],
    },
    "pressure_scalp": {
        "name": "Pressure Scalp",
        "tagline": "Ride the order-flow pressure, take quick cents.",
        "description": (
            "When the composite buy/sell pressure score crosses your threshold, "
            "buy that side (within your entry price range). Take profit or stop "
            "out in cents; anything still open settles at round end. NOTE: live "
            "testing went 0W/11L — the quote reprices before the signal. Kept "
            "for experimentation."
        ),
        "params": [
            {"key": "pressure_min", "label": "Pressure threshold", "default": 65, "min": 30, "max": 95},
            {"key": "entry_min_cents", "label": "Min entry (¢)", "default": 35, "min": 1, "max": 95},
            {"key": "entry_max_cents", "label": "Max entry (¢)", "default": 68, "min": 5, "max": 99},
            {"key": "tp_cents", "label": "Take profit (¢)", "default": 10, "min": 1, "max": 50},
            {"key": "sl_cents", "label": "Stop loss (¢)", "default": 14, "min": 1, "max": 50},
            {"key": "order_usd", "label": "Order size ($)", "default": 1, "min": 1, "max": 500},
            {"key": "max_entries", "label": "Max entries / round", "default": 3, "min": 1, "max": 10},
        ],
    },
    "momentum_rider": {
        "name": "Momentum Rider",
        "tagline": "Jump on sharp 1-second price moves before quotes reprice.",
        "description": (
            "When the underlying price moves hard within one second (momentum in "
            "bps beyond your trigger), buy the side of the move — the idea is the "
            "prediction-market quote lags the spot move. Exits by take-profit / "
            "stop-loss in cents, otherwise settles at round end."
        ),
        "params": [
            {"key": "mom_bps_min", "label": "Momentum trigger (bps)", "default": 2.5, "min": 0.5, "max": 50, "step": 0.5},
            {"key": "mom_window_s", "label": "Momentum window (s)", "default": 1, "min": 1, "max": 10},
            {"key": "entry_min_cents", "label": "Min entry (¢)", "default": 30, "min": 1, "max": 95},
            {"key": "entry_max_cents", "label": "Max entry (¢)", "default": 75, "min": 5, "max": 99},
            {"key": "tp_cents", "label": "Take profit (¢)", "default": 8, "min": 1, "max": 50},
            {"key": "sl_cents", "label": "Stop loss (¢)", "default": 8, "min": 1, "max": 50},
            {"key": "order_usd", "label": "Order size ($)", "default": 1, "min": 1, "max": 500},
            {"key": "max_entries", "label": "Max entries / round", "default": 3, "min": 1, "max": 10},
        ],
    },
    "cross_hunter": {
        "name": "Cross Hunter",
        "tagline": "Buy the instant the TWAP crosses the price-to-beat.",
        "description": (
            "The moment the rolling TWAP clears the price-to-beat by a real "
            "margin, the likely outcome flips — but quotes often lag. This bot "
            "buys the crossing side immediately if it is still cheap enough, "
            "with cent-based take-profit / stop-loss, otherwise holds to "
            "settlement."
        ),
        "params": [
            {"key": "max_entry_cents", "label": "Max entry price (¢)", "default": 78, "min": 5, "max": 99},
            {"key": "tp_cents", "label": "Take profit (¢)", "default": 10, "min": 1, "max": 50},
            {"key": "sl_cents", "label": "Stop loss (¢)", "default": 10, "min": 1, "max": 50},
            {"key": "order_usd", "label": "Order size ($)", "default": 1, "min": 1, "max": 500},
            {"key": "max_entries", "label": "Max entries / round", "default": 2, "min": 1, "max": 10},
        ],
    },
}


def strategy_catalog() -> list[dict]:
    return [
        {"id": key, "name": s["name"], "tagline": s["tagline"],
         "description": s["description"], "params": s["params"]}
        for key, s in STRATEGIES.items()
    ]


def default_params(strategy: str) -> dict:
    return {p["key"]: p["default"] for p in STRATEGIES[strategy]["params"]}


def sanitize_params(strategy: str, given: dict) -> dict:
    """Clamp user-supplied params to the strategy schema."""
    out: dict = {}
    schema = {p["key"]: p for p in STRATEGIES[strategy]["params"]}
    for key, value in (given or {}).items():
        spec = schema.get(key)
        if spec is None:
            continue
        if spec.get("type") == "text":
            out[key] = str(value).strip()[:80]
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        v = max(float(spec.get("min", v)), min(float(spec.get("max", v)), v))
        out[key] = v
    return out


def taker_fee(shares: float, price_usd: float) -> float:
    return FEE_RATE * shares * min(price_usd, 1.0 - price_usd)


class BotManager:
    """Owns all bots; evaluates running ones every 250 ms."""

    def __init__(self, hub) -> None:
        self._hub = hub
        self.bots: dict[str, dict] = {}
        self.activity = ActivityFeed()   # shared by all copy bots
        self._rt: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._http: httpx.AsyncClient | None = None
        self._dirty: set[str] = set()
        self._last_save = 0.0
        self._retain_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle + persistence
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=15.0)
        self.activity.start()
        self._load()
        self._task = asyncio.create_task(self._loop(), name="bot-manager")
        self._retain_task = asyncio.create_task(
            self._retain_running(), name="bot-retain",
        )
        if self.bots:
            logger.info("bot manager: %d bot(s) loaded", len(self.bots))

    async def _retain_running(self) -> None:
        """Start market pipelines in the background so HTTP /health is instant."""
        for bot in self.bots.values():
            if bot["status"] != "running":
                continue
            for asset in bot_assets(bot):
                try:
                    await self._hub.retain(asset, bot["id"])
                except Exception:
                    logger.exception("retain failed for %s on %s", bot["id"], asset)

    async def stop(self) -> None:
        await self.activity.stop()
        if self._retain_task is not None:
            self._retain_task.cancel()
            try:
                await self._retain_task
            except (asyncio.CancelledError, Exception):
                pass
            self._retain_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        http = getattr(self, "_http", None)
        if http is not None:
            try:
                await http.aclose()
            except Exception:
                pass
            self._http = None
        self._save(force=True)

    def _load(self) -> None:
        self.bots = storage.load_bots()
        if not self.bots and LEGACY_JSON.exists():
            self._migrate_legacy_json()
        # Copy bots follow their leader everywhere — enforce asset=ALL
        # (also migrates bots created before multi-market copying).
        for bot in self.bots.values():
            if bot.get("strategy") == "copy_trader" and bot.get("asset") != COPY_ALL:
                logger.info("migrating copy bot %s to ALL markets", bot["name"])
                bot["asset"] = COPY_ALL
                storage.save_bot(bot)
        self._seed_defaults()

    def _seed_defaults(self) -> None:
        """Ensure starter paper bots exist on fresh deployments."""
        existing = {
            (b.get("name"), b.get("asset"), b.get("strategy"))
            for b in self.bots.values()
        }
        for spec in SEED_PAPER_BOTS:
            key = (spec["name"], spec["asset"], spec["strategy"])
            if key in existing:
                continue
            bot = {
                "id": "b_" + uuid.uuid4().hex[:8],
                "name": spec["name"][:40],
                "asset": spec["asset"],
                "strategy": spec["strategy"],
                "params": sanitize_params(spec["strategy"], spec["params"]),
                "status": spec.get("status", "stopped"),
                "start_balance": float(spec.get("start_balance", 1000.0)),
                "balance": float(spec.get("start_balance", 1000.0)),
                "position": None,
                "trades": [],
                "stats": {"rounds": 0, "wins": 0, "losses": 0, "fees": 0.0},
                "created_ms": time.time() * 1000.0,
            }
            self.bots[bot["id"]] = bot
            storage.save_bot(bot)
            logger.info("seeded paper bot %s (%s)", bot["name"], bot["id"])

    def _migrate_legacy_json(self) -> None:
        """One-time import of data/bots.json into SQLite (with trades)."""
        try:
            data = json.loads(LEGACY_JSON.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            for bot_id, bot in data.items():
                if not isinstance(bot, dict):
                    continue
                self.bots[bot_id] = bot
                storage.save_bot(bot)
                for trade in bot.get("trades", []):
                    storage.add_trade(bot_id, trade)
            LEGACY_JSON.rename(LEGACY_JSON.with_suffix(".json.migrated"))
            logger.info("bot manager: migrated %d bot(s) from bots.json", len(self.bots))
        except Exception:
            logger.exception("bot manager: legacy migration failed")

    def _save(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (not self._dirty or now - self._last_save < 2.0):
            return
        for bot_id in list(self._dirty):
            bot = self.bots.get(bot_id)
            if bot is not None:
                try:
                    storage.save_bot(bot)
                except Exception:
                    logger.debug("bot save failed", exc_info=True)
        self._dirty.clear()
        self._last_save = now

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(self, name: str, asset: str, strategy: str,
                     params: dict, start_balance: float) -> dict:
        bot = {
            "id": "b_" + uuid.uuid4().hex[:8],
            "name": name[:40] or STRATEGIES[strategy]["name"],
            "asset": asset,
            "strategy": strategy,
            "params": params,
            "status": "stopped",
            "start_balance": start_balance,
            "balance": start_balance,
            "position": None,
            "trades": [],
            "stats": {"rounds": 0, "wins": 0, "losses": 0, "fees": 0.0},
            "created_ms": time.time() * 1000.0,
        }
        self.bots[bot["id"]] = bot
        storage.save_bot(bot)
        return bot

    async def set_running(self, bot_id: str, running: bool) -> dict | None:
        bot = self.bots.get(bot_id)
        if bot is None:
            return None
        if running and bot["status"] != "running":
            bot["status"] = "running"
            for asset in bot_assets(bot):
                await self._hub.retain(asset, bot_id)
            self._log(bot, "START", None, None, None, None, "bot started")
        elif not running and bot["status"] == "running":
            bot["status"] = "stopped"
            pos = bot["position"]
            if pos is not None:
                pipe = self._hub.pipelines.get(pos.get("asset") or bot["asset"])
                if pipe is not None:
                    self.sell(bot, pipe, "flatten on stop")
            for asset in bot_assets(bot):
                await self._hub.unretain(asset, bot_id)
            self._log(bot, "STOP", None, None, None, None, "bot stopped")
        self._save(force=True)
        return bot

    async def update(self, bot_id: str, name: str | None = None,
                     params: dict | None = None) -> dict | None:
        bot = self.bots.get(bot_id)
        if bot is None:
            return None
        if params:
            bot["params"].update(sanitize_params(bot["strategy"], params))
        if name:
            bot["name"] = str(name)[:40]
        storage.save_bot(bot)
        return bot

    async def reset(self, bot_id: str) -> dict | None:
        """Fresh balance + cleared history; strategy/params/status stay."""
        bot = self.bots.get(bot_id)
        if bot is None:
            return None
        bot["balance"] = bot["start_balance"]
        bot["position"] = None
        bot["trades"] = []
        bot["stats"] = {"rounds": 0, "wins": 0, "losses": 0, "fees": 0.0}
        bot.pop("pending_official", None)
        self._rt.pop(bot_id, None)
        storage.delete_trades(bot_id)
        self._log(bot, "RESET", None, None, None, None,
                  f"reset to fresh ${bot['start_balance']:.0f} balance")
        storage.save_bot(bot)
        return bot

    async def delete(self, bot_id: str) -> bool:
        bot = self.bots.pop(bot_id, None)
        if bot is None:
            return False
        if bot["status"] == "running":
            for asset in bot_assets(bot):
                await self._hub.unretain(asset, bot_id)
        self._rt.pop(bot_id, None)
        storage.delete_bot(bot_id)
        storage.delete_trades(bot_id)
        return True

    def public_view(self, bot: dict, trades: int = 8, include_brain: bool = False) -> dict:
        pipe = self._hub.pipelines.get(bot["asset"])
        equity = bot["balance"]
        pos = bot["position"]
        pos_view = None
        if pos is not None:
            mark_pipe = self._hub.pipelines.get(pos.get("asset") or bot["asset"]) or pipe
            mark = None
            if mark_pipe is not None and mark_pipe.last_quote:
                key = "up_bid" if pos["side"] == "UP" else "down_bid"
                mark = mark_pipe.last_quote.get(key)
            mark_usd = (mark / 100.0) if mark is not None else pos["entry_cents"] / 100.0
            equity += pos["shares"] * mark_usd
            pos_view = {
                "side": pos["side"], "shares": round(pos["shares"], 2),
                "entry_cents": pos["entry_cents"],
                "mark_cents": mark,
                "cost": round(pos["cost"], 2),
                "window_id": pos["window_id"],
                "round_label": round_label_et(pos["window_id"]),
                "waiting_official": bool(pos.get("awaiting_official")),
                "asset": pos.get("asset") or bot["asset"],
            }
        book = bot.get("book")
        book_view = None
        if book and (book["UP"]["shares"] or book["DOWN"]["shares"]):
            up_sh, down_sh = book["UP"]["shares"], book["DOWN"]["shares"]
            pairs = min(up_sh, down_sh)
            total_cost = (book["UP"]["cost"] + book["UP"]["fees"]
                          + book["DOWN"]["cost"] + book["DOWN"]["fees"])
            # Mark: pairs are worth $1 each; excess marked at its bid.
            excess_side = "UP" if up_sh > down_sh else "DOWN"
            excess = abs(up_sh - down_sh)
            mark = None
            if pipe is not None and pipe.last_quote:
                mark = pipe.last_quote.get(f"{excess_side.lower()}_bid")
            excess_val = excess * ((mark / 100.0) if mark is not None else 0.5)
            equity += pairs + excess_val
            book_view = {
                "up_shares": round(up_sh, 1), "down_shares": round(down_sh, 1),
                "pairs": round(pairs, 1),
                "cost": round(total_cost, 2),
                "pair_edge": round(pairs - total_cost, 2),
                "window_id": book["window_id"],
                "round_label": round_label_et(book["window_id"]),
            }
        pf = bot.get("portfolio") or {}
        pf_view = []
        for pos_g in pf.values():
            mark = pos_g.get("mark_cents", pos_g["entry_cents"])
            equity += pos_g["shares"] * mark / 100.0
            pf_view.append({
                "title": pos_g.get("title") or pos_g["event_slug"],
                "outcome": pos_g["outcome"],
                "shares": round(pos_g["shares"], 1),
                "entry_cents": round(pos_g["entry_cents"], 1),
                "mark_cents": round(mark, 1),
                "cost": round(pos_g["cost"], 2),
                "upnl": round(pos_g["shares"] * mark / 100.0 - pos_g["cost"], 2),
            })
        pf_view.sort(key=lambda x: -abs(x["upnl"]))
        view = {
            "id": bot["id"], "name": bot["name"], "asset": bot["asset"],
            "strategy": bot["strategy"],
            "strategy_name": STRATEGIES.get(bot["strategy"], {}).get("name", bot["strategy"]),
            "params": bot["params"], "status": bot["status"],
            "start_balance": bot["start_balance"],
            "balance": round(bot["balance"], 2),
            "equity": round(equity, 2),
            "pnl": round(equity - bot["start_balance"], 2),
            "pnl_pct": round((equity / bot["start_balance"] - 1) * 100, 2) if bot["start_balance"] else 0,
            "position": pos_view,
            "book": book_view,
            "portfolio": pf_view,
            "stats": bot["stats"],
            "trades": bot["trades"][-trades:][::-1],
            "created_ms": bot["created_ms"],
        }
        if include_brain:
            view["brain"] = self.brain(bot)
        return view

    # ------------------------------------------------------------------
    # Live brain (what does the strategy see RIGHT NOW?)
    # ------------------------------------------------------------------

    def brain(self, bot: dict) -> dict:
        pipe = self._hub.pipelines.get(bot["asset"])
        if pipe is None and bot["asset"] == COPY_ALL:
            pipe = next(iter(self._hub.pipelines.values()), None)
        if pipe is None:
            return {"summary": "market pipeline offline", "conditions": []}
        rt = self._rt.setdefault(bot["id"], _fresh_rt())
        fn = _BRAINS.get(bot["strategy"])
        if fn is None:
            return {"summary": "no introspection for this strategy", "conditions": []}
        try:
            return fn(self, bot, rt, pipe)
        except Exception:
            logger.debug("brain failed", exc_info=True)
            return {"summary": "brain error", "conditions": []}

    # ------------------------------------------------------------------
    # Evaluation loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            try:
                for bot in list(self.bots.values()):
                    pipe = self._hub.pipelines.get(bot["asset"])
                    if pipe is None and bot["asset"] == COPY_ALL:
                        pipe = next(iter(self._hub.pipelines.values()), None)
                    if pipe is None:
                        continue
                    pos = bot["position"]
                    if pos is not None:
                        # Settle against the POSITION's own market (copy
                        # bots hold positions on any asset).
                        pos_pipe = self._hub.pipelines.get(
                            pos.get("asset") or bot["asset"]) or pipe
                        if pos["window_id"] != pos_pipe.window_id:
                            await self._try_settle(bot, pos_pipe, pos)
                    book = bot.get("book")
                    if (book and book.get("window_id") != pipe.window_id
                            and (book["UP"]["shares"] or book["DOWN"]["shares"])):
                        await self._try_settle_book(bot, pipe, book)
                    self._reconcile(bot, pipe)
                    await self._manage_portfolio(bot)
                    if bot["status"] == "running":
                        self._evaluate(bot, pipe)
                self._save()
            except Exception:
                logger.exception("bot manager loop error")

    def _evaluate(self, bot: dict, pipe) -> None:
        rt = self._rt.setdefault(bot["id"], _fresh_rt())
        now = pipe.poly_now_ms()
        wid = pipe.window_id
        if rt.get("round") != wid:
            rt["round"] = wid
            rt["entries"] = {}
            rt["switches"] = 0
            rt["adverse_since"] = None
        if bot["position"] is not None and bot["position"]["window_id"] != wid:
            return  # waiting for settlement — no new activity till resolved
        if lock_profit_if_bid(self, bot, pipe):
            return
        fn = _EVALUATORS.get(bot["strategy"])
        if fn is not None:
            fn(self, bot, rt, pipe, now)

    # ------------------------------------------------------------------
    # Paper execution
    # ------------------------------------------------------------------

    def _quote(self, pipe, side: str):
        q = pipe.last_quote
        if not q:
            return None, None, None
        s = side.lower()
        return q.get(f"{s}_ask"), q.get(f"{s}_ask_size"), q.get(f"{s}_bid")

    def buy(self, bot: dict, rt: dict, pipe, side: str, order_usd: float,
            max_entry_cents: float, reason: str) -> bool:
        if bot["position"] is not None:
            return False
        now = pipe.poly_now_ms()
        if pipe.window_end_ms - now < 5000 or now - pipe.window_start_ms < 2000:
            return False
        ask, ask_size, bid = self._quote(pipe, side)
        if ask is None or ask > max_entry_cents or ask <= 0 or ask >= 100:
            return False
        if bid is None or bid <= 0 or (ask - bid) > 20:
            return False
        price = ask / 100.0
        shares = order_usd / price
        if ask_size is not None:
            shares = min(shares, ask_size)
        if shares < MIN_ORDER_SHARES:
            return False
        cost = shares * price
        fee = taker_fee(shares, price)
        if cost + fee > bot["balance"]:
            return False
        bot["balance"] -= cost + fee
        bot["stats"]["fees"] = round(bot["stats"]["fees"] + fee, 4)
        bot["position"] = {
            "side": side, "shares": shares, "cost": cost,
            "entry_cents": ask, "fee_paid": fee,
            "window_id": pipe.window_id, "entry_t": now,
            "asset": pipe.asset.key,
        }
        wid = pipe.window_id
        rt["entries"][wid] = rt["entries"].get(wid, 0) + 1
        self._log(bot, "BUY", side, ask, shares, -(cost + fee), reason, wid)
        return True

    def buy_book(self, bot: dict, rt: dict, pipe, side: str, order_usd: float,
                 max_entry_cents: float, reason: str) -> bool:
        """Accumulate into a TWO-SIDED book (dip-harvester style).

        Unlike ``buy``, multiple adds per round and both sides at once
        are allowed. The book settles as a whole: the winning pile pays
        $1/share.
        """
        now = pipe.poly_now_ms()
        if pipe.window_end_ms - now < 5000 or now - pipe.window_start_ms < 2000:
            return False
        book = bot.get("book")
        if book is not None and book.get("window_id") != pipe.window_id:
            return False  # previous round's book still awaiting settlement
        ask, ask_size, bid = self._quote(pipe, side)
        if ask is None or ask > max_entry_cents or ask <= 0 or ask >= 100:
            return False
        if bid is None or bid <= 0 or (ask - bid) > 20:
            return False
        price = ask / 100.0
        shares = order_usd / price
        if ask_size is not None:
            shares = min(shares, ask_size)
        if shares < MIN_ORDER_SHARES:
            return False
        cost = shares * price
        fee = taker_fee(shares, price)
        if cost + fee > bot["balance"]:
            return False
        if book is None:
            book = {"window_id": pipe.window_id,
                    "UP": {"shares": 0.0, "cost": 0.0, "fees": 0.0},
                    "DOWN": {"shares": 0.0, "cost": 0.0, "fees": 0.0}}
            bot["book"] = book
        bot["balance"] -= cost + fee
        bot["stats"]["fees"] = round(bot["stats"]["fees"] + fee, 4)
        pile = book[side]
        pile["shares"] += shares
        pile["cost"] += cost
        pile["fees"] += fee
        self._log(bot, "BUY", side, ask, shares, -(cost + fee), reason, pipe.window_id)
        return True

    async def _try_settle_book(self, bot: dict, pipe, book: dict) -> None:
        """Settle a two-sided book: the winning pile redeems at $1/share."""
        wid = book["window_id"]
        now = pipe.poly_now_ms()
        end_ms = (wid + 1) * CONFIG.window_ms
        label = round_label_et(wid)
        result = pipe.round_results.get(wid)

        outcome = None
        how = None
        provisional = False
        if result is not None and result.get("source") == "market":
            outcome, how = result.get("outcome"), "official resolution"
        if outcome is None:
            db_row = storage.get_round(bot["asset"], wid)
            if db_row is not None and db_row.get("outcome_official"):
                outcome, how = db_row["outcome_official"], "official resolution (backscan)"
        if outcome is None:
            waited = now - end_ms
            if waited < OFFICIAL_WAIT_MS:
                return
            if result is not None and result.get("outcome") is not None:
                outcome = result.get("outcome")
                how = "provisional local math — auto-corrects if official differs"
                provisional = True
            else:
                if now >= book.get("fetch_next", 0):
                    book["fetch_next"] = now + 10_000
                    fetched = await self._fetch_official(bot["asset"], wid)
                    if fetched is not None:
                        outcome, how = fetched, "official resolution (direct lookup)"
                        storage.upsert_round(bot["asset"], wid, outcome_official=fetched)
                if outcome is None:
                    if waited < VOID_DEADLINE_MS:
                        return
        total_cost = (book["UP"]["cost"] + book["UP"]["fees"]
                      + book["DOWN"]["cost"] + book["DOWN"]["fees"])
        up_sh, down_sh = book["UP"]["shares"], book["DOWN"]["shares"]
        if outcome is None:
            bot["balance"] += total_cost
            bot["stats"]["fees"] = round(
                bot["stats"]["fees"] - book["UP"]["fees"] - book["DOWN"]["fees"], 4)
            self._log(bot, "VOID", None, None, up_sh + down_sh, 0.0,
                      f"round {label}: no result — book refunded", wid)
        else:
            proceeds = up_sh if outcome == "UP" else down_sh
            bot["balance"] += proceeds
            pnl = proceeds - total_cost
            self._bump_winloss(bot, pnl)
            pairs = min(up_sh, down_sh)
            self._log(
                bot, "SETTLE", outcome, 100.0 if pnl > 0 else 0.0,
                round(proceeds, 2), pnl,
                f"round {label} resolved {outcome} ({how}) — book U{up_sh:.0f}/"
                f"D{down_sh:.0f} ({pairs:.0f} pairs), winner pile paid ${proceeds:.2f}",
                wid,
            )
            if provisional:
                bot.setdefault("pending_official", {})[str(wid)] = {
                    "book": {"UP": round(up_sh, 4), "DOWN": round(down_sh, 4)},
                    "outcome": outcome, "t_ms": now,
                }
        bot["stats"]["rounds"] += 1
        bot["book"] = None
        self._dirty.add(bot["id"])

    # -- generic portfolio (copying ANY Polymarket market) ---------------

    def gbuy(self, bot: dict, tr: dict, order_usd: float, slippage_cents: float,
             max_open: int, reason: str) -> bool:
        """Open a copy position on an arbitrary market.

        No book feed exists outside the crypto 5m series, so the fill is
        modeled at the LEADER'S price plus a slippage allowance — stated
        honestly in the log.
        """
        token = tr.get("token_id")
        if not token or tr.get("outcome_index") is None or not tr.get("condition_id"):
            return False
        pf = bot.setdefault("portfolio", {})
        if token in pf or len(pf) >= max_open:
            return False
        price_c = min(99.0, tr["price_cents"] + slippage_cents)
        price = price_c / 100.0
        if price <= 0:
            return False
        shares = order_usd / price
        cost = shares * price
        if cost > bot["balance"]:
            return False
        bot["balance"] -= cost
        pf[token] = {
            "token_id": token,
            "condition_id": tr["condition_id"],
            "outcome": tr["outcome"],
            "outcome_index": tr["outcome_index"],
            "event_slug": tr["event_slug"],
            "title": tr.get("title") or tr["event_slug"],
            "shares": shares, "cost": cost, "entry_cents": price_c,
            "mark_cents": price_c,
            "opened_ms": time.time() * 1000.0,
        }
        self._log(bot, "BUY", tr["outcome"][:8], price_c, shares, -cost, reason,
                  label=tr.get("title") or tr["event_slug"])
        self._dirty.add(bot["id"])
        return True

    def gsell(self, bot: dict, token: str, price_cents: float, reason: str) -> bool:
        pf = bot.get("portfolio") or {}
        pos = pf.get(token)
        if pos is None:
            return False
        price_c = max(1.0, price_cents)
        proceeds = pos["shares"] * price_c / 100.0
        bot["balance"] += proceeds
        pnl = proceeds - pos["cost"]
        self._bump_winloss(bot, pnl)
        self._log(bot, "SELL", pos["outcome"][:8], price_c, pos["shares"], pnl,
                  reason, label=pos.get("title") or pos["event_slug"])
        pf.pop(token, None)
        self._dirty.add(bot["id"])
        return True

    async def _manage_portfolio(self, bot: dict) -> None:
        """Refresh marks and redeem resolved markets (throttled to 60s)."""
        pf = bot.get("portfolio")
        if not pf or self._http is None:
            return
        rt = self._rt.setdefault(bot["id"], _fresh_rt())
        now = time.time() * 1000.0
        if now < rt.get("pf_next", 0):
            return
        rt["pf_next"] = now + 60_000
        for token in list(pf.keys()):
            pos = pf.get(token)
            if pos is None:
                continue
            # Resolution first (Gamma by condition id).
            try:
                resp = await self._http.get(
                    f"{CONFIG.gamma_base_url}/markets",
                    params={"condition_ids": pos["condition_id"]},
                )
                markets = resp.json() if resp.status_code == 200 else []
                market = markets[0] if isinstance(markets, list) and markets else None
                if isinstance(market, dict):
                    prices = market.get("outcomePrices")
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                    values = [float(x) for x in prices] if isinstance(prices, list) else []
                    resolved = str(market.get("umaResolutionStatus") or "").lower() == "resolved"
                    decisive = values and max(values) >= 0.999
                    if values and (resolved or decisive):
                        idx = pos["outcome_index"]
                        won = 0 <= idx < len(values) and values[idx] >= 0.999
                        proceeds = pos["shares"] if won else 0.0
                        bot["balance"] += proceeds
                        pnl = proceeds - pos["cost"]
                        self._bump_winloss(bot, pnl)
                        self._log(
                            bot, "SETTLE", pos["outcome"][:8],
                            100.0 if won else 0.0, pos["shares"], pnl,
                            f"market resolved — {'WON' if won else 'lost'} "
                            f"({pos['outcome']})",
                            label=pos.get("title") or pos["event_slug"],
                        )
                        pf.pop(token, None)
                        self._dirty.add(bot["id"])
                        continue
            except Exception:
                pass
            # Mark refresh (public CLOB midpoint).
            try:
                resp = await self._http.get(
                    "https://clob.polymarket.com/midpoint",
                    params={"token_id": token},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    mid = float(data.get("mid") or data.get("price"))
                    if 0 < mid < 1:
                        pos["mark_cents"] = round(mid * 100.0, 1)
            except Exception:
                pass
            await asyncio.sleep(0.15)

    def sell(self, bot: dict, pipe, reason: str) -> bool:
        pos = bot["position"]
        if pos is None or pos["window_id"] != pipe.window_id:
            return False
        _ask, _sz, bid = self._quote(pipe, pos["side"])
        if bid is None or bid <= 0:
            return False
        price = bid / 100.0
        proceeds = pos["shares"] * price
        fee = taker_fee(pos["shares"], price)
        bot["balance"] += proceeds - fee
        bot["stats"]["fees"] = round(bot["stats"]["fees"] + fee, 4)
        pnl = (proceeds - fee) - (pos["cost"] + pos["fee_paid"])
        self._bump_winloss(bot, pnl)
        self._log(bot, "SELL", pos["side"], bid, pos["shares"], pnl, reason, pos["window_id"])
        bot["position"] = None
        return True

    async def _fetch_official(self, asset: str, wid: int) -> str | None:
        """Last-resort direct Gamma lookup for a round's official outcome."""
        if self._http is None:
            return None
        slug = f"{asset.lower()}-updown-5m-{wid * (CONFIG.window_ms // 1000)}"
        try:
            resp = await self._http.get(
                f"{CONFIG.gamma_base_url}/markets",
                params={"slug": slug, "closed": "true"},
            )
            if resp.status_code != 200:
                return None
            return parse_resolution(resp.json())
        except Exception:
            return None

    async def _try_settle(self, bot: dict, pipe, pos: dict) -> None:
        """Settle an ended-round position — OFFICIAL outcome preferred.

        Resolution ladder: in-memory official -> DB official (backscan)
        -> decisive local math -> provisional local math -> direct Gamma
        lookup (survives server restarts that wipe in-memory rounds) ->
        VOID only after 10 minutes with every avenue exhausted.
        """
        wid = pos["window_id"]
        pos_asset = pos.get("asset") or bot["asset"]
        now = pipe.poly_now_ms()
        end_ms = (wid + 1) * CONFIG.window_ms
        result = pipe.round_results.get(wid)
        label = round_label_et(wid)

        outcome = None
        how = None
        if result is not None and result.get("source") == "market":
            outcome = result.get("outcome")
            how = "official resolution"
        if outcome is None:
            db_row = storage.get_round(pos_asset, wid)
            if db_row is not None and db_row.get("outcome_official"):
                outcome = db_row["outcome_official"]
                how = "official resolution (backscan)"
        if outcome is None and result is not None and result.get("outcome") is not None:
            final, ptb = result.get("final_twap"), result.get("ptb")
            margin = abs((final - ptb) / ptb * 10_000.0) if final and ptb else 0.0
            if margin >= LOCAL_SETTLE_MARGIN_BPS:
                outcome = result.get("outcome")
                how = f"local TWAP math (decisive, {margin:.1f} bps)"
        provisional = False
        if outcome is None:
            waited = now - end_ms
            if waited < OFFICIAL_WAIT_MS:
                pos["awaiting_official"] = True
                return  # keep waiting for the official resolution
            if result is not None and result.get("outcome") is not None:
                outcome = result.get("outcome")
                how = "provisional local math — auto-corrects if official differs"
                provisional = True
            else:
                # No local result at all (server restarted mid-round):
                # ask Polymarket directly, retrying every 10s.
                if now >= pos.get("fetch_next", 0):
                    pos["fetch_next"] = now + 10_000
                    fetched = await self._fetch_official(pos_asset, wid)
                    if fetched is not None:
                        outcome = fetched
                        how = "official resolution (direct lookup)"
                        storage.upsert_round(pos_asset, wid, outcome_official=fetched)
                if outcome is None:
                    if waited < VOID_DEADLINE_MS:
                        pos["awaiting_official"] = True
                        return  # keep trying — voiding is the last resort
                    logger.warning(
                        "bot %s: voiding %s round %s — no outcome anywhere",
                        bot["name"], bot["asset"], label,
                    )

        if outcome is None:
            refund = pos["cost"] + pos["fee_paid"]
            bot["balance"] += refund
            bot["stats"]["fees"] = round(bot["stats"]["fees"] - pos["fee_paid"], 4)
            self._log(bot, "VOID", pos["side"], None, pos["shares"], 0.0,
                      f"round {label}: no result — entry refunded", wid)
        else:
            won = outcome == pos["side"]
            proceeds = pos["shares"] * (1.0 if won else 0.0)
            bot["balance"] += proceeds
            pnl = proceeds - (pos["cost"] + pos["fee_paid"])
            self._bump_winloss(bot, pnl)
            self._log(
                bot, "SETTLE", pos["side"], 100.0 if won else 0.0,
                pos["shares"], pnl,
                f"round {label} resolved {outcome} ({how})", wid,
            )
            if provisional:
                bot.setdefault("pending_official", {})[str(wid)] = {
                    "side": pos["side"], "shares": round(pos["shares"], 4),
                    "outcome": outcome, "t_ms": now, "asset": pos_asset,
                }
        bot["stats"]["rounds"] += 1
        bot["position"] = None

    def _reconcile(self, bot: dict, pipe) -> None:
        """Correct provisional settles once the official outcome lands."""
        pending = bot.get("pending_official")
        if not pending:
            return
        now = pipe.poly_now_ms()
        for wid_str in list(pending.keys()):
            entry = pending[wid_str]
            wid = int(wid_str)
            e_asset = entry.get("asset") or bot["asset"]
            e_pipe = self._hub.pipelines.get(e_asset) or pipe
            result = e_pipe.round_results.get(wid)
            official = result.get("outcome") if result and result.get("source") == "market" else None
            if official is None:
                db_row = storage.get_round(e_asset, wid)
                if db_row is not None:
                    official = db_row.get("outcome_official")
            if official is None:
                if now - entry["t_ms"] > RECONCILE_TTL_MS:
                    pending.pop(wid_str, None)
                    self._dirty.add(bot["id"])
                continue
            pending.pop(wid_str, None)
            self._dirty.add(bot["id"])
            if official == entry["outcome"]:
                continue  # provisional verdict confirmed
            if "book" in entry:
                # Two-sided book: swap which pile got paid.
                delta = entry["book"].get(official, 0.0) - entry["book"].get(entry["outcome"], 0.0)
                bot["balance"] += delta
                self._log(
                    bot, "ADJUST", official, None, None, round(delta, 2),
                    f"round {round_label_et(wid)} OFFICIALLY resolved {official} — "
                    f"book re-settled (was {entry['outcome']})", wid,
                )
                continue
            # Official disagrees: flip the settlement.
            won_now = official == entry["side"]
            delta = entry["shares"] * (1.0 if won_now else -1.0)
            bot["balance"] += delta
            stats = bot["stats"]
            if won_now:
                stats["wins"] += 1
                stats["losses"] = max(0, stats["losses"] - 1)
            else:
                stats["wins"] = max(0, stats["wins"] - 1)
                stats["losses"] += 1
            self._log(
                bot, "ADJUST", entry["side"], 100.0 if won_now else 0.0,
                entry["shares"], round(delta, 2),
                f"round {round_label_et(wid)} OFFICIALLY resolved {official} — "
                f"correcting provisional {entry['outcome']}", wid,
            )

    def _bump_winloss(self, bot: dict, pnl: float) -> None:
        if pnl > 0:
            bot["stats"]["wins"] += 1
        elif pnl < 0:
            bot["stats"]["losses"] += 1

    def _log(self, bot: dict, action: str, side, price_cents, shares, pnl,
             reason: str, window_id: int | None = None,
             label: str | None = None) -> None:
        trade = {
            "t": time.time() * 1000.0,
            "window_id": window_id,
            "round_label": label if label is not None
            else (round_label_et(window_id) if window_id is not None else None),
            "action": action,
            "side": side,
            "price_cents": round(price_cents, 1) if price_cents is not None else None,
            "shares": round(shares, 2) if shares is not None else None,
            "pnl": round(pnl, 2) if pnl is not None else None,
            "reason": reason,
            "balance": round(bot["balance"], 2),
        }
        bot["trades"].append(trade)
        if len(bot["trades"]) > MAX_TRADES_KEPT:
            del bot["trades"][: len(bot["trades"]) - MAX_TRADES_KEPT]
        self._dirty.add(bot["id"])
        try:
            storage.add_trade(bot["id"], trade)
        except Exception:
            logger.debug("trade insert failed", exc_info=True)


def _fresh_rt() -> dict:
    return {"round": None, "entries": {}, "cooldown_until": 0.0,
            "cross_sign": 0, "switches": 0, "adverse_since": None}


# ---------------------------------------------------------------------------
# Strategy evaluators
# ---------------------------------------------------------------------------


def _projected_margin_bps(pipe) -> float | None:
    ptb = pipe.ptb
    if ptb is None or ptb <= 0 or pipe.ptb_source == "exchange_approx":
        return None
    projected = pipe.rolling.value_at(pipe.window_end_ms)
    if projected is None:
        return None
    return (projected - ptb) / ptb * 10_000.0


LOCK_PROFIT_CENTS = 99.0  # bid this high means the round is decided — sell vs waiting $1 settle


def _exits(mgr: BotManager, bot: dict, pipe, tp: float, sl: float) -> bool:
    pos = bot["position"]
    if pos is None or pos["window_id"] != pipe.window_id:
        return False
    _ask, _sz, bid = mgr._quote(pipe, pos["side"])
    if bid is None:
        return False
    if bid >= LOCK_PROFIT_CENTS:
        return mgr.sell(bot, pipe, f"locked profit @{bid:.0f}¢")
    move = bid - pos["entry_cents"]
    if move >= tp:
        return mgr.sell(bot, pipe, f"take profit {move:+.0f}c")
    if move <= -sl:
        return mgr.sell(bot, pipe, f"stop loss {move:+.0f}c")
    return False


def lock_profit_if_bid(mgr, bot: dict, pipe) -> bool:
    """Sell when the held side is locked near 100¢ (take the locked profit)."""
    pos = bot.get("position")
    if pos is None or pos.get("window_id") != pipe.window_id:
        return False
    _ask, _sz, bid = mgr._quote(pipe, pos["side"])
    if bid is None or bid < LOCK_PROFIT_CENTS:
        return False
    return mgr.sell(bot, pipe, f"locked profit @{bid:.0f}¢")


def _eval_twap_lockin(mgr, bot, rt, pipe, now):
    if bot["position"] is not None:
        lock_profit_if_bid(mgr, bot, pipe)
        return
    p = bot["params"]
    if rt["entries"].get(pipe.window_id, 0) >= 1:
        return
    remaining = pipe.window_end_ms - now
    if not (10_000 <= remaining <= float(p["entry_window_s"]) * 1000.0):
        return
    margin = _projected_margin_bps(pipe)
    if margin is None or abs(margin) < float(p["min_margin_bps"]):
        return
    side = "UP" if margin > 0 else "DOWN"
    mgr.buy(bot, rt, pipe, side, float(p["order_usd"]), float(p["max_entry_cents"]),
            f"lock-in: projected final {margin:+.1f} bps vs PTB, {remaining / 1000:.0f}s left")


def _eval_twap_lockin_cut(mgr, bot, rt, pipe, now):
    p = bot["params"]
    pos = bot["position"]
    margin = _projected_margin_bps(pipe)

    if pos is not None:
        if pos["window_id"] != pipe.window_id:
            return
        if lock_profit_if_bid(mgr, bot, pipe):
            return
        if margin is None:
            return
        side = pos["side"]
        adverse = margin < -float(p["cut_margin_bps"]) if side == "UP" \
            else margin > float(p["cut_margin_bps"])
        if not adverse:
            rt["adverse_since"] = None
            return
        if rt["adverse_since"] is None:
            rt["adverse_since"] = now
            return
        if now - rt["adverse_since"] >= float(p["cut_confirm_s"]) * 1000.0:
            mgr.sell(bot, pipe,
                     f"cut loss: projection {margin:+.1f} bps now AGAINST {side}")
            rt["adverse_since"] = None
        return

    rt["adverse_since"] = None
    if rt["entries"].get(pipe.window_id, 0) >= 1:
        return  # one shot per round, cut or not
    remaining = pipe.window_end_ms - now
    if not (10_000 <= remaining <= float(p["entry_window_s"]) * 1000.0):
        return
    if margin is None or abs(margin) < float(p["min_margin_bps"]):
        return
    side = "UP" if margin > 0 else "DOWN"
    mgr.buy(bot, rt, pipe, side, float(p["order_usd"]), float(p["max_entry_cents"]),
            f"lock-in: projected final {margin:+.1f} bps vs PTB, {remaining / 1000:.0f}s left")


def _eval_winning_hold(mgr, bot, rt, pipe, now):
    p = bot["params"]
    wid = pipe.window_id
    remaining = pipe.window_end_ms - now
    margin = _projected_margin_bps(pipe)
    pos = bot["position"]

    if pos is None:
        rt["adverse_since"] = None
        if rt["entries"].get(wid, 0) >= 1 and rt["switches"] == 0:
            return  # already had the round's initial entry (and exited)
        if remaining < 60_000:
            return  # too late for a near-50c hold entry
        if margin is None or abs(margin) < float(p["conf_bps"]):
            return
        side = "UP" if margin > 0 else "DOWN"
        ask, _sz, _bid = mgr._quote(pipe, side)
        if ask is None or not (float(p["entry_min_cents"]) <= ask <= float(p["entry_max_cents"])):
            return
        mgr.buy(bot, rt, pipe, side, float(p["order_usd"]), float(p["entry_max_cents"]),
                f"hold entry {side} @{ask:.0f}c (projection {margin:+.1f} bps)")
        return

    if pos["window_id"] != wid or margin is None:
        return
    if remaining <= float(p["stop_switch_s"]) * 1000.0:
        rt["adverse_since"] = None
        return  # ride it out — too late to switch
    side = pos["side"]
    adverse = margin < -float(p["switch_margin_bps"]) if side == "UP" \
        else margin > float(p["switch_margin_bps"])
    if not adverse:
        rt["adverse_since"] = None
        return
    if rt["adverse_since"] is None:
        rt["adverse_since"] = now
        return
    if now - rt["adverse_since"] < float(p["switch_confirm_s"]) * 1000.0:
        return
    if rt["switches"] >= int(p["max_switches"]):
        return
    other = "DOWN" if side == "UP" else "UP"
    if mgr.sell(bot, pipe, f"switch: projection {margin:+.1f} bps against {side}"):
        rt["switches"] += 1
        rt["adverse_since"] = None
        mgr.buy(bot, rt, pipe, other, float(p["order_usd"]), float(p["switch_max_cents"]),
                f"switch to {other} (projection {margin:+.1f} bps)")


def _rolling_ask_high(pipe, side: str, lookback_ms: float, now: float) -> float | None:
    """Highest ask for a side over the lookback (from the quote history)."""
    key = f"{side.lower()}_ask"
    cutoff = now - lookback_ms
    high = None
    for q in reversed(pipe.quotes):
        if q["t"] < cutoff:
            break
        v = q.get(key)
        if v is not None and (high is None or v > high):
            high = v
    return high


def _eval_dip_harvester(mgr, bot, rt, pipe, now):
    p = bot["params"]
    remaining = pipe.window_end_ms - now
    if remaining < float(p["no_buy_last_s"]) * 1000.0:
        return
    if now < rt.get("cooldown_until", 0):
        return
    book = bot.get("book")
    if book is not None and book.get("window_id") != pipe.window_id:
        return  # waiting for last round's book to settle
    lookback_ms = float(p["lookback_s"]) * 1000.0
    per_side_cap = float(p["per_side_max_usd"])
    for side in ("UP", "DOWN"):
        spent = 0.0
        if book is not None:
            spent = book[side]["cost"] + book[side]["fees"]
        if spent >= per_side_cap:
            continue
        ask, _sz, _bid = mgr._quote(pipe, side)
        if ask is None or ask > float(p["max_price_cents"]):
            continue
        high = _rolling_ask_high(pipe, side, lookback_ms, now)
        if high is None or high - ask < float(p["dip_cents"]):
            continue
        if mgr.buy_book(bot, rt, pipe, side, float(p["order_usd"]),
                        float(p["max_price_cents"]),
                        f"dip: {side} ask {ask:.0f}¢, was {high:.0f}¢ "
                        f"{p['lookback_s']:.0f}s-high (−{high - ask:.0f}¢)"):
            rt["cooldown_until"] = now + float(p["min_gap_s"]) * 1000.0
            book = bot.get("book")
            break  # one buy per tick


def _eval_cross_line(mgr, bot, rt, pipe, now):
    p = bot["params"]
    wid = pipe.window_id
    pos = bot["position"]

    # ---- manage the open position ----
    if pos is not None:
        if pos["window_id"] != wid:
            return  # settlement owns it
        _ask, _sz, bid = mgr._quote(pipe, pos["side"])
        if bid is not None:
            move = bid - pos["entry_cents"]
            tp = float(p.get("tp_cents", 0))
            if tp > 0 and move >= tp:
                mgr.sell(bot, pipe, f"profit target {move:+.0f}¢ (cross line)")
                return
            if move <= -float(p["sl_cents"]):
                mgr.sell(bot, pipe, f"safety stop {move:+.0f}¢")
                return
        if int(p.get("use_momrev", 0)):
            bps = pipe.tick_engine.momentum_bps(1000, now)
            adverse = bps is not None and (
                bps < -float(p["rev_bps"]) if pos["side"] == "UP"
                else bps > float(p["rev_bps"])
            )
            if adverse:
                if rt.get("rev_since") is None:
                    rt["rev_since"] = now
                elif now - rt["rev_since"] >= float(p["rev_confirm_s"]) * 1000.0:
                    mgr.sell(bot, pipe,
                             f"momentum reversed {bps:+.1f} bps against {pos['side']}")
                    rt["rev_since"] = None
            else:
                rt["rev_since"] = None
        return

    rt["rev_since"] = None

    # ---- entry: TWAP crossing the price-to-beat line ----
    ptb = pipe.ptb
    twap = pipe.rolling.value
    if ptb is None or twap is None or pipe.ptb_source == "exchange_approx":
        rt["cross_sign"] = 0
        return
    delta_bps = (twap - ptb) / ptb * 10_000.0
    thr = float(p["min_cross_bps"])
    sign = 1 if delta_bps > thr else (-1 if delta_bps < -thr else 0)
    prev = rt["cross_sign"]
    crossed = sign != 0 and prev != 0 and sign != prev
    if sign != 0:
        rt["cross_sign"] = sign
    if not crossed:
        return
    if rt["entries"].get(wid, 0) >= int(p["max_entries"]):
        return
    side = "UP" if sign > 0 else "DOWN"
    mgr.buy(bot, rt, pipe, side, float(p["order_usd"]), float(p["max_entry_cents"]),
            f"line cross -> {side} ({delta_bps:+.1f} bps past PTB)")


def _eval_next_round_scalp(mgr, bot, rt, pipe, now):
    p = bot["params"]
    wid = pipe.window_id
    remaining = pipe.window_end_ms - now
    age_ms = now - pipe.window_start_ms

    # Manage an open scalp first: tp/sl in cents, then the time exit.
    pos = bot["position"]
    if pos is not None:
        if pos["window_id"] != wid:
            return  # settlement pipeline owns it now
        if _exits(mgr, bot, pipe, float(p["tp_cents"]), float(p["sl_cents"])):
            return
        if now - pos["entry_t"] >= float(p["max_hold_s"]) * 1000.0:
            mgr.sell(bot, pipe, f"time exit after {p['max_hold_s']:.0f}s (scalp)")
        return

    # Final seconds: record the dying round's momentum as next round's
    # signal — measured over the WHOLE capture window, not a 3-tick
    # snapshot (a flat last instant hid real closing drift in testing).
    if remaining <= float(p["capture_last_s"]) * 1000.0:
        bps = pipe.tick_engine.momentum_bps(float(p["capture_last_s"]) * 1000.0, now)
        if bps is not None:
            rt["end_signal"] = {
                "wid": wid, "bps": bps,
                "pressure": pipe._last_pressure.get("score"), "t": now,
            }
        return

    # Fresh round: fire the carried signal near 50¢.
    sig = rt.get("end_signal")
    if (
        sig is not None
        and sig["wid"] == wid - 1
        and age_ms <= float(p["entry_window_s"]) * 1000.0
        and rt["entries"].get(wid, 0) == 0
        and abs(sig["bps"]) >= float(p["signal_bps_min"])
    ):
        side = "UP" if sig["bps"] > 0 else "DOWN"
        ask, _sz, _bid = mgr._quote(pipe, side)
        if ask is not None and float(p["entry_min_cents"]) <= ask <= float(p["entry_max_cents"]):
            mgr.buy(bot, rt, pipe, side, float(p["order_usd"]), float(p["entry_max_cents"]),
                    f"next-round scalp: prev round ended {sig['bps']:+.1f} bps "
                    f"momentum, entry @{ask:.0f}¢")
        return

    # Continuous variant: re-scalp live momentum bursts mid-round.
    if (
        int(p.get("continuous", 0))
        and remaining > 30_000
        and now >= rt.get("cooldown_until", 0)
        and rt["entries"].get(wid, 0) < int(p["max_entries"])
    ):
        bps = pipe.tick_engine.momentum_bps(1000, now)
        if bps is None or abs(bps) < float(p["signal_bps_min"]):
            return
        side = "UP" if bps > 0 else "DOWN"
        ask, _sz, _bid = mgr._quote(pipe, side)
        if ask is None or not (float(p["re_entry_min_cents"]) <= ask <= float(p["re_entry_max_cents"])):
            return
        if mgr.buy(bot, rt, pipe, side, float(p["order_usd"]), float(p["re_entry_max_cents"]),
                   f"live re-scalp: {bps:+.1f} bps/1s @{ask:.0f}¢"):
            rt["cooldown_until"] = now + 8000


def _copy_slug_map(mgr) -> dict:
    """Current-round event slug -> pipeline, across ALL live markets."""
    out = {}
    for pl in mgr._hub.pipelines.values():
        market = pl.polymarket.market
        if market is not None and market.get("slug"):
            out[market["slug"]] = pl
    return out


def _eval_copy_trader(mgr, bot, rt, pipe, now):
    p = bot["params"]
    wallet = str(p.get("wallet") or "").lower()
    if not wallet.startswith("0x"):
        return
    # First tick: skip history so we only copy trades made from now on.
    if "copy_seq" not in rt:
        rt["copy_seq"] = mgr.activity.latest_seq
        return
    # ALL markets: any leader trade whose event matches one of our live
    # pipelines' current rounds is copyable.
    rt["copy_seq"], leader_trades = mgr.activity.since(rt["copy_seq"], wallet)
    if not leader_trades:
        return
    slug_map = _copy_slug_map(mgr)
    for tr in leader_trades:
        # Age measured from OUR reception (the local clock is skewed vs
        # Polymarket's trade timestamps, so t_ms deltas mislead).
        age_s = (time.time() * 1000.0 - tr["recv_ms"]) / 1000.0
        if tr["size"] < float(p["min_leader_size"]):
            continue
        if age_s > float(p["max_copy_delay_s"]):
            continue
        pl = slug_map.get(tr["event_slug"])
        if pl is None:
            # ANY other Polymarket market: copy into the generic
            # portfolio at the leader's price + slippage.
            leader = tr["name"] or wallet[:8]
            if tr["side"] == "BUY":
                mgr.gbuy(bot, tr, float(p["order_usd"]),
                         float(p.get("slippage_cents", 1)),
                         int(p.get("max_open", 10)),
                         f"copy {leader}: bought '{tr['outcome']}' "
                         f"{tr['size']:.0f}sh @ {tr['price_cents']:.0f}¢ "
                         f"({age_s:.1f}s after feed)")
            elif int(p.get("copy_sells", 1)):
                mgr.gsell(bot, tr.get("token_id"),
                          max(1.0, tr["price_cents"] - float(p.get("slippage_cents", 1))),
                          f"copy {leader}: sold '{tr['outcome']}' "
                          f"{tr['size']:.0f}sh @ {tr['price_cents']:.0f}¢ "
                          f"({age_s:.1f}s after feed)")
            continue
        lag = f"{tr['event_slug'].split('-')[0].upper()}, {age_s:.1f}s after feed"
        pos = bot["position"]
        if tr["side"] == "BUY":
            if pos is not None:
                continue  # single-position engine: already in
            if now < rt.get("cooldown_until", 0):
                continue
            if mgr.buy(bot, rt, pl, tr["outcome"], float(p["order_usd"]),
                       float(p["max_entry_cents"]),
                       f"copy {tr['name'] or wallet[:8]}: bought {tr['outcome']} "
                       f"{tr['size']:.0f}sh @ {tr['price_cents']:.0f}¢ ({lag})"):
                rt["cooldown_until"] = now + float(p["min_copy_gap_s"]) * 1000.0
        elif tr["side"] == "SELL" and int(p.get("copy_sells", 1)):
            if (pos is None or pos["side"] != tr["outcome"]
                    or pos.get("asset") != pl.asset.key):
                continue
            mgr.sell(bot, pl,
                     f"copy {tr['name'] or wallet[:8]}: sold {tr['outcome']} "
                     f"{tr['size']:.0f}sh @ {tr['price_cents']:.0f}¢ ({lag})")


def _eval_pressure_scalp(mgr, bot, rt, pipe, now):
    p = bot["params"]
    if _exits(mgr, bot, pipe, float(p["tp_cents"]), float(p["sl_cents"])):
        return
    if bot["position"] is not None or now < rt["cooldown_until"]:
        return
    if rt["entries"].get(pipe.window_id, 0) >= int(p["max_entries"]):
        return
    score = pipe._last_pressure.get("score")
    if score is None or abs(score) < float(p["pressure_min"]):
        return
    side = "UP" if score > 0 else "DOWN"
    ask, _sz, _bid = mgr._quote(pipe, side)
    if ask is None or not (float(p["entry_min_cents"]) <= ask <= float(p["entry_max_cents"])):
        return
    if mgr.buy(bot, rt, pipe, side, float(p["order_usd"]), float(p["entry_max_cents"]),
               f"pressure {score:+.0f}"):
        rt["cooldown_until"] = now + 5000


def _eval_momentum_rider(mgr, bot, rt, pipe, now):
    p = bot["params"]
    if _exits(mgr, bot, pipe, float(p["tp_cents"]), float(p["sl_cents"])):
        return
    if bot["position"] is not None or now < rt["cooldown_until"]:
        return
    if rt["entries"].get(pipe.window_id, 0) >= int(p["max_entries"]):
        return
    window_s = float(p.get("mom_window_s", 1))
    bps = pipe.tick_engine.momentum_bps(window_s * 1000.0, now)
    if bps is None or abs(bps) < float(p["mom_bps_min"]):
        return
    side = "UP" if bps > 0 else "DOWN"
    ask, _sz, _bid = mgr._quote(pipe, side)
    if ask is None or not (float(p["entry_min_cents"]) <= ask <= float(p["entry_max_cents"])):
        return
    if mgr.buy(bot, rt, pipe, side, float(p["order_usd"]), float(p["entry_max_cents"]),
               f"momentum {bps:+.1f} bps/{window_s:.0f}s"):
        rt["cooldown_until"] = now + 5000


def _eval_cross_hunter(mgr, bot, rt, pipe, now):
    p = bot["params"]
    if _exits(mgr, bot, pipe, float(p["tp_cents"]), float(p["sl_cents"])):
        return
    ptb = pipe.ptb
    twap = pipe.rolling.value
    if ptb is None or twap is None:
        rt["cross_sign"] = 0
        return
    delta_bps = (twap - ptb) / ptb * 10_000.0
    sign = 1 if delta_bps > 0.5 else (-1 if delta_bps < -0.5 else 0)
    prev = rt["cross_sign"]
    crossed = sign != 0 and prev != 0 and sign != prev
    if sign != 0:
        rt["cross_sign"] = sign
    if bot["position"] is not None or not crossed:
        return
    if rt["entries"].get(pipe.window_id, 0) >= int(p["max_entries"]):
        return
    side = "UP" if sign > 0 else "DOWN"
    mgr.buy(bot, rt, pipe, side, float(p["order_usd"]), float(p["max_entry_cents"]),
            f"TWAP crossed PTB -> {side}")


_EVALUATORS = {
    "twap_lockin": _eval_twap_lockin,
    "twap_lockin_cut": _eval_twap_lockin_cut,
    "cross_line": _eval_cross_line,
    "next_round_scalp": _eval_next_round_scalp,
    "copy_trader": _eval_copy_trader,
    "dip_harvester": _eval_dip_harvester,
    "winning_hold": _eval_winning_hold,
    "pressure_scalp": _eval_pressure_scalp,
    "momentum_rider": _eval_momentum_rider,
    "cross_hunter": _eval_cross_hunter,
}


# ---------------------------------------------------------------------------
# Brains: live condition checklists for the detail page
# ---------------------------------------------------------------------------


def _c(label, value, ok):
    return {"label": label, "value": value, "ok": bool(ok)}


def _common_market(pipe):
    now = pipe.poly_now_ms()
    remaining = (pipe.window_end_ms - now) / 1000.0
    margin = _projected_margin_bps(pipe)
    return now, remaining, margin


def _brain_twap_lockin(mgr, bot, rt, pipe):
    p = bot["params"]
    now, remaining, margin = _common_market(pipe)
    in_window = 10 <= remaining <= float(p["entry_window_s"])
    margin_ok = margin is not None and abs(margin) >= float(p["min_margin_bps"])
    side = None if margin is None else ("UP" if margin > 0 else "DOWN")
    ask = None
    if side:
        ask, _sz, _bid = mgr._quote(pipe, side)
    price_ok = ask is not None and ask <= float(p["max_entry_cents"])
    entered = rt["entries"].get(pipe.window_id, 0) >= 1
    holding = bot["position"] is not None
    if holding:
        summary = f"HOLDING {bot['position']['side']} — waiting for settlement"
    elif entered:
        summary = "done for this round"
    elif not in_window:
        summary = f"waiting for entry window (last {p['entry_window_s']:.0f}s), {remaining:.0f}s left"
    elif not margin_ok:
        summary = "in window — projection margin too thin"
    elif not price_ok:
        summary = f"signal {side} but price too expensive"
    else:
        summary = f"conditions met — about to buy {side}"
    return {"summary": summary, "conditions": [
        _c("time remaining", f"{remaining:.0f}s", True),
        _c(f"inside entry window (≤{p['entry_window_s']:.0f}s)", f"{remaining:.0f}s", in_window),
        _c("trusted price-to-beat", pipe.ptb_source or "—",
           pipe.ptb is not None and pipe.ptb_source != "exchange_approx"),
        _c(f"projected margin ≥ {p['min_margin_bps']}bps",
           f"{margin:+.2f} bps" if margin is not None else "—", margin_ok),
        _c(f"{side or 'side'} ask ≤ {p['max_entry_cents']:.0f}¢",
           f"{ask:.0f}¢" if ask is not None else "—", price_ok),
        _c("not yet entered this round", "entered" if entered else "clear", not entered),
    ]}


def _brain_twap_lockin_cut(mgr, bot, rt, pipe):
    p = bot["params"]
    base = _brain_twap_lockin(mgr, bot, rt, pipe)
    pos = bot["position"]
    if pos is not None:
        now, remaining, margin = _common_market(pipe)
        side = pos["side"]
        adverse = margin is not None and (
            margin < -float(p["cut_margin_bps"]) if side == "UP"
            else margin > float(p["cut_margin_bps"])
        )
        timer = (now - rt["adverse_since"]) / 1000.0 if rt.get("adverse_since") else 0.0
        base["conditions"].append(_c(
            f"cut-loss watch (fires at {p['cut_margin_bps']}bps against, {p['cut_confirm_s']:.0f}s confirm)",
            f"ADVERSE {timer:.1f}s" if adverse else
            (f"{margin:+.2f} bps with us" if margin is not None else "—"),
            not adverse,
        ))
        base["summary"] = (
            f"HOLDING {side} — projection AGAINST us, cutting in "
            f"{max(0.0, float(p['cut_confirm_s']) - timer):.1f}s" if adverse
            else f"HOLDING {side} — projection with us, cut-loss armed"
        )
    return base


def _brain_winning_hold(mgr, bot, rt, pipe):
    p = bot["params"]
    now, remaining, margin = _common_market(pipe)
    pos = bot["position"]
    conditions = [
        _c("time remaining", f"{remaining:.0f}s", True),
        _c("projection margin",
           f"{margin:+.2f} bps" if margin is not None else "—", margin is not None),
        _c("switches used", f"{rt['switches']}/{int(p['max_switches'])}",
           rt["switches"] < int(p["max_switches"])),
    ]
    if pos is None:
        side = None if margin is None else ("UP" if margin > 0 else "DOWN")
        ask = None
        if side:
            ask, _sz, _bid = mgr._quote(pipe, side)
        in_range = ask is not None and float(p["entry_min_cents"]) <= ask <= float(p["entry_max_cents"])
        conditions += [
            _c(f"entry signal ≥ {p['conf_bps']}bps",
               f"{margin:+.2f}" if margin is not None else "—",
               margin is not None and abs(margin) >= float(p["conf_bps"])),
            _c(f"{side or 'side'} price in {p['entry_min_cents']:.0f}–{p['entry_max_cents']:.0f}¢",
               f"{ask:.0f}¢" if ask is not None else "—", in_range),
            _c("enough round left (≥60s)", f"{remaining:.0f}s", remaining >= 60),
        ]
        summary = "flat — waiting for a near-50¢ entry on the projected side"
    else:
        side = pos["side"]
        adverse = margin is not None and (
            margin < -float(p["switch_margin_bps"]) if side == "UP"
            else margin > float(p["switch_margin_bps"])
        )
        timer = (now - rt["adverse_since"]) / 1000.0 if rt["adverse_since"] else 0.0
        conditions += [
            _c(f"holding {side}", f"{pos['shares']:.0f} sh @ {pos['entry_cents']:.0f}¢", True),
            _c("projection still with us",
               f"{margin:+.2f} bps" if margin is not None else "—", not adverse),
            _c(f"adverse confirmation ({p['switch_confirm_s']:.0f}s)",
               f"{timer:.1f}s" if adverse else "not adverse", not adverse or timer < float(p["switch_confirm_s"])),
            _c(f"switching allowed (> {p['stop_switch_s']:.0f}s left)",
               f"{remaining:.0f}s", remaining > float(p["stop_switch_s"])),
        ]
        if adverse:
            summary = f"HOLDING {side} — projection against us, confirming switch ({timer:.1f}s)"
        else:
            summary = f"HOLDING {side} — projection favors us, riding to settlement"
    return {"summary": summary, "conditions": conditions}


def _brain_scalp_common(mgr, bot, rt, pipe, trigger_label, trigger_value, trigger_ok, p):
    now, remaining, _m = _common_market(pipe)
    pos = bot["position"]
    conditions = [
        _c("time remaining", f"{remaining:.0f}s", True),
        _c(trigger_label, trigger_value, trigger_ok),
        _c("entries left",
           f"{rt['entries'].get(pipe.window_id, 0)}/{int(p['max_entries'])}",
           rt["entries"].get(pipe.window_id, 0) < int(p["max_entries"])),
    ]
    if pos is not None:
        _ask, _sz, bid = mgr._quote(pipe, pos["side"])
        move = (bid - pos["entry_cents"]) if bid is not None else None
        conditions.append(_c(
            f"position {pos['side']} (TP +{p['tp_cents']:.0f} / SL −{p['sl_cents']:.0f})",
            f"{move:+.0f}¢" if move is not None else "—", move is None or move > -float(p["sl_cents"])))
        summary = f"in position {pos['side']}, {move:+.0f}¢" if move is not None else f"in position {pos['side']}"
    else:
        summary = "flat — watching for trigger"
    return {"summary": summary, "conditions": conditions}


def _brain_dip_harvester(mgr, bot, rt, pipe):
    p = bot["params"]
    now, remaining, _m = _common_market(pipe)
    lookback_ms = float(p["lookback_s"]) * 1000.0
    book = bot.get("book")
    conditions = [_c("time remaining", f"{remaining:.0f}s",
                     remaining > float(p["no_buy_last_s"]))]
    for side in ("UP", "DOWN"):
        ask, _sz, _bid = mgr._quote(pipe, side)
        high = _rolling_ask_high(pipe, side, lookback_ms, now)
        dip = (high - ask) if (high is not None and ask is not None) else None
        spent = 0.0
        if book and book.get("window_id") == pipe.window_id:
            spent = book[side]["cost"] + book[side]["fees"]
        conditions.append(_c(
            f"{side} dip (≥{p['dip_cents']:.0f}¢ off {p['lookback_s']:.0f}s-high, "
            f"spent ${spent:.2f}/${p['per_side_max_usd']:.0f})",
            f"ask {ask:.0f}¢, high {high:.0f}¢ (−{dip:.0f}¢)" if dip is not None else "—",
            dip is not None and dip >= float(p["dip_cents"])
            and spent < float(p["per_side_max_usd"]),
        ))
    if book and (book["UP"]["shares"] or book["DOWN"]["shares"]):
        pairs = min(book["UP"]["shares"], book["DOWN"]["shares"])
        cost = (book["UP"]["cost"] + book["UP"]["fees"]
                + book["DOWN"]["cost"] + book["DOWN"]["fees"])
        conditions.append(_c(
            "book (pairs pay $1 each at settlement)",
            f"U{book['UP']['shares']:.0f} / D{book['DOWN']['shares']:.0f} "
            f"= {pairs:.0f} pairs, cost ${cost:.2f}", True))
        locked = pairs - cost
        summary = (f"harvesting — {pairs:.0f} pairs held, "
                   f"{'locked +' if locked > 0 else 'net '}${locked:.2f} vs cost, riding to settlement")
    else:
        summary = "watching both sides for panic dips"
    return {"summary": summary, "conditions": conditions}


def _brain_cross_line(mgr, bot, rt, pipe):
    p = bot["params"]
    now, remaining, _m = _common_market(pipe)
    ptb = pipe.ptb
    twap = pipe.rolling.value
    delta = (twap - ptb) / ptb * 10_000.0 if (ptb and twap) else None
    pos = bot["position"]
    exit_mode = (f"TP +{p['tp_cents']:.0f}¢" if float(p.get("tp_cents", 0)) > 0
                 else f"momentum-reverse ({p['rev_bps']}bps/{p['rev_confirm_s']:.0f}s)")
    conditions = [
        _c("time remaining", f"{remaining:.0f}s", True),
        _c("trusted price-to-beat", pipe.ptb_source or "—",
           ptb is not None and pipe.ptb_source != "exchange_approx"),
        _c(f"TWAP vs line (cross fires past ±{p['min_cross_bps']}bps)",
           f"{delta:+.2f} bps" if delta is not None else "—", delta is not None),
        _c("entries this round",
           f"{rt['entries'].get(pipe.window_id, 0)}/{int(p['max_entries'])}",
           rt["entries"].get(pipe.window_id, 0) < int(p["max_entries"])),
        _c("exit mode", exit_mode + f" · stop −{p['sl_cents']:.0f}¢", True),
    ]
    if pos is not None:
        _ask, _sz, bid = mgr._quote(pipe, pos["side"])
        move = (bid - pos["entry_cents"]) if bid is not None else None
        bps = pipe.tick_engine.momentum_bps(1000, now)
        conditions.append(_c(
            f"position {pos['side']} from {pos['entry_cents']:.0f}¢",
            f"{move:+.0f}¢ now · 1s mom {bps:+.1f} bps" if move is not None and bps is not None
            else "—", move is None or move > -float(p["sl_cents"])))
        summary = f"IN {pos['side']} after cross — {exit_mode} armed"
    else:
        side_now = "UP" if (delta or 0) > 0 else "DOWN"
        summary = (f"watching the line — TWAP {abs(delta):.1f} bps "
                   f"{'above' if (delta or 0) > 0 else 'below'} ({side_now} side), "
                   "waiting for a cross" if delta is not None
                   else "waiting for PTB/TWAP")
    return {"summary": summary, "conditions": conditions}


def _brain_next_round_scalp(mgr, bot, rt, pipe):
    p = bot["params"]
    now, remaining, _m = _common_market(pipe)
    wid = pipe.window_id
    age_s = (now - pipe.window_start_ms) / 1000.0
    sig = rt.get("end_signal")
    pos = bot["position"]
    live_bps = pipe.tick_engine.momentum_bps(1000, now)

    sig_txt = "none captured yet"
    sig_ok = False
    if sig is not None:
        fresh = sig["wid"] == wid - 1
        sig_ok = fresh and abs(sig["bps"]) >= float(p["signal_bps_min"])
        sig_txt = (f"{sig['bps']:+.1f} bps ({'prev round' if fresh else 'stale'})")
    conditions = [
        _c("round age / time left", f"{age_s:.0f}s / {remaining:.0f}s", True),
        _c(f"carried signal ≥ {p['signal_bps_min']}bps", sig_txt, sig_ok),
        _c(f"inside entry window (first {p['entry_window_s']:.0f}s)",
           f"{age_s:.0f}s", age_s <= float(p["entry_window_s"])),
        _c("scalps this round",
           f"{rt['entries'].get(wid, 0)}/{int(p['max_entries']) if int(p.get('continuous', 0)) else 1}",
           True),
    ]
    if int(p.get("continuous", 0)):
        conditions.append(_c(
            f"live momentum (re-scalp ≥ {p['signal_bps_min']}bps)",
            f"{live_bps:+.1f} bps" if live_bps is not None else "—",
            live_bps is not None and abs(live_bps) >= float(p["signal_bps_min"])))
    if pos is not None:
        held_s = (now - pos["entry_t"]) / 1000.0
        _ask, _sz, bid = mgr._quote(pipe, pos["side"])
        move = (bid - pos["entry_cents"]) if bid is not None else None
        conditions.append(_c(
            f"scalping {pos['side']} (TP +{p['tp_cents']:.0f}/SL −{p['sl_cents']:.0f}/max {p['max_hold_s']:.0f}s)",
            f"{move:+.0f}¢ after {held_s:.0f}s" if move is not None else f"{held_s:.0f}s held",
            move is None or move > -float(p["sl_cents"])))
        summary = f"IN {pos['side']} scalp — {move:+.0f}¢, exits armed" if move is not None \
            else f"IN {pos['side']} scalp"
    elif remaining <= float(p["capture_last_s"]):
        summary = "capturing end-of-round momentum for the next open"
    elif sig_ok and age_s <= float(p["entry_window_s"]):
        summary = "signal armed — waiting for a 49–51¢ ask"
    else:
        summary = "waiting — no strong carried signal this open"
    return {"summary": summary, "conditions": conditions}


def _brain_copy_trader(mgr, bot, rt, pipe):
    p = bot["params"]
    wallet = str(p.get("wallet") or "").lower()
    feed = mgr.activity
    now_ms = time.time() * 1000.0
    feed_age = (now_ms - feed.last_frame_ms) / 1000.0 if feed.last_frame_ms else None
    slug_map = _copy_slug_map(mgr)
    # Latest leader trade anywhere (display only).
    _seq, recent = feed.since(0, wallet or None)
    last = recent[-1] if recent else None
    pos = bot["position"]
    conditions = [
        _c("leader wallet set", wallet[:12] + "…" if wallet else "—", wallet.startswith("0x")),
        _c("activity feed live",
           f"{feed_age:.1f}s ago" if feed_age is not None else "no frames",
           feed.connected and feed_age is not None and feed_age < 30),
        _c("markets watched", ", ".join(sorted(
            s.split("-")[0].upper() for s in slug_map)) or "none live",
           len(slug_map) > 0),
        _c("leader's last trade seen",
           f"{last['side']} {last['outcome']} {last['size']:.0f}sh @ "
           f"{last['price_cents']:.0f}¢ ({last['event_slug'].split('-')[0].upper()})"
           if last else "none yet", True),
    ]
    pf = bot.get("portfolio") or {}
    conditions.append(_c(
        f"open copies on other markets (max {bot['params'].get('max_open', 10):.0f})",
        f"{len(pf)} position(s)" if pf else "none", True))
    if pos is not None:
        conditions.append(_c(
            f"copying position {pos['side']} on {pos.get('asset', '?')}",
            f"{pos['shares']:.1f} sh @ {pos['entry_cents']:.0f}¢", True))
        summary = (f"IN {pos['side']} on {pos.get('asset', '?')} — "
                   "waiting for leader's exit or settlement")
    elif not wallet.startswith("0x"):
        summary = "no leader wallet configured — set it in ⚙ settings"
    elif pf:
        summary = (f"holding {len(pf)} copied position(s) across markets — "
                   "watching for more")
    else:
        summary = "flat — watching the leader on EVERY Polymarket market"
    return {"summary": summary, "conditions": conditions}


def _brain_pressure(mgr, bot, rt, pipe):
    p = bot["params"]
    score = pipe._last_pressure.get("score")
    return _brain_scalp_common(
        mgr, bot, rt, pipe,
        f"|pressure| ≥ {p['pressure_min']:.0f}",
        f"{score:+.0f}" if score is not None else "—",
        score is not None and abs(score) >= float(p["pressure_min"]), p)


def _brain_momentum(mgr, bot, rt, pipe):
    p = bot["params"]
    window_s = float(p.get("mom_window_s", 1))
    bps = pipe.tick_engine.momentum_bps(window_s * 1000.0, pipe.poly_now_ms())
    return _brain_scalp_common(
        mgr, bot, rt, pipe,
        f"|{window_s:.0f}s momentum| ≥ {p['mom_bps_min']}bps",
        f"{bps:+.1f} bps" if bps is not None else "no ticks in window",
        bps is not None and abs(bps) >= float(p["mom_bps_min"]), p)


def _brain_cross(mgr, bot, rt, pipe):
    p = bot["params"]
    ptb = pipe.ptb
    twap = pipe.rolling.value
    delta = (twap - ptb) / ptb * 10_000.0 if (ptb and twap) else None
    return _brain_scalp_common(
        mgr, bot, rt, pipe,
        "TWAP vs PTB (waiting for a >0.5bps cross)",
        f"{delta:+.2f} bps" if delta is not None else "—",
        delta is not None, p)


_BRAINS = {
    "twap_lockin": _brain_twap_lockin,
    "twap_lockin_cut": _brain_twap_lockin_cut,
    "cross_line": _brain_cross_line,
    "next_round_scalp": _brain_next_round_scalp,
    "copy_trader": _brain_copy_trader,
    "dip_harvester": _brain_dip_harvester,
    "winning_hold": _brain_winning_hold,
    "pressure_scalp": _brain_pressure,
    "momentum_rider": _brain_momentum,
    "cross_hunter": _brain_cross,
}
