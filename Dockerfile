# Rebuild regularly for Debian security updates; pin the resolved digest in releases.
FROM python:3.14-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    JARVIS_MODE=production JARVIS_HOST=0.0.0.0 JARVIS_PORT=8000 \
    JARVIS_STATE_DIR=/var/lib/jarvis JARVIS_DB_PATH=/var/lib/jarvis/jarvis.sqlite3
RUN apt-get update && apt-get install -y --no-install-recommends git bubblewrap nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 jarvis && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent jarvis \
    && mkdir -p /var/lib/jarvis && chown 10001:10001 /var/lib/jarvis && chmod 700 /var/lib/jarvis
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
# Explicit allowlist: no .env, Git history, local state, credentials or host venv.
COPY main.py /app/main.py
COPY Dockerfile compose.yaml .dockerignore /app/
COPY core /app/core
COPY models /app/models
COPY agents /app/agents
COPY evaluator /app/evaluator
COPY tools /app/tools
COPY static /app/static
COPY tests /app/tests
COPY docs /app/docs
USER 10001:10001
VOLUME ["/var/lib/jarvis"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "core.deployment_healthcheck"]
STOPSIGNAL SIGTERM
CMD ["python", "-m", "core.deployment_server"]
