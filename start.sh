#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PORT:-8010}"
exec uvicorn backend.main:app --host 0.0.0.0 --port "$PORT"
