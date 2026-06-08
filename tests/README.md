# Testing

本目录包含三类测试：

- 单元测试：直接测试 `ollama_gateway` 模块里的辅助函数和 OpenAI/Ollama 请求响应转换，不需要启动 Ollama 或 Gateway。
- 代理契约测试：使用 fake upstream 验证 Gateway 转发请求和处理响应的行为，不需要真实 Ollama。
- 集成测试：通过 Docker Compose 启动 Ollama 和 Gateway，并发送真实请求。

## 目录结构

| 路径 | 类型 | 说明 |
| --- | --- | --- |
| `unit/` | 单元测试 | 测试纯函数、请求 ID 和请求/响应转换逻辑 |
| `contract/` | 代理契约测试 | 使用 fake upstream 验证 Gateway 转发路径、headers、body、SSE 和断连处理 |
| `integration/` | 集成测试 | 使用真实 Ollama 和 Gateway 验证端到端行为 |
| `test.sh` | 集成测试入口 | 容器内启动 Ollama、拉取模型、启动 Gateway、运行 unittest |

## 快速运行

以下命令默认从仓库根目录执行。

运行不依赖 Ollama / Docker 的快速测试：

```bash
.venv/bin/python -m unittest discover -s tests/unit -p "test_*.py" -v
.venv/bin/python -m unittest discover -s tests/contract -p "test_*.py" -v
```

运行完整集成测试：

```bash
OLLAMA_TAG=0.30.0 docker compose --env-file .env -f docker/docker-compose-test.yml up -d
```

重新跑测试前清理容器：

```bash
OLLAMA_TAG=0.30.0 docker compose --env-file .env -f docker/docker-compose-test.yml down
OLLAMA_TAG=0.30.0 docker compose --env-file .env -f docker/docker-compose-test.yml up
```

修改 `docker/Dockerfile`、`requirements.txt`、`scripts/install_python.sh` 或系统依赖后，需要重构镜像：

```bash
OLLAMA_TAG=0.30.0 docker compose --env-file .env -f docker/docker-compose-test.yml up --build
```

清理容器和 volume：

```bash
OLLAMA_TAG=0.30.0 docker compose --env-file .env -f docker/docker-compose-test.yml down -v --remove-orphans
```

## 测试覆盖

| 文件 | 类型 | 覆盖内容 |
| --- | --- | --- |
| `unit/test_task_id.py` | 单元测试 | `X-Task-Id` 透传、空值处理、自动 UUID 生成 |
| `unit/test_gateway_helpers.py` | 单元测试 | 请求头过滤、流式识别、`/v1/chat/completions -> /api/chat`、`/v1/completions -> /api/generate` 的转换 |
| `contract/test_gateway_forwarding.py` | 代理契约测试 | 使用 fake upstream 验证 Gateway 发往 Ollama 的路径、headers、body、缺参错误、上游错误、透传路径和 SSE 结构 |
| `integration/test_gateway_inference.py` | 集成测试 | 真实请求 `/v1/chat/completions`，验证 `X-Task-Id`、OpenAI 风格响应，以及 `options.num_ctx` 是否反映到 Ollama `/api/ps` 的 `context_length` |
| `test.sh` | 集成测试入口 | 启动 Ollama、拉取模型、启动 Gateway、运行 unittest |

`contract/test_gateway_forwarding.py` 不需要真实 Ollama，重点防止上线前漏掉这类代理问题：

- 转换请求体后仍转发原始 `Content-Length`
- 上游路径没有转成 `/api/chat` 或 `/api/generate`
- `options.num_ctx` 等字段没有进入 Ollama 原生请求
- 缺少 `model`、`messages` 或 `prompt` 时仍调用上游
- 未转换的 `/v1/*` 路径透传时携带危险协议头
- 上游错误响应没有正确返回
- chat/completions 或 completions 流式请求没有输出 OpenAI SSE 和 `[DONE]`
- 流式或非流式客户端断开时没有关闭上游连接

## 集成测试流程

`docker/docker-compose-test.yml` 会挂载仓库根目录到容器内 `/app`，并执行 `tests/test.sh`。`tests/test.sh` 会依次执行：

1. 启动 Ollama。
2. 等待 `/api/version` 就绪。
3. 检查 `TEST_MODEL`，不存在时自动拉取。
4. 启动 Gateway。
5. 等待 `/v1/models` 就绪。
6. 执行：

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

聊天补全集成测试允许上游模型失败，但仍会验证响应返回了请求关联 ID。

## 常用调试

查看日志：

```bash
OLLAMA_TAG=0.30.0 docker compose --env-file .env -f docker/docker-compose-test.yml logs -f
```

进入测试容器：

```bash
OLLAMA_TAG=0.30.0 docker compose --env-file .env -f docker/docker-compose-test.yml exec gateway bash
```

测试脚本结束后会保持容器运行，方便查看：

- `/tmp/ollama.log`
- `/tmp/gateway.log`

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TEST_MODEL` | `qwen3:0.6b` | 集成测试使用的模型 |
| `PYTHONPATH` | `/app` | Python module path |
