# EMA-RSI Intraday Algo — DO App Platform image (live paper-trading service).
# Replaces the Python buildpack so Chromium's system libraries can be
# installed for the automated Kite TOTP login (playwright headless flow).
# bookworm pin: playwright 1.52's --with-deps knows Debian 12 package names
# (trixie renamed ttf-unifont -> fonts-unifont and the install exits 100)
FROM python:3.11-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

WORKDIR /app

# C toolchain for source-only deps (lz4 3.1.3 via truedata-ws has no wheel)
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer (cached until pyproject/uv.lock change)
COPY pyproject.toml uv.lock .python-version* ./
RUN uv sync --locked --no-default-groups

ENV PATH="/app/.venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Chromium + its OS libraries (--with-deps runs apt-get; root inside image)
RUN uv run --no-sync playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8080
CMD ["sh", "-c", "cd ema-rsi-intraday-algo/backend && uvicorn app.api.server:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --loop uvloop --http httptools --log-level warning --access-log"]
