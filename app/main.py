import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import psutil

import docker
from dateutil import parser as date_parser
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

log = logging.getLogger("dboard")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI()
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
)
executor = ThreadPoolExecutor(max_workers=16)

try:
    _docker = docker.from_env()
except Exception as e:
    _docker = None
    log.error("Docker unavailable: %s", e)

# ── Caddy route injection ─────────────────────────────────────────────────────

CADDY_ROUTE_MARKER = "dboard-injected"

_DBOARD_ROUTE_TEMPLATE = {
    "match": [{"path_regexp": {"pattern": "^/dboard(/|$)"}}],
    "handle": [
        {
            "handler": "subroute",
            "routes": [
                {
                    "handle": [
                        {"handler": "rewrite", "strip_path_prefix": "/dboard"},
                        {
                            "handler": "reverse_proxy",
                            "upstreams": [{"dial": "{dial}"}],
                        },
                    ]
                }
            ],
        }
    ],
    # non-terminal: path-only match, lets host-routes still handle non-/dboard paths
    "@id": CADDY_ROUTE_MARKER,
}


def _find_caddy_container():
    if not _docker:
        return None
    for c in _docker.containers.list():
        tags = c.image.tags or []
        if any("caddy" in t.lower() for t in tags):
            return c
    return None


def _own_dial_address() -> str:
    """Resolve own container name for use as a Caddy upstream dial address."""
    try:
        hostname = open("/etc/hostname").read().strip()
        for c in _docker.containers.list():
            if c.short_id == hostname or c.id.startswith(hostname):
                return f"{c.name.lstrip('/')}:8000"
    except Exception:
        pass
    return os.environ.get("DBOARD_DIAL", "dboard-dboard-1:8000")


def _caddy_exec(caddy_container, cmd: list[str]) -> tuple[int, bytes]:
    result = caddy_container.exec_run(cmd)
    return result.exit_code, result.output or b""


def _find_srv0(caddy) -> str | None:
    """Find the server key that listens on :443."""
    code, out = _caddy_exec(
        caddy, ["curl", "-sf", "http://localhost:2019/config/apps/http/servers/"]
    )
    if code != 0:
        return None
    try:
        servers = json.loads(out)
        for name, srv in servers.items():
            if ":443" in srv.get("listen", []):
                return name
        # fallback: first server
        return next(iter(servers), "srv0")
    except Exception:
        return "srv0"


def _inject_caddy_route_sync() -> str:
    """Check Caddy routes and ensure /dboard/* is at position 0. Returns status string."""
    caddy = _find_caddy_container()
    if not caddy:
        return "caddy container not found"

    srv = _find_srv0(caddy) or "srv0"
    routes_url = f"http://localhost:2019/config/apps/http/servers/{srv}/routes"

    # Read current routes
    code, out = _caddy_exec(caddy, ["curl", "-sf", routes_url])
    if code != 0:
        return f"caddy admin unreachable (exit {code})"

    try:
        routes = json.loads(out)
        if not isinstance(routes, list):
            routes = []
    except Exception:
        return f"invalid routes JSON: {out[:80]}"

    if not routes:
        return "routes empty — caddy not ready yet"

    # Check if already at position 0
    if routes[0].get("@id") == CADDY_ROUTE_MARKER:
        return "already present"

    # Remove any stale dboard entry (might be at wrong position)
    filtered = [r for r in routes if r.get("@id") != CADDY_ROUTE_MARKER]

    # Build route with resolved dial address
    dial = _own_dial_address()
    route = json.loads(json.dumps(_DBOARD_ROUTE_TEMPLATE))
    route["handle"][0]["routes"][0]["handle"][1]["upstreams"][0]["dial"] = dial

    # Replace the routes array using DELETE + PUT.
    # We MUST target /routes directly — PATCH on /servers/srv0 replaces the
    # entire server object (removing listen: [":443"]) which breaks Caddy.
    new_routes = [route] + filtered
    routes_payload = json.dumps(new_routes)

    # Delete existing routes array, then recreate it with our route prepended.
    # The window between DELETE and PUT is <100ms so impact is negligible.
    del_code, _ = _caddy_exec(
        caddy,
        ["curl", "-sf", "-X", "DELETE", routes_url],
    )
    if del_code != 0:
        return "DELETE routes failed"

    code, out = _caddy_exec(
        caddy,
        [
            "curl", "-sf", "-X", "PUT",
            "-H", "Content-Type: application/json",
            "-d", routes_payload,
            routes_url,
        ],
    )
    if code != 0:
        return f"inject failed (exit {code}): {out[:120]}"

    log.info("Injected /dboard/* at position 0 → %s (server: %s)", dial, srv)
    return f"injected → {dial}"


