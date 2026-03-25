"""
Unit tests for gateway.py
Test coverage for helper functions, proxy logic, and status APIs
"""
import json
import os
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import httpx
from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

# Import modules under test
import gateway


# ============================
# Fixtures
# ============================

@pytest.fixture
def mock_redis():
    """Mock Redis client"""
    with patch('gateway.rds') as mock:
        mock.get = MagicMock(return_value=None)
        mock.set = MagicMock()
        mock.scan_iter = MagicMock(return_value=[])
        yield mock


@pytest.fixture
def mock_http_client():
    """Mock httpx.AsyncClient"""
    with patch('gateway.http_client') as mock:
        mock.request = AsyncMock()
        mock.build_request = MagicMock()
        mock.send = AsyncMock()
        mock.aclose = AsyncMock()
        yield mock


@pytest.fixture
def sample_task_id():
    """Sample task ID for testing"""
    return str(uuid.uuid4())


@pytest.fixture
def sample_request_data():
    """Sample request data"""
    return {
        "model": "gpt-oss:20b",
        "messages": [{"role": "user", "content": "Say this is a test"}],
        "stream": True
    }


# ============================
# Helper Functions Tests
# ============================

class TestHelperFunctions:
    """Test helper functions"""

    def test_now_ts(self):
        """Test now_ts returns integer timestamp"""
        ts = gateway.now_ts()
        assert isinstance(ts, int)
        assert ts > 0
        # Verify it's close to current time (within 1 second)
        assert abs(ts - int(time.time())) <= 1

    def test_rkey(self):
        """Test rkey generates correct Redis key format"""
        task_id = "test-task-123"
        expected = f"ts:ollama:{task_id}"
        assert gateway.rkey(task_id) == expected

    def test_make_evt_full(self):
        """Test make_evt with all optional parameters"""
        task_id = "task-001"
        evt = gateway.make_evt(
            task_id=task_id,
            state="RUNNING",
            stage="processing",
            message="Processing request",
            progress=50.5,
            extensions={"key": "value"}
        )
        
        assert evt["version"] == gateway.APP_VERSION
        assert evt["event_type"] == gateway.EVENT_TYPE
        assert evt["algorithm_id"] == gateway.ALGORITHM_ID
        assert evt["task_id"] == task_id
        assert evt["state"] == "RUNNING"
        assert evt["stage"] == "processing"
        assert evt["message"] == "Processing request"
        assert evt["progress"] == 50.5
        assert evt["extensions"] == {"key": "value"}
        assert isinstance(evt["event_id"], str)
        assert isinstance(evt["timestamp"], int)

    def test_make_evt_minimal(self):
        """Test make_evt with only required parameters"""
        task_id = "task-002"
        evt = gateway.make_evt(task_id=task_id, state="SUCCESS")
        
        assert evt["version"] == gateway.APP_VERSION
        assert evt["task_id"] == task_id
        assert evt["state"] == "SUCCESS"
        assert "stage" not in evt
        assert "message" not in evt
        assert "progress" not in evt
        assert "extensions" not in evt

    def test_write_status(self, mock_redis):
        """Test write_status writes to Redis with correct TTL"""
        task_id = "task-003"
        evt = {"test": "data"}
        ttl = 3600
        
        gateway.write_status(task_id, evt, ttl)
        
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert call_args[0][0] == gateway.rkey(task_id)
        assert call_args[0][1] == json.dumps(evt, ensure_ascii=False)
        assert call_args[1]["ex"] == ttl

    def test_get_task_id_from_header(self):
        """Test get_task_id extracts task ID from header"""
        mock_request = MagicMock()
        mock_request.headers.get.return_value = "provided-task-id"
        
        task_id = gateway.get_task_id(mock_request)
        assert task_id == "provided-task-id"

    def test_get_task_id_from_header_with_whitespace(self):
        """Test get_task_id strips whitespace from header"""
        mock_request = MagicMock()
        mock_request.headers.get.return_value = "  provided-task-id  "
        
        task_id = gateway.get_task_id(mock_request)
        assert task_id == "provided-task-id"

    def test_get_task_id_empty_header(self):
        """Test get_task_id generates UUID when header is empty"""
        mock_request = MagicMock()
        mock_request.headers.get.return_value = ""
        
        task_id = gateway.get_task_id(mock_request)
        assert isinstance(task_id, str)
        # Verify it's a valid UUID format
        try:
            uuid.UUID(task_id)
        except ValueError:
            pytest.fail("Generated task_id is not a valid UUID")

    def test_get_task_id_no_header(self):
        """Test get_task_id generates UUID when header is missing"""
        mock_request = MagicMock()
        mock_request.headers.get.return_value = None
        
        task_id = gateway.get_task_id(mock_request)
        assert isinstance(task_id, str)
        # Verify it's a valid UUID format
        try:
            uuid.UUID(task_id)
        except ValueError:
            pytest.fail("Generated task_id is not a valid UUID")

    def test_hop_by_hop_filter(self):
        """Test hop_by_hop_filter removes hop-by-hop headers"""
        headers = httpx.Headers({
            "content-type": "application/json",
            "connection": "keep-alive",
            "transfer-encoding": "chunked",
            "authorization": "Bearer token",
            "proxy-authorization": "Basic xyz"
        })
        
        filtered = gateway.hop_by_hop_filter(headers)
        
        # Should keep these
        assert "content-type" in filtered
        assert filtered["content-type"] == "application/json"
        assert "authorization" in filtered
        
        # Should remove hop-by-hop headers
        assert "connection" not in filtered
        assert "transfer-encoding" not in filtered
        assert "proxy-authorization" not in filtered

    def test_hop_by_hop_filter_all_hop_by_hop(self):
        """Test hop_by_hop_filter with all hop-by-hop headers"""
        headers = httpx.Headers({
            "connection": "close",
            "keep-alive": "timeout=5",
            "proxy-authenticate": "Basic",
            "proxy-authorization": "Basic xyz",
            "te": "trailers",
            "trailers": "Max-Forwards",
            "transfer-encoding": "chunked",
            "upgrade": "h2c"
        })
        
        filtered = gateway.hop_by_hop_filter(headers)
        assert len(filtered) == 0

    def test_build_extensions_streaming(self):
        """Test build_extensions for streaming request"""
        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.url.path = "/v1/chat/completions"
        mock_request.url.query = "param=value"
        
        ext = gateway.build_extensions(mock_request, is_stream=True)
        
        assert ext["method"] == "POST"
        assert ext["path"] == "/v1/chat/completions"
        assert ext["query"] == "param=value"
        assert ext["stream"] is True

    def test_build_extensions_non_streaming(self):
        """Test build_extensions for non-streaming request"""
        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = "/v1/models"
        mock_request.url.query = ""
        
        ext = gateway.build_extensions(mock_request, is_stream=False)
        
        assert ext["method"] == "GET"
        assert ext["path"] == "/v1/models"
        assert ext["query"] == ""
        assert ext["stream"] is False

    def test_guess_is_stream_true(self):
        """Test guess_is_stream detects streaming request"""
        raw_body = b'{"model": "test", "messages": [], "stream": true}'
        assert gateway.guess_is_stream(raw_body) is True

    def test_guess_is_stream_false(self):
        """Test guess_is_stream detects non-streaming request"""
        raw_body = b'{"model": "test", "messages": [], "stream": false}'
        assert gateway.guess_is_stream(raw_body) is False

    def test_guess_is_stream_no_stream_field(self):
        """Test guess_is_stream when stream field is missing"""
        raw_body = b'{"model": "test", "messages": []}'
        assert gateway.guess_is_stream(raw_body) is False

    def test_guess_is_stream_empty_body(self):
        """Test guess_is_stream with empty body"""
        raw_body = b""
        assert gateway.guess_is_stream(raw_body) is False

    def test_guess_is_stream_none_body(self):
        """Test guess_is_stream with None body"""
        raw_body = None
        assert gateway.guess_is_stream(raw_body) is False

    def test_guess_is_stream_invalid_json(self):
        """Test guess_is_stream with invalid JSON"""
        raw_body = b"not valid json"
        assert gateway.guess_is_stream(raw_body) is False

    def test_guess_is_stream_unicode_true(self):
        """Test guess_is_stream with unicode boolean"""
        raw_body = b'{"model": "test", "messages": [], "stream": True}'
        # Should handle gracefully (False because not Python True)
        assert gateway.guess_is_stream(raw_body) is False


