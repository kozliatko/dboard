"""Tests for the optional Google OAuth login gate (app/auth.py)."""
import time

import jwt
import pytest
from fastapi.testclient import TestClient


# ── enabled() / allowlist parsing ─────────────────────────────────────────────

class TestEnabled:
    def test_disabled_when_nothing_set(self, monkeypatch):
        import auth
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_ALLOWED_EMAILS", raising=False)
        assert auth.enabled() is False

    def test_disabled_when_only_some_set(self, monkeypatch):
        import auth
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_EMAILS", "a@b.com")
        assert auth.enabled() is False

    def test_enabled_when_all_three_set(self, monkeypatch):
        import auth
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
        monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_EMAILS", "a@b.com")
        assert auth.enabled() is True

    def test_allowed_emails_case_insensitive_and_trimmed(self, monkeypatch):
        import auth
        monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_EMAILS", " Alice@Example.com , bob@example.com ,")
        assert auth._allowed_emails() == {"alice@example.com", "bob@example.com"}


# ── session cookie round trip + live allowlist re-check ──────────────────────

class TestSessionCookie:
    def test_round_trip(self, monkeypatch):
        import auth
        monkeypatch.setenv("GOOGLE_OAUTH_SESSION_SECRET", "test-secret")
        monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_EMAILS", "alice@example.com")
        token = auth._make_session_cookie("alice@example.com")
        assert auth._email_from_session_cookie(token) == "alice@example.com"

    def test_revoked_immediately_when_removed_from_allowlist(self, monkeypatch):
        import auth
        monkeypatch.setenv("GOOGLE_OAUTH_SESSION_SECRET", "test-secret")
        monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_EMAILS", "alice@example.com")
        token = auth._make_session_cookie("alice@example.com")
        assert auth._email_from_session_cookie(token) == "alice@example.com"

        # Admin edits the allowlist — no new token is issued, but the *same*
        # still-unexpired cookie must stop working on the very next request.
        monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_EMAILS", "bob@example.com")
        assert auth._email_from_session_cookie(token) is None

    def test_expired_token_rejected(self, monkeypatch):
        import auth
        monkeypatch.setenv("GOOGLE_OAUTH_SESSION_SECRET", "test-secret")
        monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_EMAILS", "alice@example.com")
        now = int(time.time())
        expired = jwt.encode(
            {"email": "alice@example.com", "iat": now - 100, "exp": now - 1},
            "test-secret", algorithm="HS256",
        )
        assert auth._email_from_session_cookie(expired) is None

    def test_garbage_token_rejected(self, monkeypatch):
        import auth
        monkeypatch.setenv("GOOGLE_OAUTH_SESSION_SECRET", "test-secret")
        monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_EMAILS", "alice@example.com")
        assert auth._email_from_session_cookie("not-a-jwt") is None

    def test_wrong_signing_key_rejected(self, monkeypatch):
        import auth
        monkeypatch.setenv("GOOGLE_OAUTH_SESSION_SECRET", "test-secret")
        monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_EMAILS", "alice@example.com")
        forged = jwt.encode(
            {"email": "alice@example.com", "exp": int(time.time()) + 3600},
            "wrong-secret", algorithm="HS256",
        )
        assert auth._email_from_session_cookie(forged) is None


# ── _safe_next (open-redirect guard) ──────────────────────────────────────────

class TestSafeNext:
    @pytest.mark.parametrize("path", ["/", "/api/tokens", "/some/deep/path"])
    def test_local_paths_pass_through(self, path):
        import auth
        assert auth._safe_next(path) == path

    @pytest.mark.parametrize("path", [
        "//evil.com", "http://evil.com", "https://evil.com/x", "javascript:alert(1)",
    ])
    def test_non_local_paths_fall_back_to_root(self, path):
        import auth
        assert auth._safe_next(path) == "/"


# ── guard() ────────────────────────────────────────────────────────────────────

