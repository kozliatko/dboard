"""Optional Google OAuth login gate.

Disabled unless GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and
GOOGLE_OAUTH_ALLOWED_EMAILS are all set — with none of them set, every route
behaves exactly as before and dboard relies on whatever the reverse proxy
already provides (e.g. Caddy Basic Auth).

When enabled, every route except /healthz, /auth/* and /static/* requires a
session cookie backed by a real Google sign-in AND membership in the
server-side email allowlist. There is no "any Google account" mode — the
allowlist is re-checked on every request (not just at login), so removing an
email from GOOGLE_OAUTH_ALLOWED_EMAILS revokes that person's access
immediately, without waiting for their session to expire.
"""
import logging
import os
import secrets
import time
from urllib.parse import quote, unquote, urlencode

import httpx2 as httpx
import jwt
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

log = logging.getLogger("dboard.auth")

_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]

SESSION_COOKIE = "dboard_session"
_STATE_COOKIE = "dboard_oauth_state"
_SESSION_TTL = 14 * 86400  # 14 days
_STATE_TTL = 600  # 10 minutes — just long enough to complete the Google redirect

# Paths the auth gate never blocks: health checks, the login/callback/logout
# flow itself (it would be unreachable otherwise), and static assets (not
# sensitive, and the login page has no need to load them).
EXEMPT_PATHS = {"/healthz"}
EXEMPT_PREFIXES = ("/auth/", "/static/")

_http = httpx.Client(timeout=10.0)
_jwks_client = None  # lazily created; see _get_jwks_client()


def _client_id() -> str:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()


def _allowed_emails() -> set[str]:
    raw = os.environ.get("GOOGLE_OAUTH_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def enabled() -> bool:
    return bool(_client_id() and _client_secret() and _allowed_emails())


_generated_secret: dict = {}


def _session_secret() -> str:
    """HMAC key for signing session cookies.

    Uses GOOGLE_OAUTH_SESSION_SECRET if set, so sessions survive a restart.
    Otherwise a random key is generated once per process — every open
    session is signed out the next time the container restarts. Fine for a
    small-team dashboard, but set the env var explicitly to avoid surprise
    logouts.
    """
    env = os.environ.get("GOOGLE_OAUTH_SESSION_SECRET", "").strip()
    if env:
        return env
    if "key" not in _generated_secret:
        _generated_secret["key"] = secrets.token_urlsafe(32)
        log.warning(
            "GOOGLE_OAUTH_SESSION_SECRET not set — generated a random session "
            "key for this process. Every signed-in session will be logged out "
            "on the next restart. Set GOOGLE_OAUTH_SESSION_SECRET to avoid this."
        )
    return _generated_secret["key"]


def _get_jwks_client():
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(_JWKS_URL)
    return _jwks_client


def _verify_id_token(id_token: str) -> dict:
    signing_key = _get_jwks_client().get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=_client_id(),
        issuer=_ISSUERS,
    )


def _make_session_cookie(email: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"email": email, "iat": now, "exp": now + _SESSION_TTL},
        _session_secret(),
        algorithm="HS256",
    )


