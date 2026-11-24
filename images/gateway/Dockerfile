# The gateway. Build context: repo root.
FROM python:3.12-slim
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "pyyaml>=6" "jsonschema>=4.21" "httpx>=0.27" "confluent-kafka>=2.4" \
 && useradd --uid 1000 --user-group --no-create-home agent
WORKDIR /app
COPY configui/__init__.py configui/store.py /app/configui/
COPY configui/schemas /app/configui/schemas
COPY harness_core /app/harness_core
COPY gateway /app/gateway
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
CMD ["python", "-m", "gateway"]
