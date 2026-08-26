FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends adb ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install .

RUN useradd --create-home --uid 10001 agent \
    && mkdir -p /app/data \
    && chown -R agent:agent /app
USER agent

EXPOSE 8787
CMD ["semantic-mobile-agent"]
