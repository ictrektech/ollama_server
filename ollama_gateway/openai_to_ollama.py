import json
from typing import Any, Dict, List, Optional, Tuple


OPENAI_TO_OLLAMA_OPTIONS = {
    "max_tokens": "num_predict",
    "max_completion_tokens": "num_predict",
    "temperature": "temperature",
    "top_p": "top_p",
    "seed": "seed",
    "stop": "stop",
    "frequency_penalty": "repeat_penalty",
    "presence_penalty": "presence_penalty",
}


def extract_data_uri_payload(value: str) -> str:
    if value.startswith("data:") and "," in value:
        return value.split(",", 1)[1]
    return value


def normalize_openai_content(content: Any) -> Tuple[str, List[str]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content), []

    text_parts: List[str] = []
    images: List[str] = []
    for part in content:
        if not isinstance(part, dict):
            text_parts.append(str(part))
            continue

        part_type = part.get("type")
        if part_type in {"text", "input_text"}:
            text_parts.append(str(part.get("text", "")))
        elif part_type in {"image_url", "input_image"}:
            image_value = part.get("image_url") or part.get("image")
            if isinstance(image_value, dict):
                image_value = image_value.get("url")
            if isinstance(image_value, str):
                images.append(extract_data_uri_payload(image_value))

    return "".join(text_parts), images


def normalize_tool_calls(tool_calls: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(tool_calls, list):
        return None

    normalized = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            continue

        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                arguments = {"arguments": arguments}

        normalized.append({
            "function": {
                "name": function.get("name", ""),
                "arguments": arguments,
            }
        })

    return normalized or None


def normalize_openai_messages(messages: Any) -> List[Dict[str, Any]]:
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")

    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("each message must be an object")

        content, images = normalize_openai_content(message.get("content"))
        out: Dict[str, Any] = {
            "role": message.get("role", "user"),
            "content": content,
        }

        thinking = message.get("thinking", message.get("reasoning_content"))
        if thinking is not None:
            out["thinking"] = thinking
        if images:
            out["images"] = images

        tool_calls = normalize_tool_calls(message.get("tool_calls"))
        if tool_calls is not None:
            out["tool_calls"] = tool_calls

        tool_name = message.get("tool_name") or message.get("name")
        if tool_name is not None:
            out["tool_name"] = tool_name

        normalized.append(out)

    return normalized


def map_response_format(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if value.get("type") == "json_object":
        return "json"
    if value.get("type") == "json_schema":
        json_schema = value.get("json_schema")
        if isinstance(json_schema, dict) and "schema" in json_schema:
            return json_schema["schema"]
    return value


def map_reasoning_to_think(value: Any, model: str) -> Any:
    if isinstance(value, dict):
        value = value.get("effort")
    if value is None:
        return None
    if model.startswith("gpt-oss"):
        return value
    return value in {"low", "medium", "high"}


def build_ollama_options(openai_payload: Dict[str, Any]) -> Dict[str, Any]:
    options: Dict[str, Any] = {}
    for openai_name, ollama_name in OPENAI_TO_OLLAMA_OPTIONS.items():
        if openai_name in openai_payload and openai_payload[openai_name] is not None:
            options[ollama_name] = openai_payload[openai_name]

    native_options = openai_payload.get("options")
    if native_options is not None:
        if not isinstance(native_options, dict):
            raise ValueError("options must be an object")
        options.update(native_options)

    return options


def build_ollama_chat_payload(openai_payload: Dict[str, Any]) -> Dict[str, Any]:
    model = openai_payload.get("model")
    if not model:
        raise ValueError("model is required")

    options = build_ollama_options(openai_payload)
    payload: Dict[str, Any] = {
        "model": model,
        "messages": normalize_openai_messages(openai_payload.get("messages")),
        "stream": openai_payload.get("stream", False) is True,
    }
    if options:
        payload["options"] = options

    if "format" in openai_payload:
        payload["format"] = openai_payload["format"]
    elif "response_format" in openai_payload:
        payload["format"] = map_response_format(openai_payload["response_format"])

    for key in ("keep_alive", "tools"):
        if key in openai_payload and openai_payload[key] is not None:
            payload[key] = openai_payload[key]

    think = openai_payload.get("think")
    if think is None:
        think = map_reasoning_to_think(openai_payload.get("reasoning_effort"), model)
    if think is None:
        think = map_reasoning_to_think(openai_payload.get("reasoning"), model)
    if think is not None:
        payload["think"] = think

    return payload


def normalize_completion_prompt(prompt: Any) -> str:
    if prompt is None:
        raise ValueError("prompt is required")
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        if all(isinstance(item, str) for item in prompt):
            return "\n".join(prompt)
        if all(isinstance(item, int) for item in prompt):
            return " ".join(str(item) for item in prompt)
    return str(prompt)


def build_ollama_generate_payload(openai_payload: Dict[str, Any]) -> Dict[str, Any]:
    model = openai_payload.get("model")
    if not model:
        raise ValueError("model is required")

    payload: Dict[str, Any] = {
        "model": model,
        "prompt": normalize_completion_prompt(openai_payload.get("prompt")),
        "stream": openai_payload.get("stream", False) is True,
    }

    options = build_ollama_options(openai_payload)
    if options:
        payload["options"] = options

    if "format" in openai_payload:
        payload["format"] = openai_payload["format"]
    elif "response_format" in openai_payload:
        payload["format"] = map_response_format(openai_payload["response_format"])

    for key in ("suffix", "system", "template", "context", "raw", "keep_alive", "images"):
        if key in openai_payload and openai_payload[key] is not None:
            payload[key] = openai_payload[key]

    think = openai_payload.get("think")
    if think is None:
        think = map_reasoning_to_think(openai_payload.get("reasoning_effort"), model)
    if think is None:
        think = map_reasoning_to_think(openai_payload.get("reasoning"), model)
    if think is not None:
        payload["think"] = think

    return payload
