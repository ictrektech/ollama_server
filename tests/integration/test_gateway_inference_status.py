"""Integration tests for Redis task status retrieval functionality."""

import json
import os
import unittest
import redis
import httpx


BASE_URL = "http://localhost:11535"
OLLAMA_BASE_URL = "http://127.0.0.1:11434"
TASK_ID = "demo-001"
TEST_MODEL = os.getenv("TEST_MODEL", "qwen3:0.6b")

# Redis配置 (从环境变量读取，与 ollama_gateway.gateway 保持一致)
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_USER = os.getenv("REDIS_USER", "default")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_DB = int(os.getenv("REDIS_DB", "0"))


class TestRedisTaskStatus(unittest.TestCase):
    """Test cases for verifying Redis task status storage and retrieval."""

    def test_chat_completion_records_final_status_in_redis(self):
        """
        Verify that:
        1. A chat completion request is proxied to Ollama.
        2. The task status is stored in Redis with the correct key format.
        3. The final Redis state matches the upstream result.
        """

        request_data = {
            "model": TEST_MODEL,
            "messages": [{"role": "user", "content": "Say this is a test"}],
            "stream": False
        }

        # 发送HTTP请求
        with httpx.Client(timeout=120) as client:
            response = client.post(
                f"{BASE_URL}/v1/chat/completions",
                headers={"X-Task-Id": TASK_ID},
                json=request_data
            )

        if 200 <= response.status_code < 300:
            self.assertTrue(
                response.headers.get("content-type", "").startswith("application/json"),
                "Successful response should be JSON"
            )
            response_data = response.json()
            print(response_data)
            self.assertIn("choices", response_data)
            self.assertTrue(len(response_data["choices"]) > 0)
            expected_state = "SUCCESS"
        else:
            print(f"[DEBUG] Upstream returned {response.status_code}: {response.text}")
            self.assertGreaterEqual(response.status_code, 400)
            expected_state = "FAILED"

        # 从Redis获取状态
        r = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            username=REDIS_USER or None,
            password=REDIS_PASSWORD or None,
            db=REDIS_DB,
            decode_responses=True
        )

        key = f"ts:ollama:{TASK_ID}"
        data = r.get(key)
        indexed_rank = r.zrevrank("ts:ollama:index", TASK_ID)

        self.assertIsNotNone(data, f"Redis key '{key}' should exist")
        self.assertIsNotNone(indexed_rank, "Task should be present in the Redis sorted-set index")

        # 解析并验证Redis中的数据
        parsed = json.loads(data)
        self.assertIn("state", parsed, "Redis data should contain 'state' field")
        self.assertIn("task_id", parsed, "Redis data should contain 'task_id' field")
        self.assertEqual(parsed["task_id"], TASK_ID)
        self.assertEqual(parsed["state"], expected_state)

        extensions = parsed.get("extensions", {})
        self.assertEqual(extensions.get("method"), "POST")
        self.assertEqual(extensions.get("path"), "/v1/chat/completions")
        self.assertEqual(extensions.get("stream"), False)
        self.assertEqual(extensions.get("requested_stream"), False)
        self.assertEqual(extensions.get("response_stream"), False)

        print(f"[DEBUG] Redis data for {TASK_ID}: {json.dumps(parsed, indent=2)}")

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
