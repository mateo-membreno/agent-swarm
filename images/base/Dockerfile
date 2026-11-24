# Base image for every agent sandbox. Build context: repo root.
FROM python:3.12-slim
RUN useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin agent \
 && mkdir /workspace && chown agent:agent /workspace
COPY sdk/python/harness /opt/sdk/harness
ENV PYTHONPATH=/opt/sdk PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
WORKDIR /workspace
