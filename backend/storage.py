"""SQLite persistence: bots, every trade, and per-round parity records.

Plain synchronous sqlite3 (WAL) — every write here is a sub-millisecond
single-row statement, far below the async loop's latency budget.

The ``rounds`` table doubles as the RESOLUTION PARITY STUDY: for every
round we record both price-to-beat candidates (rolling TWAP at start vs
chainlink price at start), both final candidates (60s-lookback TWAP at
end vs whole-range TWAP), our local verdict and the official Polymarket
resolution — so the exact resolution formula proves itself empirically.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "twaphunter.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_ms REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    t REAL NOT NULL,
    window_id INTEGER,
    round_label TEXT,
    action TEXT NOT NULL,
    side TEXT,
    price_cents REAL,
    shares REAL,
    pnl REAL,
    reason TEXT,
    balance REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_bot ON trades(bot_id, t);
CREATE TABLE IF NOT EXISTS rounds (
    asset TEXT NOT NULL,
    window_id INTEGER NOT NULL,
    start_ms REAL,
    round_label TEXT,
    ptb_twap REAL,
    ptb_price REAL,
    final_twap REAL,
    final_range_twap REAL,
    outcome_local TEXT,
    outcome_official TEXT,
    updated_ms REAL,
    PRIMARY KEY (asset, window_id)
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_ms REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS real_bots (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_ms REAL NOT NULL
);
"""


class Storage:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- bots ------------------------------------------------------------

    def save_bot(self, bot: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO bots(id, data, updated_ms) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_ms=excluded.updated_ms",
                (bot["id"], json.dumps(bot), time.time() * 1000.0),
            )
            self._conn.commit()

    def delete_bot(self, bot_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM bots WHERE id=?", (bot_id,))
            self._conn.commit()

    def load_bots(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT id, data FROM bots").fetchall()
        out: dict[str, dict] = {}
        for bot_id, data in rows:
            try:
                out[bot_id] = json.loads(data)
            except ValueError:
                logger.warning("storage: undecodable bot row %s", bot_id)
        return out

    # -- trades ----------------------------------------------------------

    def add_trade(self, bot_id: str, trade: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO trades(bot_id, t, window_id, round_label, action, side, "
                "price_cents, shares, pnl, reason, balance) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bot_id, trade.get("t"), trade.get("window_id"),
                    trade.get("round_label"), trade.get("action"), trade.get("side"),
                    trade.get("price_cents"), trade.get("shares"), trade.get("pnl"),
                    trade.get("reason"), trade.get("balance"),
                ),
            )
            self._conn.commit()

    def trades_for(self, bot_id: str, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT t, window_id, round_label, action, side, price_cents, shares, "
                "pnl, reason, balance FROM trades WHERE bot_id=? ORDER BY t DESC, id DESC LIMIT ?",
                (bot_id, limit),
            ).fetchall()
        keys = ["t", "window_id", "round_label", "action", "side", "price_cents",
                "shares", "pnl", "reason", "balance"]
        return [dict(zip(keys, row)) for row in rows]

    def delete_trades(self, bot_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM trades WHERE bot_id=?", (bot_id,))
            self._conn.commit()

    # -- rounds (parity study) -------------------------------------------

    def upsert_round(self, asset: str, window_id: int, **fields) -> None:
        cols = ["start_ms", "round_label", "ptb_twap", "ptb_price", "final_twap",
                "final_range_twap", "outcome_local", "outcome_official"]
        sets = {k: v for k, v in fields.items() if k in cols and v is not None}
        if not sets:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO rounds(asset, window_id, updated_ms) VALUES(?,?,?) "
                "ON CONFLICT(asset, window_id) DO NOTHING",
                (asset, window_id, time.time() * 1000.0),
            )
            assignments = ", ".join(f"{k}=?" for k in sets)
            self._conn.execute(
                f"UPDATE rounds SET {assignments}, updated_ms=? WHERE asset=? AND window_id=?",
                [*sets.values(), time.time() * 1000.0, asset, window_id],
            )
            self._conn.commit()

    def get_round(self, asset: str, window_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT asset, window_id, round_label, outcome_local, outcome_official "
                "FROM rounds WHERE asset=? AND window_id=?",
                (asset, window_id),
            ).fetchone()
        if row is None:
            return None
        keys = ["asset", "window_id", "round_label", "outcome_local", "outcome_official"]
        return dict(zip(keys, row))

    def rounds_for(self, asset: str | None = None, limit: int = 200) -> list[dict]:
        query = (
            "SELECT asset, window_id, start_ms, round_label, ptb_twap, ptb_price, "
            "final_twap, final_range_twap, outcome_local, outcome_official "
            "FROM rounds {} ORDER BY window_id DESC LIMIT ?"
        )
        args: list = []
        where = ""
        if asset:
            where = "WHERE asset=?"
            args.append(asset)
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(query.format(where), args).fetchall()
        keys = ["asset", "window_id", "start_ms", "round_label", "ptb_twap", "ptb_price",
                "final_twap", "final_range_twap", "outcome_local", "outcome_official"]
        return [dict(zip(keys, row)) for row in rows]

    def parity_summary(self) -> dict:
        """How often each candidate formula matches the official outcome."""
        rows = self.rounds_for(limit=2000)
        combos = {
            "twap_end_vs_ptb_twap": ("final_twap", "ptb_twap"),
            "twap_end_vs_ptb_price": ("final_twap", "ptb_price"),
            "range_twap_vs_ptb_twap": ("final_range_twap", "ptb_twap"),
            "range_twap_vs_ptb_price": ("final_range_twap", "ptb_price"),
        }
        out = {}
        for name, (final_key, ptb_key) in combos.items():
            n = hit = 0
            for r in rows:
                official = r.get("outcome_official")
                final, ptb = r.get(final_key), r.get(ptb_key)
                if official not in ("UP", "DOWN") or final is None or ptb is None:
                    continue
                predicted = "UP" if final >= ptb else "DOWN"
                n += 1
                hit += 1 if predicted == official else 0
            out[name] = {"n": n, "hit": hit, "pct": round(100.0 * hit / n, 1) if n else None}
        return out

    # -- accounts --------------------------------------------------------

    def save_account(self, account: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO accounts(id, data, updated_ms) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_ms=excluded.updated_ms",
                (account["id"], json.dumps(account), time.time() * 1000.0),
            )
            self._conn.commit()

    def delete_account(self, account_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            self._conn.commit()

    def get_account(self, account_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def load_accounts(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT id, data FROM accounts").fetchall()
        out: dict[str, dict] = {}
        for account_id, data in rows:
            try:
                out[account_id] = json.loads(data)
            except ValueError:
                logger.warning("storage: undecodable account row %s", account_id)
        return out

    def real_bots_for_account(self, account_id: str) -> list[dict]:
        return [b for b in self.load_real_bots().values() if b.get("account_id") == account_id]

    # -- real bots -------------------------------------------------------

    def save_real_bot(self, bot: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO real_bots(id, data, updated_ms) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_ms=excluded.updated_ms",
                (bot["id"], json.dumps(bot), time.time() * 1000.0),
            )
            self._conn.commit()

    def delete_real_bot(self, bot_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM real_bots WHERE id=?", (bot_id,))
            self._conn.commit()

    def load_real_bots(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT id, data FROM real_bots").fetchall()
        out: dict[str, dict] = {}
        for bot_id, data in rows:
            try:
                out[bot_id] = json.loads(data)
            except ValueError:
                logger.warning("storage: undecodable real bot row %s", bot_id)
        return out


storage = Storage()
