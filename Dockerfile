ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}

ARG ARCH=amd64
ARG JETPACK=false
ARG PROXY

ENV DEBIAN_FRONTEND=noninteractive

RUN chmod 1777 /tmp && apt-get update && apt-get install -y \
    curl wget ca-certificates \
    tar zstd \
    && update-ca-certificates && rm -rf /var/lib/apt/lists/*

# 通用包（Zstandard tarball）
RUN set -eux; \
    URL="https://ollama.com/download/ollama-linux-${ARCH}.tar.zst"; \
    echo "Downloading Ollama tar.zst from ${URL} ..."; \
    for i in 1 2 3 4 5; do \
      if [ -n "${PROXY:-}" ]; then \
        echo "using proxy ${PROXY:-<unset>}"; \
        http_proxy="${PROXY:-}" https_proxy="${PROXY:-}" \
        HTTP_PROXY="${PROXY:-}" HTTPS_PROXY="${PROXY:-}" \
        wget -O /tmp/ollama.tar.zst \
          --timeout=30 --tries=3 \
          --retry-on-http-error=429,500,502,503,504 \
          --no-cache --no-cookies --server-response \
          "$URL" && break; \
      else \
        wget -O /tmp/ollama.tar.zst \
          --timeout=30 --tries=3 \
          --retry-on-http-error=429,500,502,503,504 \
          --no-cache --no-cookies --server-response \
          "$URL" && break; \
      fi; \
      echo "retry $i ..."; sleep 2; \
      rm -f /tmp/ollama.tar.zst; \
    done; \
    test -s /tmp/ollama.tar.zst; \
    # 按官方包结构解压到 /usr（包内通常带 usr/bin、usr/lib 等路径） \
    tar --zstd -xf /tmp/ollama.tar.zst -C /usr; \
    # 兜底：确保 PATH 上能找到（一般 /usr/bin 已在 PATH） \
    if [ -x /usr/bin/ollama ]; then ln -sf /usr/bin/ollama /usr/local/bin/ollama; fi; \
    chmod +x /usr/bin/ollama || true

# Jetson 专用包（按需）
RUN set -eux; \
    if [ "${JETPACK}" = "true" ]; then \
      JURL="https://ollama.com/download/ollama-linux-arm64-jetpack6.tar.zst"; \
      echo "Installing Jetson-specific Ollama build from ${JURL}"; \
      for i in 1 2 3 4 5; do \
        if [ -n "${PROXY:-}" ]; then \
          http_proxy="${PROXY}" https_proxy="${PROXY}" HTTP_PROXY="${PROXY}" HTTPS_PROXY="${PROXY}" \
          wget -O /tmp/ollamaj.tar.zst \
            --timeout=30 --tries=3 \
            --retry-on-http-error=429,500,502,503,504 \
            --no-cache --no-cookies --server-response \
            "$JURL" && break; \
        else \
          wget -O /tmp/ollamaj.tar.zst \
            --timeout=30 --tries=3 \
            --retry-on-http-error=429,500,502,503,504 \
            --no-cache --no-cookies --server-response \
            "$JURL" && break; \
        fi; \
        echo "retry $i ..."; sleep 2; \
        rm -f /tmp/ollamaj.tar.zst; \
      done; \
      test -s /tmp/ollamaj.tar.zst; \
      tar --zstd -xf /tmp/ollamaj.tar.zst -C /usr; \
      if [ -x /usr/bin/ollama ]; then ln -sf /usr/bin/ollama /usr/local/bin/ollama; fi; \
      chmod +x /usr/bin/ollama || true; \
    fi

# ✅ 清除构建期代理环境变量
ENV http_proxy= \
    https_proxy= \
    HTTP_PROXY= \
    HTTPS_PROXY= \
    no_proxy= \
    NO_PROXY=