"""Tests for API token validator functions in main.py."""
import json
import pytest
from unittest.mock import patch

from main import (
    _check_anthropic,
    _check_github,
    _check_gemini,
    _check_openai,
    _check_deepseek,
    _check_tavily,
    _check_gitlab,
)

FAKE_KEY = "sk-test-key-1234567890"


def _ok(body, headers=None):
    """Helper: simulate a successful HTTP response."""
    return (200, json.dumps(body), headers or {})


def _err(code=401):
    """Helper: simulate an error HTTP response."""
    return (code, json.dumps({"error": "unauthorized"}), {})


def _net_err():
    """Helper: simulate a network-level error."""
    return (None, "Connection refused", {})


# ── Anthropic ─────────────────────────────────────────────────────────────────

class TestCheckAnthropic:
    def test_valid(self):
        body = {"data": [{"id": "claude-opus-4-8"}, {"id": "claude-sonnet-4-6"}]}
        hdrs = {"anthropic-ratelimit-requests-limit": "1000",
                "anthropic-ratelimit-requests-remaining": "950"}
        with patch("main._http_get", return_value=(200, json.dumps(body), hdrs)):
            r = _check_anthropic(FAKE_KEY)
        assert r["valid"] is True
        assert r["detail"] == "2 models"
        labels = [e["label"] for e in r["extras"]]
        assert "Models" in labels
        assert "Rate limit" in labels

    def test_invalid_key(self):
        with patch("main._http_get", return_value=_err(401)):
            r = _check_anthropic(FAKE_KEY)
        assert r["valid"] is False
        assert "401" in r["detail"]

    def test_network_error(self):
        with patch("main._http_get", return_value=_net_err()):
            r = _check_anthropic(FAKE_KEY)
        assert r["valid"] is False


# ── GitHub ────────────────────────────────────────────────────────────────────

class TestCheckGitHub:
    USER = {"login": "octocat", "name": "The Octocat",
            "public_repos": 8, "total_private_repos": 2}
    RL   = {"rate": {"limit": 5000, "remaining": 4999}}

    def test_valid(self):
        hdrs = {"x-oauth-scopes": "repo, user"}
        with patch("main._http_get", side_effect=[
            (200, json.dumps(self.USER), hdrs),
            (200, json.dumps(self.RL), {}),
        ]):
            r = _check_github(FAKE_KEY)
        assert r["valid"] is True
        assert "@octocat" in r["detail"]
        labels = [e["label"] for e in r["extras"]]
        assert "Scopes" in labels
        assert "Rate limit" in labels

    def test_invalid_token(self):
        with patch("main._http_get", return_value=_err(401)):
            r = _check_github(FAKE_KEY)
        assert r["valid"] is False

    def test_name_same_as_login_not_duplicated(self):
        user = {**self.USER, "name": "octocat"}  # name == login
        with patch("main._http_get", side_effect=[
            (200, json.dumps(user), {}),
            (200, json.dumps(self.RL), {}),
        ]):
            r = _check_github(FAKE_KEY)
        assert r["detail"] == "@octocat"  # no parenthetical duplicate


# ── Gemini ────────────────────────────────────────────────────────────────────

class TestCheckGemini:
    def test_valid(self):
        body = {"models": [
            {"name": "models/gemini-2.0-flash"},
            {"name": "models/gemini-1.5-pro"},
            {"name": "models/embedding-001"},
        ]}
        with patch("main._http_get", return_value=_ok(body)):
            r = _check_gemini(FAKE_KEY)
        assert r["valid"] is True
        assert "3 models" in r["detail"]
        gemini_extra = next(e for e in r["extras"] if e["label"] == "Gemini models")
        assert "gemini-2.0-flash" in gemini_extra["value"]

    def test_invalid_key(self):
        with patch("main._http_get", return_value=_err(403)):
            r = _check_gemini(FAKE_KEY)
        assert r["valid"] is False


# ── OpenAI ────────────────────────────────────────────────────────────────────

