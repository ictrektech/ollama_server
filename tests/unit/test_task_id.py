"""Unit tests for task ID / UUID generation logic."""

import unittest
from unittest.mock import Mock, MagicMock
import uuid

from ollama_gateway.gateway import get_task_id


class TestGetTaskId(unittest.TestCase):
    """Test cases for the get_task_id() function."""

    def _make_request(self, headers: dict | None = None) -> Mock:
        """Create a mock Request object with specified headers."""
        req = Mock()
        req.headers = MagicMock()
        if headers is None:
            req.headers.get.return_value = None
        else:
            req.headers.get.side_effect = lambda key: headers.get(key)
        return req

    def test_header_missing_returns_valid_uuid(self):
        """When x-task-id header is missing, should generate a valid UUID."""
        req = self._make_request(headers={})
        result = get_task_id(req)
        print(f"[DEBUG] Generated UUID: {result}")

        # Should be a valid UUID
        parsed = uuid.UUID(result)
        self.assertEqual(str(parsed), result)

    def test_header_missing_generates_unique_uuids(self):
        """When x-task-id header is missing, each call should generate a unique UUID."""
        req = self._make_request(headers={})
        results = [get_task_id(req) for _ in range(10)]
        print(f"[DEBUG] Generated UUIDs: {results}")

        # All 10 UUIDs should be unique
        self.assertEqual(len(set(results)), 10)

    def test_header_empty_string_returns_uuid(self):
        """When x-task-id header is empty string, should generate a valid UUID."""
        req = self._make_request(headers={"x-task-id": ""})
        result = get_task_id(req)
        print(f"[DEBUG] Empty header -> Generated UUID: {result}")

        parsed = uuid.UUID(result)
        self.assertEqual(str(parsed), result)

    def test_header_whitespace_only_returns_uuid(self):
        """When x-task-id header is only whitespace, should generate a valid UUID."""
        req = self._make_request(headers={"x-task-id": "   "})
        result = get_task_id(req)
        print(f"[DEBUG] Whitespace header -> Generated UUID: {result}")

        parsed = uuid.UUID(result)
        self.assertEqual(str(parsed), result)

    def test_header_with_value_passes_through(self):
        """When x-task-id header has a value, it should be passed through unchanged."""
        req = self._make_request(headers={"x-task-id": "my-task-123"})
        result = get_task_id(req)
        print(f"[DEBUG] Header 'my-task-123' -> Passed through: {result}")

        self.assertEqual(result, "my-task-123")

    def test_header_with_value_trims_whitespace(self):
        """When x-task-id header has whitespace, it should be trimmed."""
        req = self._make_request(headers={"x-task-id": "  my-task-123  "})
        result = get_task_id(req)
        print(f"[DEBUG] Header '  my-task-123  ' -> Trimmed: '{result}'")

        self.assertEqual(result, "my-task-123")

    def test_header_with_uuid_value_passes_through(self):
        """When x-task-id header is a UUID string, it should be passed through."""
        original_uuid = "550e8400-e29b-41d4-a716-446655440000"
        req = self._make_request(headers={"x-task-id": original_uuid})
        result = get_task_id(req)
        print(f"[DEBUG] Header '{original_uuid}' -> Passed through: {result}")

        self.assertEqual(result, original_uuid)


if __name__ == "__main__":
    unittest.main()
