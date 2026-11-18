# ApplyPilot's daily run (prepare) as a container for AWS ECS Fargate.
# Personal data is never baked in: data/ is downloaded from S3 at run time, secrets come from Parameter Store.

# Microsoft's Playwright image: Ubuntu 24.04 + Python 3.12 + Chromium and its system libraries.
# The version must match playwright in uv.lock.
FROM mcr.microsoft.com/playwright/python:v1.65.0-noble

# Time zone data, so "today" and the 7:00 schedule mean your local day.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# uv, the same package manager used on the Mac, from its official image.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1

# 1. Dependencies first: this layer is cached and only rebuilt when pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 2. Then the code (changes often, so it comes last).
COPY README.md ./
COPY jobagent ./jobagent
COPY templates ./templates
RUN uv sync --frozen --no-dev && mkdir -p data output logs && chown -R pwuser:pwuser /app

# Which commit this image was built from; shown in the daily email.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} TZ=Asia/Kolkata

# Run as the image's ordinary user, not root.
USER pwuser
ENTRYPOINT ["/app/.venv/bin/python", "-m", "jobagent", "cloud-prepare"]
