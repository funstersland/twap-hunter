"""Production entrypoint — run with: python -m backend"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _project_root() -> Path:
    pkg = Path(__file__).resolve().parent
    if pkg.name == "backend":
        return pkg.parent
    return pkg


def main() -> None:
    root = _project_root()
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    port = int(os.environ.get("PORT", "8010"))
    print(f"Twap Hunter starting on 0.0.0.0:{port} (root={root})", flush=True)

    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL startup error: {exc}", flush=True)
        traceback.print_exc()
        sys.exit(1)
