#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8010}"
exec uvicorn backend.main:app --host 0.0.0.0 --port "$PORT"
