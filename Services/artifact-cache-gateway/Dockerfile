ARG BASE_IMAGE=python:3.10-slim
FROM ${BASE_IMAGE}

USER root
WORKDIR /app
COPY pyproject.toml setup.cfg README.md /app/
COPY src/ /app/src/
COPY config/ /app/config/

ENV PYTHONUNBUFFERED=1 \
    ARTIFACT_CACHE_STORAGE=s3 \
    ARTIFACT_CACHE_DIR=/data/artifact-cache

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
RUN sed -E -i \
      "s|https?://[^[:space:]]+/debian-security|${APT_MIRROR}/debian-security|g; \
       s|https?://[^[:space:]]+/debian|${APT_MIRROR}/debian|g" \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" . \
    && (id -u gateway >/dev/null 2>&1 || useradd --create-home --uid 10001 gateway) \
    && mkdir -p /data/artifact-cache/tmp \
    && chown -R gateway:gateway /data/artifact-cache

USER gateway
EXPOSE 8080

CMD ["artifact-cache-gateway", "--host", "0.0.0.0", "--port", "8080"]
