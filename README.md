# Ollama OpenAI Gateway

一个运行在 Ollama 前面的轻量 OpenAI 兼容网关。Gateway 保留 `X-Task-Id` 作为请求关联 ID，并将 `/v1/chat/completions` 转换为 Ollama 原生 `/api/chat`，让 OpenAI 风格请求也能使用 Ollama 原生请求级 `options`（例如 `options.num_ctx`）。其他 `/v1/*` 接口继续透传给 Ollama。

## 功能

- OpenAI 风格 `/v1/chat/completions` 兼容 Ollama 原生 `/api/chat`，支持请求级 `options.num_ctx`
- OpenAI 风格 `/v1/completions` 兼容 Ollama 原生 `/api/generate`，支持请求级 `options.num_ctx`
- 透传其他 Ollama OpenAI 风格接口：`/v1/responses`、`/v1/models`、`/v1/embeddings`、`/v1/images/generations`
- 支持客户端传入 `X-Task-Id`；未传入时自动生成并通过响应头返回
- 根据上游响应自动区分普通 JSON 和 SSE 流式响应
- Ollama 和 Gateway 在同一个镜像/容器中运行，外部只访问 Gateway 端口 `11535`
- 增加 gateway 客户端手动中断检测，及时中断 Ollama 上游推理请求，节约算力
- `/metrics` 提供经过 Gateway 的活跃槽位、prefill/decode 阶段和 token/s 指标，便于 Model Hub 展示运行状态
- 可选向 Model Hub 注册自身管理接口，便于 Model Hub 查询版本、模型列表、运行状态并启动/停止模型

## 架构

```text
Client / OpenAI SDK
        |
        | http://<host>:11535/v1/...
        v
  Ollama Gateway
        |
        | http://127.0.0.1:11434
        v
      Ollama
```

## 代码结构

| 文件 | 说明 |
| --- | --- |
| `ollama_gateway/gateway.py` | FastAPI 入口、路由分发、上游转发、流式响应和断连处理 |
| `ollama_gateway/openai_to_ollama.py` | 将 OpenAI 风格请求转换为 Ollama 原生 `/api/chat` / `/api/generate` 请求 |
| `ollama_gateway/ollama_to_openai.py` | 将 Ollama 原生响应转换回 OpenAI 风格响应和 SSE chunk |

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
# 按需修改 .env

docker compose --env-file .env -f docker/docker-compose.yml up -d --build
docker compose --env-file .env -f docker/docker-compose.yml ps
docker compose --env-file .env -f docker/docker-compose.yml logs -f gateway
```

默认端口：

| 服务 | 地址 |
| --- | --- |
| Gateway | `http://localhost:11535` |
| Ollama | `127.0.0.1:11434`，仅容器内部使用 |

## 使用示例

### OpenAI SDK

```bash
python examples/openai_sdk_keep_alive.py
```

完整示例见 [`examples/openai_sdk_keep_alive.py`](./examples/openai_sdk_keep_alive.py)。

### curl

可以使用该方式控制不同模型的上下文窗口大小。OpenAI SDK 通过 `extra_body` 传入 Ollama 原生 `options`；curl 可直接在请求体中传入 `options`。

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

