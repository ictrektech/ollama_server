#!/bin/sh
set -e

export OLLAMA_HOST=127.0.0.1:11434
ollama serve >/tmp/ollama.log 2>&1 &

exec uvicorn gateway:app --host 0.0.0.0 --port 11535