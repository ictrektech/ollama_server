"""Unit tests for Redis task status storage helpers."""

import unittest

from ollama_gateway.task_status import StatusConfig, TaskStatusStore


class FakePipeline:
    def __init__(self):
        self.ops = []

    def set(self, *args, **kwargs):
        self.ops.append(("set", args, kwargs))

    def zadd(self, *args, **kwargs):
        self.ops.append(("zadd", args, kwargs))

    def zremrangebyscore(self, *args, **kwargs):
        self.ops.append(("zremrangebyscore", args, kwargs))

    async def execute(self):
        return self.ops


class FakeRedis:
    def __init__(self):
        self.pipelines = []

    def pipeline(self):
        pipe = FakePipeline()
        self.pipelines.append(pipe)
        return pipe


class TestTaskStatusStore(unittest.IsolatedAsyncioTestCase):
    def _store(self, cleanup_interval_sec=60):
        return TaskStatusStore(
            FakeRedis(),
            StatusConfig(
                app_version="1.0",
                event_type="task.status.update",
                algorithm_id="ollama-openai",
                index_key="ts:ollama:index",
                ttl_running=3600,
                ttl_done=86400,
                cleanup_interval_sec=cleanup_interval_sec,
            ),
            now_func=lambda: 100,
        )

    async def test_index_cleanup_is_throttled(self):
        store = self._store(cleanup_interval_sec=60)

        await store.write_status("task-1", store.make_evt("task-1", "RUNNING"), 3600)
        await store.write_status("task-2", store.make_evt("task-2", "RUNNING"), 3600)

        cleanup_ops = [
            op
            for pipe in store.redis.pipelines
            for op in pipe.ops
            if op[0] == "zremrangebyscore"
        ]
        self.assertEqual(len(cleanup_ops), 1)

    async def test_index_cleanup_can_run_on_every_write(self):
        store = self._store(cleanup_interval_sec=0)

        await store.write_status("task-1", store.make_evt("task-1", "RUNNING"), 3600)
        await store.write_status("task-2", store.make_evt("task-2", "RUNNING"), 3600)

        cleanup_ops = [
            op
            for pipe in store.redis.pipelines
            for op in pipe.ops
            if op[0] == "zremrangebyscore"
        ]
        self.assertEqual(len(cleanup_ops), 2)


if __name__ == "__main__":
    unittest.main()