def _email_from_session_cookie(token: str) -> str | None:
    """Decode a session cookie and re-check it against the *current* allowlist.

    Re-checking here (not just at login) is what makes removing someone from
    GOOGLE_OAUTH_ALLOWED_EMAILS take effect immediately, instead of only once
    their 14-day session happens to expire.
    """
    try:
        claims = jwt.decode(token, _session_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    email = (claims.get("email") or "").lower()
    return email if email in _allowed_emails() else None


def current_email(request: Request) -> str | None:
    """The signed-in, still-allowlisted email for this request, or None."""
    if not enabled():
        return None
    token = request.cookies.get(SESSION_COOKIE)
    return _email_from_session_cookie(token) if token else None


def _safe_next(path: str) -> str:
    """Only ever redirect back to a local path — never an open redirect."""
    if path.startswith("/") and not path.startswith("//") and "://" not in path:
        return path
    return "/"


def _is_secure(request: Request) -> bool:
    return request.url.scheme == "https"


def guard(request: Request) -> Response | None:
    """Called on every request. Returns a Response to short-circuit it, or
    None to let the request through to its normal handler."""
    if not enabled():
        return None

    path = request.url.path
    if path in EXEMPT_PATHS or any(path.startswith(p) for p in EXEMPT_PREFIXES):
        return None

    if current_email(request) is not None:
        return None

    if path == "/":
        return RedirectResponse(f"/auth/login?next={_safe_next(path)}")
    return JSONResponse({"error": "unauthorized"}, status_code=401)


router = APIRouter()


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/auth/login")
async def login(request: Request, next: str = "/"):
    if not enabled():
        return JSONResponse({"error": "Google OAuth is not configured"}, status_code=404)

    state = secrets.token_urlsafe(24)
    redirect_uri = str(request.url_for("oauth_callback"))
    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "prompt": "select_account",
    }
    resp = RedirectResponse(f"{_AUTHORIZE_URL}?{urlencode(params)}")
    # token_urlsafe() output is [A-Za-z0-9_-] only, so "." safely separates it
    # from the percent-encoded next-path with no ambiguity. Plain "/" (or any
    # separator containing one) makes Python's http.cookies wrap the value in
    # literal DQUOTEs, which real browsers send back as part of the value —
    # breaking the state comparison below.
    resp.set_cookie(
        _STATE_COOKIE,
        f"{state}.{quote(_safe_next(next), safe='')}",
        max_age=_STATE_TTL,
        httponly=True,
        secure=_is_secure(request),
        samesite="lax",
        path="/auth",
    )
    return resp


def _denied(message: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=\"sk\"><head><meta charset=\"utf-8\">"
        "<title>dboard — pristup zamietnuty</title><style>"
        "body{background:#0b0e14;color:#d1d5db;font-family:system-ui,sans-serif;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}"
        ".card{text-align:center;max-width:26rem;padding:2rem}"
        "h1{color:#f87171;font-size:1.1rem;margin:0 0 .75rem}"
        "a{color:#818cf8}</style></head><body>"
        f"<div class=\"card\"><h1>Pristup zamietnuty</h1><p>{message}</p>"
        "<p><a href=\"/auth/login\">Skusit znova</a></p></div></body></html>",
        status_code=403,
    )


@router.get("/auth/callback", name="oauth_callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if not enabled():
        return JSONResponse({"error": "Google OAuth is not configured"}, status_code=404)

    cookie_val = request.cookies.get(_STATE_COOKIE, "")
    expected_state, _, next_encoded = cookie_val.partition(".")
    next_path = unquote(next_encoded)

    if error or not code or not state or not secrets.compare_digest(state, expected_state):
        log.warning("OAuth callback rejected: error=%r state_ok=%s", error, bool(expected_state))
        return _denied("Prihlasenie zlyhalo alebo bolo zrusene.")

    try:
        token_resp = _http.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "redirect_uri": str(request.url_for("oauth_callback")),
                "grant_type": "authorization_code",
            },
        )
        token_resp.raise_for_status()
        id_token = token_resp.json()["id_token"]
        claims = _verify_id_token(id_token)
    except Exception as e:
        log.warning("OAuth token exchange/verification failed: %s", e)
        return _denied("Prihlasenie zlyhalo.")

    email = (claims.get("email") or "").strip().lower()
    if not claims.get("email_verified") or email not in _allowed_emails():
        log.warning("OAuth login denied for %s (not on the allowlist)", email or "<unknown>")
        return _denied("Tento Google ucet nema pristup k dboard.")

    log.info("OAuth login granted for %s", email)
    resp = RedirectResponse(_safe_next(next_path))
    resp.delete_cookie(_STATE_COOKIE, path="/auth")
    resp.set_cookie(
        SESSION_COOKIE,
        _make_session_cookie(email),
        max_age=_SESSION_TTL,
        httponly=True,
        secure=_is_secure(request),
        samesite="lax",
        path="/",
    )
    return resp


@router.get("/auth/logout")
async def logout():
    resp = RedirectResponse("/auth/login")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