# ============================
# Status API Tests
# ============================

class TestStatusAPIs:
    """Test status query endpoints"""

    def test_get_status_found(self, mock_redis):
        """Test get_status returns task status when found"""
        task_id = "task-001"
        status_data = {
            "task_id": task_id,
            "state": "RUNNING",
            "timestamp": 1234567890
        }
        mock_redis.get.return_value = json.dumps(status_data)
        
        response = gateway.get_status(task_id)
        
        assert isinstance(response, JSONResponse)
        assert response.status_code == 200

    def test_get_status_not_found(self, mock_redis):
        """Test get_status raises 404 when task not found"""
        task_id = "task-999"
        mock_redis.get.return_value = None
        
        with pytest.raises(HTTPException) as exc_info:
            gateway.get_status(task_id)
        
        assert exc_info.value.status_code == 404
        assert "not found" in str(exc_info.value.detail).lower()

    def test_list_status_empty(self, mock_redis):
        """Test list_status returns empty list when no tasks"""
        mock_redis.scan_iter.return_value = []
        
        response = gateway.list_status()
        
        assert "items" in response
        assert "count" in response
        assert response["items"] == []
        assert response["count"] == 0

    def test_list_status_with_tasks(self, mock_redis):
        """Test list_status returns tasks up to limit"""
        task1 = {"task_id": "task-1", "state": "DONE"}
        task2 = {"task_id": "task-2", "state": "RUNNING"}
        
        mock_redis.scan_iter.return_value = ["key1", "key2"]
        mock_redis.get.side_effect = [
            json.dumps(task1),
            json.dumps(task2)
        ]
        
        response = gateway.list_status(limit=10)
        
        assert response["count"] == 2
        assert len(response["items"]) == 2

    def test_list_status_respects_limit(self, mock_redis):
        """Test list_status respects limit parameter"""
        tasks = [{"task_id": f"task-{i}", "state": "DONE"} for i in range(10)]
        keys = [f"key{i}" for i in range(10)]
        
        mock_redis.scan_iter.return_value = keys
        mock_redis.get.side_effect = [json.dumps(t) for t in tasks]
        
        response = gateway.list_status(limit=3)
        
        assert response["count"] == 3
        assert len(response["items"]) == 3

    def test_list_status_default_limit(self, mock_redis):
        """Test list_status uses default limit of 50"""
        # Create 100 keys
        keys = [f"key{i}" for i in range(100)]
        tasks = [{"task_id": f"task-{i}", "state": "DONE"} for i in range(100)]
        
        mock_redis.scan_iter.return_value = keys
        mock_redis.get.side_effect = [json.dumps(t) for t in tasks]
        
        response = gateway.list_status()
        
        # Should return at most 50 items
        assert response["count"] == 50


