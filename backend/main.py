"""Twap Hunter — FastAPI app: static UI + batched market-data WebSocket.

Multi-market: each WebSocket client subscribes to ONE asset
(``/ws?asset=BTC``); pipelines run concurrently per asset, started on
demand and stopped ~60s after their last subscriber leaves. Different
browser tabs can watch different markets independently.

Run from the project root:
    .venv/Scripts/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8010
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import ASSETS, CONFIG, DEFAULT_ASSET
from backend.engine.bots import (
    BotManager, STRATEGIES, default_params, sanitize_params, strategy_catalog,
)
from backend.engine.pipeline import Pipeline
from backend.engine.real_bots import RealBotManager
from backend.feeds.polymarket import parse_resolution
from backend.feeds.account_data import fetch_account_snapshot, fetch_profile
from backend.storage import storage
from backend import accounts as account_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("twap-hunter")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Boot stamp: pages compare this while polling and auto-reload
# themselves when the server (and thus the frontend code) changes.
BOOT_ID = str(int(time.time() * 1000))

# Idle pipelines survive this long without subscribers (page refreshes,
# brief disconnects) before their feeds are torn down.
REAP_GRACE_S = 60.0


class Hub:
    """Concurrent per-asset pipelines + per-client subscriptions."""

    def __init__(self) -> None:
        self.pipelines: dict[str, Pipeline] = {}
        self.subscribers: dict[str, set[WebSocket]] = {}
        self.retained: dict[str, set[str]] = {}   # asset -> bot ids keeping it alive
        self._reapers: dict[str, asyncio.Task] = {}
        self._broadcaster: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._broadcaster = asyncio.create_task(self._broadcast_loop(), name="broadcaster")
        self._backscan = asyncio.create_task(self._backscan_loop(), name="resolution-backscan")

    async def stop(self) -> None:
        for attr in ("_broadcaster", "_backscan"):
            task = getattr(self, attr, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                setattr(self, attr, None)
        for task in self._reapers.values():
            task.cancel()
        self._reapers.clear()
        pipelines, self.pipelines = dict(self.pipelines), {}
        for pipeline in pipelines.values():
            await pipeline.stop()

    async def _ensure_pipeline(self, asset_key: str) -> Pipeline:
        reaper = self._reapers.pop(asset_key, None)
        if reaper is not None:
            reaper.cancel()
        pipeline = self.pipelines.get(asset_key)
        if pipeline is None:
            logger.info("starting pipeline %s", asset_key)
            pipeline = Pipeline(ASSETS[asset_key])
            await pipeline.start()
            self.pipelines[asset_key] = pipeline
        return pipeline

    def _in_use(self, asset_key: str) -> bool:
        return bool(self.subscribers.get(asset_key)) or bool(self.retained.get(asset_key))

    def _maybe_reap(self, asset_key: str) -> None:
        if (
            not self._in_use(asset_key)
            and asset_key in self.pipelines
            and asset_key not in self._reapers
        ):
            self._reapers[asset_key] = asyncio.create_task(
                self._reap(asset_key), name=f"reap-{asset_key}"
            )

    async def acquire(self, asset_key: str, ws: WebSocket) -> Pipeline:
        """Subscribe ``ws`` to ``asset_key``, starting its pipeline if needed."""
        async with self._lock:
            pipeline = await self._ensure_pipeline(asset_key)
            self.subscribers.setdefault(asset_key, set()).add(ws)
            return pipeline

    async def release(self, asset_key: str, ws: WebSocket) -> None:
        async with self._lock:
            subs = self.subscribers.get(asset_key)
            if subs is not None:
                subs.discard(ws)
            self._maybe_reap(asset_key)

    async def retain(self, asset_key: str, token: str) -> Pipeline:
        """A running bot keeps its market's pipeline alive (no WebSocket)."""
        async with self._lock:
            pipeline = await self._ensure_pipeline(asset_key)
            self.retained.setdefault(asset_key, set()).add(token)
            return pipeline

    async def unretain(self, asset_key: str, token: str) -> None:
        async with self._lock:
            tokens = self.retained.get(asset_key)
            if tokens is not None:
                tokens.discard(token)
            self._maybe_reap(asset_key)

    async def _reap(self, asset_key: str) -> None:
        await asyncio.sleep(REAP_GRACE_S)
        async with self._lock:
            self._reapers.pop(asset_key, None)
            if self._in_use(asset_key):
                return  # someone came back
            pipeline = self.pipelines.pop(asset_key, None)
        if pipeline is not None:
            logger.info("stopping idle pipeline %s", asset_key)
            await pipeline.stop()

    async def _backscan_loop(self) -> None:
        """Sweep recent rounds missing an official outcome.

        Polymarket resolutions can index several minutes late — the
        per-market poller misses them, which starves both the parity
        study and bot settlement reconciliation. This closes the gap.
        """
        client = httpx.AsyncClient(timeout=15.0)
        try:
            while True:
                await asyncio.sleep(60)
                try:
                    await self._backscan_once(client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("resolution backscan error", exc_info=True)
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    async def _backscan_once(self, client: httpx.AsyncClient) -> None:
        now_ms = time.time() * 1000.0
        window_ms = CONFIG.window_ms
        todo = [
            r for r in storage.rounds_for(limit=60)
            if not r.get("outcome_official")
            and (r["window_id"] + 1) * window_ms < now_ms - 90_000     # ended >90s ago
            and (r["window_id"] + 1) * window_ms > now_ms - 3_600_000  # within the hour
        ]
        for r in todo[:10]:
            slug = f"{r['asset'].lower()}-updown-5m-{r['window_id'] * (window_ms // 1000)}"
            try:
                resp = await client.get(
                    f"{CONFIG.gamma_base_url}/markets",
                    params={"slug": slug, "closed": "true"},
                )
                if resp.status_code != 200:
                    continue
                outcome = parse_resolution(resp.json())
            except Exception:
                continue
            if outcome is None:
                continue
            storage.upsert_round(r["asset"], r["window_id"], outcome_official=outcome)
            pipeline = self.pipelines.get(r["asset"])
            if pipeline is not None:
                rec = pipeline.round_results.get(r["window_id"])
                if rec is not None:
                    rec["outcome"] = outcome
                    rec["source"] = "market"
                else:
                    pipeline.round_results[r["window_id"]] = {
                        "outcome": outcome, "source": "market",
                    }
            logger.info("backscan: %s round %s officially %s", r["asset"], slug, outcome)
            await asyncio.sleep(0.3)

    async def _broadcast_loop(self) -> None:
        interval = CONFIG.broadcast_interval_ms / 1000.0
        while True:
            await asyncio.sleep(interval)
            for key, pipeline in list(self.pipelines.items()):
                # Drain even without subscribers so pending buffers stay
                # bounded during the reap grace period.
                batch = pipeline.drain_batch()
                if batch is None:
                    continue
                subs = self.subscribers.get(key)
                if not subs:
                    continue
                text = json.dumps(batch)
                dead = []
                for ws in subs:
                    try:
                        await ws.send_text(text)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    subs.discard(ws)


hub = Hub()
bot_manager = BotManager(hub)
real_bot_manager = RealBotManager(hub, bot_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await hub.start()
    await bot_manager.start()
    await real_bot_manager.start()
    logger.info("Twap Hunter up — pipelines start on demand per client")
    try:
        yield
    finally:
        await real_bot_manager.stop()
        await bot_manager.stop()
        await hub.stop()


app = FastAPI(title="Twap Hunter", lifespan=lifespan)


@app.get("/api/assets")
async def get_assets() -> dict:
    return {
        "assets": list(ASSETS.keys()),
        "running": sorted(hub.pipelines.keys()),
    }


# ---------------------------------------------------------------------------
# Paper-trading bots (simulation only — no real orders exist anywhere)
# ---------------------------------------------------------------------------


@app.get("/api/strategies")
async def get_strategies() -> dict:
    return {"strategies": strategy_catalog(), "assets": list(ASSETS.keys())}


@app.get("/api/bots")
async def get_bots() -> dict:
    return {
        "boot": BOOT_ID,
        "bots": [bot_manager.public_view(b) for b in bot_manager.bots.values()],
    }


@app.get("/api/bots/{bot_id}")
async def get_bot(bot_id: str):
    bot = bot_manager.bots.get(bot_id)
    if bot is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    view = bot_manager.public_view(bot, trades=100, include_brain=True)
    view["boot"] = BOOT_ID
    return view


@app.get("/api/bots/{bot_id}/trades")
async def get_bot_trades(bot_id: str, limit: int = 500):
    if bot_id not in bot_manager.bots:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"trades": storage.trades_for(bot_id, max(1, min(5000, limit)))}


@app.get("/api/rounds")
async def get_rounds(asset: str | None = None, limit: int = 100):
    return {
        "rounds": storage.rounds_for(asset.upper() if asset else None, max(1, min(1000, limit))),
        "parity": storage.parity_summary(),
    }


@app.post("/api/bots")
async def create_bot(payload: dict = Body(...)):
    strategy = str(payload.get("strategy", ""))
    asset = str(payload.get("asset", "")).upper()
    if strategy not in STRATEGIES:
        return JSONResponse({"error": f"unknown strategy {strategy!r}"}, status_code=400)
    if strategy == "copy_trader":
        asset = "ALL"   # copy bots follow their leader on every market
    elif asset not in ASSETS:
        return JSONResponse({"error": f"unknown asset {asset!r}"}, status_code=400)
    params = default_params(strategy)
    params.update(sanitize_params(strategy, payload.get("params") or {}))
    try:
        start_balance = max(10.0, min(100_000.0, float(payload.get("start_balance", 1000.0))))
    except (TypeError, ValueError):
        start_balance = 1000.0
    name = str(payload.get("name", "")).strip()
    bot = await bot_manager.create(name, asset, strategy, params, start_balance)
    if payload.get("start"):
        await bot_manager.set_running(bot["id"], True)
    return bot_manager.public_view(bot)


@app.post("/api/bots/{bot_id}/start")
async def start_bot(bot_id: str):
    bot = await bot_manager.set_running(bot_id, True)
    if bot is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return bot_manager.public_view(bot)


@app.post("/api/bots/{bot_id}/stop")
async def stop_bot(bot_id: str):
    bot = await bot_manager.set_running(bot_id, False)
    if bot is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return bot_manager.public_view(bot)


@app.post("/api/bots/{bot_id}/update")
async def update_bot(bot_id: str, payload: dict = Body(...)):
    bot = await bot_manager.update(
        bot_id, payload.get("name"), payload.get("params"))
    if bot is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return bot_manager.public_view(bot)


@app.post("/api/bots/{bot_id}/reset")
async def reset_bot(bot_id: str):
    bot = await bot_manager.reset(bot_id)
    if bot is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return bot_manager.public_view(bot)


@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: str):
    ok = await bot_manager.delete(bot_id)
    return {"deleted": ok}


