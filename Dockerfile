FROM python:3.12-slim

# uv, pinned, copied from its official image.
COPY --from=ghcr.io/astral-sh/uv:0.9.0 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    BOARD_DB=/data/board.db

WORKDIR /app

# Dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY board ./board
COPY client ./client
RUN uv sync --frozen --no-dev

# Non-root. /data is created and owned here, so a new named volume mounted on
# it inherits this ownership.
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin board \
    && mkdir -m 700 /data && chown board:board /data
USER board
VOLUME /data
EXPOSE 8787

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4).status == 200 else 1)"]

# --no-access-log keeps invite and setup codes out of the logs. Forwarded
# headers are trusted ('*') ONLY because port 8787 is never published: the only
# thing that can reach it is Caddy, on the compose network. Do not add `ports:`
# to the board service.
CMD ["uvicorn", "board.main:app", "--host", "0.0.0.0", "--port", "8787", \
     "--proxy-headers", "--forwarded-allow-ips=*", "--no-access-log"]
