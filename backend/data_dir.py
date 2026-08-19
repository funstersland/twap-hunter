"""Writable data directory — honors DATA_DIR, falls back to /tmp on Railway."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    raw = os.environ.get("DATA_DIR", "").strip()
    primary = Path(raw) if raw else _ROOT / "data"
    for candidate in (primary, Path("/tmp/twaphunter-data")):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            if candidate != primary:
                logger.warning("using fallback data dir %s", candidate)
            return candidate
        except OSError as exc:
            logger.warning("data dir unavailable %s: %s", candidate, exc)
    raise RuntimeError("no writable data directory found")
