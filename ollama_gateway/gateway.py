import os
import json
import time
import uuid
import asyncio
from dataclasses import dataclass
from contextlib import asynccontextmanager, suppress
from typing import Optional, Dict, Any, Callable

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from .ollama_to_openai import (
    convert_ollama_tool_calls,
    convert_ollama_chat_response,
    convert_ollama_chat_stream_chunk,
    convert_ollama_generate_response,
    convert_ollama_generate_stream_chunk,
    normalize_finish_reason,
    usage_from_ollama,
)
from .openai_to_ollama import (
    OPENAI_TO_OLLAMA_OPTIONS,
    build_ollama_chat_payload,
    build_ollama_generate_payload,
    build_ollama_options,
    extract_data_uri_payload,
    map_reasoning_to_think,
    map_response_format,
    normalize_completion_prompt,
    normalize_openai_content,
    normalize_openai_messages,
    normalize_tool_calls,
)
from .task_status import (
    StatusConfig,
    TaskStatusStore,
    make_evt as make_status_evt,
    now_ts as status_now_ts,
    rkey as status_rkey,
    status_index_cutoff as task_status_index_cutoff,
)

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
STATUS_INDEX_CLEANUP_INTERVAL_SEC = float(os.getenv("STATUS_INDEX_CLEANUP_INTERVAL_SEC", "60"))

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


def status_config() -> StatusConfig:
    return StatusConfig(
        app_version=APP_VERSION,
        event_type=EVENT_TYPE,
        algorithm_id=ALGORITHM_ID,
        index_key=STATUS_INDEX_KEY,
        ttl_running=TTL_RUNNING,
        ttl_done=TTL_DONE,
        cleanup_interval_sec=STATUS_INDEX_CLEANUP_INTERVAL_SEC,
    )


def create_status_store(redis) -> TaskStatusStore:
    return TaskStatusStore(redis, status_config())

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
    app.state.status_store = create_status_store(app.state.redis)
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
    return status_now_ts()


def rkey(task_id: str) -> str:
    return status_rkey(task_id)


def status_index_cutoff() -> int:
    return task_status_index_cutoff(now_ts(), TTL_RUNNING, TTL_DONE)


def status_store() -> TaskStatusStore:
    store = getattr(app.state, "status_store", None)
    if store is None:
        store = create_status_store(app.state.redis)
        app.state.status_store = store
    return store


