#!/bin/bash

set -e

# =========================
# 参数解析
# =========================
NO_CACHE=false

while getopts "c" opt; do
    case $opt in
        c)
            NO_CACHE=true
            ;;
        *)
            echo "Usage: $0 [-c]"
            echo "  -c    docker build with --no-cache"
            exit 1
            ;;
    esac
done

# =========================
# 基础变量
# =========================
BASE_IMAGE="ubuntu:22.04"
PLAT="$(dpkg --print-architecture)"
JETPACK=false
JETPACK_MAJOR=""
JETSON_L4T=""
JETSON_THOR=false

# 统一成 ollama 下载用的 ARCH
case "$PLAT" in
    amd64|x86_64)
        ARCH="amd64"
        P="amd"
        ;;
    arm64|aarch64)
        ARCH="arm64"
        P="arm"
        ;;
    *)
        echo "Unsupported architecture: $PLAT"
        exit 1
        ;;
esac

# =========================
# Jetson / JetPack / Thor 检测
# =========================
if [[ -f /proc/device-tree/model ]] && tr -d '\0' </proc/device-tree/model | grep -qi "nvidia jetson"; then
    JETPACK=true
elif uname -a | grep -qi "tegra"; then
    JETPACK=true
fi

if [[ "$JETPACK" == "true" ]]; then
    # 解析 L4T 主版本，比如 R35 / R36 / R38
    if [[ -f /etc/nv_tegra_release ]]; then
        JETSON_L4T="$(sed -n 's/^# R\([0-9]\+\).*/\1/p' /etc/nv_tegra_release | head -n1)"
    fi

    # 再从 model 补充判断 Thor
    MODEL_STR=""
    if [[ -f /proc/device-tree/model ]]; then
        MODEL_STR="$(tr -d '\0' </proc/device-tree/model || true)"
    fi

    # Thor / T5000 / T4000 / AGX Thor
    if echo "$MODEL_STR" | grep -Eqi 'thor|t5000|t4000'; then
        JETSON_THOR=true
    fi

    # 保险起见：L4T R38 也直接判定为 Thor / JetPack 7 线
    if [[ "$JETSON_L4T" == "38" ]]; then
        JETSON_THOR=true
    fi

    case "$JETSON_L4T" in
        35)
            JETPACK_MAJOR="5"
            ;;
        36)
            JETPACK_MAJOR="6"
            ;;
        38)
            JETPACK_MAJOR="7"
            ;;
        *)
            JETPACK_MAJOR="unknown"
            ;;
    esac
fi

# =========================
# BASE_IMAGE 选择
# =========================
if [[ "$JETPACK" == "true" ]]; then
    if [[ "$JETSON_THOR" == "true" ]]; then
        # Thor = JetPack 7 / L4T R38 / Ubuntu 24.04 / SBSA
        # 不再硬套 r36 l4t-jetpack
        BASE_IMAGE="ubuntu:24.04"
    else
        # 老 Jetson 继续走 L4T 容器
        case "$JETPACK_MAJOR" in
            6)
                BASE_IMAGE="nvcr.io/nvidia/l4t-jetpack:r36.4.0"
                ;;
            5)
                BASE_IMAGE="nvcr.io/nvidia/l4t-jetpack:r35.4.1"
                ;;
            *)
                # 未知 Jetson 版本，保守退回 Ubuntu 22.04
                BASE_IMAGE="ubuntu:22.04"
                ;;
        esac
    fi
else
    # 非 Jetson
    if [[ "$ARCH" == "amd64" ]]; then
        BASE_IMAGE="nvidia/cuda:12.8.1-runtime-ubuntu22.04"
    else
        BASE_IMAGE="ubuntu:24.04"
    fi
fi

BUILD_DOCKER="docker build"

echo "Ollama server will be included in the build."
echo "Detected:"
echo "  PLAT=$PLAT"
echo "  ARCH=$ARCH"
echo "  JETPACK=$JETPACK"
echo "  JETPACK_MAJOR=$JETPACK_MAJOR"
echo "  JETSON_L4T=${JETSON_L4T:-unknown}"
echo "  JETSON_THOR=$JETSON_THOR"
echo "  BASE_IMAGE=$BASE_IMAGE"

# =========================
# docker build 参数拼装
# =========================
# =========================
# docker build 参数拼装
# =========================
DOCKER_BUILD_ARGS=(
    --build-arg BASE_IMAGE="$BASE_IMAGE"
    --build-arg PROXY="$PROXY"
    --build-arg ARCH="$ARCH"
    --build-arg JETPACK="$JETPACK"
    --build-arg JETPACK_MAJOR="$JETPACK_MAJOR"
)

if $NO_CACHE; then
    echo "Docker build: --no-cache enabled"
    DOCKER_BUILD_ARGS+=(--no-cache)
fi

export DOCKER_BUILDKIT=0

$BUILD_DOCKER \
    "${DOCKER_BUILD_ARGS[@]}" \
    -t ollama_server \
    .

# =========================
# 版本与推送
# =========================
OLLAMA_VERSION=$(curl -s https://api.github.com/repos/ollama/ollama/releases/latest \
    | jq -r .tag_name | sed 's/^v//')

TAG_PLATFORM="$P"
if [[ "$JETPACK" == "true" ]]; then
    if [[ "$JETSON_THOR" == "true" ]]; then
        TAG_PLATFORM="${P}_thor"
    else
        TAG_PLATFORM="${P}_l4t"
    fi
fi

IMAGE_TAG="swr.cn-southwest-2.myhuaweicloud.com/ictrek/ollama_server:${TAG_PLATFORM}_${OLLAMA_VERSION}"

docker tag ollama_server "$IMAGE_TAG"
docker push "$IMAGE_TAG"