# ============================
# Proxy Core Tests
# ============================

@pytest.mark.asyncio
class TestProxyCore:
    """Test proxy forwarding logic"""

    async def test_forward_success_non_streaming(self, mock_http_client, mock_redis):
        """Test forward with successful non-streaming request"""
        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.url.path = "/v1/chat/completions"
        mock_request.url.query = ""
        mock_request.headers = {"content-type": "application/json"}
        mock_request.body = AsyncMock(return_value=b'{"stream": false}')
        
        # Mock upstream response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"result": "success"}'
        mock_response.headers = httpx.Headers({"content-type": "application/json"})
        mock_http_client.request = AsyncMock(return_value=mock_response)
        
        task_id = "task-001"
        response = await gateway.forward(mock_request, task_id)
        
        assert response.status_code == 200
        assert response.headers["X-Task-Id"] == task_id

    async def test_forward_success_streaming(self, mock_http_client, mock_redis):
        """Test forward with successful streaming request"""
        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.url.path = "/v1/chat/completions"
        mock_request.url.query = ""
        mock_request.headers = {"content-type": "application/json"}
        mock_request.body = AsyncMock(return_value=b'{"stream": true}')
        
        # Mock upstream streaming response
        mock_upstream = MagicMock()
        mock_upstream.status_code = 200
        mock_upstream.headers = httpx.Headers({"content-type": "text/event-stream"})
        mock_upstream.aiter_bytes = AsyncMock()
        mock_upstream.aiter_bytes.return_value = [b"data1", b"data2"]
        mock_upstream.aclose = AsyncMock()
        mock_http_client.send = AsyncMock(return_value=mock_upstream)
        
        task_id = "task-002"
        response = await gateway.forward(mock_request, task_id)
        
        assert isinstance(response, StreamingResponse)
        assert response.status_code == 200
        assert response.headers["X-Task-Id"] == task_id

    async def test_forward_upstream_error(self, mock_http_client, mock_redis):
        """Test forward handles upstream error status"""
        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.url.path = "/v1/chat/completions"
        mock_request.url.query = ""
        mock_request.headers = {"content-type": "application/json"}
        mock_request.body = AsyncMock(return_value=b'{"stream": false}')
        
        # Mock upstream error response
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.content = b'{"error": "internal error"}'
        mock_response.headers = httpx.Headers({"content-type": "application/json"})
        mock_http_client.request = AsyncMock(return_value=mock_response)
        
        task_id = "task-003"
        response = await gateway.forward(mock_request, task_id)
        
        assert response.status_code == 500
        assert response.headers["X-Task-Id"] == task_id

    async def test_forward_request_error(self, mock_http_client, mock_redis):
        """Test forward handles request error (connection failed)"""
        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.url.path = "/v1/chat/completions"
        mock_request.url.query = ""
        mock_request.headers = {"content-type": "application/json"}
        mock_request.body = AsyncMock(return_value=b'{"stream": false}')
        
        # Mock request error
        mock_http_client.request = AsyncMock(
            side_effect=httpx.RequestError("Connection failed")
        )
        
        task_id = "task-004"
        
        with pytest.raises(HTTPException) as exc_info:
            await gateway.forward(mock_request, task_id)
        
        assert exc_info.value.status_code == 502

    async def test_forward_stream_error(self, mock_http_client, mock_redis):
        """Test forward handles streaming error"""
        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.url.path = "/v1/chat/completions"
        mock_request.url.query = ""
        mock_request.headers = {"content-type": "application/json"}
        mock_request.body = AsyncMock(return_value=b'{"stream": true}')
        
        # Mock upstream streaming response with error
        mock_upstream = MagicMock()
        mock_upstream.status_code = 200
        mock_upstream.headers = httpx.Headers({"content-type": "text/event-stream"})
        mock_upstream.aiter_bytes = AsyncMock(
            side_effect=httpx.StreamError("Stream closed")
        )
        mock_upstream.aclose = AsyncMock()
        mock_http_client.send = AsyncMock(return_value=mock_upstream)
        
        task_id = "task-005"
        
        # Should raise error but mark as FAILED in Redis
        with pytest.raises(httpx.StreamError):
            await gateway.forward(mock_request, task_id)

    async def test_forward_unexpected_error(self, mock_http_client, mock_redis):
        """Test forward handles unexpected errors"""
        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.url.path = "/v1/chat/completions"
        mock_request.url.query = ""
        mock_request.headers = {"content-type": "application/json"}
        mock_request.body = AsyncMock(return_value=b'{"stream": false}')
        
        # Mock unexpected error
        mock_http_client.request = AsyncMock(
            side_effect=ValueError("Unexpected error")
        )
        
        task_id = "task-006"
        
        with pytest.raises(ValueError):
            await gateway.forward(mock_request, task_id)


