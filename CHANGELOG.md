# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Application icon: SVG master plus favicon (`.ico` + PNG), Apple touch icon and
  PWA manifest icons, wired into the page `<head>` with `theme-color`.
- Documentation: how the disk-probe mechanism works and a step-by-step guide for
  monitoring additional separate disks.

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

[Unreleased]: https://github.com/kozliatko/dboard/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kozliatko/dboard/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kozliatko/dboard/releases/tag/v0.1.0