运行状态统计以 Gateway 为入口。通过 `http://<host>:11535/v1/chat/completions`、`/v1/completions`、`/api/chat` 或 `/api/generate` 发起的请求会计入 `/metrics` 的槽位；直接执行 `ollama run` 或直连 `11434` 会绕过 Gateway，无法被 `/metrics` 统计。流式请求运行中会按已收到的 chunk 估算 decode token/s，请求结束时如果 Ollama 返回官方 `eval_count` / `eval_duration`，则以官方统计覆盖。

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

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `UPSTREAM_BASE` | `http://127.0.0.1:11434` | Ollama 上游地址 |
| `UPSTREAM_STARTUP_TIMEOUT_SEC` | `30` | Gateway 等待 Ollama 就绪的秒数 |
| `DISCONNECT_POLL_SEC` | `0.1` | 检查客户端是否断开的间隔秒数 |
| `MODEL_HUB_REGISTER_URL` | 空 | Model Hub 后端地址；为空时不注册，例如 `http://model-hub-backend:5005` |
| `MODEL_HUB_SERVICE_ID` | 容器 hostname | 注册到 Model Hub 的稳定服务 ID |
| `MODEL_HUB_SERVICE_NAME` | `MODEL_HUB_SERVICE_ID` | Model Hub 中展示的服务名称 |
| `MODEL_HUB_SERVICE_BASE_URL` | `http://<service_id>:11434` | Model Hub 访问 Ollama 原生 API 的 vos_default 地址 |
| `MODEL_HUB_SERVICE_GATEWAY_URL` | `http://<service_id>:11535` | Model Hub 访问 OpenAI Gateway 的 vos_default 地址 |
| `MODEL_HUB_SERVICE_ROLE` | `generate` | 服务用途，可填 `generate` 或 `embedding` |

### 注册到 Model Hub

在 VOS 部署中，`ollama_server` 应加入 `vos_default` 网络，并配置稳定 alias。启动脚本会在后台向 Model Hub 注册，不影响 Gateway 正常启动：

```yaml
services:
  ollama-server:
    image: swr.cn-southwest-2.myhuaweicloud.com/ictrek/ollama_server:<tag>
    networks:
      vos_default:
        aliases:
          - my-ollama-server
    environment:
      MODEL_HUB_REGISTER_URL: http://model-hub-backend:5005
      MODEL_HUB_SERVICE_ID: my-ollama-server
      MODEL_HUB_SERVICE_NAME: My Ollama Server
      MODEL_HUB_SERVICE_BASE_URL: http://my-ollama-server:11434
      MODEL_HUB_SERVICE_GATEWAY_URL: http://my-ollama-server:11535
      MODEL_HUB_SERVICE_ROLE: generate
```

Model Hub 对 `kind=ollama` 使用原生接口管理：

| 能力 | Ollama 接口 |
| --- | --- |
| 健康/版本 | `GET /api/version` |
| 已下载模型 | `GET /api/tags` |
| 已启动模型、显存/上下文 | `GET /api/ps` |
| 启动 generate 模型 | `POST /api/generate` + `keep_alive` |
| 启动 embedding 模型 | `POST /api/embed` + `keep_alive` |
| 停止模型 | 同接口传 `keep_alive: 0` |

## 常用命令

```bash
# 查看日志
docker compose --env-file .env -f docker/docker-compose.yml logs -f gateway

# 拉取模型
docker exec ollama_server ollama pull qwen3:0.6b

# 停止服务
docker compose --env-file .env -f docker/docker-compose.yml down
```

## 测试相关

可以参考 [tests/README.md](./tests/README.md)

## 目录结构

Docker 相关文件集中在 `docker/`，容器启动和构建辅助脚本集中在 `scripts/`：

| 路径 | 说明 |
| --- | --- |
| `docker/Dockerfile*` | 各镜像 profile |
| `docker/docker-compose.yml` | 默认运行环境 |
| `docker/docker-compose-test.yml` | 集成测试环境 |
| `scripts/start.sh` | 容器启动入口 |
| `scripts/install_python.sh` | 基础镜像 Python 安装脚本 |
| `scripts/build_image.sh` | 构建、推送并写入飞书的脚本 |

## 构建镜像

默认镜像使用 `docker/Dockerfile`：

### Dockerfile Profiles

Dockerfile profile 只决定如何构建镜像，发布目标通过 `--target` 单独指定：

| Profile | Description |
|---------|-------------|
| `Dockerfile` | 官方 Ollama 基础镜像，可用于多个平台 |
| `Dockerfile_l4t` | Jetson (L4T) 设备 |
| `Dockerfile_thor` | Thor (ARM + CUDA 13) 设备，支持 ghfast.top 镜像加速 |
| `Dockerfile_cu128` | CUDA 12.8 |

