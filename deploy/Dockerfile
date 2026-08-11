# syntax=docker/dockerfile:1
# approvo multi-stage image (~180 MB runtime with kubectl + helm)
#
# Runtime tools:
#   - kubectl (deployer method=kubectl)
#   - helm    (deployer method=helm)
#
# 非 root 运行,数据落 /data,配置读 /app/config.yaml.
ARG PYTHON_VERSION=3.12
ARG HELM_VERSION=v3.14.4
# Base image 前缀:
#   默认 "python" = docker.io/library/python (社区/开源用户)
#   内网 build 传 --build-arg PYTHON_BASE=harbor.internal.example.com/proxy/library/python
ARG PYTHON_BASE=python

# ---------- Stage 1: builder ----------
FROM ${PYTHON_BASE}:${PYTHON_VERSION}-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip wheel --no-cache-dir -w /wheels -r requirements.txt

# ---------- Stage 2: runtime ----------
FROM ${PYTHON_BASE}:${PYTHON_VERSION}-slim AS runtime

ARG HELM_VERSION

# 装 CLI 工具:kubectl (method=kubectl) + helm (method=helm)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && KUBECTL_VER="$(curl -fsSL https://dl.k8s.io/release/stable.txt)" \
    && curl -fsSLo /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VER}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" -o /tmp/helm.tar.gz \
    && tar -zxf /tmp/helm.tar.gz -C /tmp \
    && mv /tmp/linux-amd64/helm /usr/local/bin/helm \
    && chmod +x /usr/local/bin/helm \
    && rm -rf /tmp/helm.tar.gz /tmp/linux-amd64 \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY app ./app
COPY scripts ./scripts

# 非 root 运行
RUN useradd -u 10001 -m -s /bin/false app && chown -R app /app
USER 10001

ENV CONFIG_PATH=/app/config.yaml \
    DB_PATH=/data/release.db \
    PYTHONUNBUFFERED=1

EXPOSE 8700

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8700/healthz',timeout=3); \
sys.exit(0 if r.status==200 else 1)"

LABEL org.opencontainers.image.title="approvo" \
      org.opencontainers.image.description="Feishu-based release approval gate for CI/CD pipelines" \
      org.opencontainers.image.source="https://github.com/Sagaway-Co/Approvo-core" \
      org.opencontainers.image.licenses="Apache-2.0"

CMD ["python", "-m", "app.main"]
