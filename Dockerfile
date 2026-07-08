# AlgoTrader Pro — DO App Platform image
# Replaces the Python buildpack so Chromium's system libraries can be
# installed for the automated Kite TOTP login (playwright headless flow).
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

WORKDIR /app

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
CMD ["sh", "-c", "cd algotrader_v4 && uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --loop uvloop --http httptools --log-level warning --access-log"]
