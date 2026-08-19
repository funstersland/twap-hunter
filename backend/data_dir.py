"""Writable data directory — honors DATA_DIR, with an ephemeral fallback.

On Railway (and any container) the filesystem is EPHEMERAL: anything not
on a mounted Volume is wiped on every redeploy/restart. To persist the
SQLite DB and encrypted account keys:

  1. Add a Railway Volume and mount it at, e.g., ``/data``.
  2. Set the ``DATA_DIR`` variable to that same path (``/data``).

The resolution is cached on first use so every module shares one
directory, and its status — whether it is an actual persistent volume or
an ephemeral fallback — is exposed via :func:`data_dir_status` (surfaced
on ``/health``) so persistence can be verified at a glance.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_FALLBACK = Path("/tmp/twaphunter-data")

_resolved: Path | None = None
_status: dict | None = None


def _on_separate_mount(path: Path) -> bool:
    """True if ``path`` lives on a different device than the FS root.

    A Railway Volume is a separate mount from the ephemeral container
    root, so a differing ``st_dev`` is a reliable "this is a real
    persistent volume" signal. On Windows (local dev) this simply
    returns False, which is fine — persistence only matters in the
    container.
    """
    try:
        root = os.stat(os.path.abspath(os.sep)).st_dev
        return os.stat(path).st_dev != root
    except OSError:
        return False


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.warning("data dir unavailable %s: %s", path, exc)
        return False


def _resolve() -> tuple[Path, dict]:
    raw = os.environ.get("DATA_DIR", "").strip()
    requested = Path(raw) if raw else _ROOT / "data"

    if _writable(requested):
        chosen, fell_back = requested, False
    elif _writable(_FALLBACK):
        chosen, fell_back = _FALLBACK, True
    else:
        raise RuntimeError("no writable data directory found")

    # Persistence only matters in a deploy environment (ephemeral FS).
    # Local dev writes to ./data and never redeploys, so stay quiet there.
    on_deploy = os.environ.get("RAILWAY_ENVIRONMENT") is not None
    on_volume = _on_separate_mount(chosen)
    persistent = bool(raw) and not fell_back and on_volume
    warning = None
    if fell_back:
        warning = (f"DATA_DIR '{raw or '(unset)'}' unwritable — using "
                   f"EPHEMERAL {chosen}; data is LOST on restart.")
        logger.warning(warning)
    elif on_deploy and not raw:
        warning = ("DATA_DIR not set — using ephemeral container storage; "
                   "data is LOST on redeploy. Set DATA_DIR to a mounted "
                   "Railway Volume path (e.g. /data) to persist.")
        logger.warning(warning)
    elif on_deploy and not on_volume:
        warning = (f"DATA_DIR={raw} is set but is NOT a mounted volume "
                   f"(same device as the container root) — data will be "
                   f"LOST on redeploy. Attach a Railway Volume at {raw}.")
        logger.warning(warning)
    elif persistent:
        logger.info("data dir: %s (persistent volume)", chosen)
    else:
        logger.info("data dir: %s", chosen)

    return chosen, {
        "path": str(chosen),
        "data_dir_env": raw or None,
        "persistent": persistent,
        "fallback": fell_back,
        "warning": warning,
    }


def data_dir() -> Path:
    """The resolved writable data directory (cached after first call)."""
    global _resolved, _status
    if _resolved is None:
        _resolved, _status = _resolve()
    return _resolved


def data_dir_status() -> dict:
    """Resolution details, for readiness checks / ``/health``."""
    data_dir()
    return dict(_status or {})
