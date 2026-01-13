#!/bin/bash

# 帮助文档
BASE_IMAGE="ubuntu:22.04"
USE_CUDA=false

PLAT=`dpkg --print-architecture`
if [[ "$PLAT" == *"amd"* ]]; then
    P="amd"
elif [[ "$PLAT" == *"arm"* ]]; then
    P="arm"
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

BUILD_DOCKER="docker build"

ARCH="${P}64"
# V=${P}${VO}${VER}
# # VL=${P}${VC}_latest
# VL=${P}${VO}_latest
echo "Ollama server will be included in the build."
if [[ "$P" == "arm" && "$JETPACK" == "true" ]]; then
    BASE_IMAGE="nvcr.io/nvidia/l4t-jetpack:r36.4.0"
else
    BASE_IMAGE="nvidia/cuda:12.8.1-runtime-ubuntu22.04"
fi

export DOCKER_BUILDKIT=0
$BUILD_DOCKER \
    --build-arg BASE_IMAGE=$BASE_IMAGE \
    --build-arg PROXY=$PROXY \
    --build-arg ARCH="${P}64" \
    --build-arg JETPACK=$JETPACK \
    -t ollama_server \
    .
# rm -rf ollama/
OLLAMA_VERSION=$(curl -s https://api.github.com/repos/ollama/ollama/releases/latest \
    | jq -r .tag_name | sed 's/^v//');
if $JETPACK; then
    P="${P}_l4t"
fi
docker tag ollama_server swr.cn-southwest-2.myhuaweicloud.com/ictrek/ollama_server:"$P"_"$OLLAMA_VERSION"
docker push swr.cn-southwest-2.myhuaweicloud.com/ictrek/ollama_server:"$P"_"$OLLAMA_VERSION"