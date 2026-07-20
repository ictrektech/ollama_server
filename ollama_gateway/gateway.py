import os
import json
import time
import uuid
import asyncio
import re
from dataclasses import dataclass
from contextlib import asynccontextmanager, suppress
from typing import Dict, Any, Callable

import httpx
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
# ---------------------------
# Config
# ---------------------------
APP_VERSION = "1.0"

# Same-container upstream
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "http://127.0.0.1:11434").rstrip("/")
UPSTREAM_STARTUP_TIMEOUT_SEC = float(os.getenv("UPSTREAM_STARTUP_TIMEOUT_SEC", "30"))

DISCONNECT_POLL_SEC = float(os.getenv("DISCONNECT_POLL_SEC", "0.1"))
OLLAMA_NUM_PARALLEL = int(os.getenv("OLLAMA_NUM_PARALLEL", "1") or "1")

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
    app.state.http_client = httpx.AsyncClient(timeout=None)

    try:
        await wait_for_upstream(app.state.http_client)
        print(f"Gateway v{APP_VERSION} active. Proxying to {UPSTREAM_BASE}")
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(
    title="Ollama OpenAI Gateway",
    version=APP_VERSION,
    lifespan=lifespan,
)

# ---------------------------
# Helpers
# ---------------------------
def now_ts() -> int:
    return int(time.time())


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


@dataclass
class ActiveRequest:
    id: str
    model: str
    started_at: float
    phase: str = "prefill"
    phase_started_at: float = 0.0
    last_updated_at: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_completion_tokens: int = 0
    prefill_tps: float = 0.0
    decode_tps: float = 0.0


def estimate_stream_tokens(text: str) -> int:
    if not text:
        return 0

    cjk_chars = len(re.findall(r"[\u3400-\u9fff]", text))
    non_cjk = re.sub(r"[\u3400-\u9fff]", " ", text)
    words = len(re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]", non_cjk))
    return max(1, cjk_chars + words)


class SlotTracker:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: Dict[str, ActiveRequest] = {}

    async def begin(self, model: str) -> str:
        request_id = uuid.uuid4().hex
        key = model or "-"
        now = time.monotonic()
        async with self._lock:
            self._active[request_id] = ActiveRequest(
                id=request_id,
                model=key,
                started_at=now,
                phase_started_at=now,
                last_updated_at=now,
            )
        return request_id

    async def mark_decode(self, request_id: str) -> None:
        now = time.monotonic()
        async with self._lock:
            active = self._active.get(request_id)
            if active and active.phase == "prefill":
                active.phase = "decode"
                active.phase_started_at = now
                active.last_updated_at = now

    async def update_from_ollama_chunk(self, request_id: str, data: Dict[str, Any]) -> None:
        content = ""
        message = data.get("message")
        if isinstance(message, dict):
            content = str(message.get("content") or message.get("thinking") or message.get("reasoning_content") or "")
        elif data.get("response") is not None:
            content = str(data.get("response") or "")
        elif data.get("thinking") is not None or data.get("reasoning_content") is not None:
            content = str(data.get("thinking") or data.get("reasoning_content") or "")

        if content:
            await self.mark_decode(request_id)

        prompt_tokens = int(data.get("prompt_eval_count") or 0)
        completion_tokens = int(data.get("eval_count") or 0)
        prompt_duration_ns = int(data.get("prompt_eval_duration") or 0)
        completion_duration_ns = int(data.get("eval_duration") or 0)

        now = time.monotonic()
        async with self._lock:
            active = self._active.get(request_id)
            if not active:
                return
            active.last_updated_at = now
            if content:
                active.estimated_completion_tokens += estimate_stream_tokens(content)
            if prompt_tokens:
                active.prompt_tokens = prompt_tokens
            if completion_tokens:
                active.completion_tokens = completion_tokens
            if prompt_tokens and prompt_duration_ns > 0:
                active.prefill_tps = prompt_tokens / (prompt_duration_ns / 1_000_000_000)
            if completion_tokens and completion_duration_ns > 0:
                active.decode_tps = completion_tokens / (completion_duration_ns / 1_000_000_000)
            elif active.estimated_completion_tokens and active.phase == "decode":
                elapsed = max(0.001, now - active.phase_started_at)
                active.decode_tps = active.estimated_completion_tokens / elapsed

    async def end(self, request_id: str) -> None:
        async with self._lock:
            self._active.pop(request_id, None)

    async def snapshot(self) -> Dict[str, Any]:
        async with self._lock:
            active = list(self._active.values())

        active_total = len(active)
        by_model: Dict[str, int] = {}
        phase_by_model: Dict[str, Dict[str, int]] = {}
        model_metrics: Dict[str, Dict[str, Any]] = {}
        requests = []

        for item in active:
            by_model[item.model] = by_model.get(item.model, 0) + 1
            phase_counts = phase_by_model.setdefault(item.model, {})
            phase_counts[item.phase] = phase_counts.get(item.phase, 0) + 1
            model_item = model_metrics.setdefault(item.model, {
                "active_slots": 0,
                "total_slots": OLLAMA_NUM_PARALLEL,
                "slot_usage": f"0/{OLLAMA_NUM_PARALLEL}",
                "phase": item.phase,
                "phase_counts": {},
                "prefill_tokens_per_second": 0.0,
                "decode_tokens_per_second": 0.0,
                "tokens_per_second": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            })
            model_item["active_slots"] += 1
            model_item["slot_usage"] = f"{model_item['active_slots']}/{OLLAMA_NUM_PARALLEL}"
            model_item["phase_counts"] = phase_counts
            if item.phase == "decode":
                model_item["phase"] = "decode"
            if item.prefill_tps:
                model_item["prefill_tokens_per_second"] = round(item.prefill_tps, 2)
            if item.decode_tps:
                model_item["decode_tokens_per_second"] = round(item.decode_tps, 2)
                model_item["tokens_per_second"] = round(item.decode_tps, 2)
            model_item["prompt_tokens"] += item.prompt_tokens
            model_item["completion_tokens"] += item.completion_tokens or item.estimated_completion_tokens
            requests.append({
                "id": item.id,
                "model": item.model,
                "phase": item.phase,
                "elapsed_seconds": round(max(0.0, time.monotonic() - item.started_at), 3),
                "prefill_tokens_per_second": round(item.prefill_tps, 2) if item.prefill_tps else 0,
                "decode_tokens_per_second": round(item.decode_tps, 2) if item.decode_tps else 0,
                "tokens_per_second": round(item.decode_tps, 2) if item.decode_tps else 0,
                "prompt_tokens": item.prompt_tokens,
                "completion_tokens": item.completion_tokens or item.estimated_completion_tokens,
            })

        return {
            "active_slots": active_total,
            "total_slots": OLLAMA_NUM_PARALLEL,
            "available_slots": max(0, OLLAMA_NUM_PARALLEL - active_total),
            "slot_usage": f"{active_total}/{OLLAMA_NUM_PARALLEL}",
            "active_by_model": by_model,
            "phase": "decode" if any(item.phase == "decode" for item in active) else ("prefill" if active else "idle"),
            "phase_by_model": phase_by_model,
            "model_metrics": model_metrics,
            "requests": requests,
        }


