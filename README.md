# Ollama OpenAI Gateway with Task Status

This repository provides an **OpenAI API–compatible Gateway** that runs **in the same container as Ollama**. It transparently proxies all supported `/v1/*` endpoints to Ollama while maintaining **task-level status** that can be queried via a **pull API**.

---

## Overview

- Full passthrough of Ollama's OpenAI-compatible APIs
- Task status tracking per request (`task_id`)
- Pull-based status query API (no push/reporting)
- Redis-backed status storage
- Streaming-safe (heartbeats during streams)

---

## Architecture

```
Client / OpenAI SDK
        |
        |  http://<host>:11535/v1/...
        v
+-----------------------+
|  Ollama Gateway       |
|  - OpenAI API Proxy   |
|  - Task Status Table  | ----> Redis
+-----------------------+
        |
        |  http://127.0.0.1:11434
        v
     Ollama
```

### Responsibilities

- **Ollama**
  - Pure inference backend
  - No task awareness
  - No status APIs

- **Gateway**
  - Proxies all `/v1/*` endpoints
  - Generates or accepts `task_id`
  - Writes task lifecycle states to Redis
  - Exposes `/tasks/status/*` for status queries

---

## Supported OpenAI APIs

All requests under `/v1/*` are proxied verbatim.

### Text & Multimodal

- `POST /v1/chat/completions`
  - Streaming
  - Vision (base64 images)
  - Tools / function calling
- `POST /v1/completions`
- `POST /v1/responses`
  - Streaming
  - Tools

### Models & Embeddings

- `GET /v1/models`
- `GET /v1/models/{model}`
- `POST /v1/embeddings`

> The Gateway does **not** interpret request/response payloads. It operates strictly at the HTTP layer.

---

## Task Status Model

### task_id

- Preferred: provided by client via header

```
X-Task-Id: <task_id>
```

- If absent, the Gateway generates a UUID and returns it in the response header:

```
X-Task-Id: <generated-uuid>
```

### State Lifecycle

```
PENDING -> RUNNING -> SUCCESS | FAILED
```

| State   | Meaning                          |
|---------|----------------------------------|
| PENDING | Request accepted by Gateway      |
| RUNNING | Request forwarded to Ollama      |
| SUCCESS | Upstream completed successfully |
| FAILED  | Upstream error or stream failure |

During streaming, the Gateway periodically refreshes `RUNNING` as a heartbeat.

---

## Status Storage (Redis)

- **Key**

```
ts:ollama:<task_id>
```

- **Value**
  - JSON document containing:
    - `state`
    - `stage`
    - `message`
    - `extensions`
    - `timestamp`

- **TTL Policy**

| State Type          | TTL     |
|---------------------|---------|
| PENDING / RUNNING   | 1 hour  |
| SUCCESS / FAILED    | 24 hours|

---

## Status Query APIs (Pull)

### Get Single Task Status

```
GET /tasks/status/{task_id}
```

Example:

```
curl http://localhost:11535/tasks/status/demo-001
```

Response example:

```json
{
  "version": "1.0",
  "event_type": "task.status.update",
  "event_id": "c1b0c5f3-xxxx",
  "algorithm_id": "ollama-openai",
  "task_id": "demo-001",
  "state": "RUNNING",
  "stage": "streaming",
  "message": "stream alive",
  "extensions": {
    "method": "POST",
    "path": "/v1/chat/completions",
    "stream": true
  },
  "timestamp": 1736400000
}
```

### List Recent Tasks (Debug)

```
GET /tasks/status?limit=50
```

---

## Ports & Deployment

### Default (Same Container)

| Component | Address             |
|----------:|---------------------|
| Ollama    | 127.0.0.1:11434     |
| Gateway   | 0.0.0.0:11535       |

> External clients **must** access Ollama through the Gateway port.

---

## Usage

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:11535/v1/",
    api_key="ollama"
)

resp = client.chat.completions.create(
    model="gpt-oss:20b",
    messages=[{"role": "user", "content": "Say this is a test"}],
)

print(resp.choices[0].message.content)
```

### Streaming with Explicit task_id

```bash
curl -N http://localhost:11535/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Task-Id: demo-001" \
  -d '{
    "model": "gpt-oss:20b",
    "messages": [{"role": "user", "content": "Say this is a test"}],
    "stream": true
  }'
```

Query status in parallel:

```bash
curl http://localhost:11535/tasks/status/demo-001
```

---

## Environment Variables

```
UPSTREAM_BASE=http://127.0.0.1:11434

REDIS_HOST=172.28.1.1
REDIS_PORT=6379
REDIS_USER=default
REDIS_PASSWORD=******
REDIS_DB=0

TTL_RUNNING=3600
TTL_DONE=86400

HEARTBEAT_SEC=10
```

---

## Design Notes

- No modification to Ollama internals
- No inference-progress percentage (request lifecycle only)
- Stateless OpenAI API semantics preserved
- No support for stateful `responses` (e.g., `previous_response_id`)




