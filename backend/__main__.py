"""Production entrypoint — run with: python3 -m backend"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


def _setup_path() -> None:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def main() -> None:
    _setup_path()
    port = int(os.environ.get("PORT", "8010"))
    print(f"Twap Hunter starting on 0.0.0.0:{port}", flush=True)
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
