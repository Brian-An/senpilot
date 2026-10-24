FROM ghcr.io/astral-sh/uv:0.12.9 AS uv
FROM python:3.12-slim-bookworm

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project \
    && .venv/bin/python -m playwright install --with-deps chromium \
    && apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 senpilot
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && chown -R senpilot:senpilot /app
USER senpilot
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec uv run --no-sync uvicorn senpilot.web:app --host 0.0.0.0 --port ${PORT:-8000}"]
