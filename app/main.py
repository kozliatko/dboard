import asyncio
import json
import logging
import mimetypes
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
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

log = logging.getLogger("dboard")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

_APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Ensure the web manifest is served with the spec-recommended MIME type
mimetypes.add_type("application/manifest+json", ".webmanifest")

app = FastAPI()
templates = Jinja2Templates(directory=os.path.join(_APP_DIR, "templates"))
_static_dir = os.path.join(_APP_DIR, "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")
executor = ThreadPoolExecutor(max_workers=16)

# Content-Security-Policy: only self-hosted assets execute. No inline <script>,
# no third-party CDNs. 'unsafe-inline' is allowed for styles only because the
# template uses inline style= attributes and a <style> block (CSS cannot exfiltrate).
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "worker-src 'self'; "
    "manifest-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = _CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return resp


# Docker client is created lazily and cached. The Docker API is reached over
# TCP via the read-only socket-proxy, which may not be resolvable yet at import
# time; retrying on demand avoids permanently disabling container stats if the
# proxy comes up a moment later.
_docker = None
_docker_lock = threading.Lock()


def _dock():
    global _docker
    if _docker is None:
        with _docker_lock:
            if _docker is None:
                try:
                    client = docker.from_env()
                    client.ping()
                    _docker = client
                except Exception as e:
                    log.warning("Docker connect failed (will retry): %s", e)
                    _docker = None
    return _docker


# ── Background sampling ───────────────────────────────────────────────────────
# When SAMPLE_INTERVAL > 0 a task samples metrics on a fixed cadence regardless
# of open dashboards, so history is recorded even when nobody is watching. The
# API endpoints then serve the latest in-memory snapshot (no per-request gather,
# even cadence, single DB writer). When 0, metrics are gathered on demand per
# request as before — zero load while idle.
def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int((os.environ.get(name) or "").strip() or default))
    except ValueError:
        return default


SAMPLE_INTERVAL = _env_int("SAMPLE_INTERVAL", 0)
_latest_lock = threading.Lock()
_latest_system: dict | None = None
_latest_containers: dict | None = None


async def _sampler():
    """Periodically sample system + container metrics into the latest snapshot."""
    global _latest_system, _latest_containers
    loop = asyncio.get_event_loop()
    log.info("Background sampling enabled (every %ds)", SAMPLE_INTERVAL)
    while True:
        try:
            sysd = await loop.run_in_executor(executor, _system_stats_sync)
            cond = await _collect_containers()
            with _latest_lock:
                _latest_system = sysd
                _latest_containers = cond
        except Exception as e:
            log.warning("Sampler error: %s", e)
        await asyncio.sleep(SAMPLE_INTERVAL)


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
    asyncio.create_task(_db_pruner())
    if SAMPLE_INTERVAL > 0:
        asyncio.create_task(_sampler())


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
        d = _dock()
        if not d:
            return {}
        c = d.containers.get(container_id)
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

# Each entry: (probe path inside the container, label to display).
# These map to narrow, non-sensitive read-only bind mounts in docker-compose —
# NOT the whole host root filesystem. statvfs() reports filesystem-level usage,
# so an empty probe directory on a given fs yields that fs's stats without
# exposing any of its files. Add more (mount, label) pairs as needed.
_DISK_PROBES = [
    ("/host/root", "/"),
    ("/host/boot", "/boot"),
]


