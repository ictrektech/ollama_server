import os
import json
import time
import uuid
import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Optional, Dict, Any, List, Tuple

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

# ---------------------------
# Config
# ---------------------------
APP_VERSION = "1.0"
EVENT_TYPE = "task.status.update"
STATUS_INDEX_KEY = "ts:ollama:index"

# Same-container upstream
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "http://127.0.0.1:11434").rstrip("/")
ALGORITHM_ID = os.getenv("ALGORITHM_ID", "ollama-openai")
UPSTREAM_STARTUP_TIMEOUT_SEC = float(os.getenv("UPSTREAM_STARTUP_TIMEOUT_SEC", "30"))

REDIS_HOST = os.getenv("REDIS_HOST", "172.28.1.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_USER = os.getenv("REDIS_USER", "default")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

# TTL seconds
TTL_RUNNING = int(os.getenv("TTL_RUNNING", "3600"))   # pending/running keep 1h
TTL_DONE = int(os.getenv("TTL_DONE", "86400"))        # done keep 24h

# Streaming heartbeat interval
HEARTBEAT_SEC = float(os.getenv("HEARTBEAT_SEC", "10"))
DISCONNECT_POLL_SEC = float(os.getenv("DISCONNECT_POLL_SEC", "0.1"))

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "content-length",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class ClientDisconnectError(Exception):
    pass

# ---------------------------
# Lifespan
# ---------------------------
async def wait_for_upstream(client: httpx.AsyncClient) -> None:
    if UPSTREAM_STARTUP_TIMEOUT_SEC <= 0:
        return

    deadline = time.monotonic() + UPSTREAM_STARTUP_TIMEOUT_SEC
    url = f"{UPSTREAM_BASE}/api/version"
    last_error = "not checked"

    while time.monotonic() < deadline:
        try:
            resp = await client.get(url, timeout=2.0)
            resp.raise_for_status()
            print(f"Upstream ready at {UPSTREAM_BASE}")
            return
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.5)

    raise RuntimeError(f"Upstream not ready after {UPSTREAM_STARTUP_TIMEOUT_SEC}s: {last_error}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = aioredis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        username=REDIS_USER if REDIS_USER else None,
        password=REDIS_PASSWORD if REDIS_PASSWORD else None,
        db=REDIS_DB,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    app.state.http_client = httpx.AsyncClient(timeout=None)

    try:
        await app.state.redis.ping()
        print(f"Redis ready at {REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}")
        await wait_for_upstream(app.state.http_client)
        print(f"Gateway v{APP_VERSION} active. Proxying to {UPSTREAM_BASE}")
        yield
    finally:
        await app.state.http_client.aclose()
        await app.state.redis.aclose()


app = FastAPI(
    title="Ollama OpenAI Gateway + Task Status",
    version=APP_VERSION,
    lifespan=lifespan,
)

# ---------------------------
# Helpers
# ---------------------------
def now_ts() -> int:
    return int(time.time())


def rkey(task_id: str) -> str:
    return f"ts:ollama:{task_id}"


def status_index_cutoff() -> int:
    return now_ts() - max(TTL_RUNNING, TTL_DONE)


def make_evt(
    task_id: str,
    state: str,
    stage: Optional[str] = None,
    message: Optional[str] = None,
    progress: Optional[float] = None,
    extensions: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    evt: Dict[str, Any] = {
        "version": APP_VERSION,
        "event_type": EVENT_TYPE,
        "event_id": str(uuid.uuid4()),
        "algorithm_id": ALGORITHM_ID,
        "task_id": task_id,
        "state": state,
        "timestamp": now_ts(),
    }
    if stage is not None:
        evt["stage"] = stage
    if message is not None:
        evt["message"] = message
    if progress is not None:
        evt["progress"] = progress
    if extensions is not None:
        evt["extensions"] = extensions
    return evt


async def write_status(task_id: str, evt: Dict[str, Any], ttl: int) -> None:
    pipe = app.state.redis.pipeline()
    pipe.set(rkey(task_id), json.dumps(evt, ensure_ascii=False), ex=ttl)
    pipe.zadd(STATUS_INDEX_KEY, {task_id: evt["timestamp"]})
    pipe.zremrangebyscore(STATUS_INDEX_KEY, "-inf", status_index_cutoff())
    await pipe.execute()


async def set_status(
    task_id: str,
    state: str,
    stage: str,
    message: str,
    extensions: Dict[str, Any],
    ttl: int,
) -> None:
    await write_status(task_id, make_evt(task_id, state, stage=stage, message=message, extensions=extensions), ttl)


async def finish_status(task_id: str, status_code: int, extensions: Dict[str, Any]) -> None:
    if 200 <= status_code < 300:
        await set_status(task_id, "SUCCESS", "done", "completed", extensions, TTL_DONE)
    else:
        await set_status(task_id, "FAILED", "error", f"upstream status {status_code}", extensions, TTL_DONE)


def get_task_id(req: Request) -> str:
    tid = req.headers.get("x-task-id")
    if tid and tid.strip():
        return tid.strip()
    return str(uuid.uuid4())


def proxy_headers(headers, drop_host: bool = False) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in headers.items():
        name = k.lower()
        if name in HOP_BY_HOP_HEADERS or (drop_host and name == "host"):
            continue
        out[k] = v
    return out


def build_extensions(req: Request, requested_stream: bool) -> Dict[str, Any]:
    return {
        "method": req.method,
        "path": req.url.path,
        "query": str(req.url.query) if req.url.query else "",
        "stream": requested_stream,
        "requested_stream": requested_stream,
    }


def requested_stream(raw_body: bytes) -> bool:
    if not raw_body:
        return False
    try:
        js = json.loads(raw_body.decode("utf-8"))
        return isinstance(js, dict) and js.get("stream") is True
    except Exception:
        return False


def is_event_stream(headers: httpx.Headers) -> bool:
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return content_type == "text/event-stream"


async def wait_for_client_disconnect(req: Request) -> None:
    while True:
        if await req.is_disconnected():
            raise ClientDisconnectError()
        await asyncio.sleep(DISCONNECT_POLL_SEC)


async def _cancel_task(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task


async def send_upstream(req: Request, request: httpx.Request) -> httpx.Response:
    # For non-streaming Ollama calls, response headers may not arrive until
    # generation finishes. If the downstream client disconnects while we are
    # still awaiting httpx.send(), cancelling this task closes the upstream
    # connection so Ollama can observe request cancellation and stop inference.
    send_task = asyncio.create_task(app.state.http_client.send(request, stream=True))
    disconnect_task = asyncio.create_task(wait_for_client_disconnect(req))
    try:
        done, pending = await asyncio.wait(
            {send_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disconnect_task in done:
            if send_task in done:
                with suppress(asyncio.CancelledError, Exception):
                    up = send_task.result()
                    await up.aclose()
            else:
                await _cancel_task(send_task)
            raise disconnect_task.exception() or ClientDisconnectError()

        disconnect_task.cancel()
        for task in pending:
            task.cancel()
        return send_task.result()
    finally:
        await _cancel_task(disconnect_task)


async def read_upstream_body(req: Request, up: httpx.Response) -> bytes:
    # After upstream headers are available, continue racing body reads against
    # downstream disconnects. Closing the response propagates cancellation to
    # Ollama instead of letting background generation burn compute.
    read_task = asyncio.create_task(up.aread())
    disconnect_task = asyncio.create_task(wait_for_client_disconnect(req))
    try:
        done, pending = await asyncio.wait(
            {read_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disconnect_task in done:
            await up.aclose()
            raise disconnect_task.exception() or ClientDisconnectError()

        disconnect_task.cancel()
        for task in pending:
            task.cancel()
        return read_task.result()
    finally:
        for task in (read_task, disconnect_task):
            if not task.done():
                task.cancel()


async def read_upstream_line(req: Request, iterator, up: httpx.Response) -> Optional[str]:
    line_task = asyncio.create_task(iterator.__anext__())
    disconnect_task = asyncio.create_task(wait_for_client_disconnect(req))
    try:
        done, pending = await asyncio.wait(
            {line_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disconnect_task in done:
            await up.aclose()
            raise disconnect_task.exception() or ClientDisconnectError()

        disconnect_task.cancel()
        for task in pending:
            task.cancel()

        try:
            return line_task.result()
        except StopAsyncIteration:
            return None
    finally:
        for task in (line_task, disconnect_task):
            if not task.done():
                task.cancel()


async def collect_ollama_chat_stream_response(req: Request, up: httpx.Response) -> Dict[str, Any]:
    iterator = up.aiter_lines().__aiter__()
    response: Dict[str, Any] = {"message": {"role": "assistant", "content": ""}}

    while True:
        line = await read_upstream_line(req, iterator, up)
        if line is None:
            break
        if not line:
            continue

        chunk = json.loads(line)
        response["model"] = chunk.get("model", response.get("model", ""))
        response["created_at"] = chunk.get("created_at", response.get("created_at"))

        message = chunk.get("message") if isinstance(chunk.get("message"), dict) else {}
        response_message = response.setdefault("message", {"role": "assistant", "content": ""})
        if "role" in message:
            response_message["role"] = message["role"]
        if "content" in message:
            response_message["content"] = response_message.get("content", "") + message.get("content", "")
        if "thinking" in message:
            response_message["thinking"] = response_message.get("thinking", "") + message.get("thinking", "")
        if message.get("tool_calls") is not None:
            response_message["tool_calls"] = message["tool_calls"]

        if chunk.get("done") is True:
            response["done"] = True
            for key in ("done_reason", "total_duration", "load_duration", "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration"):
                if key in chunk:
                    response[key] = chunk[key]
            break

    return response


async def collect_ollama_generate_stream_response(req: Request, up: httpx.Response) -> Dict[str, Any]:
    iterator = up.aiter_lines().__aiter__()
    response: Dict[str, Any] = {"response": ""}

    while True:
        line = await read_upstream_line(req, iterator, up)
        if line is None:
            break
        if not line:
            continue

        chunk = json.loads(line)
        response["model"] = chunk.get("model", response.get("model", ""))
        response["created_at"] = chunk.get("created_at", response.get("created_at"))
        response["response"] = response.get("response", "") + chunk.get("response", "")

        if chunk.get("done") is True:
            response["done"] = True
            for key in ("done_reason", "context", "total_duration", "load_duration", "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration"):
                if key in chunk:
                    response[key] = chunk[key]
            break

    return response


OPENAI_TO_OLLAMA_OPTIONS = {
    "max_tokens": "num_predict",
    "max_completion_tokens": "num_predict",
    "temperature": "temperature",
    "top_p": "top_p",
    "seed": "seed",
    "stop": "stop",
    "frequency_penalty": "repeat_penalty",
    "presence_penalty": "presence_penalty",
}


def extract_data_uri_payload(value: str) -> str:
    if value.startswith("data:") and "," in value:
        return value.split(",", 1)[1]
    return value


def normalize_openai_content(content: Any) -> Tuple[str, List[str]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content), []

    text_parts: List[str] = []
    images: List[str] = []
    for part in content:
        if not isinstance(part, dict):
            text_parts.append(str(part))
            continue

        part_type = part.get("type")
        if part_type in {"text", "input_text"}:
            text_parts.append(str(part.get("text", "")))
        elif part_type in {"image_url", "input_image"}:
            image_value = part.get("image_url") or part.get("image")
            if isinstance(image_value, dict):
                image_value = image_value.get("url")
            if isinstance(image_value, str):
                images.append(extract_data_uri_payload(image_value))

    return "".join(text_parts), images


def normalize_tool_calls(tool_calls: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(tool_calls, list):
        return None

    normalized = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            continue

        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                arguments = {"arguments": arguments}

        normalized.append({
            "function": {
                "name": function.get("name", ""),
                "arguments": arguments,
            }
        })

    return normalized or None


def normalize_openai_messages(messages: Any) -> List[Dict[str, Any]]:
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")

    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("each message must be an object")

        content, images = normalize_openai_content(message.get("content"))
        out: Dict[str, Any] = {
            "role": message.get("role", "user"),
            "content": content,
        }

        thinking = message.get("thinking", message.get("reasoning_content"))
        if thinking is not None:
            out["thinking"] = thinking
        if images:
            out["images"] = images

        tool_calls = normalize_tool_calls(message.get("tool_calls"))
        if tool_calls is not None:
            out["tool_calls"] = tool_calls

        tool_name = message.get("tool_name") or message.get("name")
        if tool_name is not None:
            out["tool_name"] = tool_name

        normalized.append(out)

    return normalized


def map_response_format(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if value.get("type") == "json_object":
        return "json"
    if value.get("type") == "json_schema":
        json_schema = value.get("json_schema")
        if isinstance(json_schema, dict) and "schema" in json_schema:
            return json_schema["schema"]
    return value


def map_reasoning_to_think(value: Any, model: str) -> Any:
    if isinstance(value, dict):
        value = value.get("effort")
    if value is None:
        return None
    if model.startswith("gpt-oss"):
        return value
    return value in {"low", "medium", "high"}


def build_ollama_options(openai_payload: Dict[str, Any]) -> Dict[str, Any]:
    options: Dict[str, Any] = {}
    for openai_name, ollama_name in OPENAI_TO_OLLAMA_OPTIONS.items():
        if openai_name in openai_payload and openai_payload[openai_name] is not None:
            options[ollama_name] = openai_payload[openai_name]

    native_options = openai_payload.get("options")
    if native_options is not None:
        if not isinstance(native_options, dict):
            raise ValueError("options must be an object")
        options.update(native_options)

    return options


def build_ollama_chat_payload(openai_payload: Dict[str, Any]) -> Dict[str, Any]:
    model = openai_payload.get("model")
    if not model:
        raise ValueError("model is required")

    options = build_ollama_options(openai_payload)
    payload: Dict[str, Any] = {
        "model": model,
        "messages": normalize_openai_messages(openai_payload.get("messages")),
        "stream": openai_payload.get("stream", False) is True,
    }
    if options:
        payload["options"] = options

    if "format" in openai_payload:
        payload["format"] = openai_payload["format"]
    elif "response_format" in openai_payload:
        payload["format"] = map_response_format(openai_payload["response_format"])

    for key in ("keep_alive", "tools"):
        if key in openai_payload and openai_payload[key] is not None:
            payload[key] = openai_payload[key]

    think = openai_payload.get("think")
    if think is None:
        think = map_reasoning_to_think(openai_payload.get("reasoning_effort"), model)
    if think is None:
        think = map_reasoning_to_think(openai_payload.get("reasoning"), model)
    if think is not None:
        payload["think"] = think

    return payload


def normalize_completion_prompt(prompt: Any) -> str:
    if prompt is None:
        raise ValueError("prompt is required")
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        if all(isinstance(item, str) for item in prompt):
            return "\n".join(prompt)
        if all(isinstance(item, int) for item in prompt):
            return " ".join(str(item) for item in prompt)
    return str(prompt)


def build_ollama_generate_payload(openai_payload: Dict[str, Any]) -> Dict[str, Any]:
    model = openai_payload.get("model")
    if not model:
        raise ValueError("model is required")

    payload: Dict[str, Any] = {
        "model": model,
        "prompt": normalize_completion_prompt(openai_payload.get("prompt")),
        "stream": openai_payload.get("stream", False) is True,
    }

    options = build_ollama_options(openai_payload)
    if options:
        payload["options"] = options

    if "format" in openai_payload:
        payload["format"] = openai_payload["format"]
    elif "response_format" in openai_payload:
        payload["format"] = map_response_format(openai_payload["response_format"])

    for key in ("suffix", "system", "template", "context", "raw", "keep_alive", "images"):
        if key in openai_payload and openai_payload[key] is not None:
            payload[key] = openai_payload[key]

    think = openai_payload.get("think")
    if think is None:
        think = map_reasoning_to_think(openai_payload.get("reasoning_effort"), model)
    if think is None:
        think = map_reasoning_to_think(openai_payload.get("reasoning"), model)
    if think is not None:
        payload["think"] = think

    return payload


def normalize_finish_reason(reason: Optional[str]) -> str:
    if not reason:
        return "stop"
    if reason in {"stop", "length", "tool_calls", "content_filter"}:
        return reason
    return "stop"


def convert_ollama_tool_calls(tool_calls: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(tool_calls, list):
        return None

    converted = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        converted.append({
            "id": tool_call.get("id") or f"call_{uuid.uuid4().hex}",
            "type": "function",
            "function": {
                "name": function.get("name", ""),
                "arguments": arguments,
            },
        })

    return converted or None


def usage_from_ollama(data: Dict[str, Any]) -> Dict[str, int]:
    prompt_tokens = int(data.get("prompt_eval_count") or 0)
    completion_tokens = int(data.get("eval_count") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def convert_ollama_chat_response(data: Dict[str, Any], response_id: str, created: int) -> Dict[str, Any]:
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    openai_message: Dict[str, Any] = {
        "role": message.get("role", "assistant"),
        "content": message.get("content", ""),
    }
    if "thinking" in message:
        openai_message["reasoning_content"] = message["thinking"]

    tool_calls = convert_ollama_tool_calls(message.get("tool_calls"))
    finish_reason = normalize_finish_reason(data.get("done_reason"))
    if tool_calls is not None:
        openai_message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": data.get("model", ""),
        "choices": [{
            "index": 0,
            "message": openai_message,
            "finish_reason": finish_reason,
        }],
        "usage": usage_from_ollama(data),
    }


def convert_ollama_chat_stream_chunk(data: Dict[str, Any], response_id: str, created: int) -> Dict[str, Any]:
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    delta: Dict[str, Any] = {}
    if "role" in message:
        delta["role"] = message["role"]
    if "content" in message:
        delta["content"] = message["content"]
    if "thinking" in message:
        delta["reasoning_content"] = message["thinking"]

    tool_calls = convert_ollama_tool_calls(message.get("tool_calls"))
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls

    finish_reason = None
    if data.get("done") is True:
        finish_reason = "tool_calls" if tool_calls is not None else normalize_finish_reason(data.get("done_reason"))

    chunk: Dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": data.get("model", ""),
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    if data.get("done") is True:
        chunk["usage"] = usage_from_ollama(data)
    return chunk


def convert_ollama_generate_response(data: Dict[str, Any], response_id: str, created: int) -> Dict[str, Any]:
    return {
        "id": response_id,
        "object": "text_completion",
        "created": created,
        "model": data.get("model", ""),
        "choices": [{
            "text": data.get("response", ""),
            "index": 0,
            "logprobs": None,
            "finish_reason": normalize_finish_reason(data.get("done_reason")),
        }],
        "usage": usage_from_ollama(data),
    }


def convert_ollama_generate_stream_chunk(data: Dict[str, Any], response_id: str, created: int) -> Dict[str, Any]:
    finish_reason = None
    if data.get("done") is True:
        finish_reason = normalize_finish_reason(data.get("done_reason"))

    chunk: Dict[str, Any] = {
        "id": response_id,
        "object": "text_completion",
        "created": created,
        "model": data.get("model", ""),
        "choices": [{
            "text": data.get("response", ""),
            "index": 0,
            "logprobs": None,
            "finish_reason": finish_reason,
        }],
    }
    if data.get("done") is True:
        chunk["usage"] = usage_from_ollama(data)
    return chunk


# ---------------------------
# Status APIs (Pull)
# ---------------------------
@app.get("/tasks/status/{task_id}")
async def get_status(task_id: str):
    v = await app.state.redis.get(rkey(task_id))
    if not v:
        raise HTTPException(status_code=404, detail="task_id not found")
    return JSONResponse(content=json.loads(v))


@app.get("/tasks/status")
async def list_status(limit: int = 50):
    limit = max(1, min(limit, 500))
    items = []

    task_ids = await app.state.redis.zrevrange(STATUS_INDEX_KEY, 0, limit - 1)
    if not task_ids:
        return {"items": items, "count": 0}

    keys = [rkey(task_id) for task_id in task_ids]
    values = await app.state.redis.mget(keys)
    stale_task_ids = []

    for task_id, value in zip(task_ids, values):
        if not value:
            stale_task_ids.append(task_id)
            continue
        items.append(json.loads(value))

    if stale_task_ids:
        await app.state.redis.zrem(STATUS_INDEX_KEY, *stale_task_ids)

    return {"items": items, "count": len(items)}


# ---------------------------
# Proxy core
# ---------------------------
async def forward(req: Request, task_id: str) -> Response:
    upstream_url = f"{UPSTREAM_BASE}{req.url.path}"
    if req.url.query:
        upstream_url += f"?{req.url.query}"

    ext = build_extensions(req, requested_stream=False)

    try:
        await set_status(task_id, "PENDING", "accepted", "request accepted", ext, TTL_RUNNING)

        raw_body = await req.body()
        ext = build_extensions(req, requested_stream(raw_body))
        await set_status(task_id, "RUNNING", "forwarding", "forwarding to upstream", ext, TTL_RUNNING)

        request = app.state.http_client.build_request(
            method=req.method,
            url=upstream_url,
            headers=proxy_headers(req.headers, drop_host=True),
            content=raw_body,
        )
        up = await send_upstream(req, request)
        status_code = up.status_code
        resp_headers = proxy_headers(up.headers)
        response_stream = is_event_stream(up.headers)
        ext["stream"] = response_stream
        ext["response_stream"] = response_stream

        if response_stream:
            last_hb = time.time()

            async def gen():
                nonlocal last_hb
                try:
                    async for chunk in up.aiter_bytes():
                        now = time.time()
                        if now - last_hb >= HEARTBEAT_SEC:
                            await set_status(task_id, "RUNNING", "streaming", "stream alive", ext, TTL_RUNNING)
                            last_hb = now
                        if chunk:
                            yield chunk

                    await finish_status(task_id, status_code, ext)
                except (httpx.StreamError, httpx.ReadError) as e:
                    await set_status(task_id, "FAILED", "error", f"stream error: {type(e).__name__}", ext, TTL_DONE)
                    raise
                except asyncio.CancelledError:
                    await set_status(task_id, "FAILED", "error", "client disconnected", ext, TTL_DONE)
                    raise
                except Exception as e:
                    await set_status(task_id, "FAILED", "error", f"gateway error: {type(e).__name__}", ext, TTL_DONE)
                    raise
                finally:
                    await up.aclose()

            resp = StreamingResponse(gen(), status_code=status_code, headers=resp_headers)
            resp.headers["X-Task-Id"] = task_id
            return resp

        try:
            body = await read_upstream_body(req, up)
        finally:
            await up.aclose()

        await finish_status(task_id, status_code, ext)
        resp = Response(
            content=body,
            status_code=status_code,
            headers=resp_headers,
            media_type=up.headers.get("content-type"),
        )
        resp.headers["X-Task-Id"] = task_id
        return resp

    except httpx.RequestError as e:
        await set_status(task_id, "FAILED", "error", f"upstream request error: {type(e).__name__}", ext, TTL_DONE)
        raise HTTPException(status_code=502, detail="Bad gateway")
    except ClientDisconnectError:
        await set_status(task_id, "FAILED", "error", "client disconnected", ext, TTL_DONE)
        raise HTTPException(status_code=499, detail="Client disconnected")
    except Exception as e:
        await set_status(task_id, "FAILED", "error", f"gateway error: {type(e).__name__}", ext, TTL_DONE)
        raise


async def forward_chat_completions(req: Request, task_id: str) -> Response:
    upstream_url = f"{UPSTREAM_BASE}/api/chat"
    ext = build_extensions(req, requested_stream=False)
    ext["upstream_path"] = "/api/chat"

    try:
        await set_status(task_id, "PENDING", "accepted", "request accepted", ext, TTL_RUNNING)

        raw_body = await req.body()
        try:
            openai_payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            if not isinstance(openai_payload, dict):
                raise ValueError("request body must be a JSON object")
            ollama_payload = build_ollama_chat_payload(openai_payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            await set_status(task_id, "FAILED", "error", str(e), ext, TTL_DONE)
            raise HTTPException(status_code=400, detail=str(e))

        ext = build_extensions(req, bool(ollama_payload.get("stream")))
        ext["upstream_path"] = "/api/chat"
        await set_status(task_id, "RUNNING", "forwarding", "forwarding to upstream", ext, TTL_RUNNING)

        headers = proxy_headers(req.headers, drop_host=True)
        headers["content-type"] = "application/json"
        request = app.state.http_client.build_request(
            method="POST",
            url=upstream_url,
            headers=headers,
            content=json.dumps(ollama_payload, ensure_ascii=False).encode("utf-8"),
        )
        up = await send_upstream(req, request)
        status_code = up.status_code

        if status_code < 200 or status_code >= 300:
            try:
                body = await read_upstream_body(req, up)
            finally:
                await up.aclose()

            ext["stream"] = False
            ext["response_stream"] = False
            await finish_status(task_id, status_code, ext)
            resp = Response(
                content=body,
                status_code=status_code,
                headers=proxy_headers(up.headers),
                media_type=up.headers.get("content-type"),
            )
            resp.headers["X-Task-Id"] = task_id
            return resp

        response_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = now_ts()

        if ollama_payload.get("stream") is True:
            ext["stream"] = True
            ext["response_stream"] = True
            last_hb = time.time()

            async def gen():
                nonlocal last_hb
                try:
                    async for line in up.aiter_lines():
                        now = time.time()
                        if now - last_hb >= HEARTBEAT_SEC:
                            await set_status(task_id, "RUNNING", "streaming", "stream alive", ext, TTL_RUNNING)
                            last_hb = now
                        if not line:
                            continue

                        data = json.loads(line)
                        chunk = convert_ollama_chat_stream_chunk(data, response_id, created)
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

                    yield b"data: [DONE]\n\n"
                    await finish_status(task_id, status_code, ext)
                except (httpx.StreamError, httpx.ReadError) as e:
                    await set_status(task_id, "FAILED", "error", f"stream error: {type(e).__name__}", ext, TTL_DONE)
                    raise
                except asyncio.CancelledError:
                    await set_status(task_id, "FAILED", "error", "client disconnected", ext, TTL_DONE)
                    raise
                except Exception as e:
                    await set_status(task_id, "FAILED", "error", f"gateway error: {type(e).__name__}", ext, TTL_DONE)
                    raise
                finally:
                    await up.aclose()

            resp = StreamingResponse(
                gen(),
                status_code=status_code,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )
            resp.headers["X-Task-Id"] = task_id
            return resp

        try:
            body = await read_upstream_body(req, up)
        finally:
            await up.aclose()

        ext["stream"] = False
        ext["response_stream"] = False
        try:
            ollama_response = json.loads(body.decode("utf-8"))
            if not isinstance(ollama_response, dict):
                raise ValueError("upstream response must be a JSON object")
            openai_response = convert_ollama_chat_response(ollama_response, response_id, created)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            await set_status(task_id, "FAILED", "error", f"response transform error: {type(e).__name__}", ext, TTL_DONE)
            raise HTTPException(status_code=502, detail="Bad gateway")

        await finish_status(task_id, status_code, ext)
        resp = JSONResponse(content=openai_response, status_code=status_code)
        resp.headers["X-Task-Id"] = task_id
        return resp

    except httpx.RequestError as e:
        await set_status(task_id, "FAILED", "error", f"upstream request error: {type(e).__name__}", ext, TTL_DONE)
        raise HTTPException(status_code=502, detail="Bad gateway")
    except ClientDisconnectError:
        await set_status(task_id, "FAILED", "error", "client disconnected", ext, TTL_DONE)
        raise HTTPException(status_code=499, detail="Client disconnected")


async def forward_completions(req: Request, task_id: str) -> Response:
    upstream_url = f"{UPSTREAM_BASE}/api/generate"
    ext = build_extensions(req, requested_stream=False)
    ext["upstream_path"] = "/api/generate"

    try:
        await set_status(task_id, "PENDING", "accepted", "request accepted", ext, TTL_RUNNING)

        raw_body = await req.body()
        try:
            openai_payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            if not isinstance(openai_payload, dict):
                raise ValueError("request body must be a JSON object")
            ollama_payload = build_ollama_generate_payload(openai_payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            await set_status(task_id, "FAILED", "error", str(e), ext, TTL_DONE)
            raise HTTPException(status_code=400, detail=str(e))

        ext = build_extensions(req, bool(ollama_payload.get("stream")))
        ext["upstream_path"] = "/api/generate"
        await set_status(task_id, "RUNNING", "forwarding", "forwarding to upstream", ext, TTL_RUNNING)

        headers = proxy_headers(req.headers, drop_host=True)
        headers["content-type"] = "application/json"
        request = app.state.http_client.build_request(
            method="POST",
            url=upstream_url,
            headers=headers,
            content=json.dumps(ollama_payload, ensure_ascii=False).encode("utf-8"),
        )
        up = await send_upstream(req, request)
        status_code = up.status_code

        if status_code < 200 or status_code >= 300:
            try:
                body = await read_upstream_body(req, up)
            finally:
                await up.aclose()

            ext["stream"] = False
            ext["response_stream"] = False
            await finish_status(task_id, status_code, ext)
            resp = Response(
                content=body,
                status_code=status_code,
                headers=proxy_headers(up.headers),
                media_type=up.headers.get("content-type"),
            )
            resp.headers["X-Task-Id"] = task_id
            return resp

        response_id = f"cmpl-{uuid.uuid4().hex}"
        created = now_ts()

        if ollama_payload.get("stream") is True:
            ext["stream"] = True
            ext["response_stream"] = True
            last_hb = time.time()

            async def gen():
                nonlocal last_hb
                try:
                    async for line in up.aiter_lines():
                        now = time.time()
                        if now - last_hb >= HEARTBEAT_SEC:
                            await set_status(task_id, "RUNNING", "streaming", "stream alive", ext, TTL_RUNNING)
                            last_hb = now
                        if not line:
                            continue

                        data = json.loads(line)
                        chunk = convert_ollama_generate_stream_chunk(data, response_id, created)
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

                    yield b"data: [DONE]\n\n"
                    await finish_status(task_id, status_code, ext)
                except (httpx.StreamError, httpx.ReadError) as e:
                    await set_status(task_id, "FAILED", "error", f"stream error: {type(e).__name__}", ext, TTL_DONE)
                    raise
                except asyncio.CancelledError:
                    await set_status(task_id, "FAILED", "error", "client disconnected", ext, TTL_DONE)
                    raise
                except Exception as e:
                    await set_status(task_id, "FAILED", "error", f"gateway error: {type(e).__name__}", ext, TTL_DONE)
                    raise
                finally:
                    await up.aclose()

            resp = StreamingResponse(
                gen(),
                status_code=status_code,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )
            resp.headers["X-Task-Id"] = task_id
            return resp

        try:
            body = await read_upstream_body(req, up)
        finally:
            await up.aclose()

        ext["stream"] = False
        ext["response_stream"] = False
        try:
            ollama_response = json.loads(body.decode("utf-8"))
            if not isinstance(ollama_response, dict):
                raise ValueError("upstream response must be a JSON object")
            openai_response = convert_ollama_generate_response(ollama_response, response_id, created)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            await set_status(task_id, "FAILED", "error", f"response transform error: {type(e).__name__}", ext, TTL_DONE)
            raise HTTPException(status_code=502, detail="Bad gateway")

        await finish_status(task_id, status_code, ext)
        resp = JSONResponse(content=openai_response, status_code=status_code)
        resp.headers["X-Task-Id"] = task_id
        return resp

    except httpx.RequestError as e:
        await set_status(task_id, "FAILED", "error", f"upstream request error: {type(e).__name__}", ext, TTL_DONE)
        raise HTTPException(status_code=502, detail="Bad gateway")
    except ClientDisconnectError:
        await set_status(task_id, "FAILED", "error", "client disconnected", ext, TTL_DONE)
        raise HTTPException(status_code=499, detail="Client disconnected")


# ---------------------------
# Proxy routes: all OpenAI-compat endpoints live under /v1/*
# ---------------------------
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def v1_proxy(path: str, req: Request):
    task_id = get_task_id(req)
    if req.method == "POST" and path == "chat/completions":
        return await forward_chat_completions(req, task_id)
    if req.method == "POST" and path == "completions":
        return await forward_completions(req, task_id)
    return await forward(req, task_id)
