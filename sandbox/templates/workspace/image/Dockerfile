FROM python:3.13-slim

WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv ruff ty

CMD ["sleep", "infinity"]