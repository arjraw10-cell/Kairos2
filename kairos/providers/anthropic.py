"""Anthropic Messages API wire format over plain HTTP (httpx).

Raw HTTP keeps this a generic wire-format adapter for any Messages-compatible
``base_url`` without adding a dependency.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from .base import EventStream, ProviderError, key_pool
from .config import ModelSpec

ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 32000  # required by the API; streaming avoids HTTP timeouts
THINKING_KEY = "_thinking"  # assistant-message key holding thinking blocks to echo back


def _image_block(part: dict) -> dict:
    image = part.get("image_url")
    url = image if isinstance(image, str) else (image or {}).get("url", "")
    if url.startswith("data:") and ";base64," in url:
        header, data = url.split(",", 1)
        return {"type": "image", "source": {"type": "base64", "media_type": header[5:].split(";")[0], "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _content_blocks(content: Any) -> list:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks = []
    for part in content:
        if isinstance(part, str):
            part = {"type": "text", "text": part}
        if part.get("type") == "text":
            if part.get("text"):
                blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            blocks.append(_image_block(part))
    return blocks


def translate_tools(tools: list) -> list:
    return [
        {
            "name": t["function"]["name"],
            "description": t["function"].get("description", ""),
            "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}},
        }
        for t in tools
        if t.get("type") == "function"
    ]


def translate_messages(messages: list) -> tuple[str, list]:
    """Chat messages -> (system, Messages-API messages).

    System messages move to ``system``; tool results become ``tool_result``
    blocks in a user turn; consecutive same-role turns are merged so tool
    results directly follow the assistant ``tool_use`` turn.
    """
    system_parts: list[str] = []
    out: list[dict] = []

    def add(role: str, blocks: list) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            system_parts.extend(b["text"] for b in _content_blocks(msg.get("content")))
        elif role == "tool":
            content = msg.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            add("user", [{"type": "tool_result", "tool_use_id": msg.get("tool_call_id", ""), "content": text or ""}])
        elif role == "assistant":
            blocks = list(msg.get(THINKING_KEY) or []) + _content_blocks(msg.get("content"))
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id", ""), "name": fn.get("name", ""), "input": args})
            add("assistant", blocks)
        else:
            add("user", _content_blocks(msg.get("content")))
    return "\n\n".join(system_parts), out


class _HTTPStream:
    """Closable SSE response; ``close`` releases a blocked read on hard stop."""

    def __init__(self, client: httpx.Client, response: httpx.Response):
        self.client = client
        self.response = response

    def close(self) -> None:
        self.response.close()


def parse_sse(lines):
    """Yield the JSON payload of each ``data:`` line."""
    for line in lines:
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                yield json.loads(data)


class AnthropicAdapter:
    def __init__(self, spec: ModelSpec, transport: Optional[httpx.BaseTransport] = None):
        self.spec = spec
        self._pool = key_pool(spec.api_keys)
        self._client = httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0), transport=transport)

    def request_body(self, messages: list, tools: Optional[list], effort: Optional[str]) -> dict[str, Any]:
        system, translated = translate_messages(messages)
        body: dict[str, Any] = {
            "model": self.spec.model,
            "max_tokens": MAX_TOKENS,
            "messages": translated,
            "stream": True,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = translate_tools(tools)
        if effort:
            body["thinking"] = {"type": "adaptive"}
            body["output_config"] = {"effort": effort}
        return body

    def _open(self, key: str, body: dict) -> _HTTPStream:
        url = (self.spec.base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        response = self._client.send(self._client.build_request("POST", url, headers=headers, json=body), stream=True)
        if response.status_code != 200:
            try:
                detail = json.loads(response.read()).get("error", {}).get("message", "")
            except Exception:
                detail = ""
            finally:
                response.close()
            raise ProviderError(f"HTTP {response.status_code}: {detail or response.reason_phrase}", response.status_code)
        return _HTTPStream(self._client, response)

    def stream(self, messages: list, tools: Optional[list], effort: Optional[str]) -> EventStream:
        body = self.request_body(messages, tools, effort)
        raw = self._pool.call(lambda key: self._open(key, body))
        return EventStream(self.events(parse_sse(raw.response.iter_lines())), raw.close)

    @staticmethod
    def events(payloads):
        prompt_tokens = completion_tokens = cached_tokens = 0
        thinking: dict[int, dict] = {}
        tool_indexes: set[int] = set()
        for event in payloads:
            etype = event.get("type")
            if etype == "message_start":
                usage = event.get("message", {}).get("usage", {})
                prompt_tokens = (
                    usage.get("input_tokens", 0)
                    + (usage.get("cache_creation_input_tokens") or 0)
                    + (usage.get("cache_read_input_tokens") or 0)
                )
                completion_tokens = usage.get("output_tokens", 0)
                cached_tokens = usage.get("cache_read_input_tokens") or 0
            elif etype == "content_block_start":
                index, block = event["index"], event["content_block"]
                if block["type"] == "tool_use":
                    tool_indexes.add(index)
                    yield ("tool_call", index, block["id"], block["name"], "")
                elif block["type"] in ("thinking", "redacted_thinking"):
                    thinking[index] = dict(block)
                elif block["type"] == "text" and block.get("text"):
                    yield ("text", block["text"])
            elif etype == "content_block_delta":
                index, delta = event["index"], event["delta"]
                dtype = delta.get("type")
                if dtype == "text_delta":
                    yield ("text", delta["text"])
                elif dtype == "input_json_delta" and index in tool_indexes:
                    yield ("tool_call", index, "", "", delta.get("partial_json", ""))
                elif dtype == "thinking_delta" and index in thinking:
                    thinking[index]["thinking"] = thinking[index].get("thinking", "") + delta.get("thinking", "")
                elif dtype == "signature_delta" and index in thinking:
                    thinking[index]["signature"] = thinking[index].get("signature", "") + delta.get("signature", "")
            elif etype == "message_delta":
                completion_tokens = (event.get("usage") or {}).get("output_tokens", completion_tokens)
            elif etype == "message_stop":
                yield ("usage", prompt_tokens, completion_tokens, cached_tokens)
                if thinking:
                    # Thinking blocks must be echoed back unchanged (with
                    # signatures) on the next request of a tool-use loop.
                    yield ("message_extra", THINKING_KEY, [thinking[i] for i in sorted(thinking)])
            elif etype == "error":
                error = event.get("error", {})
                status = 529 if error.get("type") == "overloaded_error" else None
                raise ProviderError(f"{error.get('type', 'error')}: {error.get('message', '')}", status)