async def _caddy_watcher():
    """Background task: ensure /dboard/ route stays in Caddy config."""
    while True:
        try:
            loop = asyncio.get_event_loop()
            status = await loop.run_in_executor(executor, _inject_caddy_route_sync)
            if status not in ("already present",):
                log.info("Caddy route status: %s", status)
        except Exception as e:
            log.warning("Caddy watcher error: %s", e)
        await asyncio.sleep(10)


async def _db_pruner():
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.get_event_loop().run_in_executor(executor, _db_prune_sync)
        except Exception as e:
            log.warning("DB pruner error: %s", e)


@app.on_event("startup")
async def startup():
    await asyncio.get_event_loop().run_in_executor(executor, _db_init)
    asyncio.create_task(_caddy_watcher())
    asyncio.create_task(_db_pruner())


# ── Docker helpers ────────────────────────────────────────────────────────────


def _extract_domains(labels: dict) -> list[str]:
    domains = []
    for k, v in sorted(labels.items()):
        if not v:
            continue
        bare = k.split(".")[0]
        if bare == "caddy" or (bare.startswith("caddy_") and bare[6:].isdigit()):
            if k == bare and not v.startswith("(") and not v.startswith(":"):
                domains.append(v)
    return domains


def _stats_sync(container_id: str) -> dict:
    try:
        c = docker.from_env().containers.get(container_id)
        if c.status != "running":
            return {}
        raw = c.stats(stream=False)

        cpu_delta = (
            raw["cpu_stats"]["cpu_usage"]["total_usage"]
            - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        sys_delta = raw["cpu_stats"].get("system_cpu_usage", 0) - raw[
            "precpu_stats"
        ].get("system_cpu_usage", 0)
        ncpu = raw["cpu_stats"].get("online_cpus") or len(
            raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [0])
        )
        cpu_pct = (cpu_delta / sys_delta * ncpu * 100.0) if sys_delta > 0 else 0.0

        mem = raw.get("memory_stats", {})
        usage = mem.get("usage", 0)
        cache = mem.get("stats", {}).get("inactive_file") or mem.get(
            "stats", {}
        ).get("cache", 0)
        usage = max(0, usage - (cache or 0))
        limit = mem.get("limit", 0)

        net = raw.get("networks", {})
        rx = sum(v.get("rx_bytes", 0) for v in net.values())
        tx = sum(v.get("tx_bytes", 0) for v in net.values())

        return {
            "cpu_percent": round(min(cpu_pct, 100.0), 1),
            "mem_mb": round(usage / 1024 / 1024, 1),
            "mem_limit_mb": round(limit / 1024 / 1024, 1),
            "mem_percent": round((usage / limit * 100.0) if limit else 0.0, 1),
            "net_rx": rx,
            "net_tx": tx,
        }
    except Exception:
        return {}


def _uptime(started_at: str) -> str | None:
    try:
        delta = datetime.now(timezone.utc) - date_parser.parse(started_at)
        d, s = delta.days, delta.seconds
        h, m = s // 3600, (s % 3600) // 60
        if d:
            return f"{d}d {h}h"
        if h:
            return f"{h}h {m}m"
        return f"{m}m"
    except Exception:
        return None


def _image_name(container) -> str:
    try:
        if container.image.tags:
            return container.image.tags[0]
        digests = container.image.attrs.get("RepoDigests", [])
        return digests[0].split("@")[0] if digests else container.image.short_id
    except Exception:
        # Image was deleted while container still exists — fall back to config string
        raw = container.attrs.get("Config", {}).get("Image") or container.attrs.get("Image", "")
        return raw or "unknown"


# ── System stats ─────────────────────────────────────────────────────────────