def make_evt(
    task_id: str,
    state: str,
    stage: Optional[str] = None,
    message: Optional[str] = None,
    progress: Optional[float] = None,
    extensions: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return make_status_evt(
        task_id,
        state,
        APP_VERSION,
        EVENT_TYPE,
        ALGORITHM_ID,
        now_ts(),
        stage=stage,
        message=message,
        progress=progress,
        extensions=extensions,
    )


async def write_status(task_id: str, evt: Dict[str, Any], ttl: int) -> None:
    await status_store().write_status(task_id, evt, ttl)


async def set_status(
    task_id: str,
    state: str,
    stage: str,
    message: str,
    extensions: Dict[str, Any],
    ttl: int,
) -> None:
    await status_store().set_status(task_id, state, stage, message, extensions, ttl)


async def finish_status(task_id: str, status_code: int, extensions: Dict[str, Any]) -> None:
    await status_store().finish_status(task_id, status_code, extensions)


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


@dataclass(frozen=True)
class OpenAICompatRoute:
    upstream_path: str
    response_id_prefix: str
    build_ollama_payload: Callable[[Dict[str, Any]], Dict[str, Any]]
    convert_response: Callable[[Dict[str, Any], str, int], Dict[str, Any]]
    convert_stream_chunk: Callable[[Dict[str, Any], str, int], Dict[str, Any]]


def build_upstream_url(path: str, query: str = "") -> str:
    upstream_url = f"{UPSTREAM_BASE}{path}"
    if query:
        upstream_url += f"?{query}"
    return upstream_url


def build_openai_extensions(req: Request, requested_stream: bool, upstream_path: str) -> Dict[str, Any]:
    ext = build_extensions(req, requested_stream)
    ext["upstream_path"] = upstream_path
    return ext


def parse_json_object(raw_body: bytes) -> Dict[str, Any]:
    payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def parse_upstream_json_object(body: bytes) -> Dict[str, Any]:
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("upstream response must be a JSON object")
    return payload


def encode_json_body(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def build_json_upstream_request(req: Request, upstream_url: str, payload: Dict[str, Any]) -> httpx.Request:
    headers = proxy_headers(req.headers, drop_host=True)
    headers["content-type"] = "application/json"
    return app.state.http_client.build_request(
        method="POST",
        url=upstream_url,
        headers=headers,
        content=encode_json_body(payload),
    )


def attach_task_id(resp: Response, task_id: str) -> Response:
    resp.headers["X-Task-Id"] = task_id
    return resp


def set_stream_flags(ext: Dict[str, Any], enabled: bool) -> None:
    ext["stream"] = enabled
    ext["response_stream"] = enabled


async def relay_upstream_error_response(
    req: Request,
    up: httpx.Response,
    task_id: str,
    status_code: int,
    ext: Dict[str, Any],
) -> Response:
    try:
        body = await read_upstream_body(req, up)
    finally:
        await up.aclose()

    set_stream_flags(ext, False)
    await finish_status(task_id, status_code, ext)
    return attach_task_id(Response(
        content=body,
        status_code=status_code,
        headers=proxy_headers(up.headers),
        media_type=up.headers.get("content-type"),
    ), task_id)


async def stream_openai_compatible_response(
    up: httpx.Response,
    task_id: str,
    status_code: int,
    ext: Dict[str, Any],
    response_id: str,
    created: int,
    convert_chunk: Callable[[Dict[str, Any], str, int], Dict[str, Any]],
) -> StreamingResponse:
    set_stream_flags(ext, True)
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
                chunk = convert_chunk(data, response_id, created)
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

    return attach_task_id(StreamingResponse(
        gen(),
        status_code=status_code,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    ), task_id)


async def build_openai_response(
    req: Request,
    up: httpx.Response,
    task_id: str,
    status_code: int,
    ext: Dict[str, Any],
    response_id: str,
    created: int,
    convert_response: Callable[[Dict[str, Any], str, int], Dict[str, Any]],
) -> Response:
    try:
        body = await read_upstream_body(req, up)
    finally:
        await up.aclose()

    set_stream_flags(ext, False)
    try:
        ollama_response = parse_upstream_json_object(body)
        openai_response = convert_response(ollama_response, response_id, created)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        await set_status(task_id, "FAILED", "error", f"response transform error: {type(e).__name__}", ext, TTL_DONE)
        raise HTTPException(status_code=502, detail="Bad gateway")

    await finish_status(task_id, status_code, ext)
    return attach_task_id(JSONResponse(content=openai_response, status_code=status_code), task_id)


async def forward_openai_compatible(req: Request, task_id: str, route: OpenAICompatRoute) -> Response:
    upstream_url = build_upstream_url(route.upstream_path)
    ext = build_openai_extensions(req, requested_stream=False, upstream_path=route.upstream_path)

    try:
        await set_status(task_id, "PENDING", "accepted", "request accepted", ext, TTL_RUNNING)

        raw_body = await req.body()
        try:
            openai_payload = parse_json_object(raw_body)
            ollama_payload = route.build_ollama_payload(openai_payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            await set_status(task_id, "FAILED", "error", str(e), ext, TTL_DONE)
            raise HTTPException(status_code=400, detail=str(e))

        ext = build_openai_extensions(req, bool(ollama_payload.get("stream")), route.upstream_path)
        await set_status(task_id, "RUNNING", "forwarding", "forwarding to upstream", ext, TTL_RUNNING)

        request = build_json_upstream_request(req, upstream_url, ollama_payload)
        up = await send_upstream(req, request)
        status_code = up.status_code

        if status_code < 200 or status_code >= 300:
            return await relay_upstream_error_response(req, up, task_id, status_code, ext)

        response_id = f"{route.response_id_prefix}-{uuid.uuid4().hex}"
        created = now_ts()

        if ollama_payload.get("stream") is True:
            return await stream_openai_compatible_response(
                up,
                task_id,
                status_code,
                ext,
                response_id,
                created,
                route.convert_stream_chunk,
            )

        return await build_openai_response(
            req,
            up,
            task_id,
            status_code,
            ext,
            response_id,
            created,
            route.convert_response,
        )

    except httpx.RequestError as e:
        await set_status(task_id, "FAILED", "error", f"upstream request error: {type(e).__name__}", ext, TTL_DONE)
        raise HTTPException(status_code=502, detail="Bad gateway")
    except ClientDisconnectError:
        await set_status(task_id, "FAILED", "error", "client disconnected", ext, TTL_DONE)
        raise HTTPException(status_code=499, detail="Client disconnected")
    except Exception as e:
        await set_status(task_id, "FAILED", "error", f"gateway error: {type(e).__name__}", ext, TTL_DONE)
        raise


CHAT_COMPLETIONS_ROUTE = OpenAICompatRoute(
    upstream_path="/api/chat",
    response_id_prefix="chatcmpl",
    build_ollama_payload=build_ollama_chat_payload,
    convert_response=convert_ollama_chat_response,
    convert_stream_chunk=convert_ollama_chat_stream_chunk,
)


COMPLETIONS_ROUTE = OpenAICompatRoute(
    upstream_path="/api/generate",
    response_id_prefix="cmpl",
    build_ollama_payload=build_ollama_generate_payload,
    convert_response=convert_ollama_generate_response,
    convert_stream_chunk=convert_ollama_generate_stream_chunk,
)


# ---------------------------
# Status APIs (Pull)
# ---------------------------
@app.get("/tasks/status/{task_id}")
async def get_status(task_id: str):
    status = await status_store().get_status(task_id)
    if not status:
        raise HTTPException(status_code=404, detail="task_id not found")
    return JSONResponse(content=status)


@app.get("/tasks/status")
async def list_status(limit: int = 50):
    return await status_store().list_status(limit)


# ---------------------------
# Proxy core
# ---------------------------
async def forward(req: Request, task_id: str) -> Response:
    upstream_url = build_upstream_url(req.url.path, str(req.url.query))

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
    return await forward_openai_compatible(req, task_id, CHAT_COMPLETIONS_ROUTE)


async def forward_completions(req: Request, task_id: str) -> Response:
    return await forward_openai_compatible(req, task_id, COMPLETIONS_ROUTE)


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