class TestCheckOpenAI:
    def test_valid(self):
        body = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4-turbo"}, {"id": "whisper-1"}]}
        with patch("main._http_get", return_value=_ok(body)):
            r = _check_openai(FAKE_KEY)
        assert r["valid"] is True
        gpt_extra = next(e for e in r["extras"] if e["label"] == "GPT models")
        assert "gpt-4o" in gpt_extra["value"]

    def test_invalid_key(self):
        with patch("main._http_get", return_value=_err(401)):
            r = _check_openai(FAKE_KEY)
        assert r["valid"] is False


# ── DeepSeek ──────────────────────────────────────────────────────────────────

class TestCheckDeepSeek:
    def test_valid_with_balance(self):
        models = {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}
        balance = {"balance_infos": [{"total_balance": "9.50", "currency": "USD"}]}
        with patch("main._http_get", side_effect=[
            _ok(models),
            _ok(balance),
        ]):
            r = _check_deepseek(FAKE_KEY)
        assert r["valid"] is True
        bal_extra = next(e for e in r["extras"] if e["label"] == "Balance")
        assert "9.50" in bal_extra["value"]

    def test_valid_balance_endpoint_fails_gracefully(self):
        models = {"data": [{"id": "deepseek-chat"}]}
        with patch("main._http_get", side_effect=[
            _ok(models),
            _err(403),
        ]):
            r = _check_deepseek(FAKE_KEY)
        assert r["valid"] is True  # still valid, balance is optional

    def test_invalid_key(self):
        with patch("main._http_get", return_value=_err(401)):
            r = _check_deepseek(FAKE_KEY)
        assert r["valid"] is False


# ── Tavily ────────────────────────────────────────────────────────────────────

class TestCheckTavily:
    def test_valid(self):
        import urllib.error
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({"response_time": 0.42}).encode()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            r = _check_tavily(FAKE_KEY)
        assert r["valid"] is True
        rt_extra = next(e for e in r["extras"] if e["label"] == "Response time")
        assert "0.42" in rt_extra["value"]

    def test_invalid_key(self):
        import urllib.error
        err = urllib.error.HTTPError(url="", code=401, msg="Unauthorized", hdrs={}, fp=None)
        with patch("urllib.request.urlopen", side_effect=err):
            r = _check_tavily(FAKE_KEY)
        assert r["valid"] is False
        assert "401" in r["detail"]


# ── GitLab ────────────────────────────────────────────────────────────────────

class TestCheckGitLab:
    TOKEN_INFO = {
        "name": "my-token",
        "scopes": ["api", "read_repository"],
        "expires_at": "2027-01-01",
        "last_used_at": "2026-06-22T08:00:00.000Z",
    }
    USER_INFO = {"username": "vajda", "name": "Jan Vajda"}

    def test_valid_with_token_endpoint(self):
        with patch("main._http_get", side_effect=[
            _ok(self.TOKEN_INFO),          # /personal_access_tokens/self
            _ok(self.USER_INFO),           # /user
        ]), patch.dict("os.environ", {"GITLAB_HOST": "git.example.com"}):
            r = _check_gitlab(FAKE_KEY)
        assert r["valid"] is True
        assert "@vajda" in r["detail"]
        assert "my-token" in r["detail"]
        labels = [e["label"] for e in r["extras"]]
        assert "Scopes" in labels
        assert "Expires" in labels
        assert "Host" in labels

    def test_valid_fallback_to_user_endpoint(self):
        # /personal_access_tokens/self returns 404, fallback to /user
        with patch("main._http_get", side_effect=[
            _err(404),                     # /personal_access_tokens/self
            _ok(self.USER_INFO),           # /user
        ]), patch.dict("os.environ", {"GITLAB_HOST": "git.example.com"}):
            r = _check_gitlab(FAKE_KEY)
        assert r["valid"] is True
        assert "@vajda" in r["detail"]

    def test_invalid_token(self):
        with patch("main._http_get", side_effect=[
            _err(401),
            _err(401),
        ]), patch.dict("os.environ", {"GITLAB_HOST": "git.example.com"}):
            r = _check_gitlab(FAKE_KEY)
        assert r["valid"] is False
