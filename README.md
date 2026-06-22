# dboard

A lightweight, self-hosted Docker infrastructure dashboard.  
Monitors containers, host system metrics, and API token validity — all in one place.

[![Build](https://github.com/kozliatko/dboard/actions/workflows/build.yml/badge.svg)](https://github.com/kozliatko/dboard/actions/workflows/build.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green)
![SQLite](https://img.shields.io/badge/SQLite-WAL-orange)

---

## Features

- **Three-tab UI** — Containers, System, Tokens
- **Container tables** — sortable and filterable; shows status, health, uptime, CPU, RAM, network I/O with per-column sparklines
- **System panel** — live CPU, RAM, swap, disk, network I/O, disk I/O rates with SVG sparklines and visual thresholds (warn/crit)
- **API token validation** — checks key validity without exposing the raw key value; shows service metadata (rate limits, model lists, account info)
- **SQLite persistence** — sparkline history survives restarts; 24-hour retention
- **Caddy auto-registration** — self-injects `/dboard/*` route via the Caddy admin API; no manual config required
- **HEALTHCHECK** — built-in Docker healthcheck on `/`

---

## Requirements

- Docker + Docker Compose
- [caddy-docker-proxy](https://github.com/lucaslorentz/caddy-docker-proxy) running with the shared `caddy` network

---

## Quick start

```bash
cp .env.example .env
# Optionally fill in API keys for token validation
docker compose up -d
```

The dashboard is available at `https://<your-domain>/dboard/` within a few seconds of startup.

---

## Configuration

### Environment variables

All variables are optional. Leave any blank to disable that token check.

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `GITHUB_TOKEN` | GitHub personal access token |
| `GITLAB_TOKEN` | GitLab personal access token |
| `GITLAB_HOST` | GitLab instance hostname (default: `gitlab.com`) |
| `GEMINI_API_KEY` | Google Gemini API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `DEEPSEEK_API_KEY` | DeepSeek API key |
| `TAVILY_API_KEY` | Tavily search API key |

Copy `.env.example` to `.env` and populate the keys you want validated.

### Volumes

| Mount | Purpose |
|---|---|
| `/var/run/docker.sock:ro` | Docker API access (read-only) |
| `/:/host:ro` | Host filesystem for disk usage stats (read-only) |
| `dboard_data:/app/data` | SQLite database persistence across restarts |

---

## API

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /api/containers` | All containers with live stats and sparkline history |
| `GET /api/system` | Host metrics + sparkline ring buffers |
| `GET /api/tokens` | Token validation results (5-min cache) |
| `GET /api/tokens?refresh=true` | Force re-validation of all tokens |

---

## Tabs

### Containers

Two tables: **Proxied** (containers with Caddy labels, shown with domain links) and **Others** (all remaining containers).

Both tables support:
- **Sort** by any column (click header, click again to reverse)
- **Filter** by name, image, status, health, or domain
- **Per-row color coding** — orange left border at ≥75% resource usage, red background at ≥90%
- **Sparklines** in CPU and RAM columns (last ~100 s of history)

### System

Live host metrics refreshed every 5 seconds. Each stat card shows:
- Current value and percentage bar
- SVG sparkline of the last ~3 minutes
- Warning (orange border) and critical (red pulsing border) thresholds

| Metric | Warn | Crit |
|---|---|---|
| CPU | ≥ 70% | ≥ 90% |
| RAM | ≥ 80% | ≥ 92% |
| Disk | ≥ 80% | ≥ 92% |
| CPU Temp | ≥ 70 °C | ≥ 85 °C |

Network I/O and Disk I/O cards show dual-line sparklines (read vs. write / rx vs. tx).

CPU temperature is shown if the host exposes temperature sensors (`coretemp`, `k10temp`, `acpitz`, or similar).

### Tokens

Validates each configured API key on page load and caches results for 5 minutes.  
A **↻ refresh** button forces immediate re-validation.

Each card shows:
- Green / red status dot
- Key hint in the form `first8chars···last4chars` — the actual key is never rendered
- Service-specific metadata:
  - **Anthropic** — model count, latest model names, request rate limit
  - **GitHub** — username, repo counts, OAuth scopes, expiry, rate limit
  - **GitLab** — host, username, token name, scopes, expiry, last used
  - **Gemini** — model count, Gemini model names
  - **OpenAI** — model count, GPT model names
  - **DeepSeek** — model names, account balance
  - **Tavily** — search API response time

---

## Architecture

```
browser
  │  (every 5 s)
  ├── GET /dboard/api/containers  ──► Docker SDK → container list + docker stats
  ├── GET /dboard/api/system      ──► psutil → CPU/RAM/disk/net/temp
  └── GET /dboard/api/tokens      ──► urllib.request → external APIs (cached 5 min)

FastAPI (uvicorn, port 8000)
  ├── ThreadPoolExecutor  — blocking docker/psutil/urllib calls
  ├── Background task     — Caddy route re-injection every 10 s
  ├── Background task     — SQLite pruner every 1 h
  └── SQLite (WAL)        — /app/data/metrics.db
          ├── sys_metrics        (one row per /api/system call)
          └── container_metrics  (one row × container per /api/containers call)

Caddy
  └── /dboard/* → reverse_proxy dboard:8000 (strip /dboard prefix)
      (route injected at startup via Caddy admin API on localhost:2019)
```

### Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, uvicorn |
| Templating | Jinja2 |
| System metrics | psutil |
| Container metrics | Docker SDK for Python |
| HTTP (token checks) | `urllib.request` (stdlib, no extra deps) |
| Database | SQLite 3 (stdlib), WAL mode |
| Frontend | Vanilla JS, Tailwind CSS (CDN) |
| Fonts | Outfit, JetBrains Mono (Google Fonts) |
| Charts | Inline SVG `<polyline>` — no chart library |

---

## Project structure

```
.
├── app/
│   ├── main.py              # FastAPI app — routes, stats, token validators, Caddy watcher
│   └── templates/
│       └── index.html       # Single-page dashboard (HTML + CSS + JS)
├── docker-compose.yml       # Service definition
├── Dockerfile               # python:3.12-slim, HEALTHCHECK, port 8000
├── requirements.txt         # Python dependencies (no SQLite — stdlib)
├── .env.example             # Environment variable template
└── .gitignore
```

---

## Caddy integration

dboard injects a route into Caddy on startup via the [Caddy admin API](https://caddyserver.com/docs/api) (`localhost:2019`).

The route uses a `path_regexp` matcher (`^/dboard(/|$)`) and strips the `/dboard` prefix before proxying to the container. It is placed at index 0 of the route list so it takes priority.

The route is re-verified every 10 seconds. If Caddy restarts and loses its config, the route is re-injected automatically.

No Caddy labels are needed on the dboard container itself.

---

## Security

| Concern | Mitigation |
|---|---|
| Docker socket exposure | Mounted read-only; only list/inspect/stats operations used |
| Host filesystem access | Mounted read-only; only `os.stat()` and `os.statvfs()` are called — no file reads |
| API key exposure | Keys read from environment; only a redacted hint (`first8···last4`) is ever sent to the browser |
| No authentication | dboard has no built-in auth; protect with Caddy [`basic_auth`](https://caddyserver.com/docs/caddyfile/directives/basic_auth) if needed |
| Secrets in git | `.env` is in `.gitignore`; only `.env.example` (with empty values) is committed |

---

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (Docker socket must be accessible)
uvicorn app.main:app --reload --port 8000

# Rebuild Docker image after code changes
docker compose up -d --build

# View logs
docker compose logs -f
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

MIT
