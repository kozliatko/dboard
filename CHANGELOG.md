# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.8] - 2026-07-23

### Added
- **Multiple tokens of the same type** — append a double-underscore label to
  any token env var to monitor several accounts/keys side by side, e.g.
  `ANTHROPIC_API_KEY__work` and `ANTHROPIC_API_KEY__personal` appear as
  separate cards "Anthropic (work)" and "Anthropic (personal)". The plain
  env var continues to work unchanged; all existing setups require no
  migration. Documented in `.env.example`.

## [0.3.7] - 2026-07-23

### Added
- **Cloudflare AI neuron usage** — the Tokens tab now shows daily neuron
  consumption and remaining quota (used / remaining against the 10,000/day
  free tier) via the Cloudflare GraphQL Analytics API
  (`aiInferenceAdaptiveGroups / sum.totalNeurons`). Requires the
  **Account Analytics: Read** permission on the token; falls back silently
  when the permission is absent so existing tokens continue to work.
- `_http_post()` helper for JSON POST requests used by the GraphQL call.

## [0.3.6] - 2026-07-23

### Added
- **Cloudflare AI token validator** — validates via
  `GET /accounts/{id}/ai/models/search`; shows total model count and top 3
  task categories (Text Generation, Text-to-Image, Text Embeddings, …).
  Requires two env vars: `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`.

## [0.3.5] - 2026-07-22

### Fixed
- Tokens tab not hidden when no API keys configured — `.tab-btn { display:flex }`
  overrode the HTML `hidden` attribute. Replaced with `style="display:none"` on
  both server-side render (Jinja2) and JS toggle so the rule always wins.
- Update notification banner always visible due to `display:flex` in inline style
  conflicting with the `hidden` attribute — replaced with `display:none` default,
  shown via `style.display = 'flex'` from JS.
- SW cache bumped to `dboard-shell-v4` to force browser eviction of stale HTML
  and JS after the server-side tokens tab fix.

### Added
- Git commit SHA displayed next to version in header (e.g. `v0.3.4 (db85eec)`),
  sourced from `GIT_COMMIT` build arg set to `github.sha` in CI.

## [0.3.4] - 2026-07-21

### Added
- **Version badge in header** — current release (read from `VERSION` file at
  startup) is displayed next to the dboard logo in grey monospace text.
- **Update notification banner** — when a new service worker activates (i.e.
  a new release is deployed), a fixed bottom-right banner appears with a
  **Reload** button and a dismiss **✕**. Clicking Reload fetches the fresh
  shell from the new cache. The banner is shown only on real updates, not on
  the first install.

### Changed
- SW cache bumped from `dboard-shell-v2` to `dboard-shell-v3` to force
  eviction of the old shell after the tokens tab hide change.

## [0.3.3] - 2026-07-21

### Added
- **Groq API key validator** — validates via `GET /openai/v1/models`; shows
  total model count and Llama model names. Uses `User-Agent: curl/8.5.0` to
  bypass Cloudflare bot protection on `api.groq.com` from inside Docker.
  Configured via `GROQ_API_KEY` environment variable.
- **Tokens tab auto-hide** — the Tokens tab (button and panel) is hidden
  entirely when no API keys are configured. If the tab was last active and
  keys are removed, the view falls back to Containers automatically.

## [0.3.2] - 2026-07-04

### Changed
- Updated all dashboard screenshots in documentation; added Networks tab screenshot.

## [0.3.1] - 2026-07-04

### Added
- **Networks tab** — dedicated fourth tab showing all Docker networks with driver
  type (bridge/host/overlay/macvlan/null), scope, internal flag, container count
  and per-network container name tags. A live badge in the tab bar shows the
  total network count. The section is built from `/networks` metadata combined
  with network membership data already present in container attrs (the `/networks`
  list endpoint omits the Containers field, so the mapping is derived from
  `NetworkSettings.Networks` on each container).
- `NETWORKS: 1` added to `socket-proxy` environment in both compose files to
  allow the Docker Networks API endpoint.
- `networks` key added to the `/api/containers` response: list of network objects
  (`name`, `id`, `driver`, `scope`, `internal`, `container_count`,
  `container_names`) sorted by driver class then by container count descending.

## [0.3.0] - 2026-06-24

### Added
- **Demo deployment mode** (`docker-compose.demo.yml`): plain HTTP on port 8700,
  no Caddy required, no auth, no compression — intended for quick local
  evaluation on a trusted network. Documented as insecure in the README with a
  feature-comparison table.
