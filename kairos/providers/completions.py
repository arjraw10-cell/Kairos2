"""OpenAI Chat Completions wire format (OpenAI, OpenRouter, compatible servers)."""
from __future__ import annotations

from typing import Any, Optional

from openai import OpenAI

from .base import EventStream, key_pool, public_fields
from .config import ModelSpec

# Assistant-message key holding OpenRouter reasoning_details to echo back:
# {"model": <model>, "details": [...]}. Sent back only to the same model.
REASONING_KEY = "_reasoning_details"


def openai_client(spec: ModelSpec, key: str, cache: dict) -> OpenAI:
    if key not in cache:
        # No SDK retries: Agent retries with backoff and retry-after, and a
        # rate-limited key should rotate at once. Both would multiply attempts.
        cache[key] = OpenAI(api_key=key, base_url=spec.base_url or None, max_retries=0)
    return cache[key]


class CompletionsAdapter:
    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self._pool = key_pool(spec.api_keys)
        self._clients: dict[str, OpenAI] = {}
        # Set by the Agent: stable for one conversation, new after a reset.
        self.session_id: Optional[str] = None

    def request_kwargs(self, messages: list, tools: Optional[list], effort: Optional[str]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.spec.model,
            "messages": [self._wire_message(m) for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        extra: dict[str, Any] = {}
        if effort in ("on", "off"):
            # Models that only toggle reasoning (e.g. MiMo on OpenRouter).
            extra["reasoning"] = {"enabled": effort == "on"}
        elif effort:
            kwargs["reasoning_effort"] = effort
        if "openrouter.ai" in self.spec.base_url:
            # session_id keeps routing the conversation to the same provider
            # endpoint, whose prompt cache is warm (OpenRouter sticky routing).
            if self.session_id:
                extra["session_id"] = self.session_id
            # Claude models cache only at breakpoints; a top-level
            # cache_control puts one on the last cacheable block of each request.
            if self.spec.model.startswith("anthropic/"):
                extra["cache_control"] = {"type": "ephemeral"}
        if extra:
            kwargs["extra_body"] = extra
        return kwargs

    def _wire_message(self, message: dict) -> dict:
        wire = public_fields(message)
        # OpenRouter: passing reasoning back unmodified lets a reasoning model
        # continue its own thinking across tool calls instead of starting over.
        saved = message.get(REASONING_KEY)
        if isinstance(saved, dict) and saved.get("model") == self.spec.model and saved.get("details"):
            wire["reasoning_details"] = saved["details"]
        return wire

    def stream(self, messages: list, tools: Optional[list], effort: Optional[str]) -> EventStream:
        kwargs = self.request_kwargs(messages, tools, effort)
        raw = self._pool.call(
            lambda key: openai_client(self.spec, key, self._clients).chat.completions.create(**kwargs)
        )
        return EventStream(self.events(raw, self.spec.model), raw.close)

    @staticmethod
    def events(chunks, model: Optional[str] = None):
        reasoning: list[dict] = []
        for chunk in chunks:
            if getattr(chunk, "usage", None):
                details = getattr(chunk.usage, "prompt_tokens_details", None)
                cached = getattr(details, "cached_tokens", 0) or 0
                yield ("usage", chunk.usage.prompt_tokens or 0, chunk.usage.completion_tokens or 0, cached)
            if not chunk.choices:
                continue
            if getattr(chunk.choices[0], "finish_reason", None) == "length":
                yield ("truncated",)
            delta = chunk.choices[0].delta
            if not delta:
                continue
            for piece in (getattr(delta, "model_extra", None) or {}).get("reasoning_details") or ():
                if isinstance(piece, dict):
                    _merge_reasoning(reasoning, piece)
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
        if reasoning:
            yield ("message_extra", REASONING_KEY, {"model": model, "details": reasoning})


def _merge_reasoning(details: list, piece: dict) -> None:
    """Join streamed reasoning_details pieces: consecutive pieces with the same
    index and type extend one detail (text/summary/data concatenate, other
    fields such as a late signature are filled in)."""
    last = details[-1] if details else None
    if last is None or last.get("index") != piece.get("index") or last.get("type") != piece.get("type"):
        details.append(dict(piece))
        return
    for key, value in piece.items():
        if key in ("text", "summary", "data") and isinstance(value, str):
            last[key] = (last.get(key) or "") + value
        elif value is not None:
            last[key] = value
