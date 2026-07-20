#!/usr/bin/env python3
"""
Register this ollama_server instance to Model Hub.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request


def getenv(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def post_json(url: str, payload: dict, timeout: float = 10.0) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def main() -> int:
    model_hub_url = getenv("MODEL_HUB_REGISTER_URL")
    if not model_hub_url:
        return 0

    service_id = getenv("MODEL_HUB_SERVICE_ID", socket.gethostname())
    service_name = getenv("MODEL_HUB_SERVICE_NAME", service_id)
    base_url = getenv("MODEL_HUB_SERVICE_BASE_URL", f"http://{service_id}:11434")
    gateway_url = getenv("MODEL_HUB_SERVICE_GATEWAY_URL", f"http://{service_id}:11535")
    role = getenv("MODEL_HUB_SERVICE_ROLE", "generate")
    interval = float(getenv("MODEL_HUB_REGISTER_RETRY_INTERVAL", "5") or "5")
    attempts = int(getenv("MODEL_HUB_REGISTER_ATTEMPTS", "60") or "60")

    payload = {
        "service_id": service_id,
        "name": service_name,
        "kind": "ollama",
        "base_url": base_url,
        "gateway_url": gateway_url,
        "role": role,
        "metadata": {
            "container_name": getenv("HOSTNAME", service_id),
            "models_base_dir": getenv("MODELS_BASE_DIR", ""),
            "models_host_dir": getenv("MODELS_HOST_DIR", ""),
            "ollama_models": getenv("OLLAMA_MODELS", ""),
            "num_parallel": getenv("OLLAMA_NUM_PARALLEL", ""),
            "context_length": getenv("OLLAMA_CONTEXT_LENGTH", ""),
        },
    }

    url = model_hub_url.rstrip("/") + "/api/v1/inference-services/register"
    last_error = ""
    for _ in range(attempts):
        try:
            post_json(url, payload)
            print(f"registered ollama_server to Model Hub: {service_id} -> {url}", flush=True)
            return 0
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
            time.sleep(interval)

    print(f"failed to register ollama_server to Model Hub: {last_error}", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
