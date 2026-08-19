"""Real-trading bots — live CLOB execution linked to Polymarket accounts."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from backend.config import CONFIG
from backend.engine.bots import (
    STRATEGIES, taker_fee, _EVALUATORS, _fresh_rt, sanitize_params,
    lock_profit_if_bid,
)
from backend.engine.clob_executor import (
    REAL_MIN_ORDER_SHARES, place_market_order, resolve_fill,
    sync_clob_balance, token_for_side, token_share_balance,
)
from backend.engine.pipeline import round_label_et
from backend.feeds.account_data import filter_for_asset, invalidate_snapshot
from backend.storage import storage

logger = logging.getLogger(__name__)

COPY_ALL = "ALL"


class RealBotManager:
    def __init__(self, hub, bot_manager) -> None:
        self._hub = hub
        self._bot_manager = bot_manager
        self.bots: dict[str, dict] = storage.load_real_bots()
        self._rt: dict[str, dict] = {}
        self._dirty: set[str] = set()
        self._loop_task: asyncio.Task | None = None
        self._boot_task: asyncio.Task | None = None
        self._normalize_statuses()
        for bot in self.bots.values():
            self._ensure_fields(bot)

    def _ensure_fields(self, bot: dict) -> None:
        bot.setdefault("balance", 0.0)
        bot.setdefault("start_balance", 0.0)
        bot.setdefault("position", None)
        bot.setdefault("trades", [])
        bot.setdefault("stats", {"rounds": 0, "wins": 0, "losses": 0, "fees": 0.0})
        bot.setdefault("last_exec_error", None)
        bot.setdefault("orders_placed", 0)
        bot.setdefault("activity_log", [])

    def _normalize_statuses(self) -> None:
        for bot in self.bots.values():
            s = bot.get("status", "stopped")
            if s in ("disarmed", "armed"):
                bot["status"] = "running" if s == "armed" else "stopped"

    async def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop(), name="real-bot-loop")
        self._boot_task = asyncio.create_task(self._boot_running(), name="real-bot-boot")

    async def _boot_running(self) -> None:
        for bot in self.bots.values():
            if bot.get("status") != "running" or bot.get("asset") == COPY_ALL:
                continue
            try:
                await self._hub.retain(bot["asset"], bot["id"])
            except Exception:
                logger.exception("retain failed for real bot %s", bot["id"])
            account = storage.get_account(bot.get("account_id", ""))
            if account:
                try:
                    self._sync_balance(bot, account)
                except Exception:
                    logger.exception("balance sync failed for real bot %s", bot["id"])

    async def stop(self) -> None:
        if self._boot_task is not None:
            self._boot_task.cancel()
            try:
                await self._boot_task
            except (asyncio.CancelledError, Exception):
                pass
            self._boot_task = None
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None
        self._save(force=True)

    def _save(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        for bot_id in list(self._dirty):
            bot = self.bots.get(bot_id)
            if bot is not None:
                storage.save_real_bot(bot)
        self._dirty.clear()

    def _running(self, bot: dict) -> bool:
        return bot.get("status") == "running"

    def _sync_balance(self, bot: dict, account: dict) -> float:
        cash = sync_clob_balance(account)
        if bot.get("start_balance", 0) <= 0 and cash > 0:
            bot["start_balance"] = cash
        bot["balance"] = cash
        bot["balance_synced_ms"] = time.time() * 1000.0
        self._dirty.add(bot["id"])
        return cash

    def _clob_fill(self, bot: dict, account: dict, resp: dict, side: str,
                   token_id: str | None = None) -> dict:
        """Confirm fill from Polymarket CLOB, then resync cash."""
        fill = resolve_fill(account, resp, side)
        if token_id and side.upper() == "BUY":
            try:
                held = token_share_balance(account, token_id)
                if held > 0:
                    fill["shares"] = held
            except Exception:
                pass
        self._sync_balance(bot, account)
        invalidate_snapshot(account)
        return fill

    def _activity(self, bot: dict, level: str, msg: str) -> None:
        entry = {"t": time.time() * 1000.0, "level": level, "msg": msg}
        log = bot.setdefault("activity_log", [])
        log.append(entry)
        if len(log) > 150:
            bot["activity_log"] = log[-150:]
        self._dirty.add(bot["id"])
        if level in ("error", "order"):
            logger.info("real bot %s [%s] %s", bot["id"], level, msg)

    def _quote(self, pipe, side: str):
        return self._bot_manager._quote(pipe, side)

    def _log(self, bot: dict, action: str, side, price_cents, shares, pnl, reason, wid=None):
        trade = {
            "t": time.time() * 1000.0,
            "action": action,
            "side": side,
            "price_cents": price_cents,
            "shares": shares,
            "pnl": pnl,
            "reason": reason,
            "window_id": wid,
            "round_label": round_label_et(wid) if wid is not None else None,
        }
        bot.setdefault("trades", []).append(trade)
        if len(bot["trades"]) > 500:
            bot["trades"] = bot["trades"][-500:]
        self._dirty.add(bot["id"])

    def _bump_winloss(self, bot: dict, pnl: float) -> None:
        if pnl > 0:
            bot["stats"]["wins"] += 1
        elif pnl < 0:
            bot["stats"]["losses"] += 1

    def buy(self, bot: dict, rt: dict, pipe, side: str, order_usd: float,
            max_entry_cents: float, reason: str) -> bool:
        if bot.get("position") is not None:
            return False
        account = storage.get_account(bot.get("account_id", ""))
        if account is None:
            bot["last_exec_error"] = "no linked account"
            self._activity(bot, "error", "no linked account")
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
        min_shares = REAL_MIN_ORDER_SHARES
        need_usd = round(min_shares * price, 2)
        if shares < min_shares:
            msg = (
                f"skip BUY {side}: lot ${order_usd:.2f} = {shares:.2f} sh @ {ask:.0f}¢ "
                f"— CLOB min is {min_shares:.0f} sh (~${need_usd:.2f}). Raise LOT."
            )
            bot["last_exec_error"] = msg
            if bot.get("_last_skip") != msg:
                bot["_last_skip"] = msg
                self._activity(bot, "skip", msg)
            return False
        cost = shares * price
        fee = taker_fee(shares, price)
        if cost + fee > bot.get("balance", 0):
            msg = (f"insufficient balance ${bot.get('balance', 0):.2f} "
                   f"need ${cost + fee:.2f} for {shares:.1f} sh @ {ask:.0f}¢")
            bot["last_exec_error"] = msg
            self._activity(bot, "skip", msg)
            return False

        token_id = token_for_side(pipe, side)
        if not token_id:
            bot["last_exec_error"] = "no market token id"
            self._activity(bot, "error", "no market token id")
            return False

        self._activity(bot, "signal",
                         f"BUY {side} ${order_usd:.2f} @ {ask:.0f}¢ — {reason}")
        cash_before = float(bot.get("balance") or 0)
        try:
            resp = place_market_order(account, token_id, "BUY", order_usd, price)
            bot["orders_placed"] = bot.get("orders_placed", 0) + 1
            fill = self._clob_fill(bot, account, resp, "BUY", token_id)
        except Exception as exc:
            msg = f"order failed: {exc}"
            bot["last_exec_error"] = msg
            self._activity(bot, "error", f"BUY {side} failed: {exc}")
            logger.warning("real buy failed %s: %r", bot["id"], exc)
            return False

        shares_filled = float(fill.get("shares") or 0)
        usd_filled = float(fill.get("usd") or 0)
        if usd_filled <= 0:
            usd_filled = max(0.0, cash_before - float(bot.get("balance") or 0))
        px = float(fill.get("price") or 0)
        if (not px) and shares_filled > 0 and usd_filled > 0:
            px = usd_filled / shares_filled
        entry_cents = fill.get("price_cents") or (px * 100.0 if px else ask)
        if shares_filled <= 0:
            bot["last_exec_error"] = "BUY posted but Polymarket reported 0 filled shares"
            self._activity(bot, "error",
                           f"BUY {side} no fill from CLOB resp={resp}")
            return False

        bot["last_exec_error"] = None
        oid = fill.get("order_id")
        self._activity(
            bot, "order",
            f"BUY {side} {shares_filled:.4f} sh @ {entry_cents:.1f}¢ "
            f"(${usd_filled:.4f} from CLOB) — {reason}"
            + (f" id={str(oid)[:16]}…" if oid else ""),
        )
        logger.info("REAL BUY %s %s fill=%s resp=%s", bot["id"], side, fill, resp)

        bot["position"] = {
            "side": side, "shares": shares_filled, "cost": usd_filled,
            "entry_cents": entry_cents, "fee_paid": float(fill.get("fee") or 0),
            "window_id": pipe.window_id, "entry_t": now,
            "asset": pipe.asset.key,
            "token_id": token_id,
            "order_id": oid,
            "tx_hashes": fill.get("tx_hashes") or [],
        }
        wid = pipe.window_id
        rt["entries"][wid] = rt["entries"].get(wid, 0) + 1
        self._dirty.add(bot["id"])
        return True

    def _finish_close(self, bot: dict, pos: dict, action: str, pnl: float, reason: str) -> None:
        self._bump_winloss(bot, pnl)
        bot["position"] = None
        bot["last_exec_error"] = None
        self._dirty.add(bot["id"])

    def _close_stale(self, bot: dict, account: dict, pos: dict) -> None:
        """Flatten leftover shares after the 5m round rolls."""
        token_id = pos.get("token_id")
        shares = float(pos.get("shares") or 0)
        if not token_id or shares <= 0:
            bot["position"] = None
            return
        cost = float(pos.get("cost") or 0) + float(pos.get("fee_paid") or 0)
        now_ms = time.time() * 1000.0
        if now_ms < float(bot.get("flatten_next_ms") or 0):
            return
        bot["flatten_next_ms"] = now_ms + 8_000
        self._activity(bot, "signal",
                       f"round ended — flatten {pos.get('side')} {shares:.1f} sh")
        cash_before = float(bot.get("balance") or 0)
        held = token_share_balance(account, token_id)
        sell_shares = min(shares, held)
        if sell_shares <= 0:
            cash = self._sync_balance(bot, account)
            delta = cash - cash_before
            if delta >= shares * 0.9:
                pnl = shares - cost
                self._activity(bot, "order", f"SETTLE {pos.get('side')} won +${pnl:.2f}")
                self._finish_close(bot, pos, "SETTLE", pnl, "round resolved — won")
            elif held <= 0.01 and delta <= 0.05:
                pnl = -cost
                self._activity(bot, "order", f"SETTLE {pos.get('side')} lost {pnl:.2f}")
                self._finish_close(bot, pos, "SETTLE", pnl, "round resolved — lost (0 shares left)")
            else:
                bot["last_exec_error"] = f"flatten: 0 sellable shares (held={held:.4f})"
            return
        sell_shares = max(0.01, round(sell_shares * 0.999, 4))
        try:
            resp = place_market_order(account, token_id, "SELL", sell_shares, 0)
            fill = self._clob_fill(bot, account, resp, "SELL", token_id)
            cash = float(bot.get("balance") or 0)
            proceeds = float(fill.get("usd") or 0)
            if proceeds <= 0:
                proceeds = max(0.0, cash - cash_before)
            pnl = proceeds - cost
            bot["orders_placed"] = bot.get("orders_placed", 0) + 1
            sh = float(fill.get("shares") or sell_shares)
            px = fill.get("price_cents")
            status = fill.get("status") or (resp.get("status") if isinstance(resp, dict) else resp)
            self._activity(
                bot, "order",
                f"SELL {pos.get('side')} {sh:.4f} sh flatten"
                + (f" @ {px:.1f}¢" if px else "")
                + f" ${proceeds:.4f} ({status})",
            )
            logger.info("REAL FLATTEN %s %s fill=%s resp=%s", bot["id"], pos.get("side"), fill, resp)
            self._finish_close(bot, pos, "SELL", pnl, "round ended — flatten leftover")
        except Exception as exc:
            cash = self._sync_balance(bot, account)
            delta = cash - cash_before
            logger.warning("real flatten failed %s: %r cash %.2f -> %.2f",
                           bot["id"], exc, cash_before, cash)
            if delta >= shares * 0.9:
                pnl = shares - cost
                self._activity(bot, "order",
                               f"SETTLE {pos.get('side')} won +${pnl:.2f}")
                self._finish_close(bot, pos, "SETTLE", pnl, "round resolved — won")
                return
            err = str(exc).lower()
            no_book = "no orderbook" in err or "404" in err
            if no_book:
                pnl = -cost
                self._activity(
                    bot, "order",
                    f"SETTLE {pos.get('side')} lost {pnl:.2f} — market gone, leftover untradeable",
                )
                self._finish_close(
                    bot, pos, "SETTLE", pnl,
                    "round resolved — lost (no orderbook)",
                )
                return
            bot["last_exec_error"] = f"waiting to flatten: {exc}"

    def sell(self, bot: dict, pipe, reason: str) -> bool:
        pos = bot.get("position")
        if pos is None or pos["window_id"] != pipe.window_id:
            return False
        account = storage.get_account(bot.get("account_id", ""))
        if account is None:
            return False

        _ask, _sz, bid = self._quote(pipe, pos["side"])
        if bid is None or bid <= 0:
            return False
        price = bid / 100.0
        shares = pos["shares"]
        token_id = pos.get("token_id") or token_for_side(pipe, pos["side"])
        if not token_id:
            bot["last_exec_error"] = "no token for sell"
            return False

        self._activity(bot, "signal", f"SELL {pos['side']} {shares:.1f} sh @ {bid:.0f}¢ — {reason}")
        cash_before = float(bot.get("balance") or 0)
        try:
            held = token_share_balance(account, token_id)
            sell_shares = min(float(shares), held) if held > 0 else float(shares)
            sell_shares = max(0.01, round(sell_shares * 0.999, 4))
            resp = place_market_order(account, token_id, "SELL", sell_shares, price)
            bot["orders_placed"] = bot.get("orders_placed", 0) + 1
            fill = self._clob_fill(bot, account, resp, "SELL", token_id)
        except Exception as exc:
            msg = f"sell failed: {exc}"
            bot["last_exec_error"] = msg
            self._activity(bot, "error", f"SELL failed: {exc}")
            logger.warning("real sell failed %s: %r", bot["id"], exc)
            return False

        proceeds = float(fill.get("usd") or 0)
        if proceeds <= 0:
            proceeds = max(0.0, float(bot.get("balance") or 0) - cash_before)
        sh = float(fill.get("shares") or sell_shares)
        px = fill.get("price_cents")
        bot["last_exec_error"] = None
        self._activity(
            bot, "order",
            f"SELL {pos['side']} {sh:.4f} sh"
            + (f" @ {px:.1f}¢" if px else "")
            + f" ${proceeds:.4f} from CLOB — {reason}",
        )
        logger.info("REAL SELL %s %s fill=%s resp=%s", bot["id"], pos["side"], fill, resp)
        pnl = proceeds - (float(pos.get("cost") or 0) + float(pos.get("fee_paid") or 0))
        self._bump_winloss(bot, pnl)
        bot["position"] = None
        self._dirty.add(bot["id"])
        return True

    async def _loop(self) -> None:
        balance_tick = 0
        while True:
            await asyncio.sleep(0.25)
            balance_tick += 1
            try:
                for bot in list(self.bots.values()):
                    if bot.get("status") != "running":
                        continue
                    asset = bot.get("asset")
                    if asset == COPY_ALL:
                        bot["last_exec_error"] = "copy_trader not supported on real yet"
                        continue

                    pipe = self._hub.pipelines.get(asset)
                    if pipe is None:
                        pipe = await self._hub.retain(asset, bot["id"])
                    account = storage.get_account(bot.get("account_id", ""))
                    if account is None:
                        continue

                    if balance_tick % 40 == 0:  # ~10s
                        self._sync_balance(bot, account)
                    elif bot.get("balance", 0) <= 0 and not bot.get("last_exec_error"):
                        bot["last_exec_error"] = (
                            "CLOB balance $0.00 — deposit USDC at polymarket.com to place trades")

                    rt = self._rt.setdefault(bot["id"], _fresh_rt())
                    now = pipe.poly_now_ms()
                    wid = pipe.window_id
                    if rt.get("round") != wid:
                        rt["round"] = wid
                        rt["entries"] = {}
                        rt["switches"] = 0
                        rt["adverse_since"] = None

                    pos = bot.get("position")
                    if pos is not None and pos["window_id"] != wid:
                        self._close_stale(bot, account, pos)
                        continue
                    if pos is not None and lock_profit_if_bid(self, bot, pipe):
                        continue

                    fn = _EVALUATORS.get(bot.get("strategy"))
                    if fn is not None:
                        fn(self, bot, rt, pipe, now)

                if balance_tick % 4 == 0:
                    self._save()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("real bot loop error")

    def _synthetic_for_brain(self, bot: dict) -> dict:
        return {
            "id": bot["id"],
            "asset": bot["asset"],
            "strategy": bot["strategy"],
            "params": bot.get("params") or {},
            "position": bot.get("position"),
            "book": bot.get("book"),
            "portfolio": bot.get("portfolio") or {},
            "status": bot.get("status", "stopped"),
        }

    def _position_from_account(self, positions, asset, pipe):
        if not positions:
            return None
        wid = pipe.window_id if pipe else None
        slug_needle = f"{asset.lower()}-updown-5m" if asset != COPY_ALL else ""
        best = None
        for p in positions:
            slug = (p.get("event_slug") or "").lower()
            if asset != COPY_ALL and slug_needle not in slug:
                continue
            outcome = str(p.get("outcome") or "").upper()
            if outcome not in ("UP", "DOWN", "YES", "NO"):
                continue
            if float(p.get("cur_price_cents") or 0) <= 0:
                continue
            side = "UP" if outcome in ("UP", "YES") else "DOWN"
            pos_view = {
                "side": side,
                "shares": round(float(p.get("size") or 0), 4),
                "entry_cents": p.get("avg_price_cents", 0),
                "mark_cents": p.get("cur_price_cents"),
                "cost": round(float(p.get("size") or 0) * float(p.get("avg_price_cents") or 0) / 100, 4),
                "window_id": wid,
                "round_label": round_label_et(wid) if wid else "",
                "waiting_official": False,
                "asset": asset,
                "title": p.get("title") or "",
            }
            if slug_needle and slug_needle in slug:
                return pos_view
            best = best or pos_view
        return best

    def _stats_from_trades(self, trades: list[dict], positions: list[dict] | None = None) -> dict:
        slugs = {t.get("event_slug") for t in trades if t.get("event_slug")}
        wins = losses = 0
        fees = 0.0
        for t in trades:
            pnl = t.get("cash_pnl")
            if pnl is None:
                continue
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
        if not wins and not losses and positions:
            for p in positions:
                pnl = p.get("cash_pnl")
                if pnl is None:
                    continue
                if pnl > 0:
                    wins += 1
                elif pnl < 0:
                    losses += 1
        return {"wins": wins, "losses": losses, "rounds": len(slugs), "fees": fees}

    def _format_trades_for_ui(self, trades: list[dict]) -> list[dict]:
        out = []
        for t in trades:
            action = str(t.get("action") or t.get("side") or "BUY").upper()
            if action not in ("BUY", "SELL"):
                action = "BUY"
            outcome = str(t.get("outcome") or "").upper()
            if outcome in ("YES",):
                outcome = "UP"
            elif outcome in ("NO",):
                outcome = "DOWN"
            shares = t.get("shares") if t.get("shares") is not None else t.get("size")
            out.append({
                "t": t.get("t"),
                "action": action,
                "side": outcome or None,
                "outcome": outcome or None,
                "price_cents": t.get("price_cents"),
                "shares": shares,
                "size": shares,
                "usd": t.get("usd"),
                "pnl": t.get("cash_pnl") if t.get("cash_pnl") is not None else t.get("pnl"),
                "cash_pnl": t.get("cash_pnl"),
                "reason": t.get("title") or t.get("event_slug") or t.get("reason") or "",
                "round_label": t.get("event_slug") or t.get("round_label") or "",
                "title": t.get("title") or "",
                "event_slug": t.get("event_slug") or "",
                "tx": t.get("tx") or "",
            })
        return out

    def public_view(self, bot: dict, snapshot: dict | None = None,
                    trades_limit: int = 20, include_brain: bool = False) -> dict:
        strat = STRATEGIES.get(bot["strategy"], {})
        account = storage.get_account(bot["account_id"])
        running = self._running(bot)

        positions = trades = []
        bot_cash_pnl = bot_pos_value = 0.0
        cash_balance = 0.0
        if snapshot:
            if snapshot.get("cash_balance") is not None:
                cash_balance = float(snapshot["cash_balance"])
            positions = filter_for_asset(snapshot.get("positions") or [], bot["asset"])
            trades = filter_for_asset(snapshot.get("trades") or [], bot["asset"])
            if bot["asset"] == COPY_ALL:
                positions = snapshot.get("positions") or []
                trades = snapshot.get("trades") or []
            bot_cash_pnl = round(sum(float(p.get("cash_pnl") or 0) for p in positions), 4)
            bot_pos_value = round(sum(float(p.get("current_value") or 0) for p in positions), 4)
        if cash_balance <= 0:
            cash_balance = float(bot.get("balance") or 0)

        pipe = self._hub.pipelines.get(bot["asset"])
        pos_view = self._position_from_account(positions, bot["asset"], pipe)
        if pos_view is None and bot.get("position"):
            # CLOB-confirmed open lot only (fill price/size from Polymarket).
            local_pos = bot["position"]
            mark = None
            if pipe and pipe.last_quote:
                key = "up_bid" if local_pos["side"] == "UP" else "down_bid"
                mark = pipe.last_quote.get(key)
            pos_view = {
                "side": local_pos["side"],
                "shares": round(float(local_pos.get("shares") or 0), 4),
                "entry_cents": local_pos.get("entry_cents"),
                "mark_cents": mark,
                "cost": round(float(local_pos.get("cost") or 0), 4),
                "window_id": local_pos.get("window_id"),
                "round_label": round_label_et(local_pos["window_id"]) if local_pos.get("window_id") is not None else "",
                "waiting_official": False,
                "asset": local_pos.get("asset") or bot["asset"],
                "order_id": local_pos.get("order_id"),
            }

        equity = round(cash_balance + bot_pos_value, 4)
        if snapshot and snapshot.get("total_equity") is not None and bot["asset"] == COPY_ALL:
            equity = round(float(snapshot["total_equity"]), 4)
        start_bal = bot.get("start_balance") or cash_balance or 1.0
        pnl = bot_cash_pnl if positions else round(equity - start_bal, 4)

        display_trades = self._format_trades_for_ui(trades[:trades_limit])

        view = {
            "id": bot["id"],
            "name": bot["name"],
            "asset": bot["asset"],
            "strategy": bot["strategy"],
            "strategy_name": strat.get("name", bot["strategy"]),
            "params": bot.get("params", {}),
            "status": "running" if running else "stopped",
            "account_id": bot["account_id"],
            "account_label": account["label"] if account else None,
            "account_address": (account.get("proxy_wallet") or account["address"]) if account else None,
            "source_paper_bot_id": bot.get("source_paper_bot_id"),
            "created_ms": bot.get("created_ms"),
            "real": True,
            "portfolio_value": equity,
            "cash_balance": cash_balance,
            "positions_value": round(bot_pos_value, 4),
            "account_cash_pnl": bot_cash_pnl,
            "profile_name": snapshot.get("profile_name") if snapshot else account.get("profile_name") if account else None,
            "positions": positions,
            "positions_count": len(positions),
            "equity": equity,
            "balance": round(cash_balance, 4),
            "pnl": pnl,
            "pnl_pct": round((pnl / start_bal) * 100, 2) if start_bal else 0,
            "start_balance": round(start_bal, 4),
            "position": pos_view,
            "book": None,
            "portfolio": [],
            "stats": self._stats_from_trades(trades, positions),
            "trades": display_trades,
            "orders_placed": bot.get("orders_placed", 0),
            "last_exec_error": bot.get("last_exec_error"),
            "balance_synced_ms": bot.get("balance_synced_ms"),
            "activity_log": list(reversed(bot.get("activity_log") or []))[:50],
        }

        if include_brain:
            view["brain"] = self._bot_manager.brain(self._synthetic_for_brain(bot))
        return view

    async def set_running(self, bot_id: str, running: bool) -> dict | None:
        bot = self.bots.get(bot_id)
        if bot is None:
            return None
        self._ensure_fields(bot)
        account = storage.get_account(bot.get("account_id", ""))
        if running and account:
            self._sync_balance(bot, account)

        bot["status"] = "running" if running else "stopped"
        if not running:
            bot["last_exec_error"] = None
        storage.save_real_bot(bot)
        asset = bot["asset"]
        if asset != COPY_ALL:
            if running:
                await self._hub.retain(asset, bot_id)
            else:
                await self._hub.unretain(asset, bot_id)
        logger.info("real bot %s %s balance=$%.2f", bot_id,
                    "started" if running else "stopped", bot.get("balance", 0))
        return bot

    def update(self, bot_id: str, name: str | None = None,
               params: dict | None = None) -> dict | None:
        bot = self.bots.get(bot_id)
        if bot is None:
            return None
        self._ensure_fields(bot)
        if params:
            bot.setdefault("params", {}).update(
                sanitize_params(bot["strategy"], params))
        if name:
            bot["name"] = str(name)[:40]
        storage.save_real_bot(bot)
        self._dirty.add(bot_id)
        return bot

    def import_from_paper(self, paper_bot: dict, account_id: str, name: str | None = None) -> dict:
        account = storage.get_account(account_id)
        if account is None:
            raise ValueError(f"unknown account {account_id!r}")

        bot_id = f"r_{uuid.uuid4().hex[:8]}"
        bot = {
            "id": bot_id,
            "name": (name or paper_bot["name"]).strip() or paper_bot["name"],
            "asset": paper_bot["asset"],
            "strategy": paper_bot["strategy"],
            "params": dict(paper_bot.get("params") or {}),
            "account_id": account_id,
            "source_paper_bot_id": paper_bot["id"],
            "status": "stopped",
            "created_ms": time.time() * 1000.0,
            "balance": 0.0,
            "start_balance": 0.0,
            "position": None,
            "trades": [],
            "stats": {"rounds": 0, "wins": 0, "losses": 0, "fees": 0.0},
            "orders_placed": 0,
            "last_exec_error": None,
        }
        if account:
            bot["balance"] = sync_clob_balance(account)
            bot["start_balance"] = bot["balance"]
        self.bots[bot_id] = bot
        storage.save_real_bot(bot)
        logger.info("imported real bot %s from paper bot %s balance=$%.2f",
                    bot_id, paper_bot["id"], bot["balance"])
        return bot

    def delete(self, bot_id: str) -> bool:
        if bot_id not in self.bots:
            return False
        del self.bots[bot_id]
        storage.delete_real_bot(bot_id)
        return True
