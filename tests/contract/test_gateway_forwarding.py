"""Unit tests for Gateway forwarding behavior with a fake upstream."""

import asyncio
import json
import unittest
from types import SimpleNamespace

import httpx
from fastapi import HTTPException

import ollama_gateway.gateway as gateway


class FakeRequest:
    def __init__(self, path, body, headers=None, method="POST", query="", disconnected=None):
        self.method = method
        self.url = SimpleNamespace(path=path, query=query)
        self.headers = headers or {}
        self._body = body
        self._disconnected = disconnected or asyncio.Event()

    async def body(self):
        return self._body

    async def is_disconnected(self):
        return self._disconnected.is_set()


class FakeUpstreamResponse:
    def __init__(self, status_code=200, body=None, headers=None, lines=None):
        self.status_code = status_code
        self._body = body or b"{}"
        self.headers = httpx.Headers(headers or {"content-type": "application/json"})
        self._lines = lines or []
        self.closed = False

    async def aread(self):
        return self._body

    async def aclose(self):
        self.closed = True

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class BlockingStreamResponse(FakeUpstreamResponse):
    def __init__(self, first_line):
        super().__init__(lines=[first_line])
        self.first_line_sent = asyncio.Event()

    async def aiter_lines(self):
        yield self._lines[0]
        self.first_line_sent.set()
        await asyncio.Event().wait()


class BlockingReadResponse(FakeUpstreamResponse):
    def __init__(self):
        super().__init__()
        self.read_started = asyncio.Event()

    async def aread(self):
        self.read_started.set()
        await asyncio.Event().wait()
        return b""


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.built_request = None
        self.sent_request = None

    def build_request(self, method, url, headers, content):
        self.built_request = {
            "method": method,
            "url": url,
            "headers": dict(headers),
            "content": content,
        }
        return self.built_request

    async def send(self, request, stream):
        self.sent_request = request
        return self.response


class BlockingSendHttpClient(FakeHttpClient):
    def __init__(self):
        super().__init__(FakeUpstreamResponse())
        self.send_started = asyncio.Event()
        self.send_cancelled = asyncio.Event()

    async def send(self, request, stream):
        self.sent_request = request
        self.send_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.send_cancelled.set()
            raise


