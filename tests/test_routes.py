"""Tests for FastAPI HTTP routes and middleware."""
import pytest
from unittest.mock import MagicMock, patch


def _stopped_container(name="myapp", image="nginx:alpine", has_caddy=False):
    c = MagicMock()
    c.id = "abc123def456"
    c.name = f"/{name}"
    c.status = "exited"
    c.labels = {"caddy": f"{name}.example.com"} if has_caddy else {}
    c.attrs = {
        "State": {"Health": None, "StartedAt": ""},
        "NetworkSettings": {"Networks": {}},
    }
    c.image.tags = [image]
    c.image.attrs = {"RepoDigests": []}
    return c


def _mock_docker(containers=()):
    client = MagicMock()
    client.containers.list.return_value = list(containers)
    client.networks.list.return_value = []
    return client


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import main
    from collections import deque

    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(main, "_db_conn", None)
    monkeypatch.setattr(main, "_docker", None)
    monkeypatch.setattr(main, "_dock", lambda: None)
    main._container_spark.clear()
    main._container_io_prev.clear()
    # Short-TTL result caches must not leak state between tests.
    main._sys_cache.clear()
    main._containers_cache.clear()
    main._networks_cache.clear()

    from fastapi.testclient import TestClient
    with TestClient(main.app, raise_server_exceptions=True) as c:
        yield c

    if main._db_conn:
        main._db_conn.close()
        monkeypatch.setattr(main, "_db_conn", None)


# ── Security headers middleware ───────────────────────────────────────────────

class TestSecurityHeaders:
    def test_csp_header_present(self, client):
        r = client.get("/")
        assert "Content-Security-Policy" in r.headers
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]

    def test_security_headers_set(self, client):
        r = client.get("/")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert r.headers.get("Referrer-Policy") == "no-referrer"
        assert "Permissions-Policy" in r.headers

    def test_csp_has_no_external_font_cdns(self, client):
        # Fonts are self-hosted — no Google Fonts exceptions in the CSP.
        r = client.get("/")
        csp = r.headers["Content-Security-Policy"]
        assert "fonts.googleapis.com" not in csp
        assert "fonts.gstatic.com" not in csp
        assert "font-src 'self'" in csp


# ── / (dashboard) ─────────────────────────────────────────────────────────────

class TestDashboard:
    def test_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_contains_version_string(self, client):
        r = client.get("/")
        import main
        assert main._VERSION in r.text

    def test_fonts_are_self_hosted(self, client):
        r = client.get("/")
        assert "/static/fonts.css" in r.text
        assert "fonts.googleapis.com" not in r.text
        assert "fonts.gstatic.com" not in r.text

    def test_font_assets_are_served(self, client):
        css = client.get("/static/fonts.css")
        assert css.status_code == 200
        assert "woff2" in css.text
        for font in ("/static/fonts/Outfit.woff2", "/static/fonts/JetBrainsMono.woff2"):
            f = client.get(font)
            assert f.status_code == 200
            assert len(f.content) > 10_000  # real font file, not an error page


# ── /api/containers ───────────────────────────────────────────────────────────

class TestApiContainers:
    def test_docker_unavailable_returns_error(self, client):
        r = client.get("/api/containers")
        assert r.status_code == 200
        data = r.json()
        assert data.get("error") == "Docker socket not available"

    def test_with_stopped_containers(self, client, monkeypatch):
        import main
        stopped = _stopped_container("web")
        proxied = _stopped_container("proxy", has_caddy=True)
        monkeypatch.setattr(main, "_dock", lambda: _mock_docker([stopped, proxied]))

        r = client.get("/api/containers")
        assert r.status_code == 200
        data = r.json()
        assert "proxied" in data
        assert "others" in data
        assert "networks" in data
        assert len(data["others"]) == 1
        assert data["others"][0]["name"] == "web"
        assert len(data["proxied"]) == 1
        assert data["proxied"][0]["name"] == "proxy"

    def test_response_includes_metadata(self, client, monkeypatch):
        import main
        monkeypatch.setattr(main, "_dock", lambda: _mock_docker())
        r = client.get("/api/containers")
        data = r.json()
        assert "retention_seconds" in data
        assert data["retention_seconds"] == main._DB_RETENTION
        assert "sample_interval" in data
        assert "updated_at" in data

    def test_empty_docker_returns_empty_lists(self, client, monkeypatch):
        import main
        monkeypatch.setattr(main, "_dock", lambda: _mock_docker())
        r = client.get("/api/containers")
        assert r.status_code == 200
        data = r.json()
        assert data["proxied"] == []
        assert data["others"] == []

    def test_on_demand_ttl_cache_dedupes(self, client, monkeypatch):
        import main
        calls = {"n": 0}

        async def fake():
            calls["n"] += 1
            return {"proxied": [], "others": [], "networks": []}

        monkeypatch.setattr(main, "_collect_containers", fake)
        client.get("/api/containers")
        client.get("/api/containers")
        assert calls["n"] == 1  # only one full Docker scan per TTL window

    def test_prunes_spark_for_removed_containers(self, client, monkeypatch):
        import main
        from collections import deque

        main._container_spark["ghost"] = {
            k: deque(maxlen=10) for k in ("cpu", "mem", "net_rx", "net_tx")
        }
        main._container_io_prev["ghost"] = {"t": 0, "rx": 0, "tx": 0}
        monkeypatch.setattr(main, "_dock", lambda: _mock_docker())

        client.get("/api/containers")

        assert "ghost" not in main._container_spark
        assert "ghost" not in main._container_io_prev