def _host_disks() -> list[dict]:
    """Probe distinct filesystems on the host (mounted at /host) via device IDs."""
    disks = []
    seen_devs: set[int] = set()
    probes = [
        "/host", "/host/boot", "/host/boot/efi",
        "/host/home", "/host/var", "/host/opt",
        "/host/data", "/host/tmp", "/host/srv",
    ]
    for path in probes:
        try:
            dev = os.stat(path).st_dev
            if dev in seen_devs:
                continue
            seen_devs.add(dev)
            st = os.statvfs(path)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - free
            if total == 0:
                continue
            mount = path[len("/host"):] or "/"
            disks.append({
                "mount": mount,
                "total_gb": round(total / 1024 ** 3, 1),
                "used_gb": round(used / 1024 ** 3, 1),
                "free_gb": round(free / 1024 ** 3, 1),
                "percent": round(used / total * 100, 1),
            })
        except Exception:
            pass
    return sorted(disks, key=lambda d: d["mount"])


def _fmt_uptime(secs: int) -> str:
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


_io_prev: dict = {}

_SPARK_N = 40  # ~200 s at 5 s refresh interval
_spark: dict[str, deque] = {
    "cpu":    deque(maxlen=_SPARK_N),
    "mem":    deque(maxlen=_SPARK_N),
    "temp":   deque(maxlen=_SPARK_N),
    "net_rx": deque(maxlen=_SPARK_N),
    "net_tx": deque(maxlen=_SPARK_N),
    "disk_r": deque(maxlen=_SPARK_N),
    "disk_w": deque(maxlen=_SPARK_N),
}

_CONTAINER_SPARK_N = 20  # ~100 s at 5 s refresh interval
_container_spark: dict[str, dict[str, deque]] = {}

# ── Persistence (SQLite) ──────────────────────────────────────────────────────

DB_PATH = "/app/data/metrics.db"
_DB_RETENTION = 86400  # 24 h
_db_lock = threading.Lock()
_db_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")
    return _db_conn