def _host_disks() -> list[dict]:
    """Report usage of the host filesystems exposed via narrow /host/* mounts."""
    disks = []
    seen_devs: set[int] = set()
    for path, label in _DISK_PROBES:
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
            disks.append({
                "mount": label,
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
# Guards _io_prev and the _spark ring buffers, which are read-modify-written
# from executor threads (concurrent /api/system requests would otherwise race).
_io_lock = threading.Lock()

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
# Previous cumulative net counters per container, for computing bytes/sec rates.
_container_io_prev: dict[str, dict] = {}

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
                mem  REAL,
                net_rx INTEGER,
                net_tx INTEGER,
                mem_mb REAL
            );
            CREATE INDEX IF NOT EXISTS con_ts_name ON container_metrics(ts, name);
            CREATE INDEX IF NOT EXISTS con_name_ts ON container_metrics(name, ts);
        """)
        # Migration: add columns to a container_metrics table created before they
        # existed (net_rx/net_tx = bytes/sec rates; mem_mb = absolute MB, summable
        # across containers for the stacked chart, unlike the per-limit mem %).
        have = {r[1] for r in conn.execute("PRAGMA table_info(container_metrics)")}
        for col, typ in (("net_rx", "INTEGER"), ("net_tx", "INTEGER"), ("mem_mb", "REAL")):
            if col not in have:
                conn.execute(f"ALTER TABLE container_metrics ADD COLUMN {col} {typ}")
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
                "SELECT cpu,mem,net_rx,net_tx FROM container_metrics "
                "WHERE name=? ORDER BY ts DESC LIMIT ?",
                (name, _CONTAINER_SPARK_N)
            ).fetchall()
            buf = {k: deque(maxlen=_CONTAINER_SPARK_N) for k in ("cpu", "mem", "net_rx", "net_tx")}
            for cv, mv, rxv, txv in reversed(c_rows):
                if cv  is not None: buf["cpu"].append(cv)
                if mv  is not None: buf["mem"].append(mv)
                if rxv is not None: buf["net_rx"].append(rxv)
                if txv is not None: buf["net_tx"].append(txv)
            _container_spark[name] = buf

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
    rows = [(ts, e["name"], e.get("cpu_percent"), e.get("mem_percent"),
             e.get("net_rx_rate"), e.get("net_tx_rate"), e.get("mem_mb")) for e in entries]
    try:
        with _db_lock:
            _db().executemany(
                "INSERT INTO container_metrics(ts,name,cpu,mem,net_rx,net_tx,mem_mb) "
                "VALUES(?,?,?,?,?,?,?)", rows
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
        with _io_lock:
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

    # Update sparkline ring buffers (shared across executor threads)
    with _io_lock:
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

# Global throttle for forced refresh. Without it, an unauthenticated caller
# could spam ?refresh=true and burn the real upstream rate limits / paid
# balances of the configured API keys. Honour a forced refresh at most once
# per interval, server-wide.
_REFRESH_MIN_INTERVAL = 60  # seconds
_last_forced_refresh = 0.0
_refresh_lock = threading.Lock()


def _key_hint(key: str) -> str:
    # Reveal as little as possible: only enough to tell two keys apart.
    if len(key) <= 12:
        return "···"
    return f"{key[:4]}···{key[-4:]}"


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


@app.get("/sw.js")
async def service_worker():
    # Served from the root so the worker can control the whole origin (a worker
    # under /static/ would be scoped to /static/ only). 'no-cache' keeps the
    # worker script itself fresh; Service-Worker-Allowed pins the broad scope.
    return FileResponse(
        os.path.join(_static_dir, "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


def _history_sync(name: str, rng: int) -> dict:
    """Downsampled container history from SQLite for the last `rng` seconds."""
    target_points = 160
    bucket = max(1, rng // target_points)
    frm = int(time.time()) - rng
    with _db_lock:
        rows = _db().execute(
            "SELECT (ts/?)*?, avg(cpu), avg(mem), avg(net_rx), avg(net_tx) "
            "FROM container_metrics WHERE name=? AND ts>=? GROUP BY ts/? ORDER BY ts/?",
            (bucket, bucket, name, frm, bucket, bucket)
        ).fetchall()
    cpu, mem, nrx, ntx = [], [], [], []
    for _b, c, m, rx, tx in rows:
        cpu.append(round(c, 1) if c is not None else None)
        mem.append(round(m, 1) if m is not None else None)
        nrx.append(round(rx) if rx is not None else None)
        ntx.append(round(tx) if tx is not None else None)
    return {"name": name, "range": rng, "interval": bucket,
            "cpu": cpu, "mem": mem, "net_rx": nrx, "net_tx": ntx}


@app.get("/api/history")
async def api_history(name: str, range: int = 3600):
    rng = max(60, min(int(range), _DB_RETENTION))
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _history_sync, name, rng)


def _system_history_sync(rng: int) -> dict:
    """Downsampled host history from SQLite for the last `rng` seconds."""
    bucket = max(1, rng // 160)
    frm = int(time.time()) - rng
    with _db_lock:
        rows = _db().execute(
            "SELECT avg(cpu),avg(mem),avg(temp),avg(net_rx),avg(net_tx),avg(disk_r),avg(disk_w) "
            "FROM sys_metrics WHERE ts>=? GROUP BY ts/? ORDER BY ts/?",
            (frm, bucket, bucket)
        ).fetchall()
    keys = ("cpu", "mem", "temp", "net_rx", "net_tx", "disk_r", "disk_w")
    out = {k: [] for k in keys}
    for row in rows:
        for k, v in zip(keys, row):
            out[k].append((round(v, 1) if k in ("cpu", "mem", "temp") else round(v))
                          if v is not None else None)
    out["range"] = rng
    out["interval"] = bucket
    return out


@app.get("/api/system-history")
async def api_system_history(range: int = 3600):
    rng = max(60, min(int(range), _DB_RETENTION))
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _system_history_sync, rng)


_STACK_TOP = 8  # keep the largest N containers; fold the rest into "other"
_STACK_EXPR = {"cpu": "avg(cpu)", "mem": "avg(mem_mb)", "net": "avg(net_rx)+avg(net_tx)"}


def _stack_sync(metric: str, rng: int) -> dict:
    """Per-container series aligned to common time buckets, for a stacked chart."""
    if metric not in _STACK_EXPR:
        metric = "cpu"
    bucket = max(1, rng // 120)
    frm = int(time.time()) - rng
    with _db_lock:
        rows = _db().execute(
            f"SELECT ts/?, name, {_STACK_EXPR[metric]} FROM container_metrics "
            "WHERE ts>=? GROUP BY ts/?, name ORDER BY ts/?",
            (bucket, frm, bucket, bucket)
        ).fetchall()

    buckets = sorted({r[0] for r in rows})
    bidx = {b: i for i, b in enumerate(buckets)}
    data: dict[str, list] = {}
    for b, name, val in rows:
        data.setdefault(name, [0.0] * len(buckets))[bidx[b]] = round(val or 0, 2)

    totals = {n: sum(v) for n, v in data.items()}
    names = sorted(data, key=lambda n: totals[n], reverse=True)
    out = [{"name": n, "data": data[n]} for n in names[:_STACK_TOP]]
    if len(names) > _STACK_TOP:
        other = [0.0] * len(buckets)
        for n in names[_STACK_TOP:]:
            for i, v in enumerate(data[n]):
                other[i] += v
        out.append({"name": "other", "data": [round(x, 2) for x in other]})

    return {"metric": metric, "range": rng, "interval": bucket,
            "count": len(buckets), "containers": out}


@app.get("/api/stack")
async def api_stack(metric: str = "cpu", range: int = 3600):
    rng = max(60, min(int(range), _DB_RETENTION))
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _stack_sync, metric, rng)


@app.get("/api/system")
async def api_system():
    if SAMPLE_INTERVAL > 0:
        with _latest_lock:
            if _latest_system is not None:
                return _latest_system
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _system_stats_sync)


@app.get("/api/tokens")
async def api_tokens(refresh: bool = False):
    now = time.monotonic()

    # Throttle forced refreshes globally so the upstream APIs can't be hammered.
    if refresh:
        global _last_forced_refresh
        with _refresh_lock:
            if now - _last_forced_refresh < _REFRESH_MIN_INTERVAL:
                refresh = False  # too soon — serve from cache instead
            else:
                _last_forced_refresh = now

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
    if SAMPLE_INTERVAL > 0:
        with _latest_lock:
            if _latest_containers is not None:
                return _latest_containers
    return await _collect_containers()


async def _collect_containers() -> dict:
    """Gather all container stats, update sparklines, persist, return the payload."""
    d = _dock()
    if not d:
        return {"error": "Docker socket not available", "proxied": [], "others": []}

    try:
        all_containers = d.containers.list(all=True)
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
        now = time.monotonic()
        for p, stats in zip(all_running, results):
            p.update(stats)
            name = p["name"]
            if name not in _container_spark:
                _container_spark[name] = {
                    k: deque(maxlen=_CONTAINER_SPARK_N)
                    for k in ("cpu", "mem", "net_rx", "net_tx")
                }
            if p.get("cpu_percent") is not None:
                _container_spark[name]["cpu"].append(p["cpu_percent"])
            if p.get("mem_percent") is not None:
                _container_spark[name]["mem"].append(p["mem_percent"])

            # Network rate (bytes/sec) from the delta of cumulative counters.
            if p.get("net_rx") is not None:
                prev = _container_io_prev.get(name)
                dt = now - prev["t"] if prev else 0
                if prev and dt > 0:
                    rx_rate = max(0, round((p["net_rx"] - prev["rx"]) / dt))
                    tx_rate = max(0, round((p["net_tx"] - prev["tx"]) / dt))
                    p["net_rx_rate"] = rx_rate
                    p["net_tx_rate"] = tx_rate
                    _container_spark[name]["net_rx"].append(rx_rate)
                    _container_spark[name]["net_tx"].append(tx_rate)
                _container_io_prev[name] = {"t": now, "rx": p["net_rx"], "tx": p["net_tx"]}

    # Persist container stats to DB (fire-and-forget)
    if all_running:
        loop.run_in_executor(executor, _db_write_containers, all_running)

    for p in proxied + others:
        name = p["name"]
        if name in _container_spark:
            p["cpu_spark"] = list(_container_spark[name]["cpu"])
            p["mem_spark"] = list(_container_spark[name]["mem"])
            p["net_rx_spark"] = list(_container_spark[name]["net_rx"])
            p["net_tx_spark"] = list(_container_spark[name]["net_tx"])
        p.pop("_id", None)

    proxied.sort(key=lambda x: (x["status"] != "running", x["name"].lower()))
    others.sort(key=lambda x: (x["status"] != "running", x["name"].lower()))

    return {
        "proxied": proxied,
        "others": others,
        "running_proxied": sum(1 for p in proxied if p["status"] == "running"),
        "running_others": sum(1 for p in others if p["status"] == "running"),
        "sample_interval": SAMPLE_INTERVAL or 5,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
