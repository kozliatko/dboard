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
