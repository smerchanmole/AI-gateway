FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/app/.venv/bin:$PATH \
    PRISMA_HOME_DIR=/app/.prisma

RUN apt-get update \
    && apt-get install --no-install-recommends -y openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir -r requirements.txt \
    && DATABASE_URL=postgresql://build:build@127.0.0.1:5432/build \
       /app/.venv/bin/prisma generate \
       --schema /app/.venv/lib/python3.12/site-packages/litellm_proxy_extras/schema.prisma

COPY . .
RUN mkdir -p /app/runtime \
    && chown -R 10001:10001 /app

USER 10001:10001
EXPOSE 8090

CMD ["/app/.venv/bin/python", "/app/app.py"]
