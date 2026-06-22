# Contributing to dboard

Thanks for your interest. dboard is a focused homelab tool — contributions that keep it simple and self-contained are most welcome.

---

## What fits

- Bug fixes
- New token validators (follow the pattern in `_check_*` functions)
- Additional system metrics (psutil-based, no new dependencies)
- UI improvements that stay within the existing tech stack
- Performance improvements

## What doesn't fit

- Authentication / multi-user systems (out of scope — use a reverse proxy)
- External monitoring backends (Prometheus, InfluxDB, etc.)
- New Python dependencies unless there's a compelling reason
- Breaking the single-container, single-file-per-layer architecture

---

## Development setup

```bash
git clone <repo>
cd dboard

pip install -r requirements.txt

# Docker socket must be accessible
uvicorn app.main:app --reload --port 8000
# → http://localhost:8000
```

For a full environment including Caddy:

```bash
cp .env.example .env
docker compose up -d --build
```

---

## Project layout

```
app/main.py          # All backend logic — keep it in one file
app/templates/
  index.html         # All frontend — HTML + CSS + JS, no build step
docker-compose.yml
Dockerfile
requirements.txt
```

The intentional constraint is **no build toolchain** — no npm, no bundler, no transpiler. Tailwind is loaded from CDN. JS is plain ES2017. If a change requires a build step it probably doesn't belong here.

---

## Adding a token validator

1. Add a `_check_<service>(key: str) -> dict` function in `main.py` near the other validators.

   The function must return:
   ```python
   {
       "valid": bool,
       "detail": str,          # short human-readable summary
       "extras": _extras(      # list of {label, value} pairs — never include the key itself
           ("Label", value),
           ...
       ),
   }
   ```
   Use `_http_get()` for all outbound requests (stdlib urllib, no new deps).

2. Register it in `_TOKEN_DEFS`:
   ```python
   {"id": "myservice", "name": "My Service", "env_var": "MYSERVICE_API_KEY", "fn": _check_myservice},
   ```

3. Add the env var to `.env.example` (empty value) and `docker-compose.yml` environment section.

---

## Code style

- Standard library over third-party packages wherever reasonable
- No type: ignore comments — fix the types instead
- No comments explaining *what* the code does — only *why* if it's non-obvious
- JS: vanilla ES2017, no frameworks, no bundler
- CSS: inline `<style>` block in `index.html`, consistent with the existing dark-theme variables

---

## Submitting changes

1. Fork the repo and create a branch: `git checkout -b feature/my-change`
2. Make your changes — keep commits focused
3. Verify the dashboard works end-to-end with `docker compose up -d --build`
4. Open a pull request with a short description of what changed and why

There are no automated tests — verification is done by running the app.

---

## Reporting issues

Open a GitHub issue with:
- What you expected
- What actually happened
- Docker version, host OS
- Relevant log output (`docker compose logs dboard`)

Do **not** include API keys or other secrets in issue reports.
