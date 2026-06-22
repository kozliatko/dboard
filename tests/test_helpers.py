"""Tests for pure helper functions in main.py."""
import pytest
from main import _extract_domains, _fmt_uptime, _key_hint, _extras, _uptime


# ── _extract_domains ──────────────────────────────────────────────────────────

class TestExtractDomains:
    def test_single_caddy_label(self):
        assert _extract_domains({"caddy": "example.com"}) == ["example.com"]

    def test_indexed_caddy_labels(self):
        labels = {
            "caddy_0": "app.example.com",
            "caddy_0.reverse_proxy": "{{upstreams 3000}}",
            "caddy_1": "api.example.com",
            "caddy_1.reverse_proxy": "{{upstreams 8080}}",
        }
        assert _extract_domains(labels) == ["app.example.com", "api.example.com"]

    def test_ignores_sub_keys(self):
        labels = {
            "caddy": "example.com",
            "caddy.reverse_proxy": "{{upstreams 3000}}",
            "caddy.encode": "gzip",
        }
        assert _extract_domains(labels) == ["example.com"]

    def test_ignores_snippet_values(self):
        # Values starting with '(' are Caddy snippets, not domains
        assert _extract_domains({"caddy": "(my-snippet)"}) == []

    def test_ignores_port_values(self):
        # Values starting with ':' are port bindings, not domains
        assert _extract_domains({"caddy": ":8080"}) == []

    def test_empty_labels(self):
        assert _extract_domains({}) == []

    def test_no_caddy_labels(self):
        assert _extract_domains({"com.docker.compose.project": "myapp"}) == []

    def test_empty_caddy_value(self):
        assert _extract_domains({"caddy": ""}) == []


# ── _fmt_uptime ───────────────────────────────────────────────────────────────

class TestFmtUptime:
    def test_minutes_only(self):
        assert _fmt_uptime(300) == "5m"

    def test_hours_and_minutes(self):
        assert _fmt_uptime(3661) == "1h 1m"

    def test_days_and_hours(self):
        assert _fmt_uptime(90061) == "1d 1h"

    def test_zero(self):
        assert _fmt_uptime(0) == "0m"

    def test_exactly_one_day(self):
        assert _fmt_uptime(86400) == "1d 0h"

    def test_exactly_one_hour(self):
        assert _fmt_uptime(3600) == "1h 0m"


# ── _key_hint ─────────────────────────────────────────────────────────────────

class TestKeyHint:
    def test_normal_key(self):
        key = "sk-ant-api03-abcdefgh1234"
        hint = _key_hint(key)
        assert hint.startswith("sk-ant-a")
        assert hint.endswith("1234")
        assert "···" in hint
        assert key not in hint

    def test_hint_never_reveals_middle(self):
        key = "sk-ant-api03-SECRETSECRET-end"
        hint = _key_hint(key)
        assert "SECRETSECRET" not in hint

    def test_short_key_returns_placeholder(self):
        assert _key_hint("short") == "···"

    def test_exactly_eight_chars_returns_placeholder(self):
        assert _key_hint("12345678") == "···"

    def test_structure(self):
        hint = _key_hint("abcdefghijklmnop")
        # first 8 + ··· + last 4
        assert hint == "abcdefgh···mnop"


# ── _extras ───────────────────────────────────────────────────────────────────

class TestExtras:
    def test_filters_none(self):
        result = _extras(("Label", None), ("Other", "value"))
        assert len(result) == 1
        assert result[0] == {"label": "Other", "value": "value"}

    def test_filters_empty_string(self):
        result = _extras(("Label", ""), ("Other", "value"))
        assert len(result) == 1

    def test_filters_empty_list(self):
        result = _extras(("Label", []), ("Other", "value"))
        assert len(result) == 1

    def test_keeps_zero(self):
        # 0 is a valid value and must not be filtered
        result = _extras(("Count", 0))
        assert len(result) == 1
        assert result[0]["value"] == 0

    def test_all_present(self):
        result = _extras(("A", "1"), ("B", "2"), ("C", "3"))
        assert [e["label"] for e in result] == ["A", "B", "C"]


# ── _uptime ───────────────────────────────────────────────────────────────────

class TestUptime:
    def test_returns_string_for_valid_timestamp(self):
        result = _uptime("2020-01-01T00:00:00Z")
        assert result is not None
        assert isinstance(result, str)

    def test_returns_none_for_invalid_timestamp(self):
        assert _uptime("not-a-date") is None

    def test_returns_none_for_empty_string(self):
        assert _uptime("") is None

    def test_format_contains_time_unit(self):
        result = _uptime("2020-01-01T00:00:00Z")
        assert any(u in result for u in ["m", "h", "d"])