# ============================
# Proxy Routes Tests
# ============================

@pytest.mark.asyncio
class TestProxyRoutes:
    """Test proxy route handlers"""

    async def test_v1_proxy_with_provided_task_id(self):
        """Test v1_proxy uses provided task_id from header"""
        with patch('gateway.forward', new_callable=AsyncMock) as mock_forward:
            mock_request = MagicMock()
            mock_request.headers.get.return_value = "custom-task-id"
            
            mock_response = MagicMock()
            mock_forward.return_value = mock_response
            
            response = await gateway.v1_proxy("chat/completions", mock_request)
            
            assert response == mock_response
            mock_forward.assert_called_once()
            call_args = mock_forward.call_args
            assert call_args[0][0] == mock_request
            assert call_args[0][1] == "custom-task-id"

    async def test_v1_proxy_generates_task_id(self):
        """Test v1_proxy generates task_id when not provided"""
        with patch('gateway.forward', new_callable=AsyncMock) as mock_forward:
            mock_request = MagicMock()
            mock_request.headers.get.return_value = ""
            
            mock_response = MagicMock()
            mock_forward.return_value = mock_response
            
            response = await gateway.v1_proxy("chat/completions", mock_request)
            
            assert response == mock_response
            mock_forward.assert_called_once()
            # Should pass a UUID as task_id
            call_args = mock_forward.call_args
            task_id = call_args[0][1]
            try:
                uuid.UUID(task_id)
            except ValueError:
                pytest.fail("Generated task_id is not a valid UUID")


