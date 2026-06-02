# Testing

本目录包含两类测试：

- 单元测试：直接测试 `ollama_gateway` 模块里的辅助函数和 OpenAI/Ollama 请求响应转换，不需要启动 Redis、Ollama 或 Gateway。
- 集成测试：通过 Docker Compose 启动 Redis、Ollama 和 Gateway，发送真实请求，并检查 Redis 任务状态。

## 快速运行

运行完整集成测试：

```bash
OLLAMA_TAG=0.30.0 docker compose -f docker-compose-test.yml up -d
```

重新跑测试前清理容器：

```bash
docker compose -f docker-compose-test.yml down
docker compose -f docker-compose-test.yml up
```

修改 `Dockerfile`、`requirements.txt`、`install_python.sh` 或系统依赖后，需要重构镜像：

```bash
docker compose -f docker-compose-test.yml up --build
```

清理容器和 volume：

```bash
docker compose -f docker-compose-test.yml down -v --remove-orphans
```

## 测试覆盖

| 文件 | 类型 | 覆盖内容 |
| --- | --- | --- |
| `test_task_id.py` | 单元测试 | `X-Task-Id` 透传、空值处理、自动 UUID 生成 |
| `test_gateway_helpers.py` | 单元测试 | 请求头过滤、流式识别、状态 TTL、`/v1/chat/completions -> /api/chat`、`/v1/completions -> /api/generate` 的转换 |
| `test_gateway_forwarding.py` | 代理契约测试 | 使用 fake upstream 验证 Gateway 发往 Ollama 的路径、headers、body、缺参错误、上游错误、透传路径和 SSE 结构 |
| `test_task_status_store.py` | 单元测试 | Redis 状态写入、状态索引清理节流 |
| `test_gateway_inference_status.py` | 集成测试 | 真实请求 `/v1/chat/completions`，验证 OpenAI 风格响应、Redis 最终任务状态，以及 `options.num_ctx` 是否反映到 Ollama `/api/ps` 的 `context_length` |
| `test.sh` | 测试入口 | 启动 Ollama、拉取模型、启动 Gateway、运行 unittest |

`test_gateway_forwarding.py` 不需要真实 Ollama 或 Redis，重点防止上线前漏掉这类代理问题：

- 转换请求体后仍转发原始 `Content-Length`
- 上游路径没有转成 `/api/chat` 或 `/api/generate`
- `options.num_ctx` 等字段没有进入 Ollama 原生请求
- 缺少 `model`、`messages` 或 `prompt` 时仍调用上游
- 未转换的 `/v1/*` 路径透传时携带危险协议头
- 上游错误响应没有正确返回和记录
- chat/completions 或 completions 流式请求没有输出 OpenAI SSE 和 `[DONE]`
- 流式或非流式客户端断开时没有关闭上游连接并记录 `FAILED / client disconnected`

## 集成测试流程

`tests/test.sh` 会依次执行：

1. 启动 Ollama。
2. 等待 `/api/version` 就绪。
3. 检查 `TEST_MODEL`，不存在时自动拉取。
4. 启动 Gateway。
5. 等待 `/v1/models` 就绪。
6. 执行：

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

聊天补全集成测试允许上游模型失败：如果 Ollama 返回错误，测试会验证任务状态写成 `FAILED`，不会把模型偶发错误误判为 Gateway 逻辑失败。

## 常用调试

查看日志：

```bash
OLLAMA_TAG=0.30.0 docker compose -f docker-compose-test.yml logs -f
```

进入测试容器：

```bash
OLLAMA_TAG=0.30.0 docker compose -f docker-compose-test.yml exec gateway bash
```

测试脚本结束后会保持容器运行，方便查看：

- `/tmp/ollama.log`
- `/tmp/gateway.log`

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `REDIS_HOST` | `redis` | Redis service hostname |
| `REDIS_PASSWORD` | `.env` 中配置 | Redis 密码 |
| `TEST_MODEL` | `qwen3:0.6b` | 集成测试使用的模型 |
| `PYTHONPATH` | `/app` | Python module path |
