ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}

ARG ARCH=amd64
ARG JETPACK=false
ARG PROXY

ENV DEBIAN_FRONTEND=noninteractive

RUN chmod 1777 /tmp && apt-get update && apt-get install -y \
    curl wget ca-certificates \
    tar \
    &&  update-ca-certificates && rm -rf /var/lib/apt/lists/*

# COPY ollama/ /tmp/

# 通用包
RUN set -eux; \
    URL="https://ollama.com/download/ollama-linux-${ARCH}.tgz"; \
    echo "Downloading Ollama tgz from ${URL} ..."; \
    for i in 1 2 3 4 5; do \
      if [ -n "${PROXY:-}" ]; then \
        echo "using proxy ${PROXY:-<unset>}"; \
        http_proxy="${PROXY:-}" https_proxy="${PROXY:-}" \
        HTTP_PROXY="${PROXY:-}" HTTPS_PROXY="${PROXY:-}" \
        wget -O /tmp/ollama.tgz \
        --timeout=30 --tries=3 \
        --retry-on-http-error=429,500,502,503,504 \
        --no-cache --no-cookies --server-response \
        "$URL" && break; \
      else \
        wget -O /tmp/ollama.tgz \
        --timeout=30 --tries=3 \
        --retry-on-http-error=429,500,502,503,504 \
        --no-cache --no-cookies --server-response \
        "$URL" && break; \
      fi; \
      echo "retry $i ..."; sleep 2; \
      rm -f /tmp/ollama.tgz; \
    done; \
    test -s /tmp/ollama.tgz; \
    mkdir -p /usr/local/ollama; \
    tar -xzf /tmp/ollama.tgz -C /usr/local/ollama; \
    ln -sf /usr/local/ollama/bin/ollama /usr/local/bin/ollama; \
    chmod +x /usr/local/ollama/bin/ollama

# Jetson 专用包（按需）
RUN set -eux; \
    if [ "${JETPACK}" = "true" ]; then \
      JURL="https://ollama.com/download/ollama-linux-arm64-jetpack6.tgz"; \
      echo "Installing Jetson-specific Ollama build from ${JURL}"; \
      for i in 1 2 3 4 5; do \
        if [ -n "${PROXY:-}" ]; then \
          http_proxy="${PROXY}" https_proxy="${PROXY}" HTTP_PROXY="${PROXY}" HTTPS_PROXY="${PROXY}" \
          wget -O /tmp/ollamaj.tgz \
          --timeout=30 --tries=3 \
          --retry-on-http-error=429,500,502,503,504 \
          --no-cache --no-cookies --server-response \
          "$JURL" && break; \
        else \
          wget -O /tmp/ollamaj.tgz \
          --timeout=30 --tries=3 \
          --retry-on-http-error=429,500,502,503,504 \
          --no-cache --no-cookies \
          --server-response "$JURL" && break; \
        fi; \
        echo "retry $i ..."; sleep 2; \
        rm -f /tmp/ollamaj.tgz; \
      done; \
      test -s /tmp/ollamaj.tgz; \
      tar -xzf /tmp/ollamaj.tgz -C /usr/local/ollama; \
    fi

# ✅ 清除构建期代理环境变量
ENV http_proxy= \
    https_proxy= \
    HTTP_PROXY= \
    HTTPS_PROXY= \
    no_proxy= \
    NO_PROXY=

# ✅ 暴露两个服务端口
EXPOSE 11434
EXPOSE 11435

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
  && rm -rf /var/lib/apt/lists/*

# 复制 gateway 文件
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

COPY gateway.py /app/gateway.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# 暴露端口：对外只需要 Gateway 端口
EXPOSE 11535

# Redis 配置（你也可以放到 compose 里传）
ENV REDIS_HOST=172.28.1.1 \
    REDIS_PORT=6379 \
    REDIS_USER=default \
    REDIS_PASSWORD=Rr123456 \
    UPSTREAM_BASE=http://127.0.0.1:11434

CMD ["/app/start.sh"]