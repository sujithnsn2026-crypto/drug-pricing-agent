# The hosted app: web UI + Ask agent + MCP endpoint (/mcp), one process, no Spark.
# Gold data is NOT baked in: it is mounted at /data/gold (Azure Files) so a weekly data
# refresh needs no new image. Without a mount it falls back to the synthetic eval fixture.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DPA_GOLD_DIR=/data/gold DPA_PRICEWATCH_DIR=/data/pricewatch_runs

WORKDIR /app
# Dependencies first, so code-only changes reuse the cached layer.
COPY pyproject.toml ./
RUN mkdir -p src/dpa && touch src/dpa/__init__.py \
 && pip install -e ".[agent]" && rm -rf src
COPY src ./src
COPY prompts ./prompts
COPY config ./config
COPY evals/fixture ./evals/fixture
RUN pip install --no-deps -e . \
 && useradd --uid 10001 --no-create-home app && mkdir -p /data && chown app /data
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4)"
CMD ["uvicorn", "dpa.ai.web:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
