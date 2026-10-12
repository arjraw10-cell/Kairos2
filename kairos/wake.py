"""Wake scheduling for background events that arrive while the agent is idle.

Hosts (the gateway, the offline CLI) pass every ``agent.on_background_event``
event to :meth:`WakeScheduler.push`. Events that arrive while the host is idle
are batched for ``debounce`` seconds and handed to ``start_wake`` once, which
starts a "wake run" whose input is :func:`format_notifications`. Events marked
``in_run`` are being injected into the active run by the Agent itself and are
ignored here, so every event is delivered exactly once.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

NOTIFICATION_PREFIX = "[Background notification]"


def format_notifications(events: list[dict]) -> str:
    """Render events as the user-role message the model reads."""
    return "\n\n".join(f"{NOTIFICATION_PREFIX}\n{event.get('text', '')}" for event in events)


class WakeScheduler:
    def __init__(
        self,
        start_wake: Callable[[list[dict]], None],
        is_idle: Callable[[], bool],
        debounce: float = 2.0,
        cap: int = 10,
        on_paused: Optional[Callable[[int], None]] = None,
    ):
        self._start_wake = start_wake
        self._is_idle = is_idle
        self._debounce = debounce
        self._cap = cap
        self._on_paused = on_paused
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self._timer: Optional[threading.Timer] = None
        self._wakes = 0  # consecutive wakes since the last user message
        self._paused = False
        self._closed = False

    def push(self, event: dict) -> None:
        """Record an event; thread-safe. ``in_run`` events are the run's own."""
        if event.get("in_run"):
            return
        with self._lock:
            self._pending.append(event)
            self._arm_locked()

    def take_pending(self) -> list[dict]:
        """Return and forget undelivered events (attach them to the next run)."""
        with self._lock:
            events, self._pending = self._pending, []
        return events

    def user_message_sent(self) -> None:
        """A user message resets the consecutive-wake count and un-pauses."""
        with self._lock:
            self._wakes = 0
            self._paused = False
            self._arm_locked()

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def close(self) -> None:
        """Stop waking (the host is going away); pending events stay takeable."""
        with self._lock:
            self._closed = True
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def _arm_locked(self) -> None:
        # The batch window starts at the first pending event; later events
        # join it rather than pushing the wake further out.
        if self._timer or self._paused or self._closed or not self._pending:
            return
        self._timer = threading.Timer(self._debounce, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        # is_idle may take host locks, so call it outside ours.
        idle = self._is_idle()
        events: list[dict] = []
        paused_count = None
        with self._lock:
            self._timer = None
            if self._closed or self._paused or not self._pending:
                return
            if not idle:
                # Busy: a user run will take the events, or we retry later.
                self._arm_locked()
                return
            if self._wakes >= self._cap:
                self._paused = True
                paused_count = len(self._pending)
            else:
                events, self._pending = self._pending, []
                self._wakes += 1
        if paused_count is not None:
            if self._on_paused:
                self._on_paused(paused_count)
            return
        self._start_wake(events)
