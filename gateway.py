import os
import json
import time
import uuid
from typing import Optional, Dict, Any

import httpx
import redis
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

# ---------------------------
# Config
# ---------------------------
APP_VERSION = "1.0"
EVENT_TYPE = "task.status.update"

# Same-container upstream
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "http://127.0.0.1:11434")
ALGORITHM_ID = os.getenv("ALGORITHM_ID", "ollama-openai")

REDIS_HOST = os.getenv("REDIS_HOST", "172.28.1.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_USER = os.getenv("REDIS_USER", "default")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

# TTL seconds
TTL_RUNNING = int(os.getenv("TTL_RUNNING", "3600"))   # running/pending keep 1h
TTL_DONE = int(os.getenv("TTL_DONE", "86400"))        # done keep 24h

# Streaming heartbeat interval
HEARTBEAT_SEC = float(os.getenv("HEARTBEAT_SEC", "10"))

# ---------------------------
# Clients
# ---------------------------
rds = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    username=REDIS_USER if REDIS_USER else None,
    password=REDIS_PASSWORD if REDIS_PASSWORD else None,
    db=REDIS_DB,
    decode_responses=True,
)

app = FastAPI(title="Ollama OpenAI Gateway + Task Status", version=APP_VERSION)
http_client = httpx.AsyncClient(timeout=None)


# ---------------------------
# Helpers
# ---------------------------
def now_ts() -> int:
    return int(time.time())


def rkey(task_id: str) -> str:
    return f"ts:ollama:{task_id}"


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


def write_status(task_id: str, evt: Dict[str, Any], ttl: int) -> None:
    rds.set(rkey(task_id), json.dumps(evt, ensure_ascii=False), ex=ttl)


def get_task_id(req: Request) -> str:
    tid = req.headers.get("x-task-id")
    if tid and tid.strip():
        return tid.strip()
    return str(uuid.uuid4())


def hop_by_hop_filter(headers: httpx.Headers) -> Dict[str, str]:
    # Remove hop-by-hop headers for proxy correctness
    hop = {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade",
    }
    out: Dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in hop:
            continue
        out[k] = v
    return out


def build_extensions(req: Request, is_stream: bool) -> Dict[str, Any]:
    return {
        "method": req.method,
        "path": req.url.path,
        "query": str(req.url.query) if req.url.query else "",
        "stream": is_stream,
    }


def guess_is_stream(raw_body: bytes) -> bool:
    if not raw_body:
        return False
    try:
        js = json.loads(raw_body.decode("utf-8"))
        return isinstance(js, dict) and js.get("stream") is True
    except Exception:
        return False


# ---------------------------
# Status APIs (Pull)
# ---------------------------
@app.get("/tasks/status/{task_id}")
def get_status(task_id: str):
    v = rds.get(rkey(task_id))
    if not v:
        raise HTTPException(status_code=404, detail="task_id not found")
    return JSONResponse(content=json.loads(v))


@app.get("/tasks/status")
def list_status(limit: int = 50):
    # Debug/ops helper. Use SCAN, capped by limit.
    items = []
    cnt = 0
    for k in rds.scan_iter(match="ts:ollama:*"):
        v = rds.get(k)
        if not v:
            continue
        items.append(json.loads(v))
        cnt += 1
        if cnt >= limit:
            break
    return {"items": items, "count": len(items)}


# ---------------------------
# Proxy core
# ---------------------------
async def forward(req: Request, task_id: str) -> Response:
    upstream_url = f"{UPSTREAM_BASE}{req.url.path}"
    if req.url.query:
        upstream_url += f"?{req.url.query}"

    raw_body = await req.body()
    is_stream = guess_is_stream(raw_body)
    ext = build_extensions(req, is_stream)

    # Record lifecycle: PENDING -> RUNNING
    write_status(task_id, make_evt(task_id, "PENDING", stage="queued", message="request accepted", extensions=ext), TTL_RUNNING)
    write_status(task_id, make_evt(task_id, "RUNNING", stage="forwarding", message="forwarding to upstream", extensions=ext), TTL_RUNNING)

    # Prepare headers (remove host; keep others to preserve OpenAI compat)
    headers = dict(req.headers)
    headers.pop("host", None)

    try:
        if is_stream:
            # Critical fix: keep upstream stream open until generator completes
            request = http_client.build_request(
                method=req.method,
                url=upstream_url,
                headers=headers,
                content=raw_body,
            )
            up = await http_client.send(request, stream=True)
            status_code = up.status_code
            resp_headers = hop_by_hop_filter(up.headers)
            last_hb = time.time()

            async def gen():
                nonlocal last_hb
                try:
                    async for chunk in up.aiter_bytes():
                        now = time.time()
                        if now - last_hb >= HEARTBEAT_SEC:
                            write_status(
                                task_id,
                                make_evt(task_id, "RUNNING", stage="streaming", message="stream alive", extensions=ext),
                                TTL_RUNNING,
                            )
                            last_hb = now
                        if chunk:
                            yield chunk

                    # Upstream finished normally
                    if 200 <= status_code < 300:
                        write_status(task_id, make_evt(task_id, "SUCCESS", stage="done", message="completed", extensions=ext), TTL_DONE)
                    else:
                        write_status(task_id, make_evt(task_id, "FAILED", stage="error", message=f"upstream status {status_code}", extensions=ext), TTL_DONE)

                except (httpx.StreamError, httpx.ReadError) as e:
                    write_status(task_id, make_evt(task_id, "FAILED", stage="error", message=f"stream error: {type(e).__name__}", extensions=ext), TTL_DONE)
                    raise
                except Exception as e:
                    # Client disconnects can appear as cancellations; still mark failed for traceability
                    write_status(task_id, make_evt(task_id, "FAILED", stage="error", message=f"gateway error: {type(e).__name__}", extensions=ext), TTL_DONE)
                    raise
                finally:
                    # Always close upstream stream
                    await up.aclose()

            resp = StreamingResponse(gen(), status_code=status_code, headers=resp_headers)
            resp.headers["X-Task-Id"] = task_id
            return resp

        # Non-streaming request
        up = await http_client.request(
            method=req.method,
            url=upstream_url,
            headers=headers,
            content=raw_body,
        )

        resp_headers = hop_by_hop_filter(up.headers)

        if 200 <= up.status_code < 300:
            write_status(task_id, make_evt(task_id, "SUCCESS", stage="done", message="completed", extensions=ext), TTL_DONE)
        else:
            write_status(task_id, make_evt(task_id, "FAILED", stage="error", message=f"upstream status {up.status_code}", extensions=ext), TTL_DONE)

        resp = Response(
            content=up.content,
            status_code=up.status_code,
            headers=resp_headers,
            media_type=up.headers.get("content-type"),
        )
        resp.headers["X-Task-Id"] = task_id
        return resp

    except httpx.RequestError as e:
        write_status(task_id, make_evt(task_id, "FAILED", stage="error", message=f"upstream request error: {type(e).__name__}", extensions=ext), TTL_DONE)
        raise HTTPException(status_code=502, detail="Bad gateway")
    except Exception as e:
        # Any other error
        write_status(task_id, make_evt(task_id, "FAILED", stage="error", message=f"gateway error: {type(e).__name__}", extensions=ext), TTL_DONE)
        raise


# ---------------------------
# Proxy routes: all OpenAI-compat endpoints live under /v1/*
# ---------------------------
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def v1_proxy(path: str, req: Request):
    task_id = get_task_id(req)
    return await forward(req, task_id)


# ---------------------------
# Shutdown
# ---------------------------
@app.on_event("shutdown")
async def _shutdown():
    await http_client.aclose()