# ---------------------------------------------------------------------------
# Real trading — accounts + imported bot configs (no order execution yet)
# ---------------------------------------------------------------------------


@app.get("/api/accounts")
async def get_accounts() -> dict:
    accounts = storage.load_accounts()
    views = []
    for account in accounts.values():
        if not account.get("proxy_wallet"):
            profile = await fetch_profile(
                account.get("derived_address") or account["address"])
            if profile.get("proxy_wallet"):
                account["proxy_wallet"] = profile["proxy_wallet"]
                account["profile_name"] = profile.get("profile_name")
                storage.save_account(account)
        # Re-detect CLOB auth if balance was zero (may need sig type 3)
        if account.get("signature_type") is None:
            from backend.engine.clob_executor import _CLIENT_CACHE
            _CLIENT_CACHE.pop(account.get("id", ""), None)
        snap = await fetch_account_snapshot(account)
        views.append(account_store.public_view(account, snap))
    return {"boot": BOOT_ID, "accounts": views}


@app.get("/api/accounts/{account_id}")
async def get_account(account_id: str):
    account = storage.get_account(account_id)
    if account is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    snap = await fetch_account_snapshot(account)
    view = account_store.public_view(account, snap)
    view["trades"] = snap.get("trades") or []
    return view


@app.post("/api/accounts")
async def create_account(payload: dict = Body(...)):
    label = str(payload.get("label", ""))
    address = str(payload.get("address", ""))
    private_key = str(payload.get("private_key", ""))
    if not private_key.strip():
        return JSONResponse({"error": "private_key is required"}, status_code=400)
    try:
        label, address, warning = account_store.validate_account_input(
            label, address, private_key)
        derived = account_store.derive_address(private_key)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    profile = await fetch_profile(derived)
    if not profile:
        profile = await fetch_profile(address)
    proxy_wallet = profile.get("proxy_wallet") or address

    account = {
        "id": account_store.new_account_id(),
        "label": label,
        "address": address,
        "proxy_wallet": proxy_wallet,
        "derived_address": derived,
        "profile_name": profile.get("profile_name"),
        "address_mismatch": derived.lower() != address.lower(),
        "encrypted_key": account_store.encrypt_key(private_key),
        "created_ms": time.time() * 1000.0,
    }
    from backend.engine.clob_executor import sync_clob_balance
    account["balance_hint"] = sync_clob_balance(account)
    storage.save_account(account)
    snap = await fetch_account_snapshot(account)
    view = account_store.public_view(account, snap)
    if warning:
        view["warning"] = warning
    return view


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str):
    if storage.get_account(account_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    linked = storage.real_bots_for_account(account_id)
    if linked:
        return JSONResponse(
            {"error": f"account linked to {len(linked)} real bot(s) — delete those first"},
            status_code=409,
        )
    storage.delete_account(account_id)
    return {"deleted": True}


@app.get("/api/real/bots")
async def get_real_bots() -> dict:
    snapshots: dict[str, dict] = {}
    bots = list(real_bot_manager.bots.values())
    for bot in bots:
        account = storage.get_account(bot["account_id"])
        if account is None:
            continue
        addr = (account.get("proxy_wallet") or account["address"]).lower()
        if addr not in snapshots:
            snapshots[addr] = await fetch_account_snapshot(account)
    views = []
    for bot in bots:
        account = storage.get_account(bot["account_id"])
        addr = (account.get("proxy_wallet") or account["address"]).lower() if account else ""
        snap = snapshots.get(addr) if account else None
        views.append(real_bot_manager.public_view(bot, snap))
    return {"boot": BOOT_ID, "bots": views}


@app.get("/api/real/bots/{bot_id}")
async def get_real_bot(bot_id: str):
    bot = real_bot_manager.bots.get(bot_id)
    if bot is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    account = storage.get_account(bot["account_id"])
    snap = await fetch_account_snapshot(account) if account else None
    view = real_bot_manager.public_view(
        bot, snap, trades_limit=100, include_brain=True)
    view["boot"] = BOOT_ID
    return view


@app.post("/api/real/bots/{bot_id}/start")
async def start_real_bot(bot_id: str):
    bot = await real_bot_manager.set_running(bot_id, True)
    if bot is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    account = storage.get_account(bot["account_id"])
    snap = await fetch_account_snapshot(account) if account else None
    return real_bot_manager.public_view(bot, snap, include_brain=True)


@app.post("/api/real/bots/{bot_id}/stop")
async def stop_real_bot(bot_id: str):
    bot = await real_bot_manager.set_running(bot_id, False)
    if bot is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    account = storage.get_account(bot["account_id"])
    snap = await fetch_account_snapshot(account) if account else None
    return real_bot_manager.public_view(bot, snap, include_brain=True)


@app.post("/api/real/bots/{bot_id}/update")
async def update_real_bot(bot_id: str, payload: dict = Body(...)):
    bot = real_bot_manager.update(
        bot_id, payload.get("name"), payload.get("params"))
    if bot is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    account = storage.get_account(bot["account_id"])
    snap = await fetch_account_snapshot(account) if account else None
    return real_bot_manager.public_view(bot, snap, include_brain=True)


@app.post("/api/real/bots/import")
async def import_real_bot(payload: dict = Body(...)):
    paper_bot_id = str(payload.get("paper_bot_id", ""))
    account_id = str(payload.get("account_id", ""))
    paper_bot = bot_manager.bots.get(paper_bot_id)
    if paper_bot is None:
        return JSONResponse({"error": "paper bot not found"}, status_code=404)
    if not account_id:
        return JSONResponse({"error": "account_id is required"}, status_code=400)
    name = str(payload.get("name", "")).strip() or None
    try:
        bot = real_bot_manager.import_from_paper(paper_bot, account_id, name)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return real_bot_manager.public_view(bot)


@app.delete("/api/real/bots/{bot_id}")
async def delete_real_bot(bot_id: str):
    ok = real_bot_manager.delete(bot_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"deleted": True}


@app.get("/real")
async def real_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "real.html")


@app.get("/real/{bot_id}")
async def real_bot_detail_page(bot_id: str) -> FileResponse:
    return FileResponse(FRONTEND_DIR / "real-bot.html")


@app.get("/bots")
async def bots_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "bots.html")


@app.get("/bots/{bot_id}")
async def bot_detail_page(bot_id: str) -> FileResponse:
    return FileResponse(FRONTEND_DIR / "bot.html")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    asset = str(ws.query_params.get("asset", DEFAULT_ASSET)).upper()
    if asset not in ASSETS:
        asset = DEFAULT_ASSET
    pipeline = await hub.acquire(asset, ws)
    try:
        init = pipeline.init_payload()
        init["boot"] = BOOT_ID
        await ws.send_text(json.dumps(init))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("type") == "set_asset":
                new_asset = str(msg.get("asset", "")).upper()
                if new_asset in ASSETS and new_asset != asset:
                    await hub.release(asset, ws)
                    asset = new_asset
                    pipeline = await hub.acquire(asset, ws)
                    init = pipeline.init_payload()
                    init["boot"] = BOOT_ID
                    await ws.send_text(json.dumps(init))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.release(asset, ws)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