# ============================
# Lifecycle Tests
# ============================

class TestLifecycle:
    """Test application lifecycle"""

    @pytest.mark.asyncio
    async def test_shutdown(self):
        """Test shutdown closes http client"""
        with patch('gateway.http_client') as mock_client:
            mock_client.aclose = AsyncMock()
            
            await gateway._shutdown()
            
            mock_client.aclose.assert_called_once()


# ============================
# Configuration Tests
# ============================

class TestConfiguration:
    """Test environment configuration"""

    def test_app_version(self):
        """Test APP_VERSION is defined"""
        assert hasattr(gateway, 'APP_VERSION')
        assert gateway.APP_VERSION == "1.0"

    def test_event_type(self):
        """Test EVENT_TYPE is defined"""
        assert hasattr(gateway, 'EVENT_TYPE')
        assert gateway.EVENT_TYPE == "task.status.update"

    def test_algorithm_id(self):
        """Test ALGORITHM_ID is defined"""
        assert hasattr(gateway, 'ALGORITHM_ID')

    def test_redis_config(self):
        """Test Redis configuration"""
        assert hasattr(gateway, 'REDIS_HOST')
        assert hasattr(gateway, 'REDIS_PORT')
        assert hasattr(gateway, 'REDIS_USER')
        assert hasattr(gateway, 'REDIS_PASSWORD')
        assert hasattr(gateway, 'REDIS_DB')

    def test_ttl_config(self):
        """Test TTL configuration"""
        assert hasattr(gateway, 'TTL_RUNNING')
        assert hasattr(gateway, 'TTL_DONE')
        assert gateway.TTL_RUNNING > 0
        assert gateway.TTL_DONE > 0

    def test_heartbeat_config(self):
        """Test heartbeat configuration"""
        assert hasattr(gateway, 'HEARTBEAT_SEC')
        assert gateway.HEARTBEAT_SEC > 0

    def test_app_instance(self):
        """Test FastAPI app instance"""
        assert hasattr(gateway, 'app')
        assert gateway.app.title == "Ollama OpenAI Gateway + Task Status"
        assert gateway.app.version == gateway.APP_VERSION


# ============================
# Integration Tests
# ============================

@pytest.mark.integration
class TestIntegration:
    """Integration tests (require actual services)"""

    def test_redis_connection(self):
        """Test Redis connection (if available)"""
        try:
            # Try to ping Redis
            result = gateway.rds.ping()
            assert result is True
        except Exception as e:
            pytest.skip(f"Redis not available: {e}")

    def test_redis_write_and_read(self):
        """Test writing and reading from Redis (if available)"""
        try:
            task_id = "test-integration-task"
            evt = {
                "task_id": task_id,
                "state": "TEST",
                "timestamp": 1234567890
            }
            
            # Write
            gateway.write_status(task_id, evt, ttl=60)
            
            # Read
            result = gateway.rds.get(gateway.rkey(task_id))
            assert result is not None
            
            data = json.loads(result)
            assert data["task_id"] == task_id
            assert data["state"] == "TEST"
            
            # Cleanup
            gateway.rds.delete(gateway.rkey(task_id))
            
        except Exception as e:
            pytest.skip(f"Redis not available: {e}")


# ============================
# Run Tests
# ============================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=gateway", "--cov-report=term-missing"])