- Health check on `socket-proxy`: port 2375 is probed with `nc -z` every 30 s.
  `dboard` uses `depends_on: condition: service_healthy` so it waits for a
  confirmed-healthy proxy before starting, eliminating the race condition on
  stack restart. `nc` is used instead of `wget` to avoid interception by a
  transparent HTTP proxy (e.g. Squid).
- Hide the **Proxied** section in the Containers tab when no proxied containers
  exist — the entire block (header, filter, table) is suppressed rather than
  showing an empty table.
- Global stacked resource chart at the top of the Containers tab: per-container
  CPU / memory / network usage stacked over time, with metric and range
  (10m / 1h / 6h / 24h) toggles and a legend. Hovering shows a vertical guide,
  highlights the band under the cursor and a tooltip with the container name,
  its value and the time at that point. Backed by a new `GET /api/stack`
  endpoint (top 8 containers by usage + an aggregated `other` band). Adds a
  `mem_mb` column to `container_metrics` so memory can be summed in absolute MB
  (with an automatic migration).
- System detail overlay: clicking any System-tab card opens a combined host
  overlay with larger CPU, memory, CPU-temp (if available), network-I/O and
  disk-I/O charts, the same live / 1h / 6h / 24h range selector, and host
  metadata. Backed by a new `GET /api/system-history` endpoint over `sys_metrics`.
- Selectable time range in the container detail overlay: **live / 1h / 6h / 24h**.
  The live view uses the in-memory ring buffer (~10 min, auto-updating); longer
  ranges are served by a new `GET /api/history` endpoint that reads and
  downsamples `container_metrics` from SQLite (up to the 24h retention). Each
  chart's X axis reflects the actual data extent.
- Container detail overlay: clicking a container row opens a Beszel-style modal
  with larger live CPU, memory and network-rate area charts, network I/O totals
  and metadata. Charts now have a time (X) axis with labels and a gridline.
  Updates live while open; closes on backdrop click, the × button or Escape.
- Per-container network history: `container_metrics` gains `net_rx`/`net_tx`
  columns (bytes/sec rates, like `sys_metrics`), with an automatic migration for
  existing databases. Drives the network chart in the detail overlay.
- Optional background metrics sampling (`SAMPLE_INTERVAL`, default 30s, set 0 to
  disable). When enabled, a task samples system + container metrics on a fixed
  cadence so history is recorded even with no dashboard open; the API endpoints
  then serve the latest in-memory snapshot (single DB writer, even cadence).
- Installable Progressive Web App: an app-shell service worker (`/sw.js`,
  served at root scope) caches the static shell with stale-while-revalidate and
  bypasses `/api/*` so metrics stay live; works offline. Manifest gains an `id`
  and maskable icons; CSP gains `worker-src`/`manifest-src`.
- Application icon: SVG master plus favicon (`.ico` + PNG), Apple touch icon and
  PWA manifest icons, wired into the page `<head>` with `theme-color`. Also shown
  as the logo in the page header.
- Documentation: how the disk-probe mechanism works and a step-by-step guide for
  monitoring additional separate disks.

### Changed
- Both compose files now reference the pre-built image
  `ghcr.io/kozliatko/dboard:latest` from GHCR instead of building locally.
  Deployment is now `docker compose pull && docker compose up -d` with no
  build toolchain required on the host.
- `NO_PROXY` / `no_proxy` set to `socket-proxy,localhost,127.0.0.1` in both
  compose files so the Docker SDK bypasses any corporate HTTP proxy (e.g.
  Squid) for inter-container traffic to `socket-proxy:2375`.
- CI: `provenance: false` and `sbom: false` added to `build-push-action` to
  prevent untagged attestation manifests from appearing as GHCR versions and
  confusing the version-prune step.
- CI: GHCR version pruning replaced the `actions/delete-package-versions@v5`
  action (Node 20, no v6 available) with a plain `gh api` shell script — no
  Node.js runtime needed, no deprecation warnings.
- Dockerfile assets stage switched from `debian:12-slim` + `apt-get` to
  BuildKit `ADD <url>` — downloads the Tailwind CLI via the Docker daemon HTTP
  client, bypassing container network isolation without needing a package
  manager.

## [0.2.0] - 2026-06-22

Security hardening release. **Contains breaking deployment changes — see
_Upgrade notes_ below.**

