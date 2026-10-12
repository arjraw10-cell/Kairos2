"""OpenAI Responses API wire format.

Ported from the old local proxy (minimaxproxy/openaiproxy.py): Kairos keeps
Chat Completions history; this module translates it to Responses input items
and normalizes Responses stream events.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from openai import OpenAI

from .base import EventStream, ProviderError, key_pool
from .completions import openai_client
from .config import ModelSpec


def translate_tools(tools: list) -> list:
    """Chat tools nest the function under ``function``; Responses tools are flat."""
    out = []
    for tool in tools:
        if tool.get("type") == "function" and "function" in tool:
            fn = tool["function"]
            entry = {"type": "function", "name": fn["name"]}
            for key in ("description", "parameters", "strict"):
                if fn.get(key) is not None:
                    entry[key] = fn[key]
            out.append(entry)
        else:
            out.append(tool)
    return out


def translate_content(content: Any) -> Any:
    """Chat ``text``/``image_url`` parts become ``input_text``/``input_image``."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "input_text", "text": part})
        elif part.get("type") == "text":
            parts.append({"type": "input_text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            url, detail = (image, "auto") if isinstance(image, str) else (image.get("url"), image.get("detail") or "auto")
            parts.append({"type": "input_image", "image_url": url, "detail": detail})
        else:
            parts.append(part)  # already a Responses part
    return parts


def translate_messages(messages: list) -> list:
    """Chat messages -> Responses input items.

    An assistant tool call becomes a ``function_call`` item whose ``call_id``
    is the Chat ``tool_call_id`` that the matching ``function_call_output``
    refers back to.
    """
    out = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")
        if role == "tool":
            output = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            out.append({"type": "function_call_output", "call_id": msg.get("tool_call_id", ""), "output": output or ""})
        elif role == "assistant" and msg.get("tool_calls"):
            if content:
                out.append({"role": "assistant", "content": translate_content(content)})
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                call_id = tc.get("id", "")
                out.append({
                    "type": "function_call",
                    "id": f"fc_{call_id}",
                    "call_id": call_id,
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments") or "{}",
                })
        else:
            out.append({"role": role, "content": translate_content(content)})
    return out


class ResponsesAdapter:
    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self._pool = key_pool(spec.api_keys)
        self._clients: dict[str, OpenAI] = {}

    def request_kwargs(self, messages: list, tools: Optional[list], effort: Optional[str]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.spec.model,
            "input": translate_messages(messages),
            "stream": True,
        }
        if tools:
            kwargs["tools"] = translate_tools(tools)
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        return kwargs

    def stream(self, messages: list, tools: Optional[list], effort: Optional[str]) -> EventStream:
        kwargs = self.request_kwargs(messages, tools, effort)
        raw = self._pool.call(
            lambda key: openai_client(self.spec, key, self._clients).responses.create(**kwargs)
        )
        return EventStream(self.events(raw), raw.close)

    @staticmethod
    def events(stream):
        streamed_args: set[int] = set()
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                yield ("text", event.delta)
            elif etype == "response.output_item.added":
                item = event.item
                if getattr(item, "type", "") == "function_call":
                    yield ("tool_call", event.output_index, item.call_id, item.name, "")
            elif etype == "response.function_call_arguments.delta":
                streamed_args.add(event.output_index)
                yield ("tool_call", event.output_index, "", "", event.delta)
            elif etype == "response.output_item.done":
                item = event.item
                # Arguments normally stream as deltas; fall back to the final
                # item if a server sent them only here.
                if getattr(item, "type", "") == "function_call" and event.output_index not in streamed_args:
                    yield ("tool_call", event.output_index, "", "", item.arguments or "")
            elif etype in ("response.completed", "response.incomplete"):
                usage = getattr(event.response, "usage", None)
                if usage:
                    details = getattr(usage, "input_tokens_details", None)
                    cached = getattr(details, "cached_tokens", 0) or 0
                    yield ("usage", usage.input_tokens or 0, usage.output_tokens or 0, cached)
            elif etype == "response.failed":
                error = getattr(event.response, "error", None)
                raise ProviderError(f"Response failed: {getattr(error, 'message', None) or error}")
            elif etype == "error":
                raise ProviderError(f"Stream error: {getattr(event, 'message', None) or event}")
