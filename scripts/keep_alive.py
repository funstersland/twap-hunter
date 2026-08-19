"""Keep Twap Hunter uvicorn alive: restart on crash, restart if health-check fails.

Runs forever on this machine (survives the chat). Prevents Windows sleep while running.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
HOST = "127.0.0.1"
PORT = 8010
HEALTH = f"http://{HOST}:{PORT}/api/bots"
LOG = ROOT / "data" / "watchdog.log"
PID_FILE = ROOT / "data" / "watchdog.pid"
RESTART_DELAY_S = 3
HEALTH_EVERY_S = 20
HEALTH_TIMEOUT_S = 12
HEALTH_FAILS_BEFORE_RESTART = 3

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def prevent_sleep() -> None:
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log("sleep inhibited (system will stay awake)")
    except Exception as exc:
        log(f"could not inhibit sleep: {exc}")


def kill_port(port: int) -> None:
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], text=True, errors="ignore")
    except Exception:
        return
    pids = set()
    for line in out.splitlines():
        if f":{port} " not in line or "LISTENING" not in line:
            continue
        parts = line.split()
        if parts:
            try:
                pids.add(int(parts[-1]))
            except ValueError:
                pass
    me = os.getpid()
    for pid in pids:
        if pid in (0, me):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            log(f"stopped pid {pid} on port {port}")
        except OSError:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True, check=False)
            except Exception:
                pass
    if pids:
        time.sleep(1.2)


def healthy() -> bool:
    try:
        import urllib.request
        req = urllib.request.Request(HEALTH, method="GET")
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT_S) as resp:
            return 200 <= getattr(resp, "status", 200) < 500
    except Exception:
        return False


def spawn() -> subprocess.Popen:
    kill_port(PORT)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    log(f"starting uvicorn on {HOST}:{PORT}")
    return subprocess.Popen(
        [str(PYTHON), "-m", "uvicorn", "backend.main:app",
         "--host", HOST, "--port", str(PORT)],
        cwd=str(ROOT),
        env=env,
    )


def main() -> int:
    if not PYTHON.exists():
        log(f"missing venv python: {PYTHON}")
        return 1
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    prevent_sleep()
    log("watchdog started — will restart the server if it dies")
    child: subprocess.Popen | None = None
    fails = 0
    try:
        while True:
            if child is None or child.poll() is not None:
                code = None if child is None else child.returncode
                if child is not None:
                    log(f"uvicorn exited code={code} — restarting in {RESTART_DELAY_S}s")
                    time.sleep(RESTART_DELAY_S)
                child = spawn()
                fails = 0
                continue

            time.sleep(HEALTH_EVERY_S)
            prevent_sleep()
            if healthy():
                fails = 0
                continue
            fails += 1
            log(f"health check failed ({fails}/{HEALTH_FAILS_BEFORE_RESTART})")
            if fails < HEALTH_FAILS_BEFORE_RESTART:
                continue
            log("server unresponsive — killing and restarting")
            try:
                child.terminate()
                child.wait(timeout=8)
            except Exception:
                try:
                    child.kill()
                except Exception:
                    pass
            kill_port(PORT)
            child = None
            fails = 0
    except KeyboardInterrupt:
        log("watchdog stopped")
        if child and child.poll() is None:
            child.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
