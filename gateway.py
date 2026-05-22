import os
import json
import time
import uuid
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

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

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

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
        up = await app.state.http_client.send(request, stream=True)
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
            body = await up.aread()
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
    except Exception as e:
        await set_status(task_id, "FAILED", "error", f"gateway error: {type(e).__name__}", ext, TTL_DONE)
        raise


# ---------------------------
# Proxy routes: all OpenAI-compat endpoints live under /v1/*
# ---------------------------
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def v1_proxy(path: str, req: Request):
    task_id = get_task_id(req)
    return await forward(req, task_id)
