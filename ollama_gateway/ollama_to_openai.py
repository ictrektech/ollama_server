import json
import uuid
from typing import Any, Dict, List, Optional


def normalize_finish_reason(reason: Optional[str]) -> str:
    if not reason:
        return "stop"
    if reason in {"stop", "length", "tool_calls", "content_filter"}:
        return reason
    return "stop"


def convert_ollama_tool_calls(tool_calls: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(tool_calls, list):
        return None

    converted = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        converted.append({
            "id": tool_call.get("id") or f"call_{uuid.uuid4().hex}",
            "type": "function",
            "function": {
                "name": function.get("name", ""),
                "arguments": arguments,
            },
        })

    return converted or None


def usage_from_ollama(data: Dict[str, Any]) -> Dict[str, int]:
    prompt_tokens = int(data.get("prompt_eval_count") or 0)
    completion_tokens = int(data.get("eval_count") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def convert_ollama_chat_response(data: Dict[str, Any], response_id: str, created: int) -> Dict[str, Any]:
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    openai_message: Dict[str, Any] = {
        "role": message.get("role", "assistant"),
        "content": message.get("content", ""),
    }
    if "thinking" in message:
        openai_message["reasoning_content"] = message["thinking"]

    tool_calls = convert_ollama_tool_calls(message.get("tool_calls"))
    finish_reason = normalize_finish_reason(data.get("done_reason"))
    if tool_calls is not None:
        openai_message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": data.get("model", ""),
        "choices": [{
            "index": 0,
            "message": openai_message,
            "finish_reason": finish_reason,
        }],
        "usage": usage_from_ollama(data),
    }


def convert_ollama_chat_stream_chunk(data: Dict[str, Any], response_id: str, created: int) -> Dict[str, Any]:
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    delta: Dict[str, Any] = {}
    if "role" in message:
        delta["role"] = message["role"]
    if "content" in message:
        delta["content"] = message["content"]
    if "thinking" in message:
        delta["reasoning_content"] = message["thinking"]

    tool_calls = convert_ollama_tool_calls(message.get("tool_calls"))
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls

    finish_reason = None
    if data.get("done") is True:
        finish_reason = "tool_calls" if tool_calls is not None else normalize_finish_reason(data.get("done_reason"))

    chunk: Dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": data.get("model", ""),
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    if data.get("done") is True:
        chunk["usage"] = usage_from_ollama(data)
    return chunk


def convert_ollama_generate_response(data: Dict[str, Any], response_id: str, created: int) -> Dict[str, Any]:
    return {
        "id": response_id,
        "object": "text_completion",
        "created": created,
        "model": data.get("model", ""),
        "choices": [{
            "text": data.get("response", ""),
            "index": 0,
            "logprobs": None,
            "finish_reason": normalize_finish_reason(data.get("done_reason")),
        }],
        "usage": usage_from_ollama(data),
    }


def convert_ollama_generate_stream_chunk(data: Dict[str, Any], response_id: str, created: int) -> Dict[str, Any]:
    finish_reason = None
    if data.get("done") is True:
        finish_reason = normalize_finish_reason(data.get("done_reason"))

    chunk: Dict[str, Any] = {
        "id": response_id,
        "object": "text_completion",
        "created": created,
        "model": data.get("model", ""),
        "choices": [{
            "text": data.get("response", ""),
            "index": 0,
            "logprobs": None,
            "finish_reason": finish_reason,
        }],
    }
    if data.get("done") is True:
        chunk["usage"] = usage_from_ollama(data)
    return chunk
