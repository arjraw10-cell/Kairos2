"""Synchronous WebSocket client for the Kairos gateway (used by the CLI).

A reader thread receives every frame: acks/errors complete the matching
``request()`` call, events go to a queue that the CLI drains on its main
thread. Command names are spelled out here instead of importing
``kairos.gateway.protocol``, because importing that package loads the server
and the Agent stack.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import threading
from typing import Any

from websockets.sync.client import connect


class GatewayError(RuntimeError):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


def gateway_url() -> str:
    """WebSocket URL from KAIROS_GATEWAY_HOST/PORT (defaults 127.0.0.1:8765)."""
    host = os.environ.get("KAIROS_GATEWAY_HOST", "").strip() or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = os.environ.get("KAIROS_GATEWAY_PORT", "").strip() or "8765"
    return f"ws://{host}:{port}/api/v1/ws"


class GatewayClient:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 30.0):
        self.url = url or gateway_url()
        if token is None:
            token = os.environ.get("KAIROS_AUTH_TOKEN", "").strip() or None
        self.token = token
        self.timeout = timeout
        self.closed = False
        self._ws = None
        self._events: queue.Queue = queue.Queue()
        self._pending: dict[str, list] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def connect(self) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        try:
            # History acks and image payloads can be large; do not cap them.
            self._ws = connect(self.url, additional_headers=headers, max_size=None, open_timeout=5)
        except Exception as exc:
            raise GatewayError(f"Cannot reach the gateway at {self.url} ({exc}).", "disconnected") from None
        threading.Thread(target=self._read_loop, name="kairos-gateway-reader", daemon=True).start()
        return self.request("gateway.hello")

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def _read_loop(self) -> None:
        try:
            for raw in self._ws:
                message = json.loads(raw)
                if message.get("type") == "event":
                    self._events.put(message)
                    continue
                with self._lock:
                    waiter = self._pending.pop(message.get("request_id"), None)
                if waiter:
                    waiter[1] = message
                    waiter[0].set()
        except Exception:
            pass
        finally:
            self.closed = True
            with self._lock:
                waiters = list(self._pending.values())
                self._pending.clear()
            for waiter in waiters:
                waiter[0].set()
            self._events.put(None)  # wake a waiting next_event()

    def request(
        self,
        command: str,
        conversation_id: str | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one command and return its ack data; raise GatewayError on error."""
        if self.closed or self._ws is None:
            raise GatewayError("Not connected to the gateway.", "disconnected")
        request_id = f"cli-{next(self._ids)}"
        waiter: list = [threading.Event(), None]
        with self._lock:
            self._pending[request_id] = waiter
        envelope: dict[str, Any] = {
            "type": "command",
            "command": command,
            "request_id": request_id,
            "payload": payload or {},
        }
        if conversation_id:
            envelope["conversation_id"] = conversation_id
        try:
            self._ws.send(json.dumps(envelope))
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            raise GatewayError("Lost connection to the gateway.", "disconnected") from None
        if not waiter[0].wait(timeout or self.timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise GatewayError(f"The gateway did not answer {command}.", "timeout")
        message = waiter[1]
        if message is None:
            raise GatewayError("Lost connection to the gateway.", "disconnected")
        if message.get("type") == "error":
            raise GatewayError(message.get("message") or "Gateway error", message.get("code") or "error")
        return message.get("data") or {}

    def next_event(self, timeout: float) -> dict[str, Any] | None:
        """Return the next event, None on timeout; raise if the socket closed."""
        try:
            event = self._events.get(timeout=timeout)
        except queue.Empty:
            event = None
        if event is None and self.closed:
            self._events.put(None)  # keep later callers from blocking
            raise GatewayError("Lost connection to the gateway.", "disconnected")
        return event
