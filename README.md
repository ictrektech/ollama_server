# Ollama OpenAI Gateway

一个运行在 Ollama 前面的轻量任务网关。Gateway 负责 `X-Task-Id`、Redis 任务状态，并将 `/v1/chat/completions` 转换为 Ollama 原生 `/api/chat`，让 OpenAI 风格请求也能使用 Ollama 原生请求级 `options`（例如 `options.num_ctx`）。其他 `/v1/*` 接口继续透传给 Ollama。

## 功能

- OpenAI 风格 `/v1/chat/completions` 兼容 Ollama 原生 `/api/chat`，支持请求级 `options.num_ctx`
- OpenAI 风格 `/v1/completions` 兼容 Ollama 原生 `/api/generate`，支持请求级 `options.num_ctx`
- 透传其他 Ollama OpenAI 风格接口：`/v1/responses`、`/v1/models`、`/v1/embeddings`、`/v1/images/generations`
- 支持客户端传入 `X-Task-Id`；未传入时自动生成并通过响应头返回
- Redis 保存任务状态：`PENDING -> RUNNING -> SUCCESS | FAILED`
- `/tasks/status` 基于 Redis sorted set 返回最近更新的任务
- 流式请求期间刷新 `RUNNING` 心跳
- 根据上游响应自动区分普通 JSON 和 SSE 流式响应
- Ollama 和 Gateway 在同一个镜像/容器中运行，外部只访问 Gateway 端口 `11535`

## 架构

```text
Client / OpenAI SDK
        |
        | http://<host>:11535/v1/...
        v
  Ollama Gateway  ---->  Redis
        |
        | http://127.0.0.1:11434
        v
      Ollama
```

## 兼容矩阵

| OpenAI 接口 | 对应 Ollama 接口 | 当前兼容状态 |
| --- | --- | --- |
| `POST /v1/chat/completions` | `POST /api/chat` | 已转换 |
| `POST /v1/completions` | `POST /api/generate` | 已转换 |
| `POST /v1/embeddings` | `POST /api/embed` | 待补 |
| `POST /v1/responses` | 桥接到 `/api/chat` | 待补 |
| `GET /v1/models` | `GET /api/tags` | 当前透传 Ollama `/v1/models` |

## 快速启动

```bash
cp env.example .env
# 按需修改 .env，至少设置 REDIS_PASSWORD

docker compose up -d --build
docker compose ps
docker compose logs -f gateway
```

默认端口：

| 服务 | 地址 |
| --- | --- |
| Gateway | `http://localhost:11535` |
| Ollama | `127.0.0.1:11434`，仅容器内部使用 |
| Redis | `.env` 中的 `REDIS_PORT` |

## 使用示例

### OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:11535/v1/",
    api_key="ollama",
)

resp = client.chat.completions.create(
    model="qwen3:0.6b",
    messages=[{"role": "user", "content": "Say this is a test"}],
)

print(resp.choices[0].message.content)
```

### curl

以使用该方式控制不同模型的上下文窗口大小

```bash
curl http://localhost:11535/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Task-Id: demo-001" \
  -d '{
    "model": "qwen3:0.6b",
    "messages": [{"role": "user", "content": "介绍一下李白"}],
    "max_tokens": 100,
    "options": {"num_ctx": 1024},
    "stream": false
  }'
```

`/v1/chat/completions` 会在 Gateway 内部转发到 Ollama `/api/chat`。OpenAI 常用参数会映射到 Ollama `options`，例如 `max_tokens` / `max_completion_tokens` -> `num_predict`；显式传入的 `options` 优先级更高。

文本补全同样支持请求级 `options`：

```bash
curl http://localhost:11535/v1/completions \
  -H "Content-Type: application/json" \
  -H "X-Task-Id: demo-002" \
  -d '{
    "model": "qwen3:0.6b",
    "prompt": "写一句关于春天的短句：",
    "options": {"num_ctx": 8192},
    "stream": false
  }'
```

查询任务状态：

```bash
curl http://localhost:11535/tasks/status/demo-001
curl "http://localhost:11535/tasks/status?limit=50"
```

`/tasks/status` 按最近更新时间倒序返回，`limit` 最大为 500。

状态响应示例：

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
    "stream": true,
    "requested_stream": true,
    "response_stream": true
  },
  "timestamp": 1736400000
}
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `REDIS_HOST` | `172.28.1.1` | Redis 地址；Docker Compose 中使用 `redis` |
| `REDIS_PORT` | `6379` | Redis 端口 |
| `REDIS_USER` | `default` | Redis 用户 |
| `REDIS_PASSWORD` | 空 | Redis 密码，生产环境必须设置 |
| `REDIS_DB` | `0` | Redis DB |
| `UPSTREAM_BASE` | `http://127.0.0.1:11434` | Ollama 上游地址 |
| `UPSTREAM_STARTUP_TIMEOUT_SEC` | `30` | Gateway 等待 Ollama 就绪的秒数 |
| `TTL_RUNNING` | `3600` | `PENDING` / `RUNNING` 状态保留秒数 |
| `TTL_DONE` | `86400` | `SUCCESS` / `FAILED` 状态保留秒数 |
| `HEARTBEAT_SEC` | `10` | 流式请求心跳刷新间隔 |
| `ALGORITHM_ID` | `ollama-openai` | 状态事件中的算法标识 |

## 常用命令

```bash
# 查看日志
docker compose logs -f gateway

# 拉取模型
docker exec ollama-gateway ollama pull qwen3:0.6b

# 检查 Redis
docker exec ollama-redis redis-cli -a your_password ping

# 停止服务
docker compose down
```

## 测试相关

可以参考 [doc](./tests/doc.md)

## 构建镜像

默认镜像使用 `Dockerfile`：

```bash
docker build \
  --build-arg OLLAMA_TAG=0.24.0 \
  --build-arg PYTHON_VERSION=3.12 \
  -t ollama-gateway:0.24.0 \
  -f Dockerfile .
```

构建镜像时将镜像版本同步飞书

```bash
bash build_image.sh --profile Dockerfile
```