def _db_init() -> None:
    """Create tables, prune stale rows, reload history into ring buffers."""
    with _db_lock:
        conn = _db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sys_metrics (
                ts      INTEGER NOT NULL,
                cpu     REAL, mem  REAL, temp REAL,
                net_rx  INTEGER, net_tx INTEGER,
                disk_r  INTEGER, disk_w INTEGER
            );
            CREATE INDEX IF NOT EXISTS sys_ts ON sys_metrics(ts);
            CREATE TABLE IF NOT EXISTS container_metrics (
                ts   INTEGER NOT NULL,
                name TEXT    NOT NULL,
                cpu  REAL,
                mem  REAL
            );
            CREATE INDEX IF NOT EXISTS con_ts_name ON container_metrics(ts, name);
        """)
        cutoff = int(time.time()) - _DB_RETENTION
        conn.execute("DELETE FROM sys_metrics WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM container_metrics WHERE ts < ?", (cutoff,))
        conn.commit()

        # Reload system sparklines
        rows = conn.execute(
            "SELECT cpu,mem,temp,net_rx,net_tx,disk_r,disk_w "
            "FROM sys_metrics ORDER BY ts DESC LIMIT ?", (_SPARK_N,)
        ).fetchall()
        for cpu, mem, temp, rx, tx, dr, dw in reversed(rows):
            if cpu  is not None: _spark["cpu"].append(cpu)
            if mem  is not None: _spark["mem"].append(mem)
            if temp is not None: _spark["temp"].append(temp)
            if rx   is not None: _spark["net_rx"].append(rx)
            if tx   is not None: _spark["net_tx"].append(tx)
            if dr   is not None: _spark["disk_r"].append(dr)
            if dw   is not None: _spark["disk_w"].append(dw)

        # Reload container sparklines
        names = [r[0] for r in conn.execute(
            "SELECT DISTINCT name FROM container_metrics WHERE ts > ?", (cutoff,)
        ).fetchall()]
        for name in names:
            c_rows = conn.execute(
                "SELECT cpu,mem FROM container_metrics WHERE name=? ORDER BY ts DESC LIMIT ?",
                (name, _CONTAINER_SPARK_N)
            ).fetchall()
            cpu_buf: deque = deque(maxlen=_CONTAINER_SPARK_N)
            mem_buf: deque = deque(maxlen=_CONTAINER_SPARK_N)
            for cv, mv in reversed(c_rows):
                if cv is not None: cpu_buf.append(cv)
                if mv is not None: mem_buf.append(mv)
            _container_spark[name] = {"cpu": cpu_buf, "mem": mem_buf}

    log.info("DB ready: %d sys rows, %d containers restored", len(rows), len(names))


def _db_prune_sync() -> None:
    cutoff = int(time.time()) - _DB_RETENTION
    with _db_lock:
        conn = _db()
        conn.execute("DELETE FROM sys_metrics WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM container_metrics WHERE ts < ?", (cutoff,))
        conn.commit()
    log.info("DB pruned (cutoff=%d)", cutoff)


def _db_write_sys(ts: int, cpu: float, mem: float, temp, net_rate, disk_rate) -> None:
    try:
        with _db_lock:
            _db().execute(
                "INSERT INTO sys_metrics(ts,cpu,mem,temp,net_rx,net_tx,disk_r,disk_w) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (ts, cpu, mem, temp,
                 net_rate["rx_bps"]   if net_rate  else None,
                 net_rate["tx_bps"]   if net_rate  else None,
                 disk_rate["read_bps"] if disk_rate else None,
                 disk_rate["write_bps"] if disk_rate else None)
            )
            _db().commit()
    except Exception as e:
        log.warning("DB write sys: %s", e)


def _db_write_containers(entries: list[dict]) -> None:
    ts = int(time.time())
    rows = [(ts, e["name"], e.get("cpu_percent"), e.get("mem_percent")) for e in entries]
    try:
        with _db_lock:
            _db().executemany(
                "INSERT INTO container_metrics(ts,name,cpu,mem) VALUES(?,?,?,?)", rows
            )
            _db().commit()
    except Exception as e:
        log.warning("DB write containers: %s", e)


def _cpu_temp() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "it8"):
            if key in temps:
                vals = [t.current for t in temps[key] if t.current and t.current > 0]
                if vals:
                    return round(max(vals), 1)
        for readings in temps.values():
            vals = [t.current for t in readings if t.current and t.current > 0]
            if vals:
                return round(max(vals), 1)
    except Exception:
        pass
    return None


def _system_stats_sync() -> dict:
    global _io_prev

    cpu_pct = psutil.cpu_percent(interval=0.3)
    cpu_count = psutil.cpu_count(logical=True) or 1
    cpu_phys = psutil.cpu_count(logical=False) or cpu_count

    try:
        load1, load5, load15 = os.getloadavg()
        load_avg = [round(load1, 2), round(load5, 2), round(load15, 2)]
    except Exception:
        load_avg = None

    mem = psutil.virtual_memory()
    mem_used = mem.total - mem.available
    swap = psutil.swap_memory()

    try:
        uptime_secs = int(float(open("/proc/uptime").read().split()[0]))
    except Exception:
        uptime_secs = None

    # I/O rates — delta from previous sample
    now = time.monotonic()
    net_rate = None
    disk_rate = None
    try:
        net_now = psutil.net_io_counters()
        disk_now = psutil.disk_io_counters()
        if _io_prev and (dt := now - _io_prev["t"]) > 0:
            net_rate = {
                "rx_bps": round((net_now.bytes_recv - _io_prev["net_rx"]) / dt),
                "tx_bps": round((net_now.bytes_sent - _io_prev["net_tx"]) / dt),
            }
            if disk_now and _io_prev.get("disk_r") is not None:
                disk_rate = {
                    "read_bps":  round((disk_now.read_bytes  - _io_prev["disk_r"]) / dt),
                    "write_bps": round((disk_now.write_bytes - _io_prev["disk_w"]) / dt),
                }
        _io_prev = {
            "t":      now,
            "net_rx": net_now.bytes_recv,
            "net_tx": net_now.bytes_sent,
            "disk_r": disk_now.read_bytes  if disk_now else None,
            "disk_w": disk_now.write_bytes if disk_now else None,
        }
    except Exception:
        pass

    cpu_temp = _cpu_temp()
    mem_pct = round(mem_used / mem.total * 100, 1) if mem.total else 0

    # Update sparkline ring buffers
    _spark["cpu"].append(round(cpu_pct, 1))
    _spark["mem"].append(mem_pct)
    if cpu_temp is not None:
        _spark["temp"].append(cpu_temp)
    if net_rate:
        _spark["net_rx"].append(net_rate["rx_bps"])
        _spark["net_tx"].append(net_rate["tx_bps"])
    if disk_rate:
        _spark["disk_r"].append(disk_rate["read_bps"])
        _spark["disk_w"].append(disk_rate["write_bps"])

    _db_write_sys(int(time.time()), round(cpu_pct, 1), mem_pct, cpu_temp, net_rate, disk_rate)

    return {
        "cpu_percent": round(cpu_pct, 1),
        "cpu_count": cpu_count,
        "cpu_phys": cpu_phys,
        "load_avg": load_avg,
        "cpu_temp": cpu_temp,
        "mem_total_mb": round(mem.total / 1048576),
        "mem_used_mb": round(mem_used / 1048576),
        "mem_percent": mem_pct,
        "swap_total_mb": round(swap.total / 1048576),
        "swap_used_mb": round(swap.used / 1048576),
        "swap_percent": round(swap.percent, 1),
        "disks": _host_disks(),
        "uptime": _fmt_uptime(uptime_secs) if uptime_secs else None,
        "net_rate": net_rate,
        "disk_rate": disk_rate,
        "sparklines": {k: list(v) for k, v in _spark.items()},
    }


# ── Token validation ──────────────────────────────────────────────────────────

def _http_get(url: str, headers: dict | None = None, timeout: int = 10) -> tuple[int | None, str, dict]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), dict(e.headers)
    except Exception as e:
        return None, str(e), {}


def _extras(*pairs) -> list[dict]:
    return [{"label": l, "value": v} for l, v in pairs if v not in (None, "", [])]


def _check_anthropic(key: str) -> dict:
    gh = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    code, body, hdrs = _http_get("https://api.anthropic.com/v1/models", headers=gh)
    if code != 200:
        return {"valid": False, "detail": f"HTTP {code}"}
    models = sorted([m["id"] for m in json.loads(body).get("data", [])], reverse=True)
    rl_limit = hdrs.get("anthropic-ratelimit-requests-limit") or hdrs.get("x-ratelimit-limit-requests")
    rl_remain = hdrs.get("anthropic-ratelimit-requests-remaining") or hdrs.get("x-ratelimit-remaining-requests")
    return {
        "valid": True,
        "detail": f"{len(models)} models",
        "extras": _extras(
            ("Models", str(len(models))),
            ("Latest", ", ".join(models[:4])),
            ("Rate limit", f"{rl_remain} / {rl_limit} req" if rl_limit and rl_remain else None),
        ),
    }


def _check_github(key: str) -> dict:
    gh_hdrs = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    code, body, hdrs = _http_get("https://api.github.com/user", headers=gh_hdrs)
    if code != 200:
        return {"valid": False, "detail": f"HTTP {code}"}
    u = json.loads(body)
    scopes = (hdrs.get("X-OAuth-Scopes") or hdrs.get("x-oauth-scopes", "")).strip()
    expiry = (hdrs.get("GitHub-Authentication-Token-Expiration") or
              hdrs.get("github-authentication-token-expiration", ""))[:10]

    rl_code, rl_body, _ = _http_get("https://api.github.com/rate_limit", headers=gh_hdrs)
    rl = json.loads(rl_body).get("rate", {}) if rl_code == 200 else {}

    name_part = f" ({u['name']})" if u.get("name") and u["name"] != u.get("login") else ""
    return {
        "valid": True,
        "detail": f"@{u.get('login')}{name_part}",
        "extras": _extras(
            ("User", f"@{u.get('login')}{name_part}"),
            ("Public repos", str(u.get("public_repos", ""))),
            ("Private repos", str(u.get("total_private_repos", "") or "")),
            ("Scopes", scopes),
            ("Expires", expiry),
            ("Rate limit", f"{rl.get('remaining')} / {rl.get('limit')} req" if rl else None),
        ),
    }


def _check_gemini(key: str) -> dict:
    code, body, _ = _http_get(
        f"https://generativelanguage.googleapis.com/v1/models?key={key}"
    )
    if code != 200:
        return {"valid": False, "detail": f"HTTP {code}"}
    models = json.loads(body).get("models", [])
    names = sorted(
        [m.get("name", "").split("/")[-1] for m in models if "gemini" in m.get("name", "").lower()],
        reverse=True,
    )
    return {
        "valid": True,
        "detail": f"{len(models)} models",
        "extras": _extras(
            ("Models", str(len(models))),
            ("Gemini models", ", ".join(names[:5])),
        ),
    }


def _check_openai(key: str) -> dict:
    code, body, _ = _http_get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    if code != 200:
        return {"valid": False, "detail": f"HTTP {code}"}
    models = sorted([m["id"] for m in json.loads(body).get("data", [])], reverse=True)
    gpt = [m for m in models if m.startswith("gpt")]
    return {
        "valid": True,
        "detail": f"{len(models)} models",
        "extras": _extras(
            ("Models", str(len(models))),
            ("GPT models", ", ".join(gpt[:4])),
        ),
    }


def _check_deepseek(key: str) -> dict:
    ds_hdrs = {"Authorization": f"Bearer {key}"}
    code, body, _ = _http_get("https://api.deepseek.com/models", headers=ds_hdrs)
    if code != 200:
        return {"valid": False, "detail": f"HTTP {code}"}
    models = [m["id"] for m in json.loads(body).get("data", [])]

    bal_code, bal_body, _ = _http_get("https://api.deepseek.com/user/balance", headers=ds_hdrs)
    balance = None
    if bal_code == 200:
        try:
            info = json.loads(bal_body).get("balance_infos", [{}])[0]
            balance = f"{info.get('total_balance', '?')} {info.get('currency', '')}"
        except Exception:
            pass

    return {
        "valid": True,
        "detail": f"{len(models)} models",
        "extras": _extras(
            ("Models", ", ".join(models)),
            ("Balance", balance),
        ),
    }


def _check_tavily(key: str) -> dict:
    data = json.dumps({"api_key": key, "query": "ping", "max_results": 1}).encode()
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
            return {
                "valid": True,
                "detail": "search API OK",
                "extras": _extras(
                    ("Response time", f"{body.get('response_time', '?')} s"),
                ),
            }
    except urllib.error.HTTPError as e:
        return {"valid": False, "detail": f"HTTP {e.code}"}
    except Exception as e:
        return {"valid": False, "detail": str(e)}


def _check_gitlab(key: str) -> dict:
    host = os.environ.get("GITLAB_HOST", "gitlab.com").strip().rstrip("/")
    base = f"https://{host}/api/v4"
    hdrs = {"PRIVATE-TOKEN": key}

    # Token metadata
    code, body, _ = _http_get(f"{base}/personal_access_tokens/self", headers=hdrs)
    if code != 200:
        # Fallback: just check /user
        code, body, _ = _http_get(f"{base}/user", headers=hdrs)
        if code != 200:
            return {"valid": False, "detail": f"HTTP {code}"}
        u = json.loads(body)
        return {
            "valid": True,
            "detail": f"@{u.get('username')}",
            "extras": _extras(
                ("Host", host),
                ("User", f"@{u.get('username')} ({u.get('name')})"),
            ),
        }

    t = json.loads(body)
    u_code, u_body, _ = _http_get(f"{base}/user", headers=hdrs)
    username = json.loads(u_body).get("username", "?") if u_code == 200 else "?"
    scopes = ", ".join(t.get("scopes", []))
    expiry = (t.get("expires_at") or "")[:10]
    return {
        "valid": True,
        "detail": f"@{username} · {t.get('name', '')}",
        "extras": _extras(
            ("Host", host),
            ("User", f"@{username}"),
            ("Token name", t.get("name")),
            ("Scopes", scopes),
            ("Expires", expiry),
            ("Last used", (t.get("last_used_at") or "")[:10]),
        ),
    }


_TOKEN_DEFS = [
    {"id": "anthropic", "name": "Anthropic", "env_var": "ANTHROPIC_API_KEY", "fn": _check_anthropic},
    {"id": "github",    "name": "GitHub",    "env_var": "GITHUB_TOKEN",       "fn": _check_github},
    {"id": "gitlab",    "name": "GitLab",    "env_var": "GITLAB_TOKEN",       "fn": _check_gitlab},
    {"id": "gemini",    "name": "Gemini",    "env_var": "GEMINI_API_KEY",     "fn": _check_gemini},
    {"id": "openai",    "name": "OpenAI",    "env_var": "OPENAI_API_KEY",     "fn": _check_openai},
    {"id": "deepseek",  "name": "DeepSeek",  "env_var": "DEEPSEEK_API_KEY",   "fn": _check_deepseek},
    {"id": "tavily",    "name": "Tavily",    "env_var": "TAVILY_API_KEY",     "fn": _check_tavily},
]

_TOKEN_CACHE: dict = {}
_TOKEN_CACHE_TTL = 300  # 5 minutes


def _key_hint(key: str) -> str:
    if len(key) <= 8:
        return "···"
    return f"{key[:8]}···{key[-4:]}"


def _check_token_sync(td: dict) -> dict:
    key = os.environ.get(td["env_var"], "").strip()
    base = {
        "id": td["id"],
        "name": td["name"],
        "env_var": td["env_var"],
        "key_hint": _key_hint(key) if key else None,
    }
    if not key:
        return {**base, "configured": False, "valid": None, "detail": None, "extras": [], "checked_at": None, "error": None}
    try:
        result = td["fn"](key)
        return {
            **base,
            "configured": True,
            "valid": result["valid"],
            "detail": result.get("detail"),
            "extras": result.get("extras", []),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": None,
        }
    except Exception as e:
        return {
            **base,
            "configured": True,
            "valid": False,
            "detail": None,
            "extras": [],
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": str(e),
        }


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/system")
async def api_system():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _system_stats_sync)


@app.get("/api/tokens")
async def api_tokens(refresh: bool = False):
    now = time.monotonic()
    results = []
    to_check = []

    for td in _TOKEN_DEFS:
        cached = _TOKEN_CACHE.get(td["id"])
        if not refresh and cached and (now - cached["_t"]) < _TOKEN_CACHE_TTL:
            results.append({k: v for k, v in cached.items() if k != "_t"})
        else:
            to_check.append(td)

    if to_check:
        loop = asyncio.get_event_loop()
        checked = await asyncio.gather(
            *[loop.run_in_executor(executor, _check_token_sync, td) for td in to_check]
        )
        for r in checked:
            _TOKEN_CACHE[r["id"]] = {**r, "_t": now}
            results.append(r)

    order = {td["id"]: i for i, td in enumerate(_TOKEN_DEFS)}
    results.sort(key=lambda r: order.get(r["id"], 99))
    return {"tokens": results, "cache_ttl": _TOKEN_CACHE_TTL}


@app.get("/api/containers")
async def api_containers():
    if not _docker:
        return {"error": "Docker socket not available", "proxied": [], "others": []}

    try:
        all_containers = _docker.containers.list(all=True)
    except Exception as e:
        return {"error": str(e), "proxied": [], "others": []}

    proxied = []
    others = []

    for c in all_containers:
        labels = c.labels
        has_caddy = any(
            k == "caddy"
            or (k.startswith("caddy_") and k.split(".")[0][6:].isdigit())
            for k in labels
        )

        domains = _extract_domains(labels) if has_caddy else []
        state = c.attrs.get("State", {})
        health_obj = state.get("Health")
        health = health_obj.get("Status") if health_obj else None

        entry = {
            "_id": c.id,
            "name": c.name.lstrip("/"),
            "image": _image_name(c),
            "status": c.status,
            "health": health,
            "domains": domains,
            "uptime": _uptime(state.get("StartedAt", "")) if c.status == "running" else None,
        }

        if has_caddy:
            proxied.append(entry)
        else:
            others.append(entry)

    all_running = [p for p in proxied + others if p["status"] == "running"]
    if all_running:
        loop = asyncio.get_event_loop()
        results = await asyncio.gather(
            *[loop.run_in_executor(executor, _stats_sync, p["_id"]) for p in all_running]
        )
        for p, stats in zip(all_running, results):
            p.update(stats)
            name = p["name"]
            if name not in _container_spark:
                _container_spark[name] = {
                    "cpu": deque(maxlen=_CONTAINER_SPARK_N),
                    "mem": deque(maxlen=_CONTAINER_SPARK_N),
                }
            if p.get("cpu_percent") is not None:
                _container_spark[name]["cpu"].append(p["cpu_percent"])
            if p.get("mem_percent") is not None:
                _container_spark[name]["mem"].append(p["mem_percent"])

    # Persist container stats to DB (fire-and-forget)
    if all_running:
        loop.run_in_executor(executor, _db_write_containers, all_running)

    for p in proxied + others:
        name = p["name"]
        if name in _container_spark:
            p["cpu_spark"] = list(_container_spark[name]["cpu"])
            p["mem_spark"] = list(_container_spark[name]["mem"])
        p.pop("_id", None)

    proxied.sort(key=lambda x: (x["status"] != "running", x["name"].lower()))
    others.sort(key=lambda x: (x["status"] != "running", x["name"].lower()))

    return {
        "proxied": proxied,
        "others": others,
        "running_proxied": sum(1 for p in proxied if p["status"] == "running"),
        "running_others": sum(1 for p in others if p["status"] == "running"),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