# ── /api/system ───────────────────────────────────────────────────────────────

class TestApiSystem:
    def test_returns_system_stats(self, client, monkeypatch):
        import main
        fake = {"cpu_percent": 12.5, "mem_percent": 45.0, "cpu_count": 4, "sparklines": {}}
        monkeypatch.setattr(main, "_system_stats_sync", lambda: fake)
        r = client.get("/api/system")
        assert r.status_code == 200
        data = r.json()
        assert data["cpu_percent"] == 12.5
        assert data["mem_percent"] == 45.0

    def test_on_demand_ttl_cache_dedupes(self, client, monkeypatch):
        import main
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            return {"cpu_percent": calls["n"], "mem_percent": 1.0}

        monkeypatch.setattr(main, "_system_stats_sync", fake)
        first = client.get("/api/system").json()
        second = client.get("/api/system").json()
        assert first["cpu_percent"] == 1
        assert second["cpu_percent"] == 1  # served from the TTL cache
        assert calls["n"] == 1  # gather happened exactly once

    def test_ttl_expiry_forces_recompute(self, client, monkeypatch):
        import main
        monkeypatch.setattr(main, "_SYS_CACHE_TTL", -1)
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            return {"cpu_percent": calls["n"], "mem_percent": 1.0}

        monkeypatch.setattr(main, "_system_stats_sync", fake)
        client.get("/api/system")
        client.get("/api/system")
        assert calls["n"] == 2

    def test_sampler_cache_used_when_available(self, client, monkeypatch):
        import main
        cached = {"cpu_percent": 99.9, "mem_percent": 11.1}
        monkeypatch.setattr(main, "SAMPLE_INTERVAL", 5)
        with main._latest_lock:
            main._latest_system = cached
        try:
            r = client.get("/api/system")
            assert r.status_code == 200
            assert r.json()["cpu_percent"] == 99.9
        finally:
            with main._latest_lock:
                main._latest_system = None
            monkeypatch.setattr(main, "SAMPLE_INTERVAL", 0)


# ── /api/history ─────────────────────────────────────────────────────────────

class TestApiHistory:
    def test_empty_history_returns_correct_shape(self, client):
        r = client.get("/api/history?name=nonexistent&range=3600")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "nonexistent"
        assert data["range"] == 3600
        assert data["cpu"] == []
        assert data["mem"] == []

    def test_range_clamped_to_retention(self, client):
        import main
        r = client.get(f"/api/history?name=x&range={main._DB_RETENTION + 99999}")
        assert r.status_code == 200
        assert r.json()["range"] == main._DB_RETENTION

    def test_range_clamped_to_minimum_60(self, client):
        r = client.get("/api/history?name=x&range=1")
        assert r.status_code == 200
        assert r.json()["range"] == 60


# ── /api/system-history ───────────────────────────────────────────────────────

class TestApiSystemHistory:
    def test_empty_system_history_shape(self, client):
        r = client.get("/api/system-history?range=3600")
        assert r.status_code == 200
        data = r.json()
        assert data["range"] == 3600
        assert data["cpu"] == []
        assert data["mem"] == []

    def test_range_clamped(self, client):
        r = client.get("/api/system-history?range=10")
        assert r.status_code == 200
        assert r.json()["range"] == 60


# ── /api/stack ────────────────────────────────────────────────────────────────

class TestApiStack:
    def test_empty_stack_returns_correct_shape(self, client):
        r = client.get("/api/stack?metric=cpu&range=3600")
        assert r.status_code == 200
        data = r.json()
        assert data["metric"] == "cpu"
        assert data["containers"] == []
        assert data["count"] == 0

    def test_invalid_metric_defaults_to_cpu(self, client):
        r = client.get("/api/stack?metric=invalid&range=3600")
        assert r.status_code == 200
        assert r.json()["metric"] == "cpu"

    def test_mem_metric(self, client):
        r = client.get("/api/stack?metric=mem&range=3600")
        assert r.status_code == 200
        assert r.json()["metric"] == "mem"


# ── /api/tokens ───────────────────────────────────────────────────────────────

class TestApiTokens:
    def test_no_keys_configured(self, client):
        r = client.get("/api/tokens")
        assert r.status_code == 200
        data = r.json()
        assert "tokens" in data
        assert "cache_ttl" in data
        for token in data["tokens"]:
            assert token["configured"] is False
            assert token["valid"] is None

    def test_cache_ttl_is_300(self, client):
        r = client.get("/api/tokens")
        assert r.json()["cache_ttl"] == 300
