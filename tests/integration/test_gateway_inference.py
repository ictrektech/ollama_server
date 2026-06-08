"""Integration tests for Gateway inference behavior."""

import os
import unittest
import httpx


BASE_URL = "http://localhost:11535"
OLLAMA_BASE_URL = "http://127.0.0.1:11434"
TEST_MODEL = os.getenv("TEST_MODEL", "qwen3:0.6b")


class TestGatewayInference(unittest.TestCase):
    def test_chat_completion_returns_task_id(self):

        request_data = {
            "model": TEST_MODEL,
            "messages": [{"role": "user", "content": "Say this is a test"}],
            "stream": False
        }

        # 发送HTTP请求
        with httpx.Client(timeout=120) as client:
            response = client.post(
                f"{BASE_URL}/v1/chat/completions",
                headers={"X-Task-Id": "demo-001"},
                json=request_data
            )

        self.assertEqual(response.headers.get("X-Task-Id"), "demo-001")
        if 200 <= response.status_code < 300:
            self.assertIn("choices", response.json())
        else:
            self.assertGreaterEqual(response.status_code, 400)

    def test_chat_completion_num_ctx_is_reflected_in_ollama_ps(self):
        """
        Verify that request-level options.num_ctx reaches Ollama and changes
        the loaded model context length reported by /api/ps.
        """

        expected_num_ctx = 16384
        request_data = {
            "model": TEST_MODEL,
            "messages": [{"role": "user", "content": "Say ok"}],
            "options": {"num_ctx": expected_num_ctx},
            # This request only needs to load the model with the requested
            # context. Keep generation deterministic and inexpensive.
            "max_tokens": 8,
            "think": False,
            "stream": False,
        }

        with httpx.Client(timeout=120) as client:
            response = client.post(
                f"{BASE_URL}/v1/chat/completions",
                headers={"X-Task-Id": "demo-num-ctx-len-check"},
                json=request_data,
            )

            if not 200 <= response.status_code < 300:
                self.fail(
                    "Chat completion request should succeed before checking "
                    f"context_length; upstream returned {response.status_code}: {response.text}"
                )

            ps_response = client.get(f"{OLLAMA_BASE_URL}/api/ps")
            ps_response.raise_for_status()
            ps_data = ps_response.json()

        loaded_model = None
        for model in ps_data.get("models", []):
            if model.get("name") == TEST_MODEL or model.get("model") == TEST_MODEL:
                loaded_model = model
                break

        self.assertIsNotNone(loaded_model, f"{TEST_MODEL} should be loaded according to /api/ps")
        self.assertEqual(
            loaded_model.get("context_length"),
            expected_num_ctx,
            f"/api/ps should report context_length={expected_num_ctx}",
        )


if __name__ == "__main__":
    unittest.main()
