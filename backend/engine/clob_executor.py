"""Polymarket CLOB client helpers — authenticated balance + order placement."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.accounts import decrypt_key
from backend.storage import storage

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
REAL_MIN_ORDER_SHARES = 1.0   # match paper / Polymarket $1 lots
_CLIENT_CACHE: dict[str, "ClobSession"] = {}


@dataclass
class ClobSession:
    client: object
    signature_type: int
    funder: str
    balance: float
    v2: bool = False


def _parse_usdc(raw: str | int | None) -> float:
    try:
        return round(int(raw or 0) / 1_000_000.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _auth_attempts(account: dict) -> list[tuple[int, str]]:
    proxy = account.get("proxy_wallet") or account["address"]
    derived = account.get("derived_address") or proxy
    if account.get("signature_type") is not None:
        funder = account.get("funder_address") or proxy
        return [(int(account["signature_type"]), funder)]
    seen = set()
    attempts = []
    for sig_type, funder in (
        (3, proxy), (3, derived),
        (1, proxy), (2, proxy), (0, derived), (0, proxy),
    ):
        key = (sig_type, funder.lower())
        if key not in seen:
            seen.add(key)
            attempts.append((sig_type, funder))
    return attempts


def _build_client(pk: str, sig_type: int, funder: str) -> tuple[object, bool]:
    """Return (client, is_v2). Sig type 3 (deposit wallet) requires py-clob-client-v2."""
    key = f"0x{pk}"
    if sig_type == 3:
        from py_clob_client_v2.client import ClobClient
        client = ClobClient(
            CLOB_HOST, chain_id=CHAIN_ID, key=key,
            signature_type=sig_type, funder=funder,
        )
        client.set_api_creds(client.create_or_derive_api_key())
        return client, True

    from py_clob_client.client import ClobClient
    client = ClobClient(
        CLOB_HOST, key=key, chain_id=CHAIN_ID,
        signature_type=sig_type, funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client, False


def _read_balance(client, sig_type: int) -> float:
    try:
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        except ImportError:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        try:
            params.signature_type = sig_type
        except Exception:
            pass
        try:
            client.update_balance_allowance(params)
        except Exception:
            pass
        bal = client.get_balance_allowance(params)
        if isinstance(bal, dict):
            return _parse_usdc(bal.get("balance"))
        return _parse_usdc(getattr(bal, "balance", 0))
    except Exception as exc:
        logger.debug("_read_balance failed: %r", exc)
        return 0.0


def discover_clob_auth(account: dict) -> ClobSession | None:
    """Try all signature types; return the session with the highest balance."""
    pk = decrypt_key(account["encrypted_key"])
    best: ClobSession | None = None

    for sig_type, funder in _auth_attempts(account):
        try:
            client, is_v2 = _build_client(pk, sig_type, funder)
            cash = _read_balance(client, sig_type)
            logger.debug("clob auth sig=%s funder=%s balance=$%.2f v2=%s",
                         sig_type, funder[:10], cash, is_v2)
            if best is None or cash > best.balance:
                best = ClobSession(client, sig_type, funder, cash, v2=is_v2)
        except Exception as exc:
            logger.debug("clob auth sig=%s funder=%s: %r", sig_type, funder[:10], exc)

    if best is not None:
        account["signature_type"] = best.signature_type
        account["funder_address"] = best.funder
        storage.save_account(account)
        logger.info(
            "clob auth for %s: sig=%s balance=$%.2f v2=%s",
            account.get("label"), best.signature_type, best.balance, best.v2,
        )
    return best


def get_session(account: dict) -> ClobSession:
    cache_key = account.get("id") or account.get("proxy_wallet") or account.get("address")
    cached = _CLIENT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    session = discover_clob_auth(account)
    if session is None:
        raise RuntimeError("could not authenticate CLOB client")
    _CLIENT_CACHE[cache_key] = session
    return session


def make_client(account: dict):
    return get_session(account).client


def sync_clob_balance(account: dict) -> float:
    """Return pUSD collateral balance from CLOB."""
    try:
        session = get_session(account)
        cash = _read_balance(session.client, session.signature_type)
        session.balance = cash
        return cash
    except Exception as exc:
        logger.warning("sync_clob_balance failed: %r", exc)
        return 0.0


def _min_buy_usd(client, token_id: str, price: float) -> float:
    """Minimum USD for a CLOB buy given min_order_size."""
    min_shares = REAL_MIN_ORDER_SHARES
    try:
        ob = client.get_order_book(str(token_id))
        if ob:
            raw = ob.get("min_order_size") if isinstance(ob, dict) else getattr(ob, "min_order_size", None)
            if raw:
                min_shares = max(min_shares, float(raw))
    except Exception:
        pass
    if price <= 0:
        return min_shares
    return round(min_shares * price * 1.02, 2)


def token_share_balance(account: dict, token_id: str) -> float:
    """Conditional-token share balance for a market outcome."""
    session = get_session(account)
    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
    except ImportError:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    params = BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL, token_id=str(token_id))
    try:
        params.signature_type = session.signature_type
    except Exception:
        pass
    try:
        session.client.update_balance_allowance(params)
    except Exception:
        pass
    bal = session.client.get_balance_allowance(params)
    raw = bal.get("balance") if isinstance(bal, dict) else getattr(bal, "balance", 0)
    try:
        return int(raw or 0) / 1_000_000.0
    except (TypeError, ValueError):
        return 0.0


def _num(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _trade_params_cls():
    try:
        from py_clob_client_v2.clob_types import TradeParams
        return TradeParams
    except ImportError:
        from py_clob_client.clob_types import TradeParams
        return TradeParams


def fetch_clob_order(account: dict, order_id: str) -> dict:
    """Authenticated CLOB order record (fill size, price, status)."""
    if not order_id:
        return {}
    try:
        raw = get_session(account).client.get_order(str(order_id))
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        logger.debug("get_order %s: %r", order_id, exc)
        return {}


def fetch_clob_trades(account: dict, trade_ids: list[str] | None = None,
                      asset_id: str | None = None) -> list[dict]:
    """Authenticated CLOB trade records for this account."""
    session = get_session(account)
    TradeParams = _trade_params_cls()
    out: list[dict] = []
    ids = [str(i) for i in (trade_ids or []) if i]
    if not ids and not asset_id:
        try:
            raw = session.client.get_trades(TradeParams(), only_first_page=True) or []
            return [t for t in raw if isinstance(t, dict)]
        except Exception as exc:
            logger.debug("get_trades: %r", exc)
            return []
    for tid in ids or [None]:
        try:
            params = TradeParams(id=tid) if tid else TradeParams(asset_id=str(asset_id))
            raw = session.client.get_trades(params, only_first_page=True) or []
            out.extend(t for t in raw if isinstance(t, dict))
        except Exception as exc:
            logger.debug("get_trades id=%s: %r", tid, exc)
    seen = set()
    uniq = []
    for t in out:
        key = t.get("id") or t.get("transaction_hash") or id(t)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(t)
    return uniq


def resolve_fill(account: dict, order_resp: dict | None, side: str) -> dict:
    """Fetch the real CLOB fill after posting — never use local quote math."""
    import time as _time

    resp = order_resp if isinstance(order_resp, dict) else {}
    oid = resp.get("orderID") or resp.get("order_id") or resp.get("id")
    trade_ids = [str(x) for x in (resp.get("tradeIDs") or []) if x]
    is_buy = side.upper() == "BUY"

    order: dict = {}
    trades: list[dict] = []
    for _ in range(4):
        if oid:
            order = fetch_clob_order(account, str(oid)) or order
        if trade_ids:
            trades = fetch_clob_trades(account, trade_ids) or trades
        matched = _num(order.get("size_matched") or order.get("size"))
        if trades or matched > 0 or _num(resp.get("takingAmount") or resp.get("makingAmount")) > 0:
            if trades or matched > 0:
                break
        _time.sleep(0.35)

    making = _num(resp.get("makingAmount") or order.get("makingAmount"))
    taking = _num(resp.get("takingAmount") or order.get("takingAmount"))
    trade_size = sum(_num(t.get("size")) for t in trades)
    trade_notional = sum(_num(t.get("size")) * _num(t.get("price")) for t in trades)
    trade_fee = sum(_num(t.get("fee") or t.get("fee_amount") or t.get("taker_fee")) for t in trades)
    avg_px = (trade_notional / trade_size) if trade_size else 0.0

    if is_buy:
        shares = taking or trade_size or _num(order.get("size_matched") or order.get("original_size"))
        usd = making or trade_notional
    else:
        shares = making or trade_size or _num(order.get("size_matched") or order.get("original_size"))
        usd = taking or trade_notional

    price = avg_px or _num(order.get("price"))
    if (not price) and shares > 0 and usd > 0:
        price = usd / shares
    if usd <= 0 and shares > 0 and price > 0:
        usd = shares * price

    status = str(order.get("status") or resp.get("status") or "").upper()
    hashes = list(resp.get("transactionsHashes") or [])
    for t in trades:
        h = t.get("transaction_hash") or t.get("transactionHash")
        if h and h not in hashes:
            hashes.append(h)

    return {
        "order_id": oid,
        "status": status,
        "shares": shares,
        "usd": usd,
        "price": price,
        "price_cents": round(price * 100.0, 2) if price else None,
        "fee": trade_fee,
        "tx_hashes": hashes,
        "filled": shares > 0 and status not in ("CANCELLED", "CANCELED", "EXPIRED"),
    }


def place_market_order(
    account: dict,
    token_id: str,
    side: str,
    amount: float,
    price: float = 0,
) -> dict:
    """Place a market order. amount = USD for BUY, shares for SELL."""
    session = get_session(account)
    is_buy = side.upper() == "BUY"

    if session.v2:
        from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType
        from py_clob_client_v2.order_utils.model.side import Side
        side_const = Side.BUY if is_buy else Side.SELL
        args = MarketOrderArgsV2(
            token_id=str(token_id),
            amount=float(amount),
            side=side_const,
            price=0,
            order_type=OrderType.FOK,
        )
        resp = session.client.create_and_post_market_order(args, order_type=OrderType.FOK)
        return resp if isinstance(resp, dict) else {"response": resp}

    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    side_const = BUY if is_buy else SELL
    args = MarketOrderArgs(
        token_id=str(token_id),
        amount=float(amount),
        side=side_const,
        price=float(price) if price > 0 else 0,
        order_type=OrderType.FOK,
    )
    signed = session.client.create_market_order(args)
    resp = session.client.post_order(signed, OrderType.FOK)
    return resp if isinstance(resp, dict) else {"response": resp}


def token_for_side(pipe, side: str) -> str | None:
    market = getattr(getattr(pipe, "polymarket", None), "market", None) or {}
    key = "up_token" if side.upper() == "UP" else "down_token"
    tok = market.get(key)
    return str(tok) if tok else None