### Added
- Read-only Docker API access via `tecnativa/docker-socket-proxy`. dboard
  reaches the Docker API over TCP with only `GET containers`/`version`
  whitelisted; all writes (POST) are blocked (403).
- HTTP Basic Auth in front of every route, configured with `caddy-docker-proxy`
  labels.
- Strict `Content-Security-Policy` (`script-src 'self'`, no inline scripts, no
  third-party CDNs) plus `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy` and `Permissions-Policy` on every response.
- Self-hosted Tailwind CSS compiled at image build time (replaces the
  `cdn.tailwindcss.com` runtime engine).
- Server-wide throttle for forced token re-validation (`?refresh=true`) to
  protect upstream rate limits and paid balances.
- `gitleaks` (secret scan) and `trivy` (dependency/config vuln scan) jobs in CI,
  gating the image build.
- Non-root container (uid 10001) with `cap_drop: ALL`, `no-new-privileges` and a
  read-only root filesystem.

### Changed
- **BREAKING:** Routing now uses `caddy-docker-proxy` labels on a dedicated
  hostname served at the root path. The `/dboard` path prefix has been removed
  (API is now at `/api/*`).
- Disk stats now use narrow read-only mounts (an empty probe directory on `/`
  plus `/boot`) instead of mounting the entire host root filesystem.
- Frontend JavaScript externalized to `/static/app.js`; inline event handlers
  replaced with `addEventListener` wiring (required by the strict CSP).
- Docker SDK client is now created lazily, shared, and retried instead of
  re-created on every stats call.
- Key hint reduced from `first8···last4` to `first4···last4`.
- All Python dependencies pinned to exact versions.

### Removed
- Caddy admin-API self-injection (which replaced the entire routes array via
  `DELETE` + `PUT`, risking an outage of every site behind Caddy) and the
  associated container `exec` calls.
- Direct `docker.sock` mount and the full `/:/host` root filesystem mount on the
  dboard container.

### Fixed
- Race condition on the shared `_io_prev` and sparkline ring buffers under
  concurrent requests (now guarded by a lock).

### Security
- A read-only bind mount of `docker.sock` does **not** restrict the Docker API;
  it stays read-write at the API level. This release routes all Docker access
  through a proxy that genuinely blocks writes, removing the container-escape
  path.
- The host root filesystem (`/etc`, `/root`, `/home`, SSH keys, other projects'
  `.env` files) is no longer exposed to the container.

### Upgrade notes
- Create the disk-probe directory on the host once:
  `sudo mkdir -p /opt/dboard/diskprobe`.
- Set your hostname and a Basic Auth hash in `docker-compose.yml`
  (`docker exec caddy caddy hash-password --plaintext '…'`; double every `$` to
  `$$`).
- The `dboard_data` volume must be recreated so the non-root user can write to
  it: `docker compose down && docker volume rm dboard_dboard_data`.
- Bookmarks to `/dboard/…` no longer work — use the new hostname root.

## [0.1.0] - 2026-06-22

Initial release.

### Added
- Three-tab dashboard: Containers, System, Tokens (tab state persisted in
  `localStorage`).
- Container tables (proxied / other) with sort, filter, and per-column
  sparklines for status, health, uptime, CPU, RAM and network I/O.
- System panel: live CPU, RAM, swap, disk, network I/O, disk I/O rates and CPU
  temperature, with SVG sparklines and warn/crit visual thresholds.
- API token validation for Anthropic, GitHub, GitLab, Gemini, OpenAI, DeepSeek
  and Tavily — verifies validity and shows service metadata without ever
  revealing the raw key.
- SQLite persistence of sparkline history (WAL mode, 24-hour retention,
  restored into ring buffers on startup).
- Docker `HEALTHCHECK` on `/`.
- GitHub Actions CI: test → build → push to GHCR.
- 57 unit tests covering helpers, token validators and the persistence layer.
- Anonymized dashboard screenshots in the documentation.

[Unreleased]: https://github.com/kozliatko/dboard/compare/v0.3.8...HEAD
[0.3.8]: https://github.com/kozliatko/dboard/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/kozliatko/dboard/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/kozliatko/dboard/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/kozliatko/dboard/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/kozliatko/dboard/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/kozliatko/dboard/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/kozliatko/dboard/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/kozliatko/dboard/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/kozliatko/dboard/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kozliatko/dboard/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kozliatko/dboard/releases/tag/v0.1.0
