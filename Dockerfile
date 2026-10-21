# Portable container for SugarMCP's HTTP transport. Plain Docker — no platform-specific
# base image or runtime. Runs unchanged on a droplet, a client VM, ECS, or anywhere Docker
# runs. No Sugar URL, credentials, or client-specific values are baked in; everything comes
# from the environment at run time (see .env.example).
FROM python:3.11-slim

# uv is the project's package manager; pip-install it so the image needs no external registry
# beyond PyPI. Deps are resolved from the committed uv.lock for a reproducible build.
RUN pip install --no-cache-dir uv

WORKDIR /app

# Install dependencies first (cached layer) from the lockfile only. --no-install-project: the
# app is run from source (python server.py), not installed as a package. --no-dev: no pytest.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Then the source. Changing code does not bust the dependency layer above.
COPY server.py transport.py ./
COPY sugar/ ./sugar/
COPY tools/ ./tools/

# Run unprivileged. The token cache lives under a writable home the app owns.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /home/app/.sugarmcp \
    && chown -R app:app /home/app /app
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    MCP_TRANSPORT=http \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8000 \
    SUGAR_CACHE_DIR=/home/app/.sugarmcp

EXPOSE 8000

# Liveness probe hits the unauthenticated /health route.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["python", "server.py", "--http"]