`Dockerfile_cu128` 默认使用内部 ARM64 基础镜像
`swr.cn-southwest-2.myhuaweicloud.com/ictrek-arm/cuda:12.8.1-runtime-ubuntu22.04`，
并根据构建平台的 `TARGETARCH` 下载对应架构的 Ollama 包。
如需构建其他架构，可通过 `CUDA_BASE_IMAGE=<image>` 覆盖该默认基础镜像。

| Target | Feishu Sheet | Image Tag Prefix |
|--------|---------------|------------------|
| `amd` | `AMD_with_cuda` | `amd` |
| `arm` | `ARM_with_cuda`、`ARM_without_cuda`、`SOPHON_bm1688` | `arm` |
| `l4t` | `l4t` | `l4t` |
| `thor` | `thor_spark` | `thor` |
| `amd_cu128` | `AMD_with_cuda` | `amd_cu128` |
| `arm_cu128` | `ARM_with_cuda`、`ARM_without_cuda`、`SOPHON_bm1688` | `arm_cu128` |

飞书列名同时作为华为云 SWR 仓库名，飞书写入值与镜像 tag 保持一致。未设置
`OLLAMA_TAG` 或设置为 `latest` 时，构建脚本会自动检测 Ollama 最新 release；GitHub
检测失败时会改用 `ghfast.top`。例如组件列为 `ollama_server`、目标为 `thor`、检测到
`OLLAMA_TAG=0.31.1` 时，推送地址为：

```text
swr.cn-southwest-2.myhuaweicloud.com/ictrek/ollama_server:thor_0.31.1
```

### Build Example

```bash
# 自动检测 Ollama 最新 release，使用通用 Dockerfile 构建并发布 Thor 目标
bash scripts/build_image.sh --profile Dockerfile --target thor

# 使用 L4T 专用 Dockerfile 构建并发布，版本同样自动检测
bash scripts/build_image.sh --profile Dockerfile_l4t --target l4t

# 可以推送已经构建并测试过的本地镜像，--dry-run 打印预发布信息
bash scripts/build_image.sh \
  --target amd \
  --skip-build \
  --dry-run

# 只查看发布计划
bash scripts/build_image.sh --profile Dockerfile --target arm --dry-run

# 自动优先使用 buildx 并通过 --load 写入本机镜像库；无 buildx 时回退到 docker build
bash scripts/build_image.sh --profile Dockerfile_cu128 --target arm_cu128

# 需要固定构建后端时可显式指定 buildx 或 docker
bash scripts/build_image.sh --profile Dockerfile_cu128 --target arm_cu128 --builder docker

# 需要复现指定版本时，也可以显式指定
OLLAMA_TAG=0.31.1 bash scripts/build_image.sh --profile Dockerfile --target arm
```

构建成功后会自动推送到华为云 SWR，并写入飞书表格对应标签页。

构建后端默认设为 `auto`：检测到 buildx 时执行 `docker buildx build --load`，确保单平台镜像
加载到本机 Docker image store 后再推送；未安装 buildx 时自动回退到 `docker build`。也可以用
`--builder buildx` 或 `--builder docker` 显式指定。预先拉取到本机的基础镜像会由对应构建后端复用。
脚本还会根据构建主机传入 `linux/arm64` 或 `linux/amd64`，避免本地同名基础镜像的架构不匹配。

`--target` 只控制发布目标、飞书位置和镜像 tag，不会改变实际构建架构。镜像应当在对应平台
构建和测试后发布；多个单架构镜像不能使用同一个仓库 tag，否则后推送的镜像会覆盖先前镜像。

### 手动构建

```bash
docker build \
  --build-arg OLLAMA_TAG=0.31.1 \
  --build-arg PYTHON_VERSION=3.12 \
  -t ollama_server:0.31.1 \
  -f docker/Dockerfile .
```