class TestGuard:
    def _req(self, path, cookies=None):
        from starlette.requests import Request
        scope = {
            "type": "http", "method": "GET", "path": path,
            "headers": [(b"cookie", "; ".join(f"{k}={v}" for k, v in (cookies or {}).items()).encode())],
            "query_string": b"", "scheme": "http", "server": ("testserver", 80),
        }
        return Request(scope)

    def test_passthrough_when_disabled(self, monkeypatch):
        import auth
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_ALLOWED_EMAILS", raising=False)
        assert auth.guard(self._req("/")) is None
        assert auth.guard(self._req("/api/tokens")) is None

    def _enable(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
        monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_EMAILS", "alice@example.com")
        monkeypatch.setenv("GOOGLE_OAUTH_SESSION_SECRET", "test-secret")

    def test_exempt_paths_pass_through_unauthenticated(self, monkeypatch):
        import auth
        self._enable(monkeypatch)
        assert auth.guard(self._req("/healthz")) is None
        assert auth.guard(self._req("/auth/login")) is None
        assert auth.guard(self._req("/static/app.js")) is None

    def test_root_redirects_to_login_when_unauthenticated(self, monkeypatch):
        import auth
        self._enable(monkeypatch)
        resp = auth.guard(self._req("/"))
        assert resp is not None
        assert resp.status_code in (302, 307)
        assert "/auth/login" in resp.headers["location"]

    def test_api_path_returns_401_json_when_unauthenticated(self, monkeypatch):
        import auth
        self._enable(monkeypatch)
        resp = auth.guard(self._req("/api/tokens"))
        assert resp is not None
        assert resp.status_code == 401

    def test_valid_session_cookie_passes_through(self, monkeypatch):
        import auth
        self._enable(monkeypatch)
        token = auth._make_session_cookie("alice@example.com")
        resp = auth.guard(self._req("/api/tokens", cookies={auth.SESSION_COOKIE: token}))
        assert resp is None


# ── HTTP-level flow via TestClient ────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(main, "_db_conn", None)
    monkeypatch.setattr(main, "_dock", lambda: None)
    with TestClient(main.app, raise_server_exceptions=True) as c:
        yield c
    if main._db_conn:
        main._db_conn.close()
        monkeypatch.setattr(main, "_db_conn", None)


class TestHttpFlowDisabled:
    def test_login_route_404_when_disabled(self, client, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_ALLOWED_EMAILS", raising=False)
        r = client.get("/auth/login", follow_redirects=False)
        assert r.status_code == 404

    def test_root_unaffected_when_disabled(self, client, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_ALLOWED_EMAILS", raising=False)
        assert client.get("/").status_code == 200

    def test_healthz_always_200(self, client, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_ALLOWED_EMAILS", raising=False)
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def _enable_oauth(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_EMAILS", "alice@example.com")
    monkeypatch.setenv("GOOGLE_OAUTH_SESSION_SECRET", "test-secret")


class TestHttpFlowEnabled:
    def test_root_redirects_when_unauthenticated(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        r = client.get("/", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert "/auth/login" in r.headers["location"]

    def test_api_returns_401_when_unauthenticated(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        r = client.get("/api/tokens")
        assert r.status_code == 401

    def test_healthz_still_200_when_enabled(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        assert client.get("/healthz").status_code == 200

    def test_login_redirects_to_google_with_state_cookie(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        r = client.get("/auth/login", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=test-client-id" in r.headers["location"]
        import auth
        assert auth._STATE_COOKIE in r.cookies

    def test_root_accessible_with_valid_session_cookie(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        import auth
        token = auth._make_session_cookie("alice@example.com")
        client.cookies.set(auth.SESSION_COOKIE, token)
        r = client.get("/")
        assert r.status_code == 200
        assert "alice@example.com" in r.text

    def test_api_accessible_with_valid_session_cookie(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        import auth
        token = auth._make_session_cookie("alice@example.com")
        client.cookies.set(auth.SESSION_COOKIE, token)
        r = client.get("/api/tokens")
        assert r.status_code == 200

    def test_callback_without_state_cookie_is_denied(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        r = client.get("/auth/callback?code=abc&state=xyz")
        assert r.status_code == 403
        assert "Pristup zamietnuty" in r.text

    def test_callback_rejects_mismatched_state(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        login = client.get("/auth/login", follow_redirects=False)
        r = client.get("/auth/callback?code=abc&state=not-the-real-state")
        assert r.status_code == 403

    def test_callback_google_error_param_is_denied(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        client.get("/auth/login", follow_redirects=False)
        r = client.get("/auth/callback?error=access_denied")
        assert r.status_code == 403

    def test_callback_success_grants_session_for_allowlisted_email(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        import auth

        login = client.get("/auth/login", follow_redirects=False)
        state = login.cookies[auth._STATE_COOKIE].split(".")[0]

        class FakeTokenResponse:
            def raise_for_status(self): pass
            def json(self): return {"id_token": "fake-id-token"}

        monkeypatch.setattr(auth._http, "post", lambda *a, **k: FakeTokenResponse())
        monkeypatch.setattr(auth, "_verify_id_token", lambda tok: {
            "email": "Alice@Example.com", "email_verified": True,
        })

        r = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert r.headers["location"] == "/"
        assert auth.SESSION_COOKIE in r.cookies

        client.cookies.set(auth.SESSION_COOKIE, r.cookies[auth.SESSION_COOKIE])
        assert client.get("/api/tokens").status_code == 200

    def test_callback_denies_email_not_on_allowlist(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        import auth

        login = client.get("/auth/login", follow_redirects=False)
        state = login.cookies[auth._STATE_COOKIE].split(".")[0]

        class FakeTokenResponse:
            def raise_for_status(self): pass
            def json(self): return {"id_token": "fake-id-token"}

        monkeypatch.setattr(auth._http, "post", lambda *a, **k: FakeTokenResponse())
        monkeypatch.setattr(auth, "_verify_id_token", lambda tok: {
            "email": "mallory@example.com", "email_verified": True,
        })

        r = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
        assert r.status_code == 403
        assert auth.SESSION_COOKIE not in r.cookies

    def test_callback_denies_unverified_email(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        import auth

        login = client.get("/auth/login", follow_redirects=False)
        state = login.cookies[auth._STATE_COOKIE].split(".")[0]

        class FakeTokenResponse:
            def raise_for_status(self): pass
            def json(self): return {"id_token": "fake-id-token"}

        monkeypatch.setattr(auth._http, "post", lambda *a, **k: FakeTokenResponse())
        monkeypatch.setattr(auth, "_verify_id_token", lambda tok: {
            "email": "alice@example.com", "email_verified": False,
        })

        r = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
        assert r.status_code == 403

    def test_logout_clears_session_cookie(self, client, monkeypatch):
        _enable_oauth(monkeypatch)
        import auth
        token = auth._make_session_cookie("alice@example.com")
        client.cookies.set(auth.SESSION_COOKIE, token)

        r = client.get("/auth/logout", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert r.headers["location"] == "/auth/login"

        client.cookies.set(auth.SESSION_COOKIE, "")
        r2 = client.get("/", follow_redirects=False)
        assert r2.status_code in (302, 307)
