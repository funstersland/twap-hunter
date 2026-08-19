"""Polymarket account storage — private keys encrypted at rest.

API responses never include the private key; only id, label, and address.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from eth_account import Account

logger = logging.getLogger(__name__)

KEY_FILE = Path(__file__).resolve().parent.parent / "data" / ".encryption_key"
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _fernet() -> Fernet:
    if not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_bytes(Fernet.generate_key())
        logger.info("generated encryption key at %s", KEY_FILE)
    return Fernet(KEY_FILE.read_bytes())


def _normalize_address(addr: str) -> str:
    addr = addr.strip()
    if not addr.startswith("0x"):
        addr = "0x" + addr
    return addr


def _normalize_key(key: str) -> str:
    key = key.strip()
    if key.startswith("0x"):
        key = key[2:]
    return key


def derive_address(private_key: str) -> str:
    """Return checksummed EOA address from a hex private key."""
    pk = _normalize_key(private_key)
    if len(pk) != 64:
        raise ValueError("private key must be 32 bytes (64 hex chars)")
    acct = Account.from_key(bytes.fromhex(pk))
    return acct.address


def validate_account_input(label: str, address: str, private_key: str) -> tuple[str, str, str | None]:
    """Validate and normalize account fields. Returns (label, address, mismatch_warning)."""
    label = label.strip()
    if not label:
        raise ValueError("label is required")
    if len(label) > 40:
        raise ValueError("label too long (max 40 chars)")

    address = _normalize_address(address)
    if not _ADDR_RE.match(address):
        raise ValueError("invalid address (expected 0x + 40 hex chars)")

    derived = derive_address(private_key)
    warning = None
    if derived.lower() != address.lower():
        warning = (
            f"address {address} does not match key-derived EOA {derived} "
            "(ok if this is a Polymarket proxy/funder address)"
        )
    return label, address, warning


def encrypt_key(private_key: str) -> str:
    pk = _normalize_key(private_key)
    return _fernet().encrypt(pk.encode()).decode()


def decrypt_key(encrypted: str) -> str:
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("could not decrypt private key") from exc


def new_account_id() -> str:
    return f"acc_{uuid.uuid4().hex[:8]}"


def public_view(account: dict, snapshot: dict | None = None) -> dict:
    view = {
        "id": account["id"],
        "label": account["label"],
        "address": account.get("proxy_wallet") or account["address"],
        "signer_address": account.get("derived_address"),
        "derived_address": account.get("derived_address"),
        "address_mismatch": account.get("address_mismatch", False),
        "profile_name": account.get("profile_name"),
        "created_ms": account.get("created_ms"),
    }
    if snapshot:
        view.update({
            "cash_balance": snapshot.get("cash_balance"),
            "positions_value": snapshot.get("positions_value"),
            "total_equity": snapshot.get("total_equity"),
            "portfolio_value": snapshot.get("total_equity"),
            "cash_pnl": snapshot.get("cash_pnl"),
            "positions_count": snapshot.get("positions_count"),
            "positions": snapshot.get("positions") or [],
            "trades": (snapshot.get("trades") or [])[:20],
            "profile_name": snapshot.get("profile_name") or view.get("profile_name"),
            "allowance_ok": snapshot.get("allowance_ok"),
        })
        if snapshot.get("error"):
            view["fetch_error"] = snapshot["error"]
        if snapshot.get("clob_error"):
            view["clob_error"] = snapshot["clob_error"]
    return view
