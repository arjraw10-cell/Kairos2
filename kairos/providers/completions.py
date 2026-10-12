"""OpenAI Chat Completions wire format (OpenAI, OpenRouter, compatible servers)."""
from __future__ import annotations

from typing import Any, Optional

from openai import OpenAI

from .base import EventStream, key_pool, public_fields
from .config import ModelSpec


def openai_client(spec: ModelSpec, key: str, cache: dict) -> OpenAI:
    if key not in cache:
        # With several keys, a rate-limited key should rotate immediately
        # rather than wait out the SDK's own retries; Agent still retries.
        retries = 0 if len(spec.api_keys) > 1 else 2
        cache[key] = OpenAI(api_key=key, base_url=spec.base_url or None, max_retries=retries)
    return cache[key]


class CompletionsAdapter:
    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self._pool = key_pool(spec.api_keys)
        self._clients: dict[str, OpenAI] = {}

    def request_kwargs(self, messages: list, tools: Optional[list], effort: Optional[str]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.spec.model,
            "messages": [public_fields(m) for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if effort in ("on", "off"):
            # Models that only toggle reasoning (e.g. MiMo on OpenRouter).
            kwargs["extra_body"] = {"reasoning": {"enabled": effort == "on"}}
        elif effort:
            kwargs["reasoning_effort"] = effort
        return kwargs

    def stream(self, messages: list, tools: Optional[list], effort: Optional[str]) -> EventStream:
        kwargs = self.request_kwargs(messages, tools, effort)
        raw = self._pool.call(
            lambda key: openai_client(self.spec, key, self._clients).chat.completions.create(**kwargs)
        )
        return EventStream(self.events(raw), raw.close)

    @staticmethod
    def events(chunks):
        for chunk in chunks:
            if getattr(chunk, "usage", None):
                yield ("usage", chunk.usage.prompt_tokens or 0, chunk.usage.completion_tokens or 0)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if not delta:
                continue
            if delta.content:
                yield ("text", delta.content)
            for tc in delta.tool_calls or ():
                fn = tc.function
                yield (
                    "tool_call",
                    tc.index,
                    tc.id or "",
                    (fn.name if fn else None) or "",
                    (fn.arguments if fn else None) or "",
                )
