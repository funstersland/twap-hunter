"""Production entrypoint — reads PORT from the environment (Railway/Heroku)."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8010"))
    print(f"Twap Hunter starting on 0.0.0.0:{port}", flush=True)
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