slot_tracker = SlotTracker()


def build_upstream_url(path: str, query: str = "") -> str:
    upstream_url = f"{UPSTREAM_BASE}{path}"
    if query:
        upstream_url += f"?{query}"
    return upstream_url


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


async def relay_upstream_error_response(
    req: Request,
    up: httpx.Response,
    task_id: str,
    status_code: int,
) -> Response:
    try:
        body = await read_upstream_body(req, up)
    finally:
        await up.aclose()

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
    response_id: str,
    created: int,
    convert_chunk: Callable[[Dict[str, Any], str, int], Dict[str, Any]],
    on_chunk: Callable[[Dict[str, Any]], Any] | None = None,
    on_close: Callable[[], Any] | None = None,
) -> StreamingResponse:
    async def gen():
        try:
            async for line in up.aiter_lines():
                if not line:
                    continue

                data = json.loads(line)
                if on_chunk:
                    result = on_chunk(data)
                    if asyncio.iscoroutine(result):
                        await result
                chunk = convert_chunk(data, response_id, created)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

            yield b"data: [DONE]\n\n"
        finally:
            await up.aclose()
            if on_close:
                result = on_close()
                if asyncio.iscoroutine(result):
                    await result

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
    response_id: str,
    created: int,
    convert_response: Callable[[Dict[str, Any], str, int], Dict[str, Any]],
    on_data: Callable[[Dict[str, Any]], Any] | None = None,
) -> Response:
    try:
        body = await read_upstream_body(req, up)
    finally:
        await up.aclose()

    try:
        ollama_response = parse_upstream_json_object(body)
        if on_data:
            result = on_data(ollama_response)
            if asyncio.iscoroutine(result):
                await result
        openai_response = convert_response(ollama_response, response_id, created)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=502, detail="Bad gateway")

    return attach_task_id(JSONResponse(content=openai_response, status_code=status_code), task_id)


