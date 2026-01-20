#!/bin/bash

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
USE_CUDA=false

PLAT=$(dpkg --print-architecture)
if [[ "$PLAT" == *"amd"* ]]; then
    P="amd"
elif [[ "$PLAT" == *"arm"* ]]; then
    P="arm"
else
    echo "Unsupported architecture: $PLAT"
    exit 1
fi

JETPACK=false

if [[ -f /proc/device-tree/model ]] && grep -qi "nvidia jetson" /proc/device-tree/model; then
    JETPACK=true
elif uname -a | grep -qi "tegra"; then
    JETPACK=true
fi

if $JETPACK; then
    echo "Running JetPack specific setup..."
    BASE_IMAGE="nvcr.io/nvidia/l4t-jetpack:r36.4.0"
    ARCH="arm64"
fi

ARCH="${P}64"
BUILD_DOCKER="docker build"

echo "Ollama server will be included in the build."

if [[ "$P" == "arm" && "$JETPACK" == "true" ]]; then
    BASE_IMAGE="nvcr.io/nvidia/l4t-jetpack:r36.4.0"
else
    BASE_IMAGE="nvidia/cuda:12.8.1-runtime-ubuntu22.04"
fi

# =========================
# docker build 参数拼装
# =========================
DOCKER_BUILD_ARGS=(
    --build-arg BASE_IMAGE="$BASE_IMAGE"
    --build-arg PROXY="$PROXY"
    --build-arg ARCH="${P}64"
    --build-arg JETPACK="$JETPACK"
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

if $JETPACK; then
    P="${P}_l4t"
fi

IMAGE_TAG="swr.cn-southwest-2.myhuaweicloud.com/ictrek/ollama_server:${P}_${OLLAMA_VERSION}"

docker tag ollama_server "$IMAGE_TAG"
docker push "$IMAGE_TAG"