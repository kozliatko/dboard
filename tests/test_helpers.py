"""Tests for pure helper functions in main.py."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from main import (
    _extract_domains, _fmt_uptime, _key_hint, _extras, _uptime,
    _stats_sync, _image_name, _networks_sync,
)


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
        assert hint.startswith("sk-a")
        assert hint.endswith("1234")
        assert "···" in hint
        assert key not in hint

    def test_hint_never_reveals_middle(self):
        key = "sk-ant-api03-SECRETSECRET-end"
        hint = _key_hint(key)
        assert "SECRETSECRET" not in hint

    def test_short_key_returns_placeholder(self):
        assert _key_hint("short") == "···"

    def test_twelve_chars_returns_placeholder(self):
        # boundary: keys of 12 chars or fewer reveal nothing
        assert _key_hint("123456789012") == "···"

    def test_structure(self):
        hint = _key_hint("abcdefghijklmnop")
        # first 4 + ··· + last 4
        assert hint == "abcd···mnop"


# ── _http_get / _http_post (httpx2 client) ────────────────────────────────────

class TestHttpHelpers:
    def test_get_returns_status_body_headers(self, monkeypatch):
        import main
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = 200
        resp.content = b'{"ok": true}'
        resp.headers = {"Content-Type": "application/json"}

        fake_client = MagicMock()
        fake_client.get.return_value = resp
        monkeypatch.setattr(main, "_http", fake_client)

        code, body, hdrs = main._http_get("https://example.com/api")
        assert code == 200
        assert body == '{"ok": true}'
        assert hdrs["Content-Type"] == "application/json"
        # the shared client is used (connection pooling), not raw urllib
        fake_client.get.assert_called_once()

    def test_get_network_error_returns_none(self, monkeypatch):
        import main
        from unittest.mock import MagicMock

        fake_client = MagicMock()
        fake_client.get.side_effect = Exception("connection refused")
        monkeypatch.setattr(main, "_http", fake_client)

        code, body, _ = main._http_get("https://example.com/api")
        assert code is None
        assert "connection refused" in body

    def test_post_sends_json_payload(self, monkeypatch):
        import main
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = 201
        resp.content = b'{"created": true}'
        resp.headers = {}

        fake_client = MagicMock()
        fake_client.post.return_value = resp
        monkeypatch.setattr(main, "_http", fake_client)

        code, body, _ = main._http_post("https://example.com/api", {"a": 1})
        assert code == 201
        assert body == '{"created": true}'
        kwargs = fake_client.post.call_args.kwargs
        assert kwargs.get("json") == {"a": 1}  # sent as JSON body


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

    def test_hours_only(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=3, minutes=15)).isoformat()
        result = _uptime(ts)
        assert result is not None
        assert "h" in result
        assert "d" not in result

    def test_minutes_only(self):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=42)).isoformat()
        result = _uptime(ts)
        assert result is not None
        assert "m" in result
        assert "h" not in result
        assert "d" not in result


# ── _read_version ─────────────────────────────────────────────────────────────

class TestReadVersion:
    def test_returns_dev_on_missing_file(self):
        import main
        with patch("builtins.open", side_effect=FileNotFoundError("no VERSION")):
            result = main._read_version()
        assert result == "dev"


# ── _env_int ──────────────────────────────────────────────────────────────────

class TestEnvInt:
    def test_invalid_value_returns_default(self, monkeypatch):
        import main
        monkeypatch.setenv("_TEST_ENV_INT", "not-a-number")
        assert main._env_int("_TEST_ENV_INT", 42) == 42

    def test_valid_value(self, monkeypatch):
        import main
        monkeypatch.setenv("_TEST_ENV_INT", "10")
        assert main._env_int("_TEST_ENV_INT", 0) == 10

    def test_negative_clamped_to_zero(self, monkeypatch):
        import main
        monkeypatch.setenv("_TEST_ENV_INT", "-5")
        assert main._env_int("_TEST_ENV_INT", 0) == 0

    def test_missing_env_uses_default(self):
        import main
        result = main._env_int("_TEST_ENV_INT_MISSING_XYZ", 99)
        assert result == 99


# ── _dock ─────────────────────────────────────────────────────────────────────

class TestDock:
    def test_returns_none_on_connect_failure(self, monkeypatch):
        import main
        monkeypatch.setattr(main, "_docker", None)
        with patch("main.docker.from_env", side_effect=Exception("connection refused")):
            result = main._dock()
        assert result is None
        monkeypatch.setattr(main, "_docker", None)  # reset global state


# ── _stats_sync ───────────────────────────────────────────────────────────────

class TestStatsSync:
    def test_running_container_returns_stats(self):
        c = MagicMock()
        c.status = "running"
        c.stats.return_value = {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 200_000_000, "percpu_usage": [0, 0]},
                "system_cpu_usage": 1_000_000_000,
                "online_cpus": 2,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 100_000_000},
                "system_cpu_usage": 500_000_000,
            },
            "memory_stats": {
                "usage": 100 * 1024 * 1024,
                "limit": 1024 * 1024 * 1024,
                "stats": {"inactive_file": 0},
            },
            "networks": {"eth0": {"rx_bytes": 1000, "tx_bytes": 500}},
        }
        result = _stats_sync(c)
        assert result["cpu_percent"] >= 0.0
        assert result["cpu_percent"] <= 100.0
        assert result["mem_mb"] == 100.0
        assert result["mem_limit_mb"] == 1024.0
        assert result["net_rx"] == 1000
        assert result["net_tx"] == 500

    def test_exited_container_returns_empty(self):
        c = MagicMock()
        c.status = "exited"
        assert _stats_sync(c) == {}

    def test_api_error_returns_empty(self):
        c = MagicMock()
        c.status = "running"
        c.stats.side_effect = Exception("Docker API unreachable")
        assert _stats_sync(c) == {}

    def test_cache_deducted_from_memory_usage(self):
        c = MagicMock()
        c.status = "running"
        c.stats.return_value = {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 100, "percpu_usage": [0]},
                "system_cpu_usage": 1000,
                "online_cpus": 1,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 100},
                "system_cpu_usage": 500,
            },
            "memory_stats": {
                "usage": 200 * 1024 * 1024,
                "limit": 1024 * 1024 * 1024,
                "stats": {"inactive_file": 50 * 1024 * 1024},
            },
            "networks": {},
        }
        result = _stats_sync(c)
        # usage = 200MB - 50MB cache = 150MB
        assert result["mem_mb"] == 150.0


# ── _image_name ───────────────────────────────────────────────────────────────

class TestImageName:
    def test_uses_config_image_without_roundtrip(self):
        # Config.Image is already present in the container attrs from
        # containers.list(); _image_name must use it without touching the
        # image API (which would be an extra /images/{id}/json round-trip).
        class NoImageApi(MagicMock):
            @property
            def image(self):
                raise AssertionError("_image_name must not hit the image API when Config.Image is set")

        c = NoImageApi()
        c.attrs = {"Config": {"Image": "nginx:alpine"}}
        assert _image_name(c) == "nginx:alpine"

    def test_uses_first_tag(self):
        c = MagicMock()
        c.image.tags = ["nginx:alpine", "nginx:latest"]
        assert _image_name(c) == "nginx:alpine"

    def test_digest_fallback_when_no_tags(self):
        c = MagicMock()
        c.image.tags = []
        c.image.attrs = {"RepoDigests": ["nginx@sha256:abc123"]}
        assert _image_name(c) == "nginx"

    def test_short_id_fallback_when_no_tags_or_digests(self):
        c = MagicMock()
        c.image.tags = []
        c.image.attrs = {"RepoDigests": []}
        c.image.short_id = "sha256abc"
        assert _image_name(c) == "sha256abc"

    def test_config_image_fallback_on_exception(self):
        c = MagicMock()
        c.image.tags = []  # falsy — proceeds to attrs.get
        c.image.attrs.get.side_effect = Exception("image deleted from registry")
        c.attrs = {"Config": {"Image": "deleted-image:v1"}, "Image": ""}
        assert _image_name(c) == "deleted-image:v1"

    def test_unknown_when_all_fallbacks_fail(self):
        c = MagicMock()
        c.image.tags = []
        c.image.attrs.get.side_effect = Exception("no image")
        c.attrs = {}
        assert _image_name(c) == "unknown"


# ── _networks_sync ────────────────────────────────────────────────────────────

class TestNetworksSync:
    def test_returns_network_list(self):
        net = MagicMock()
        net.name = "bridge"
        net.short_id = "aabbcc"
        net.attrs = {"Driver": "bridge", "Scope": "local", "Internal": False}
        docker_client = MagicMock()
        docker_client.networks.list.return_value = [net]

        result = _networks_sync(docker_client, {"bridge": ["web", "db"]})
        assert len(result) == 1
        assert result[0]["name"] == "bridge"
        assert result[0]["driver"] == "bridge"
        assert result[0]["container_count"] == 2
        assert sorted(result[0]["container_names"]) == ["db", "web"]

    def test_internal_flag_set(self):
        net = MagicMock()
        net.name = "private"
        net.short_id = "xyz"
        net.attrs = {"Driver": "bridge", "Scope": "local", "Internal": True}
        docker_client = MagicMock()
        docker_client.networks.list.return_value = [net]

        result = _networks_sync(docker_client, {})
        assert result[0]["internal"] is True

    def test_api_error_returns_empty_list(self):
        docker_client = MagicMock()
        docker_client.networks.list.side_effect = Exception("socket error")
        assert _networks_sync(docker_client, {}) == []


# ── _system_stats_sync ────────────────────────────────────────────────────────

class TestSystemStatsSync:
    def test_cpu_percent_nonblocking(self, monkeypatch):
        import main
        from unittest.mock import MagicMock

        monkeypatch.setattr(main, "_db_write_sys", lambda *a, **k: None)
        monkeypatch.setattr(main, "_host_disks", lambda: [])
        monkeypatch.setattr(main, "_cpu_temp", lambda: None)

        cpu_percent = MagicMock(return_value=12.5)
        monkeypatch.setattr(main.psutil, "cpu_percent", cpu_percent)

        mem = MagicMock()
        mem.total = 8 * 1024 ** 3
        mem.available = 4 * 1024 ** 3
        monkeypatch.setattr(main.psutil, "virtual_memory", lambda: mem)

        swap = MagicMock()
        swap.total = 0
        swap.used = 0
        swap.percent = 0
        monkeypatch.setattr(main.psutil, "swap_memory", lambda: swap)

        net = MagicMock()
        net.bytes_recv = 1000
        net.bytes_sent = 500
        monkeypatch.setattr(main.psutil, "net_io_counters", lambda: net)

        disk = MagicMock()
        disk.read_bytes = 100
        disk.write_bytes = 50
        monkeypatch.setattr(main.psutil, "disk_io_counters", lambda: disk)

        monkeypatch.setattr(main.psutil, "cpu_count", lambda logical=True: 4)
        monkeypatch.setattr(main.os, "getloadavg", lambda: (1.0, 2.0, 3.0))

        result = main._system_stats_sync()

        # Non-blocking sampling: no interval argument, never sleeps 300 ms.
        cpu_percent.assert_called_once_with(interval=None)
        assert result["cpu_percent"] == 12.5
        assert result["cpu_count"] == 4


# ── _networks_cached ──────────────────────────────────────────────────────────

class TestNetworksCached:
    def setup_method(self):
        import main
        main._networks_cache.clear()

    def test_cached_within_ttl(self, monkeypatch):
        import asyncio
        import main
        calls = {"n": 0}

        def fake(client, net_containers):
            calls["n"] += 1
            return [{"name": "bridge"}]

        monkeypatch.setattr(main, "_networks_sync", fake)

        async def run():
            await main._networks_cached(None, {})
            await main._networks_cached(None, {})

        asyncio.run(run())
        assert calls["n"] == 1  # second call served from cache

    def test_ttl_expiry_forces_recompute(self, monkeypatch):
        import asyncio
        import main
        monkeypatch.setattr(main, "_NETWORKS_CACHE_TTL", -1)
        calls = {"n": 0}

        def fake(client, net_containers):
            calls["n"] += 1
            return []

        monkeypatch.setattr(main, "_networks_sync", fake)

        async def run():
            await main._networks_cached(None, {})
            await main._networks_cached(None, {})

        asyncio.run(run())
        assert calls["n"] == 2


# ── _host_disks ───────────────────────────────────────────────────────────────

class TestHostDisks:
    def setup_method(self):
        import main
        main._DISK_CACHE.clear()

    def test_returns_disk_info(self):
        import main
        stat_result = MagicMock()
        stat_result.st_dev = 42

        sv = MagicMock()
        sv.f_frsize = 4096
        sv.f_blocks = 2_621_440   # 10 GB total
        sv.f_bavail = 1_310_720   # 5 GB free

        with patch("main.os.stat", return_value=stat_result), \
             patch("main.os.statvfs", return_value=sv):
            result = main._host_disks()

        assert len(result) == 1  # deduped by st_dev
        assert result[0]["mount"] == "/"
        assert result[0]["total_gb"] == 10.0
        assert result[0]["free_gb"] == 5.0
        assert result[0]["percent"] == 50.0

    def test_cache_prevents_repeated_stat_calls(self):
        import main
        stat_result = MagicMock()
        stat_result.st_dev = 99
        sv = MagicMock()
        sv.f_frsize = 4096
        sv.f_blocks = 1_310_720
        sv.f_bavail = 655_360

        with patch("main.os.stat", return_value=stat_result) as mock_stat, \
             patch("main.os.statvfs", return_value=sv):
            first = main._host_disks()
            second = main._host_disks()
            # os.stat should only be called on the first invocation
            assert mock_stat.call_count == len(main._DISK_PROBES)

        assert first == second

    def test_missing_path_handled_gracefully(self):
        import main
        with patch("main.os.stat", side_effect=OSError("no such file")):
            result = main._host_disks()
        assert result == []
