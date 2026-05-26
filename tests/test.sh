#!/usr/bin/env bash
set -e

echo "=========================================="
echo "Running Ollama Gateway Tests"
echo "=========================================="
echo "REDIS_HOST: ${REDIS_HOST:-redis}"
echo "TEST_MODEL: ${TEST_MODEL:-qwen3:0.6b}"
echo "=========================================="

cd /app

echo "Starting Ollama server..."
OLLAMA_HOST=127.0.0.1:11434 ollama serve > /tmp/ollama.log 2>&1 &
OLLAMA_PID=$!

if [ -z "$OLLAMA_PID" ]; then
    echo "ollama binary not running. exiting"
    exit 1
fi

# 等待 Ollama 准备好
echo "Waiting for Ollama to be ready..."
OLLAMA_READY=0
for i in {1..30}; do
    if curl -s http://127.0.0.1:11434/api/version > /dev/null 2>&1; then
        echo "Ollama is ready!"
        OLLAMA_READY=1
        break
    fi
    echo "Attempt $i/30..."
    sleep 2
done

if [ "$OLLAMA_READY" -ne 1 ]; then
    echo "Ollama did not become ready in time."
    cat /tmp/ollama.log || true
    exit 1
fi

# 检查模型是否存在，不存在则pull
echo "Checking model ${TEST_MODEL:-qwen3:0.6b} ..."
if ! curl -s http://127.0.0.1:11434/v1/models | grep -q "$TEST_MODEL"; then
    echo "Pulling model ${TEST_MODEL:-qwen3:0.6b} ..."
    curl -s http://127.0.0.1:11434/api/pull -d "{\"name\": \"$TEST_MODEL\"}" > /dev/null
    echo "Model pulled."
fi

# 启动 Gateway (在后台)
echo "Starting Gateway..."
python3 -m uvicorn gateway:app --host 0.0.0.0 --port 11535 > /tmp/gateway.log 2>&1 &
GATEWAY_PID=$!
echo "Gateway started with PID $GATEWAY_PID"

if [ -z "$GATEWAY_PID" ]; then
    echo "Ollama-gateway not running. exiting"
    exit 1
fi

# 等待 Gateway 准备好
echo "Waiting for Gateway to be ready..."
GATEWAY_READY=0
for i in {1..30}; do
    if curl -s http://localhost:11535/v1/models > /dev/null 2>&1; then
        echo "Gateway is ready!"
        GATEWAY_READY=1
        break
    fi
    echo "Attempt $i/30..."
    sleep 1
done

if [ "$GATEWAY_READY" -ne 1 ]; then
    echo "Gateway did not become ready in time."
    cat /tmp/gateway.log || true
    exit 1
fi

TEST_STATUS=0
python3 -m unittest discover -s tests -p "test_*.py" -v || TEST_STATUS=$?

# 关闭 Gateway 服务
# echo "Stopping Gateway (PID $GATEWAY_PID)..."
# kill $GATEWAY_PID 2>/dev/null || true

# echo "GATEWAY LOG: "
# cat /tmp/gateway.log

# echo "OLLAMA LOG: "
# tail -n 120 /tmp/ollama.log || true

# if [ "$TEST_STATUS" -ne 0 ]; then
#     exit "$TEST_STATUS"
# fi

# 保持容器运行
# echo "Keeping container alive. Press Ctrl+C to exit."
# tail -f /dev/null
