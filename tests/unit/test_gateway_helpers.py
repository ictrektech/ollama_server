"""Unit tests for lightweight proxy helper behavior."""

import unittest

import httpx

import ollama_gateway.gateway as gateway
from ollama_gateway.gateway import (
    build_ollama_chat_payload,
    build_ollama_generate_payload,
    convert_ollama_chat_response,
    convert_ollama_chat_stream_chunk,
    convert_ollama_generate_response,
    convert_ollama_generate_stream_chunk,
    is_event_stream,
    proxy_headers,
    requested_stream,
)


class TestGatewayHelpers(unittest.TestCase):
    def test_requested_stream_reads_openai_json_flag(self):
        body = b'{"model":"qwen3:0.6b","stream":true}'
        self.assertTrue(requested_stream(body))

    def test_requested_stream_defaults_false_for_non_json(self):
        self.assertFalse(requested_stream(b"not-json"))
        self.assertFalse(requested_stream(b""))

    def test_is_event_stream_uses_response_content_type(self):
        headers = httpx.Headers({"content-type": "text/event-stream; charset=utf-8"})
        self.assertTrue(is_event_stream(headers))
        self.assertFalse(is_event_stream(httpx.Headers({"content-type": "application/json"})))

    def test_proxy_headers_removes_hop_by_hop_and_host(self):
        headers = {
            "host": "localhost:11535",
            "connection": "keep-alive",
            "keep-alive": "timeout=5",
            "content-length": "123",
            "proxy-authenticate": "Basic realm=test",
            "proxy-authorization": "Basic secret",
            "te": "trailers",
            "trailer": "expires",
            "trailers": "expires",
            "transfer-encoding": "chunked",
            "upgrade": "websocket",
            "authorization": "Bearer test",
        }

        filtered = proxy_headers(headers, drop_host=True)

        self.assertEqual(filtered, {"authorization": "Bearer test"})

    def test_status_index_cutoff_uses_longer_status_ttl(self):
        original_running = gateway.TTL_RUNNING
        original_done = gateway.TTL_DONE
        try:
            gateway.TTL_RUNNING = 10
            gateway.TTL_DONE = 30
            now = gateway.now_ts()
            cutoff = gateway.status_index_cutoff()
            self.assertGreaterEqual(cutoff, now - 31)
            self.assertLessEqual(cutoff, now - 29)
        finally:
            gateway.TTL_RUNNING = original_running
            gateway.TTL_DONE = original_done

    def test_build_ollama_chat_payload_passes_native_options(self):
        payload = build_ollama_chat_payload({
            "model": "qwen3:0.6b",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
            "max_tokens": 64,
            "options": {"num_ctx": 8192, "num_predict": 128},
        })

        self.assertEqual(payload["model"], "qwen3:0.6b")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hello"}])
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["options"]["num_ctx"], 8192)
        self.assertEqual(payload["options"]["num_predict"], 128)

    def test_build_ollama_chat_payload_maps_openai_fields(self):
        payload = build_ollama_chat_payload({
            "model": "llama3.2",
            "messages": [{"role": "user", "content": "json please"}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "top_p": 0.9,
            "stop": ["done"],
            "reasoning_effort": "medium",
        })

        self.assertEqual(payload["format"], "json")
        self.assertEqual(payload["options"]["temperature"], 0.2)
        self.assertEqual(payload["options"]["top_p"], 0.9)
        self.assertEqual(payload["options"]["stop"], ["done"])
        self.assertTrue(payload["think"])

    def test_build_ollama_chat_payload_extracts_images(self):
        payload = build_ollama_chat_payload({
            "model": "llava",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}},
                ],
            }],
        })

        self.assertEqual(payload["messages"][0]["content"], "what is this?")
        self.assertEqual(payload["messages"][0]["images"], ["abc123"])

    def test_convert_ollama_chat_response_to_openai_shape(self):
        converted = convert_ollama_chat_response({
            "model": "qwen3:0.6b",
            "message": {"role": "assistant", "content": "hi"},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 3,
            "eval_count": 2,
        }, "chatcmpl-test", 123)

        self.assertEqual(converted["id"], "chatcmpl-test")
        self.assertEqual(converted["object"], "chat.completion")
        self.assertEqual(converted["choices"][0]["message"]["content"], "hi")
        self.assertEqual(converted["choices"][0]["finish_reason"], "stop")
        self.assertEqual(converted["usage"]["total_tokens"], 5)

    def test_convert_ollama_stream_chunk_to_openai_sse_payload_shape(self):
        converted = convert_ollama_chat_stream_chunk({
            "model": "qwen3:0.6b",
            "message": {"role": "assistant", "content": "hi"},
            "done": False,
        }, "chatcmpl-test", 123)

        self.assertEqual(converted["object"], "chat.completion.chunk")
        self.assertEqual(converted["choices"][0]["delta"]["content"], "hi")
        self.assertIsNone(converted["choices"][0]["finish_reason"])

    def test_build_ollama_generate_payload_passes_native_options(self):
        payload = build_ollama_generate_payload({
            "model": "qwen3:0.6b",
            "prompt": "complete this",
            "stream": False,
            "max_tokens": 64,
            "options": {"num_ctx": 8192, "num_predict": 128},
        })

        self.assertEqual(payload["model"], "qwen3:0.6b")
        self.assertEqual(payload["prompt"], "complete this")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["options"]["num_ctx"], 8192)
        self.assertEqual(payload["options"]["num_predict"], 128)

    def test_build_ollama_generate_payload_maps_extra_generate_fields(self):
        payload = build_ollama_generate_payload({
            "model": "llama3.2",
            "prompt": "json please",
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "top_p": 0.9,
            "suffix": "end",
            "system": "be concise",
            "keep_alive": "5m",
            "reasoning": {"effort": "low"},
        })

        self.assertEqual(payload["format"], "json")
        self.assertEqual(payload["options"]["temperature"], 0.2)
        self.assertEqual(payload["options"]["top_p"], 0.9)
        self.assertEqual(payload["suffix"], "end")
        self.assertEqual(payload["system"], "be concise")
        self.assertEqual(payload["keep_alive"], "5m")
        self.assertTrue(payload["think"])

    def test_convert_ollama_generate_response_to_openai_shape(self):
        converted = convert_ollama_generate_response({
            "model": "qwen3:0.6b",
            "response": "completed",
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 4,
            "eval_count": 2,
        }, "cmpl-test", 123)

        self.assertEqual(converted["id"], "cmpl-test")
        self.assertEqual(converted["object"], "text_completion")
        self.assertEqual(converted["choices"][0]["text"], "completed")
        self.assertEqual(converted["choices"][0]["finish_reason"], "stop")
        self.assertEqual(converted["usage"]["total_tokens"], 6)

    def test_convert_ollama_generate_stream_chunk_to_openai_sse_payload_shape(self):
        converted = convert_ollama_generate_stream_chunk({
            "model": "qwen3:0.6b",
            "response": "part",
            "done": False,
        }, "cmpl-test", 123)

        self.assertEqual(converted["object"], "text_completion")
        self.assertEqual(converted["choices"][0]["text"], "part")
        self.assertIsNone(converted["choices"][0]["finish_reason"])


if __name__ == "__main__":
    unittest.main()