async def forward_openai_compatible(req: Request, task_id: str, route: OpenAICompatRoute) -> Response:
    upstream_url = build_upstream_url(route.upstream_path)

    try:
        raw_body = await req.body()
        try:
            openai_payload = parse_json_object(raw_body)
            ollama_payload = route.build_ollama_payload(openai_payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

        model = str(ollama_payload.get("model") or openai_payload.get("model") or "-")
        slot_id = await slot_tracker.begin(model)
        slot_done = False
        async def end_slot_once() -> None:
            nonlocal slot_done
            if not slot_done:
                slot_done = True
                await slot_tracker.end(slot_id)

        request = build_json_upstream_request(req, upstream_url, ollama_payload)
        stream_response_returned = False
        try:
            up = await send_upstream(req, request)
            status_code = up.status_code

            if status_code < 200 or status_code >= 300:
                return await relay_upstream_error_response(req, up, task_id, status_code)

            response_id = f"{route.response_id_prefix}-{uuid.uuid4().hex}"
            created = now_ts()

            if ollama_payload.get("stream") is True:
                stream_response_returned = True
                return await stream_openai_compatible_response(
                    up,
                    task_id,
                    status_code,
                    response_id,
                    created,
                    route.convert_stream_chunk,
                    lambda data: slot_tracker.update_from_ollama_chunk(slot_id, data),
                    end_slot_once,
                )

            return await build_openai_response(
                req,
                up,
                task_id,
                status_code,
                response_id,
                created,
                route.convert_response,
                lambda data: slot_tracker.update_from_ollama_chunk(slot_id, data),
            )
        finally:
            if not ollama_payload.get("stream") or not stream_response_returned:
                await end_slot_once()

    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Bad gateway")
    except ClientDisconnectError:
        raise HTTPException(status_code=499, detail="Client disconnected")


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
# Proxy core
# ---------------------------
async def forward(req: Request, task_id: str) -> Response:
    upstream_url = build_upstream_url(req.url.path, str(req.url.query))
    tracked_slot_id = ""
    raw_payload: Dict[str, Any] = {}

    try:
        raw_body = await req.body()
        track_raw_generation = req.method == "POST" and req.url.path in {
            "/api/chat",
            "/api/generate",
            "/api/embed",
            "/api/embeddings",
        }
        if track_raw_generation:
            try:
                raw_payload = parse_json_object(raw_body)
                model = str(raw_payload.get("model") or "-")
                tracked_slot_id = await slot_tracker.begin(model)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                tracked_slot_id = ""

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
        raw_generation_stream = bool(tracked_slot_id and raw_payload.get("stream", True) is not False)

        if response_stream or raw_generation_stream:
            async def gen():
                pending = b""
                try:
                    async for chunk in up.aiter_bytes():
                        if chunk:
                            if tracked_slot_id:
                                pending += chunk
                                while b"\n" in pending:
                                    line, pending = pending.split(b"\n", 1)
                                    if line.strip():
                                        with suppress(json.JSONDecodeError, UnicodeDecodeError, ValueError):
                                            await slot_tracker.update_from_ollama_chunk(
                                                tracked_slot_id,
                                                parse_upstream_json_object(line.strip()),
                                            )
                            yield chunk
                finally:
                    await up.aclose()
                    if tracked_slot_id:
                        await slot_tracker.end(tracked_slot_id)

            resp = StreamingResponse(
                gen(),
                status_code=status_code,
                headers=resp_headers,
                media_type=up.headers.get("content-type"),
            )
            resp.headers["X-Task-Id"] = task_id
            return resp

        try:
            body = await read_upstream_body(req, up)
            if tracked_slot_id:
                with suppress(json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    await slot_tracker.update_from_ollama_chunk(tracked_slot_id, parse_upstream_json_object(body))
        finally:
            await up.aclose()
            if tracked_slot_id:
                await slot_tracker.end(tracked_slot_id)

        resp = Response(
            content=body,
            status_code=status_code,
            headers=resp_headers,
            media_type=up.headers.get("content-type"),
        )
        resp.headers["X-Task-Id"] = task_id
        return resp

    except httpx.RequestError:
        if tracked_slot_id:
            await slot_tracker.end(tracked_slot_id)
        raise HTTPException(status_code=502, detail="Bad gateway")
    except ClientDisconnectError:
        if tracked_slot_id:
            await slot_tracker.end(tracked_slot_id)
        raise HTTPException(status_code=499, detail="Client disconnected")


async def forward_chat_completions(req: Request, task_id: str) -> Response:
    return await forward_openai_compatible(req, task_id, CHAT_COMPLETIONS_ROUTE)


async def forward_completions(req: Request, task_id: str) -> Response:
    return await forward_openai_compatible(req, task_id, COMPLETIONS_ROUTE)


# ---------------------------
# Proxy routes: OpenAI-compatible /v1/* plus Ollama native /api/*.
# ---------------------------
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def v1_proxy(path: str, req: Request):
    task_id = get_task_id(req)
    if req.method == "POST" and path == "chat/completions":
        return await forward_chat_completions(req, task_id)
    if req.method == "POST" and path == "completions":
        return await forward_completions(req, task_id)
    return await forward(req, task_id)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def ollama_api_proxy(path: str, req: Request):
    task_id = get_task_id(req)
    return await forward(req, task_id)


@app.get("/metrics")
async def metrics():
    return await slot_tracker.snapshot()
