# Development test runtime only. Never build from a candidate-controlled context.
FROM python:3.14-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends nodejs git libseccomp2 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/runtime-requirements.txt
RUN python -m venv /runtime && /runtime/bin/pip install --no-cache-dir -r /tmp/runtime-requirements.txt \
    && rm /tmp/runtime-requirements.txt
COPY guard.py supervisor.py /opt/jarvis/
USER 10001:10001
WORKDIR /workspace
