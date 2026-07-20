#!/bin/sh
set -e


export OLLAMA_HOST="${OLLAMA_HOST:-0.0.0.0:11434}"
ollama serve >/tmp/ollama.log 2>&1 &

if [ -n "${MODEL_HUB_REGISTER_URL:-}" ]; then
  python3 /app/scripts/register_model_hub.py >/tmp/model_hub_register.log 2>&1 &
fi

exec uvicorn ollama_gateway.gateway:app --host 0.0.0.0 --port 11535
