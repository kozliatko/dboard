"""Tests for API token validator functions in main.py."""
import base64
import json
import pytest
from unittest.mock import patch, MagicMock

from main import (
    _check_anthropic,
    _check_github,
    _check_gemini,
    _check_openai,
    _check_azure_openai,
    _check_deepseek,
    _check_mistral,
    _check_tavily,
    _check_gitlab,
    _check_huggingface,
    _check_groq,
    _check_gcp,
    _check_token_sync,
    _expand_token_defs,
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


# ── Azure OpenAI ──────────────────────────────────────────────────────────────

class TestCheckAzureOpenAI:
    def test_missing_endpoint_rejected(self, monkeypatch):
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        r = _check_azure_openai(FAKE_KEY)
        assert r["valid"] is False
        assert "AZURE_OPENAI_ENDPOINT" in r["detail"]

    def test_valid(self, monkeypatch):
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://myresource.openai.azure.com")
        body = {"data": [{"id": "gpt-4-deploy"}, {"id": "gpt-35-deploy"}]}
        with patch("main._http_get", return_value=_ok(body)) as http_get:
            r = _check_azure_openai(FAKE_KEY)
        assert r["valid"] is True
        deploy_extra = next(e for e in r["extras"] if e["label"] == "Deployments")
        assert "gpt-4-deploy" in deploy_extra["value"]
        called_url = http_get.call_args.args[0]
        assert called_url.startswith("https://myresource.openai.azure.com/openai/deployments")
        assert "api-version=" in called_url
        assert http_get.call_args.kwargs["headers"] == {"api-key": FAKE_KEY}

    def test_endpoint_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://myresource.openai.azure.com/")
        with patch("main._http_get", return_value=_ok({"data": []})) as http_get:
            _check_azure_openai(FAKE_KEY)
        called_url = http_get.call_args.args[0]
        assert "azure.com//openai" not in called_url

    def test_invalid_key(self, monkeypatch):
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://myresource.openai.azure.com")
        with patch("main._http_get", return_value=_err(401)):
            r = _check_azure_openai(FAKE_KEY)
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


# ── Mistral ───────────────────────────────────────────────────────────────────

class TestCheckMistral:
    def test_valid(self):
        body = {"data": [{"id": "mistral-large-latest"}, {"id": "open-mistral-7b"}]}
        with patch("main._http_get", return_value=_ok(body)):
            r = _check_mistral(FAKE_KEY)
        assert r["valid"] is True
        models_extra = next(e for e in r["extras"] if e["label"] == "Models")
        assert "mistral-large-latest" in models_extra["value"]
        assert "open-mistral-7b" in models_extra["value"]

    def test_invalid_key(self):
        with patch("main._http_get", return_value=_err(401)):
            r = _check_mistral(FAKE_KEY)
        assert r["valid"] is False


# ── Tavily ────────────────────────────────────────────────────────────────────

class TestCheckTavily:
    def test_valid(self):
        with patch("main._http_post", return_value=_ok({"response_time": 0.42})):
            r = _check_tavily(FAKE_KEY)
        assert r["valid"] is True
        rt_extra = next(e for e in r["extras"] if e["label"] == "Response time")
        assert "0.42" in rt_extra["value"]

    def test_invalid_key(self):
        with patch("main._http_post", return_value=_err(401)):
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


# ── HuggingFace ───────────────────────────────────────────────────────────────

class TestCheckHuggingFace:
    WHOAMI = {
        "name": "myuser",
        "fullname": "My User",
        "isPro": False,
        "auth": {
            "accessToken": {
                "displayName": "my-token",
                "role": "write",
            }
        },
    }

    def test_valid_token(self):
        with patch("main._http_get", return_value=(200, json.dumps(self.WHOAMI), {})):
            r = _check_huggingface(FAKE_KEY)
        assert r["valid"] is True
        assert "@myuser" in r["detail"]
        labels = [e["label"] for e in r["extras"]]
        assert "Plan" in labels
        assert "User" in labels

    def test_pro_plan_shown(self):
        data = {**self.WHOAMI, "isPro": True}
        with patch("main._http_get", return_value=(200, json.dumps(data), {})):
            r = _check_huggingface(FAKE_KEY)
        plan_extra = next(e for e in r["extras"] if e["label"] == "Plan")
        assert plan_extra["value"] == "PRO"

    def test_fine_grained_permissions(self):
        data = json.loads(json.dumps(self.WHOAMI))
        data["auth"]["accessToken"]["role"] = "fineGrained"
        data["auth"]["accessToken"]["fineGrained"] = {
            "global": ["read"],
            "scoped": [{"permissions": ["inference.serverless.write"]}],
        }
        with patch("main._http_get", return_value=(200, json.dumps(data), {})):
            r = _check_huggingface(FAKE_KEY)
        assert r["valid"] is True
        perms_extra = next(e for e in r["extras"] if e["label"] == "Permissions")
        assert "read" in perms_extra["value"]

    def test_invalid_token(self):
        with patch("main._http_get", return_value=(401, "{}", {})):
            r = _check_huggingface(FAKE_KEY)
        assert r["valid"] is False

    def test_api_returns_error_field(self):
        data = {"error": "Token is expired or revoked"}
        with patch("main._http_get", return_value=(200, json.dumps(data), {})):
            r = _check_huggingface(FAKE_KEY)
        assert r["valid"] is False
        assert "expired" in r["detail"]


# ── Groq ──────────────────────────────────────────────────────────────────────

class TestCheckGroq:
    MODELS = {"data": [
        {"id": "llama-3.1-8b-instant"},
        {"id": "llama-3.1-70b-versatile"},
        {"id": "mixtral-8x7b-32768"},
    ]}

    def test_valid_token(self):
        with patch("main._http_get", return_value=(200, json.dumps(self.MODELS), {})):
            r = _check_groq(FAKE_KEY)
        assert r["valid"] is True
        assert "3 models" in r["detail"]

    def test_llama_models_listed_in_extras(self):
        with patch("main._http_get", return_value=(200, json.dumps(self.MODELS), {})):
            r = _check_groq(FAKE_KEY)
        llama_extra = next(e for e in r["extras"] if e["label"] == "Llama models")
        assert "llama" in llama_extra["value"].lower()

    def test_invalid_key(self):
        with patch("main._http_get", return_value=(401, "{}", {})):
            r = _check_groq(FAKE_KEY)
        assert r["valid"] is False
        assert "401" in r["detail"]


# ── GCP (early rejection paths) ───────────────────────────────────────────────

class TestCheckGCPEarlyRejections:
    def test_unparseable_input_rejected(self):
        r = _check_gcp("this is not json and not valid base64!!!")
        assert r["valid"] is False
        assert "cannot parse" in r["detail"]

    def test_wrong_key_type_rejected(self):
        creds = json.dumps({"type": "user_account"})
        r = _check_gcp(creds)
        assert r["valid"] is False
        assert "user_account" in r["detail"]

    def test_missing_client_email_rejected(self):
        creds = json.dumps({
            "type": "service_account",
            "private_key": "some-key",
            "client_email": "",
        })
        r = _check_gcp(creds)
        assert r["valid"] is False
        assert "missing" in r["detail"]

    def test_missing_private_key_rejected(self):
        creds = json.dumps({
            "type": "service_account",
            "client_email": "svc@project.iam.gserviceaccount.com",
            "private_key": "",
        })
        r = _check_gcp(creds)
        assert r["valid"] is False
        assert "missing" in r["detail"]

    def test_base64_encoded_json_parsed(self):
        creds = {"type": "user_account"}
        b64 = base64.b64encode(json.dumps(creds).encode()).decode()
        r = _check_gcp(b64)
        assert r["valid"] is False
        assert "user_account" in r["detail"]  # parsed correctly, wrong type


class TestCheckGCPTokenExchange:
    CREDS = {
        "type": "service_account",
        "client_email": "svc@project.iam.gserviceaccount.com",
        "private_key": "fake-key",
        "project_id": "project",
        "private_key_id": "keyid123",
        # An attacker with write access to the creds JSON could point this
        # elsewhere — _check_gcp must ignore it and always use Google's URL.
        "token_uri": "https://evil.example.com/token",
    }

    def test_ignores_token_uri_from_creds(self):
        with patch("jwt.encode", return_value="signed.jwt.assertion"), \
             patch("main._http_post_form", return_value=_ok({"access_token": "tok123"})) as post_form, \
             patch("main._http_get", return_value=_ok({"projectId": "project", "lifecycleState": "ACTIVE"})):
            r = _check_gcp(json.dumps(self.CREDS))
        assert r["valid"] is True
        assert post_form.call_args.args[0] == "https://oauth2.googleapis.com/token"

    def test_token_exchange_failure(self):
        with patch("jwt.encode", return_value="signed.jwt.assertion"), \
             patch("main._http_post_form", return_value=_err(400)):
            r = _check_gcp(json.dumps(self.CREDS))
        assert r["valid"] is False


# ── _check_token_sync ─────────────────────────────────────────────────────────

class TestCheckTokenSync:
    def test_unconfigured_env_var(self):
        td = {
            "id": "test-svc",
            "name": "Test Service",
            "env_var": "TEST_SVC_KEY_UNUSED_XYZ_12345",
            "fn": lambda k, d: {},
        }
        result = _check_token_sync(td)
        assert result["configured"] is False
        assert result["valid"] is None
        assert result["key_hint"] is None
        assert result["extras"] == []

    def test_configured_and_valid(self, monkeypatch):
        monkeypatch.setenv("TEST_SVC_KEY_FOR_SYNC_ABC", "sk-this-is-a-test-key-abcd")
        td = {
            "id": "test-svc",
            "name": "Test Service",
            "env_var": "TEST_SVC_KEY_FOR_SYNC_ABC",
            "fn": lambda k, d: {"valid": True, "detail": "ok", "extras": []},
        }
        result = _check_token_sync(td)
        assert result["configured"] is True
        assert result["valid"] is True
        assert result["detail"] == "ok"
        assert result["key_hint"] is not None
        assert "···" in result["key_hint"]

    def test_function_exception_is_caught(self, monkeypatch):
        monkeypatch.setenv("TEST_SVC_KEY_FOR_SYNC_ABC", "sk-this-is-a-test-key-abcd")
        def boom(k, d): raise RuntimeError("network down")
        td = {
            "id": "test-svc",
            "name": "Test Service",
            "env_var": "TEST_SVC_KEY_FOR_SYNC_ABC",
            "fn": boom,
        }
        result = _check_token_sync(td)
        assert result["configured"] is True
        assert result["valid"] is False
        assert "network down" in result["error"]


# ── _expand_token_defs ────────────────────────────────────────────────────────

class TestExpandTokenDefs:
    def test_multi_instance_via_double_underscore(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY__work", "sk-work")
        monkeypatch.setenv("ANTHROPIC_API_KEY__personal", "sk-personal")
        result = _expand_token_defs()
        ids = [td["id"] for td in result]
        assert "anthropic__work" in ids
        assert "anthropic__personal" in ids

    def test_labeled_instance_name_includes_label(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN__ci", "ghp-token")
        result = _expand_token_defs()
        ci_td = next(td for td in result if td["id"] == "github__ci")
        assert "ci" in ci_td["name"].lower()
