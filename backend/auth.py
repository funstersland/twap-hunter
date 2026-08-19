"""Site-wide password gate (cookie session).

Set SITE_PASSWORD in the environment (Railway variables or local .env).
When unset, auth is disabled so local dev keeps working without setup.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

AUTH_COOKIE = "twap_session"
AUTH_MAX_AGE_S = 30 * 24 * 3600  # 30 days

_PUBLIC_PATHS = frozenset({"/login", "/api/login", "/health"})


def _is_public(path: str) -> bool:
    if path in _PUBLIC_PATHS or path == "/style.css":
        return True
    return path.rstrip("/") == "/health"


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "").strip()


def auth_enabled() -> bool:
    return bool(SITE_PASSWORD)


def _session_secret() -> str:
    return os.environ.get("SESSION_SECRET", SITE_PASSWORD)


def auth_token() -> str:
    secret = _session_secret()
    return hmac.new(secret.encode(), b"twap-hunter-auth-v1", hashlib.sha256).hexdigest()


def verify_session(cookie_val: str | None) -> bool:
    if not auth_enabled():
        return True
    return hmac.compare_digest(cookie_val or "", auth_token())


def check_password(password: str) -> bool:
    if not auth_enabled():
        return True
    return hmac.compare_digest(password, SITE_PASSWORD)


def set_auth_cookie(response: Response) -> None:
    response.set_cookie(
        AUTH_COOKIE,
        auth_token(),
        max_age=AUTH_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("RAILWAY_ENVIRONMENT") is not None
        or os.environ.get("FORCE_SECURE_COOKIES") == "1",
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(AUTH_COOKIE, path="/")


def login_redirect(request: Request) -> RedirectResponse:
    nxt = quote(request.url.path)
    if request.url.query:
        nxt = quote(f"{request.url.path}?{request.url.query}")
    return RedirectResponse(f"/login?next={nxt}", status_code=302)


class SiteAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth_enabled():
            return await call_next(request)

        path = request.url.path
        if _is_public(path):
            return await call_next(request)

        if verify_session(request.cookies.get(AUTH_COOKIE)):
            return await call_next(request)

        if path.startswith("/api/") or path == "/ws":
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        return login_redirect(request)
