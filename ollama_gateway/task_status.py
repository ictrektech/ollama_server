import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass(frozen=True)
class StatusConfig:
    app_version: str
    event_type: str
    algorithm_id: str
    index_key: str
    ttl_running: int
    ttl_done: int
    cleanup_interval_sec: float = 60


def now_ts(now_func: Callable[[], float] = time.time) -> int:
    return int(now_func())


def rkey(task_id: str) -> str:
    return f"ts:ollama:{task_id}"


def status_index_cutoff(current_ts: int, ttl_running: int, ttl_done: int) -> int:
    return current_ts - max(ttl_running, ttl_done)


def make_evt(
    task_id: str,
    state: str,
    app_version: str,
    event_type: str,
    algorithm_id: str,
    timestamp: int,
    stage: Optional[str] = None,
    message: Optional[str] = None,
    progress: Optional[float] = None,
    extensions: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    evt: Dict[str, Any] = {
        "version": app_version,
        "event_type": event_type,
        "event_id": str(uuid.uuid4()),
        "algorithm_id": algorithm_id,
        "task_id": task_id,
        "state": state,
        "timestamp": timestamp,
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


class TaskStatusStore:
    def __init__(
        self,
        redis,
        config: StatusConfig,
        now_func: Callable[[], float] = time.time,
    ) -> None:
        self.redis = redis
        self.config = config
        self.now_func = now_func
        self._next_cleanup_ts = 0

    def now_ts(self) -> int:
        return now_ts(self.now_func)

    def status_index_cutoff(self) -> int:
        return status_index_cutoff(self.now_ts(), self.config.ttl_running, self.config.ttl_done)

    def make_evt(
        self,
        task_id: str,
        state: str,
        stage: Optional[str] = None,
        message: Optional[str] = None,
        progress: Optional[float] = None,
        extensions: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return make_evt(
            task_id,
            state,
            self.config.app_version,
            self.config.event_type,
            self.config.algorithm_id,
            self.now_ts(),
            stage=stage,
            message=message,
            progress=progress,
            extensions=extensions,
        )

    def should_cleanup_index(self, timestamp: int) -> bool:
        interval = self.config.cleanup_interval_sec
        if interval <= 0:
            return True
        if timestamp < self._next_cleanup_ts:
            return False
        self._next_cleanup_ts = timestamp + interval
        return True

    async def write_status(self, task_id: str, evt: Dict[str, Any], ttl: int) -> None:
        pipe = self.redis.pipeline()
        pipe.set(rkey(task_id), json.dumps(evt, ensure_ascii=False), ex=ttl)
        pipe.zadd(self.config.index_key, {task_id: evt["timestamp"]})
        if self.should_cleanup_index(int(evt["timestamp"])):
            pipe.zremrangebyscore(self.config.index_key, "-inf", self.status_index_cutoff())
        await pipe.execute()

    async def set_status(
        self,
        task_id: str,
        state: str,
        stage: str,
        message: str,
        extensions: Dict[str, Any],
        ttl: int,
    ) -> None:
        await self.write_status(
            task_id,
            self.make_evt(task_id, state, stage=stage, message=message, extensions=extensions),
            ttl,
        )

    async def finish_status(self, task_id: str, status_code: int, extensions: Dict[str, Any]) -> None:
        if 200 <= status_code < 300:
            await self.set_status(task_id, "SUCCESS", "done", "completed", extensions, self.config.ttl_done)
        else:
            await self.set_status(
                task_id,
                "FAILED",
                "error",
                f"upstream status {status_code}",
                extensions,
                self.config.ttl_done,
            )

    async def get_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        value = await self.redis.get(rkey(task_id))
        return json.loads(value) if value else None

    async def list_status(self, limit: int = 50) -> Dict[str, Any]:
        limit = max(1, min(limit, 500))
        items: List[Dict[str, Any]] = []

        task_ids = await self.redis.zrevrange(self.config.index_key, 0, limit - 1)
        if not task_ids:
            return {"items": items, "count": 0}

        keys = [rkey(task_id) for task_id in task_ids]
        values = await self.redis.mget(keys)
        stale_task_ids = []

        for task_id, value in zip(task_ids, values):
            if not value:
                stale_task_ids.append(task_id)
                continue
            items.append(json.loads(value))

        if stale_task_ids:
            await self.redis.zrem(self.config.index_key, *stale_task_ids)

        return {"items": items, "count": len(items)}