class TestGatewayForwarding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_http_client = getattr(gateway.app.state, "http_client", None)
        self.original_slot_tracker = gateway.slot_tracker
        gateway.slot_tracker = gateway.SlotTracker()

    async def asyncTearDown(self):
        gateway.slot_tracker = self.original_slot_tracker
        if self.original_http_client is None:
            try:
                del gateway.app.state.http_client
            except AttributeError:
                pass
        else:
            gateway.app.state.http_client = self.original_http_client

    def _json_body(self, data):
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def _client_headers(self):
        return {
            "Host": "localhost:11535",
            "Connection": "keep-alive",
            "Keep-Alive": "timeout=5",
            "Content-Type": "application/json",
            "Content-Length": "9999",
            "Proxy-Authenticate": "Basic realm=test",
            "Proxy-Authorization": "Basic secret",
            "TE": "trailers",
            "Trailer": "expires",
            "Trailers": "expires",
            "Transfer-Encoding": "chunked",
            "Upgrade": "websocket",
            "Authorization": "Bearer test",
        }

    def _assert_protocol_headers_were_dropped(self, headers):
        lowered = {key.lower(): value for key, value in headers.items()}
        self.assertNotIn("host", lowered)
        self.assertNotIn("connection", lowered)
        self.assertNotIn("keep-alive", lowered)
        self.assertNotIn("content-length", lowered)
        self.assertNotIn("proxy-authenticate", lowered)
        self.assertNotIn("proxy-authorization", lowered)
        self.assertNotIn("te", lowered)
        self.assertNotIn("trailer", lowered)
        self.assertNotIn("trailers", lowered)
        self.assertNotIn("transfer-encoding", lowered)
        self.assertNotIn("upgrade", lowered)
        self.assertEqual(lowered.get("authorization"), "Bearer test")
        self.assertEqual(lowered.get("content-type"), "application/json")

    async def test_chat_completion_forwarding_recomputes_body_and_drops_protocol_headers(self):
        upstream_body = self._json_body({
            "model": "qwen3:0.6b",
            "message": {"role": "assistant", "content": "ok"},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 3,
            "eval_count": 1,
        })
        fake_client = FakeHttpClient(FakeUpstreamResponse(body=upstream_body))
        gateway.app.state.http_client = fake_client

        req = FakeRequest(
            "/v1/chat/completions",
            self._json_body({
                "model": "qwen3:0.6b",
                "messages": [{"role": "user", "content": "hello"}],
                "options": {"num_ctx": 4096},
                "stream": False,
            }),
            headers=self._client_headers(),
        )

        resp = await gateway.forward_chat_completions(req, "task-chat")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(fake_client.built_request["method"], "POST")
        self.assertEqual(fake_client.built_request["url"], f"{gateway.UPSTREAM_BASE}/api/chat")
        self._assert_protocol_headers_were_dropped(fake_client.built_request["headers"])

        sent_body = json.loads(fake_client.built_request["content"].decode("utf-8"))
        self.assertEqual(sent_body["model"], "qwen3:0.6b")
        self.assertEqual(sent_body["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(sent_body["options"]["num_ctx"], 4096)
        self.assertFalse(sent_body["stream"])

        response_body = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(response_body["object"], "chat.completion")
        self.assertEqual(response_body["choices"][0]["message"]["content"], "ok")

    async def test_completion_forwarding_recomputes_body_and_drops_protocol_headers(self):
        upstream_body = self._json_body({
            "model": "qwen3:0.6b",
            "response": "done",
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 2,
            "eval_count": 1,
        })
        fake_client = FakeHttpClient(FakeUpstreamResponse(body=upstream_body))
        gateway.app.state.http_client = fake_client

        req = FakeRequest(
            "/v1/completions",
            self._json_body({
                "model": "qwen3:0.6b",
                "prompt": "hello",
                "max_tokens": 16,
                "options": {"num_ctx": 4096},
                "stream": False,
            }),
            headers=self._client_headers(),
        )

        resp = await gateway.forward_completions(req, "task-completion")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(fake_client.built_request["method"], "POST")
        self.assertEqual(fake_client.built_request["url"], f"{gateway.UPSTREAM_BASE}/api/generate")
        self._assert_protocol_headers_were_dropped(fake_client.built_request["headers"])

        sent_body = json.loads(fake_client.built_request["content"].decode("utf-8"))
        self.assertEqual(sent_body["model"], "qwen3:0.6b")
        self.assertEqual(sent_body["prompt"], "hello")
        self.assertEqual(sent_body["options"]["num_ctx"], 4096)
        self.assertEqual(sent_body["options"]["num_predict"], 16)
        self.assertFalse(sent_body["stream"])

        response_body = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(response_body["object"], "text_completion")
        self.assertEqual(response_body["choices"][0]["text"], "done")

    async def test_invalid_chat_json_returns_400_before_upstream_call(self):
        fake_client = FakeHttpClient(FakeUpstreamResponse())
        gateway.app.state.http_client = fake_client
        req = FakeRequest("/v1/chat/completions", b'{"bad": ', headers=self._client_headers())

        with self.assertRaises(HTTPException) as ctx:
            await gateway.forward_chat_completions(req, "task-invalid")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIsNone(fake_client.built_request)

    async def test_missing_chat_model_returns_400_before_upstream_call(self):
        fake_client = FakeHttpClient(FakeUpstreamResponse())
        gateway.app.state.http_client = fake_client
        req = FakeRequest(
            "/v1/chat/completions",
            self._json_body({"messages": [{"role": "user", "content": "hello"}]}),
            headers=self._client_headers(),
        )

        with self.assertRaises(HTTPException) as ctx:
            await gateway.forward_chat_completions(req, "task-missing-chat-model")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIsNone(fake_client.built_request)

    async def test_missing_chat_messages_returns_400_before_upstream_call(self):
        fake_client = FakeHttpClient(FakeUpstreamResponse())
        gateway.app.state.http_client = fake_client
        req = FakeRequest(
            "/v1/chat/completions",
            self._json_body({"model": "qwen3:0.6b"}),
            headers=self._client_headers(),
        )

        with self.assertRaises(HTTPException) as ctx:
            await gateway.forward_chat_completions(req, "task-missing-chat-messages")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIsNone(fake_client.built_request)

    async def test_missing_completion_prompt_returns_400_before_upstream_call(self):
        fake_client = FakeHttpClient(FakeUpstreamResponse())
        gateway.app.state.http_client = fake_client
        req = FakeRequest(
            "/v1/completions",
            self._json_body({"model": "qwen3:0.6b"}),
            headers=self._client_headers(),
        )

        with self.assertRaises(HTTPException) as ctx:
            await gateway.forward_completions(req, "task-missing-prompt")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIsNone(fake_client.built_request)

    async def test_upstream_error_is_returned_with_task_id(self):
        fake_client = FakeHttpClient(FakeUpstreamResponse(
            status_code=500,
            body=b'{"error":"upstream failed"}',
        ))
        gateway.app.state.http_client = fake_client
        req = FakeRequest(
            "/v1/chat/completions",
            self._json_body({
                "model": "qwen3:0.6b",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            }),
            headers=self._client_headers(),
        )

        resp = await gateway.forward_chat_completions(req, "task-upstream-error")

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.body, b'{"error":"upstream failed"}')
        self.assertEqual(resp.headers["X-Task-Id"], "task-upstream-error")

    async def test_streaming_chat_completion_returns_sse_done_marker(self):
        lines = [
            json.dumps({
                "model": "qwen3:0.6b",
                "message": {"role": "assistant", "content": "hi"},
                "done": False,
            }),
            json.dumps({
                "model": "qwen3:0.6b",
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 2,
                "eval_count": 1,
            }),
        ]
        fake_client = FakeHttpClient(FakeUpstreamResponse(lines=lines))
        gateway.app.state.http_client = fake_client
        req = FakeRequest(
            "/v1/chat/completions",
            self._json_body({
                "model": "qwen3:0.6b",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            }),
            headers=self._client_headers(),
        )

        resp = await gateway.forward_chat_completions(req, "task-stream")
        body = b"".join([chunk async for chunk in resp.body_iterator]).decode("utf-8")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.media_type, "text/event-stream")
        self.assertIn("data: ", body)
        self.assertIn('"object": "chat.completion.chunk"', body)
        self.assertTrue(body.endswith("data: [DONE]\n\n"))
        self.assertEqual(resp.headers["X-Task-Id"], "task-stream")

    async def test_streaming_chat_client_disconnect_closes_upstream(self):
        fake_upstream = BlockingStreamResponse(json.dumps({
            "model": "qwen3:0.6b",
            "message": {"role": "assistant", "content": "hi"},
            "done": False,
        }))
        fake_client = FakeHttpClient(fake_upstream)
        gateway.app.state.http_client = fake_client
        req = FakeRequest(
            "/v1/chat/completions",
            self._json_body({
                "model": "qwen3:0.6b",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            }),
            headers=self._client_headers(),
        )

        resp = await gateway.forward_chat_completions(req, "task-client-disconnect")

        async def consume_response():
            async for _ in resp.body_iterator:
                pass

        task = asyncio.create_task(consume_response())
        await asyncio.wait_for(fake_upstream.first_line_sent.wait(), timeout=1)
        snapshot = await gateway.slot_tracker.snapshot()
        self.assertEqual(snapshot["slot_usage"], "1/1")
        self.assertEqual(snapshot["active_by_model"], {"qwen3:0.6b": 1})
        self.assertEqual(snapshot["phase"], "decode")
        self.assertEqual(snapshot["model_metrics"]["qwen3:0.6b"]["phase"], "decode")
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(fake_upstream.closed)

    async def test_non_streaming_chat_client_disconnect_closes_upstream(self):
        disconnected = asyncio.Event()
        fake_upstream = BlockingReadResponse()
        fake_client = FakeHttpClient(fake_upstream)
        gateway.app.state.http_client = fake_client
        req = FakeRequest(
            "/v1/chat/completions",
            self._json_body({
                "model": "qwen3:0.6b",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            }),
            headers=self._client_headers(),
            disconnected=disconnected,
        )

        task = asyncio.create_task(gateway.forward_chat_completions(req, "task-non-stream-disconnect"))
        await asyncio.wait_for(fake_upstream.read_started.wait(), timeout=1)
        disconnected.set()

        with self.assertRaises(HTTPException) as ctx:
            await task

        self.assertEqual(ctx.exception.status_code, 499)
        self.assertTrue(fake_upstream.closed)

    async def test_non_streaming_chat_client_disconnect_cancels_upstream_send_before_headers(self):
        disconnected = asyncio.Event()
        fake_client = BlockingSendHttpClient()
        gateway.app.state.http_client = fake_client
        req = FakeRequest(
            "/v1/chat/completions",
            self._json_body({
                "model": "qwen3:0.6b",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            }),
            headers=self._client_headers(),
            disconnected=disconnected,
        )

        task = asyncio.create_task(gateway.forward_chat_completions(req, "task-send-disconnect"))
        await asyncio.wait_for(fake_client.send_started.wait(), timeout=1)
        disconnected.set()

        with self.assertRaises(HTTPException) as ctx:
            await task

        self.assertEqual(ctx.exception.status_code, 499)
        self.assertTrue(fake_client.send_cancelled.is_set())

    async def test_streaming_completion_returns_sse_done_marker(self):
        lines = [
            json.dumps({
                "model": "qwen3:0.6b",
                "response": "hi",
                "done": False,
            }),
            json.dumps({
                "model": "qwen3:0.6b",
                "response": "",
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 2,
                "eval_count": 1,
            }),
        ]
        fake_client = FakeHttpClient(FakeUpstreamResponse(lines=lines))
        gateway.app.state.http_client = fake_client
        req = FakeRequest(
            "/v1/completions",
            self._json_body({
                "model": "qwen3:0.6b",
                "prompt": "hello",
                "stream": True,
            }),
            headers=self._client_headers(),
        )

        resp = await gateway.forward_completions(req, "task-completion-stream")
        body = b"".join([chunk async for chunk in resp.body_iterator]).decode("utf-8")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.media_type, "text/event-stream")
        self.assertIn("data: ", body)
        self.assertIn('"object": "text_completion"', body)
        self.assertTrue(body.endswith("data: [DONE]\n\n"))
        self.assertEqual(resp.headers["X-Task-Id"], "task-completion-stream")

    async def test_unconverted_v1_path_is_passed_through_with_protocol_headers_dropped(self):
        fake_client = FakeHttpClient(FakeUpstreamResponse(
            body=b'{"object":"list","data":[]}',
        ))
        gateway.app.state.http_client = fake_client
        req = FakeRequest(
            "/v1/embeddings",
            self._json_body({"model": "qwen3:0.6b", "input": "hello"}),
            headers=self._client_headers(),
            query="trace=1",
        )

        resp = await gateway.forward(req, "task-pass-through")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(fake_client.built_request["method"], "POST")
        self.assertEqual(fake_client.built_request["url"], f"{gateway.UPSTREAM_BASE}/v1/embeddings?trace=1")
        self._assert_protocol_headers_were_dropped(fake_client.built_request["headers"])
        self.assertEqual(
            json.loads(fake_client.built_request["content"].decode("utf-8")),
            {"model": "qwen3:0.6b", "input": "hello"},
        )
        self.assertEqual(json.loads(resp.body.decode("utf-8")), {"object": "list", "data": []})


if __name__ == "__main__":
    unittest.main()
