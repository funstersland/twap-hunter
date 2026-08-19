"""Polymarket account data (READ-ONLY + authenticated CLOB balance).

Positions/trades from the public Data API; cash balance from CLOB using
the stored private key (never returned to the browser).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from backend.engine.clob_executor import sync_clob_balance

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_S = 5.0


def invalidate_snapshot(account: dict | None = None, address: str | None = None) -> None:
    """Drop cached account snapshot so the next read hits Polymarket."""
    keys = []
    if account:
        keys.extend([
            account.get("proxy_wallet"),
            account.get("address"),
            account.get("derived_address"),
            account.get("funder_address"),
        ])
    if address:
        keys.append(address)
    for k in keys:
        if k:
            _CACHE.pop(str(k).strip().lower(), None)


def _cache_get(key: str) -> dict | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    ts, data = entry
    if time.time() - ts > _CACHE_TTL_S:
        return None
    return data


def _cache_set(key: str, data: dict) -> dict:
    _CACHE[key] = (time.time(), data)
    return data


async def fetch_profile(address: str) -> dict:
    """Resolve Polymarket profile + proxy wallet for an EOA or proxy address."""
    addr = address.strip()
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                f"{GAMMA_API}/public-profile",
                params={"address": addr},
            )
            if resp.status_code != 200:
                return {}
            data = resp.json()
            return {
                "proxy_wallet": data.get("proxyWallet"),
                "profile_name": data.get("name") or data.get("pseudonym"),
                "pseudonym": data.get("pseudonym"),
                "created_at": data.get("createdAt"),
            }
    except Exception as exc:
        logger.debug("profile fetch error for %s: %r", addr, exc)
        return {}


def _parse_usdc(raw: str | int | None) -> float:
    try:
        return int(raw or 0) / 1_000_000.0
    except (TypeError, ValueError):
        return 0.0


def fetch_clob_cash(account: dict) -> dict:
    """Authenticated CLOB collateral balance using stored private key."""
    out = {"cash_balance": 0.0, "allowance_ok": False, "clob_error": None}
    if not account.get("encrypted_key"):
        return out
    try:
        cash = sync_clob_balance(account)
        out["cash_balance"] = cash
    except Exception as exc:
        out["clob_error"] = str(exc)
        logger.debug("clob balance error: %r", exc)
    return out


async def fetch_account_snapshot(account: dict) -> dict:
    """Full account snapshot: profile, cash, positions, trades, totals."""
    stored_addr = (account.get("proxy_wallet") or account.get("address") or "").strip().lower()
    if not stored_addr.startswith("0x"):
        return {"error": "invalid address", "positions": [], "trades": [], "value": None}

    cache_key = stored_addr
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Resolve proxy wallet from profile if missing or address looks like EOA-only setup
    profile = await fetch_profile(account.get("derived_address") or account.get("address"))
    query_addr = (profile.get("proxy_wallet") or stored_addr).lower()

    params = {"user": query_addr}
    out: dict[str, Any] = {
        "address": query_addr,
        "proxy_wallet": query_addr,
        "profile_name": profile.get("profile_name"),
        "pseudonym": profile.get("pseudonym"),
        "value": None,
        "cash_balance": 0.0,
        "positions_value": 0.0,
        "total_equity": 0.0,
        "positions": [],
        "trades": [],
        "positions_count": 0,
        "cash_pnl": 0.0,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            val_resp, pos_resp, tr_resp = await client.get(
                f"{DATA_API}/value", params=params,
            ), await client.get(
                f"{DATA_API}/positions",
                params={**params, "limit": 100, "sizeThreshold": 0},
            ), await client.get(
                f"{DATA_API}/trades", params={**params, "limit": 200},
            )

            if val_resp.status_code == 200:
                rows = val_resp.json()
                if isinstance(rows, list) and rows:
                    out["value"] = float(rows[0].get("value") or 0)

            if pos_resp.status_code == 200:
                positions = pos_resp.json()
                if isinstance(positions, list):
                    out["positions"] = [_norm_position(p) for p in positions]
                    out["positions_count"] = len(out["positions"])
                    out["positions_value"] = round(
                        sum(float(p.get("current_value") or 0) for p in out["positions"]), 2)
                    out["cash_pnl"] = round(
                        sum(float(p.get("cash_pnl") or 0) for p in out["positions"]), 2)

            if tr_resp.status_code == 200:
                trades = tr_resp.json()
                if isinstance(trades, list):
                    out["trades"] = [_norm_trade(t) for t in trades]
    except Exception as exc:
        logger.debug("account_data fetch error for %s: %r", query_addr, exc)
        out["error"] = str(exc)

    clob = fetch_clob_cash(account)
    out["cash_balance"] = clob["cash_balance"]
    out["allowance_ok"] = clob.get("allowance_ok", False)
    if clob.get("clob_error"):
        out["clob_error"] = clob["clob_error"]

    # Total equity = CLOB cash + open position mark value
    out["total_equity"] = round(out["cash_balance"] + out["positions_value"], 2)
    if out["value"] is not None and out["value"] > out["total_equity"]:
        out["total_equity"] = round(float(out["value"]), 2)

    return _cache_set(cache_key, out)


def _norm_position(p: dict) -> dict:
    size = float(p.get("size") or 0)
    avg = float(p.get("avgPrice") or 0)
    cur = float(p.get("curPrice") or 0)
    current_value = float(p.get("currentValue") or 0)
    if not current_value and size and cur:
        current_value = size * cur
    return {
        "title": p.get("title") or p.get("slug") or "—",
        "outcome": p.get("outcome") or "—",
        "size": round(size, 4),
        "avg_price_cents": round(avg * 100, 2),
        "cur_price_cents": round(cur * 100, 2),
        "current_value": round(current_value, 4),
        "cash_pnl": round(float(p.get("cashPnl") or 0), 4),
        "percent_pnl": round(float(p.get("percentPnl") or 0), 2),
        "event_slug": p.get("eventSlug") or p.get("slug") or "",
        "asset_id": p.get("asset") or p.get("assetId") or "",
        "condition_id": p.get("conditionId") or "",
        "redeemable": bool(p.get("redeemable")),
    }


def _norm_trade(t: dict) -> dict:
    ts = t.get("timestamp") or t.get("match_time") or t.get("created_at")
    ts_ms = None
    if ts is not None:
        ts_f = float(ts)
        ts_ms = ts_f * 1000.0 if ts_f < 1e12 else ts_f
    side = str(t.get("side") or "BUY").upper()
    outcome = str(t.get("outcome") or "").upper()
    price = float(t.get("price") or 0)
    size = float(t.get("size") or 0)
    usd = float(t.get("usdcSize") or 0) or (price * size)
    pnl = t.get("cashPnl")
    if pnl is None:
        pnl = t.get("cash_pnl")
    return {
        "t": ts_ms or time.time() * 1000.0,
        "action": side,
        "side": side,
        "outcome": outcome,
        "price_cents": round(price * 100, 2),
        "size": round(size, 4),
        "shares": round(size, 4),
        "usd": round(usd, 4),
        "title": t.get("title") or t.get("eventSlug") or t.get("slug") or "",
        "event_slug": t.get("eventSlug") or t.get("slug") or "",
        "tx": t.get("transactionHash") or t.get("transaction_hash") or "",
        "asset_id": t.get("asset") or t.get("asset_id") or "",
        "cash_pnl": round(float(pnl), 4) if pnl is not None else None,
    }


def filter_for_asset(items: list[dict], asset: str) -> list[dict]:
    """Keep items whose slug/title matches a crypto up/down asset (BTC, ETH, …)."""
    if not asset or asset == "ALL":
        return items
    needle = asset.lower()
    out = []
    for item in items:
        slug = (item.get("event_slug") or item.get("title") or "").lower()
        if needle in slug and "updown" in slug:
            out.append(item)
    return out
