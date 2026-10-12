"""Shared pieces of the provider adapters.

Adapters translate Kairos' Chat-Completions-shaped history into a wire format
and normalize the streamed reply into these events:

* ``("text", delta)``
* ``("tool_call", index, call_id, name, arguments_delta)`` - id/name may be
  empty on continuation deltas; arguments accumulate per index
* ``("usage", prompt_tokens, completion_tokens, cached_tokens)`` - cached_tokens
  is the part of prompt_tokens served from the provider's prompt cache
* ``("message_extra", key, value)`` - provider state to keep on the assistant
  message (private ``_`` keys, e.g. Anthropic thinking blocks)
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterable


class ProviderError(Exception):
    """A provider failure that did not come from the OpenAI SDK."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class EventStream:
    """Iterable of normalized events whose HTTP stream can be closed from
    another thread (hard stop)."""

    def __init__(self, events: Iterable[tuple], close: Callable[[], Any]):
        self._events = events
        self._close = close

    def __iter__(self):
        return iter(self._events)

    def close(self) -> None:
        self._close()


def public_fields(message: dict) -> dict:
    """Drop Kairos-private keys (``_image_url``, ``_thinking``, ...)."""
    return {k: v for k, v in message.items() if not k.startswith("_")}


# Statuses that mean "this key cannot serve now": try the next key.
ROTATE_STATUSES = {401, 402, 429}
COOLDOWN_SECONDS = 60.0


class KeyPool:
    """Ordered API keys with a short cooldown after auth/quota/rate failures.

    Keys are tried in order, skipping cooling keys (like the old OpenRouter
    proxy). If every key is cooling, the one that recovers first is tried.
    """

    def __init__(self, keys: Iterable[str]):
        self._keys = list(keys)
        self._cooldown: dict[str, float] = {}
        self._lock = threading.Lock()

    def _order(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            available = [k for k in self._keys if self._cooldown.get(k, 0.0) <= now]
            if available:
                return available
            return [min(self._keys, key=lambda k: self._cooldown.get(k, 0.0))]

    def _cool(self, key: str) -> None:
        with self._lock:
            self._cooldown[key] = time.monotonic() + COOLDOWN_SECONDS

    def call(self, fn: Callable[[str], Any]) -> Any:
        if not self._keys:
            raise ProviderError("No API keys configured for this provider in provider.json.")
        last_exc: Exception | None = None
        for key in self._order():
            try:
                return fn(key)
            except Exception as exc:
                if getattr(exc, "status_code", None) not in ROTATE_STATUSES:
                    raise
                self._cool(key)
                last_exc = exc
        raise last_exc


_pools: dict[tuple, KeyPool] = {}
_pools_lock = threading.Lock()


def key_pool(keys: Iterable[str]) -> KeyPool:
    """Process-wide pool per key list, so conversations share cooldowns."""
    keys = tuple(keys)
    with _pools_lock:
        if keys not in _pools:
            _pools[keys] = KeyPool(keys)
        return _pools[keys]
