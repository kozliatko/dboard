# ── Stage 1: compile a static Tailwind stylesheet (no runtime CDN / JIT) ───────
# Replaces the cdn.tailwindcss.com runtime engine, which executed third-party
# JavaScript in the page. The standalone CLI scans our markup + JS for the
# utility classes actually used and emits a plain, self-hosted CSS file.
FROM alpine:3.20 AS assets
WORKDIR /build
ARG TAILWIND_VERSION=v3.4.17
ADD --chmod=755 \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-x64" \
    /usr/local/bin/tailwindcss

COPY app/templates ./templates
COPY app/static ./static
RUN printf 'module.exports = { content: ["./templates/**/*.html","./static/**/*.js"] }\n' > tailwind.config.js \
 && printf '@tailwind base;\n@tailwind components;\n@tailwind utilities;\n' > input.css \
 && tailwindcss -c tailwind.config.js -i input.css -o ./tailwind.css --minify

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .
COPY VERSION /VERSION
# Drop in the compiled stylesheet produced by the assets stage
COPY --from=assets /build/tailwind.css /app/static/tailwind.css

# Run as an unprivileged user. The Docker API is reached over TCP through the
# read-only socket-proxy, so no socket group / root is required. /app/data is
# pre-created and owned by appuser so the named volume inherits that ownership.
RUN useradd -u 10001 -r -s /usr/sbin/nologin appuser \
 && mkdir -p /app/data \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')" || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
