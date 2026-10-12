"""Gateway runtime manager.

A Conversation is durable metadata/history.  A Runtime is the in-memory
Agent and its browser/terminal resources.  The manager serializes operations
per conversation while allowing independent conversations to run in parallel.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

from .. import providers
from ..agent import Agent
from ..config import Config
from ..wake import WakeScheduler, format_notifications
from ..tokens import status_payload
from ..workspaces import new_workspace
from .history import sanitize_for_resume
from .models import Conversation, Run
from .repository import GatewayRepository

log = logging.getLogger("kairos.gateway")
AgentFactory = Callable[[str], Agent]


@dataclass
class AgentRuntime:
    conversation_id: str
    workspace_path: str
    agent: Agent
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state_lock: threading.RLock = field(default_factory=threading.RLock)
    is_running: bool = False
    active_run_id: str | None = None
    closing: bool = False
    control_busy: bool = False
    last_activity: float = field(default_factory=time.time)
    subscribers: set[str] = field(default_factory=set)
    scheduler: WakeScheduler | None = None
    loop: asyncio.AbstractEventLoop | None = None


class GatewayManager:
    """Own persistent conversations and transient Agent runtimes."""

    def __init__(
        self,
        repository: GatewayRepository | None = None,
        default_workspace: str | None = None,
        max_concurrent_runs: int | None = None,
        agent_factory: AgentFactory | None = None,
        wake_debounce: float = 2.0,
        wake_cap: int = 10,
    ):
        # Background events on an idle conversation are batched for
        # wake_debounce seconds; at most wake_cap wakes without a user message.
        self.wake_debounce = wake_debounce
        self.wake_cap = wake_cap
        self.repository = repository or GatewayRepository(Config.KAIROS_DATA_DIR() or None)
        self.default_workspace = default_workspace or Config.KAIROS_DEFAULT_WORKSPACE()
        configured_workers = (
            Config.KAIROS_MAX_CONCURRENT_RUNS()
            if max_concurrent_runs is None
            else max_concurrent_runs
        )
        self.max_concurrent_runs = max(1, int(configured_workers))
        self.agent_factory = agent_factory or Agent
        self._runtimes: dict[str, AgentRuntime] = {}
        self._runtimes_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._run_tasks: dict[str, asyncio.Task] = {}
        self._run_tasks_lock = threading.RLock()
        self._pending_runs: dict[str, deque[str]] = {}
        self._subscribers: dict[str, dict[str, tuple[asyncio.AbstractEventLoop, Callable[[dict[str, Any]], Awaitable[None]]]]] = {}
        self._subscribers_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, self.max_concurrent_runs),
            thread_name_prefix="kairos-agent",
        )
        self._closed = False
        self._startup_recovery: list[str] = self.repository.mark_stale_runs_interrupted()
        self._legacy_import: dict[str, Any] | None = None
        legacy_file = Config.KAIROS_LEGACY_CHAT_FILE()
        if legacy_file and self.default_workspace:
            self._legacy_import = self.repository.import_legacy_chats(legacy_file, self.default_workspace)

    def resolve_workspace(self, workspace: str | None) -> str:
        # A conversation created without a workspace gets its own fresh folder.
        if not workspace:
            return str(new_workspace())
        # Workspace paths are context, not a security boundary.  Do not apply
        # an allowlist or containment check here.
        return str(Path(workspace).expanduser().resolve())

    def create_conversation(self, workspace: str | None = None, title: str = "") -> Conversation:
        return self.repository.create_conversation(self.resolve_workspace(workspace), title)

    def get_conversation(self, conversation_id: str) -> Conversation:
        conversation = self.repository.get_conversation(conversation_id)
        if not conversation:
            raise KeyError(f"Conversation not found: {conversation_id}")
        self._apply_runtime_state(conversation)
        return conversation

    def list_conversations(self, **kwargs: Any) -> list[Conversation]:
        conversations = self.repository.list_conversations(**kwargs)
        for conversation in conversations:
            self._apply_runtime_state(conversation)
        return conversations

    def list_workspaces(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.repository.list_workspaces(**kwargs)

    def _apply_runtime_state(self, conversation: Conversation) -> None:
        with self._runtimes_lock:
            runtime = self._runtimes.get(conversation.id)
        if not runtime:
            return
        with runtime.state_lock:
            is_running = runtime.is_running
            active_run_id = runtime.active_run_id
        with self._run_tasks_lock:
            pending = self._pending_runs.get(conversation.id)
            next_queued_id = pending[0] if pending else None
        conversation.runtime_loaded = True
        if is_running:
            conversation.status = "running"
            conversation.active_run_id = active_run_id
        elif next_queued_id:
            # A task can be queued for the runtime before it acquires the
            # per-conversation lock. Reflect that state instead of briefly
            # reporting the loaded runtime as idle.
            conversation.status = "queued"
            conversation.active_run_id = next_queued_id
        else:
            conversation.status = "idle"
            conversation.active_run_id = None

    def _create_runtime(self, conversation: Conversation) -> AgentRuntime:
        history = self.repository.load_history(conversation.id)
        agent = self.agent_factory(conversation.workspace_path)
        try:
            self._apply_model_settings(agent, self.repository.get_model_settings(conversation.id))
        except ValueError:
            # A setup removed from provider.json must not block loading.
            log.warning("Stored model settings for %s are invalid; using the default setup", conversation.id, exc_info=True)
        if history:
            sanitized, _needs_continue = sanitize_for_resume(history)
            if sanitized:
                # Rebuild the workspace-specific system prompt in Agent, then
                # replace it with the persisted history's system prompt and
                # messages. The stored system prompt is retained for exact
                # continuity; a fresh Agent supplied the current tools/context.
                agent.conversation_history = copy.deepcopy(sanitized)
                try:
                    tokens = getattr(agent, "tokens", None)
                    if tokens is not None:
                        tokens.start_turn(agent.conversation_history)
                        tokens.finish_turn()
                except Exception:
                    log.exception("Could not restore token counters for %s", conversation.id)
        # Notifications saved when the runtime was last unloaded ride along
        # with the next run.
        pending = self.repository.take_pending_notifications(conversation.id)
        if pending and hasattr(agent, "add_background_events"):
            agent.add_background_events(pending)
        return AgentRuntime(conversation.id, conversation.workspace_path, agent)

    def _attach_background(self, runtime: AgentRuntime) -> None:
        """Route the Agent's background events and sub-agent statuses.

        These callbacks live as long as the runtime (events arrive while it
        is idle). Idle events go through a WakeScheduler that starts a wake
        run; events during a run are injected by the Agent itself.
        """
        cid = runtime.conversation_id
        scheduler = WakeScheduler(
            start_wake=lambda events: self._start_wake(runtime, events),
            is_idle=lambda: self._runtime_is_idle(runtime),
            debounce=self.wake_debounce,
            cap=self.wake_cap,
            on_paused=lambda pending: self.emit(cid, "wake.paused", {"conversation_id": cid, "pending": pending}),
        )
        runtime.scheduler = scheduler

        def on_background_event(event: dict[str, Any]) -> None:
            self.emit(cid, "background.notification", {"conversation_id": cid, "notifications": [event]})
            scheduler.push(event)

        def on_subagent_status(status: dict[str, Any]) -> None:
            # Per-tool progress is live-only; final states are replayable.
            self.emit(cid, "subagent.status", {"conversation_id": cid, **status}, persist=status.get("state") != "running")

        setattr(runtime.agent, "on_background_event", on_background_event)
        setattr(runtime.agent, "on_subagent_status", on_subagent_status)

    def _runtime_is_idle(self, runtime: AgentRuntime) -> bool:
        with runtime.state_lock:
            if runtime.is_running or runtime.closing or runtime.control_busy:
                return False
        with self._run_tasks_lock:
            return not self._pending_runs.get(runtime.conversation_id)

    def _start_wake(self, runtime: AgentRuntime, events: list[dict[str, Any]]) -> None:
        """WakeScheduler callback (timer thread): queue a wake run."""
        def give_back() -> None:
            # Not lost: the next run (or the unload snapshot) picks them up.
            add = getattr(runtime.agent, "add_background_events", None)
            if add:
                add(events)

        def enqueue() -> None:
            try:
                self._enqueue_run(runtime, format_notifications(events), None, "wake", None, None, origin="wake")
            except Exception:
                log.warning("Could not start a wake run for %s", runtime.conversation_id, exc_info=True)
                give_back()

        try:
            runtime.loop.call_soon_threadsafe(enqueue)
        except Exception:
            give_back()

    @staticmethod
    def _has_background_work(runtime: AgentRuntime) -> bool:
        busy = getattr(runtime.agent, "has_background_work", None)
        return bool(busy and busy())

    def list_subagents(self, conversation_id: str) -> list[dict[str, Any]]:
        self.get_conversation(conversation_id)
        runtime = self.get_runtime(conversation_id)
        statuses = getattr(runtime.agent, "subagent_statuses", None) if runtime else None
        return statuses() if statuses else []

    def stop_subagents(self, conversation_id: str) -> int:
        self.get_conversation(conversation_id)
        runtime = self.get_runtime(conversation_id)
        stop = getattr(runtime.agent, "stop_subagents", None) if runtime else None
        return stop() if stop else 0

    async def load_runtime(self, conversation_id: str) -> AgentRuntime:
        conversation = self.get_conversation(conversation_id)
        loaded = False
        with self._lifecycle_lock, self._runtimes_lock:
            if self._closed:
                raise RuntimeError("Gateway is shutting down.")
            existing = self._runtimes.get(conversation_id)
            if existing:
                with existing.state_lock:
                    if existing.closing:
                        raise RuntimeError("Conversation runtime is shutting down.")
                    existing.last_activity = time.time()
                return existing
            # Keep creation inside the lifecycle/registry locks so concurrent
            # loads and unloads cannot construct duplicate Agents or tear down
            # a runtime between construction and registration.
            runtime = self._create_runtime(conversation)
            runtime.loop = asyncio.get_running_loop()
            self._attach_background(runtime)
            self._runtimes[conversation_id] = runtime
            loaded = True
        if loaded:
            self.repository.update_conversation(conversation_id, status="idle")
            self.emit(conversation_id, "conversation.loaded", {"workspace_path": conversation.workspace_path})
        return runtime

    def get_runtime(self, conversation_id: str) -> AgentRuntime | None:
        with self._runtimes_lock:
            return self._runtimes.get(conversation_id)

    def _runtime_is_running(self, runtime: AgentRuntime) -> bool:
        with runtime.state_lock:
            return runtime.is_running

    async def unload_runtime(self, conversation_id: str, force: bool = False) -> None:
        if not self.repository.get_conversation(conversation_id):
            raise KeyError(f"Conversation not found: {conversation_id}")
        # Mark the runtime while holding the same lifecycle lock used by
        # load_runtime. A concurrent load therefore observes ``closing`` and
        # cannot return a runtime that teardown is about to remove.
        with self._lifecycle_lock:
            with self._runtimes_lock:
                runtime = self._runtimes.get(conversation_id)
            if not runtime:
                return
            # New submissions then fail instead of being added while teardown
            # is in progress. If a non-forced unload is rejected, the marker is
            # cleared in the exception path so the runtime remains usable.
            with runtime.state_lock:
                if runtime.closing:
                    return
                if not force and runtime.control_busy:
                    raise RuntimeError("Conversation runtime is busy.")
                runtime.closing = True
                is_running = runtime.is_running
        with self._run_tasks_lock:
            pending_ids = list(self._pending_runs.get(conversation_id, ()))
        if not force and (is_running or pending_ids):
            with runtime.state_lock:
                runtime.closing = False
            if pending_ids:
                raise RuntimeError("Conversation has queued runs; use force=true to unload it.")
            raise RuntimeError("Conversation has an active run; interrupt it or use force=true.")

        removed = False
        try:
            # Acquire the runtime lock before teardown. A forced unload requests
            # interruption first, then waits for the active Agent worker to
            # reach its stable run boundary before closing resources.
            if force and is_running:
                runtime.agent.interrupt()
            async with runtime.lock:
                with self._lifecycle_lock, self._runtimes_lock:
                    current_runtime = self._runtimes.get(conversation_id)
                    if current_runtime is not runtime:
                        return
                with self._run_tasks_lock:
                    pending_ids = list(self._pending_runs.get(conversation_id, ()))
                if force:
                    for run_id in pending_ids:
                        try:
                            await self.cancel_run(run_id)
                        except KeyError:
                            pass
                if runtime.scheduler:
                    runtime.scheduler.close()
                # Running sub-agents cannot survive the runtime: mark them lost
                # (status + a notification for the next run).
                mark_lost = getattr(getattr(runtime.agent, "subagent_tool", None), "mark_lost", None)
                if mark_lost:
                    mark_lost()
                self._persist_runtime(runtime)
                self._close_agent_resources(runtime.agent)
                self._save_pending_notifications(runtime)
                with self._lifecycle_lock, self._runtimes_lock:
                    self._runtimes.pop(conversation_id, None)
                removed = True
                with runtime.state_lock:
                    runtime.is_running = False
                    runtime.active_run_id = None
            self.repository.update_conversation(conversation_id, status="idle", active_run_id=None)
            with self._run_tasks_lock:
                self._pending_runs.pop(conversation_id, None)
            self.emit(conversation_id, "conversation.unloaded", {})
        finally:
            if not removed:
                with runtime.state_lock:
                    runtime.closing = False

    def _save_pending_notifications(self, runtime: AgentRuntime) -> None:
        """Keep undelivered background events for the next load."""
        events = runtime.scheduler.take_pending() if runtime.scheduler else []
        drain = getattr(runtime.agent, "drain_background_events", None)
        if drain:
            events += drain()
        if events:
            self.repository.add_pending_notifications(runtime.conversation_id, events)

    @staticmethod
    def _close_agent_resources(agent: Agent) -> None:
        child_agents = getattr(getattr(agent, "subagent_tool", None), "child_agents", None)
        for child in child_agents() if child_agents else []:
            GatewayManager._close_agent_resources(child)
        try:
            browser = getattr(agent, "browser_manager", None)
            if browser is not None and bool(getattr(browser, "is_open", False)):
                browser.close()
        except Exception:
            log.exception("Failed to close browser manager")
        try:
            terminal_manager = getattr(agent, "terminal_manager", None)
            terminals = getattr(terminal_manager, "terminals", {})
            close_terminal = getattr(terminal_manager, "close_terminal", None)
            if close_terminal is not None:
                for terminal_id in list(terminals):
                    close_terminal(terminal_id)
        except Exception:
            log.exception("Failed to close terminal resources")

    def _persist_runtime(self, runtime: AgentRuntime) -> None:
        with runtime.state_lock:
            history = copy.deepcopy(runtime.agent.get_history())
            active_run_id = runtime.active_run_id
        self.repository.replace_messages(runtime.conversation_id, history, active_run_id)

    def _reserve_control(self, runtime: AgentRuntime) -> None:
        """Reserve an exclusive control operation for one runtime.

        Run submission does not wait on the runtime lock because queued runs
        are intentionally allowed. Control operations must therefore reserve
        the runtime *before* awaiting that lock; otherwise a run could be
        inserted while compaction or continuation is waiting.
        """
        with runtime.state_lock:
            if runtime.closing:
                raise RuntimeError("Conversation runtime is shutting down.")
            if runtime.control_busy:
                raise RuntimeError("Conversation runtime is busy.")
            with self._run_tasks_lock:
                if runtime.is_running:
                    raise ValueError("Conversation already has an active run.")
                if self._pending_runs.get(runtime.conversation_id):
                    raise ValueError("Conversation already has an active or queued run.")
                runtime.control_busy = True

    @staticmethod
    def _apply_model_settings(agent: Agent, settings: dict[str, Any]) -> None:
        # Injected test Agents may not implement model selection.
        if not settings or not hasattr(agent, "set_model"):
            return
        agent.set_model(settings.get("setup"), settings.get("model"), settings.get("provider"))
        if settings.get("effort"):
            agent.set_effort(settings["effort"])

    def list_models(self) -> dict[str, Any]:
        return {"setups": providers.list_setups(), "default_setup": providers.default_setup()}

    async def configure_conversation(
        self,
        conversation_id: str,
        setup: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """Choose the conversation's model and/or effort; with no changes,
        return the current model info. The choice is persisted and applied to
        a loaded runtime immediately (it takes effect at the next API step)."""
        settings = self.repository.get_model_settings(conversation_id)
        changed = bool(setup or model or effort)
        if setup or model:
            # A new model starts from its own default effort.
            settings = {"setup": setup, "model": model, "provider": provider, "effort": None}
        if effort:
            settings["effort"] = effort
        spec = providers.resolve(settings.get("setup"), model=settings.get("model"), provider=settings.get("provider"))
        if settings.get("effort"):
            providers.validate_effort(spec, settings["effort"])
        if changed:
            self.repository.set_model_settings(conversation_id, settings)
        runtime = self.get_runtime(conversation_id)
        if runtime is not None and hasattr(runtime.agent, "model_info"):
            with runtime.state_lock:
                if changed:
                    self._apply_model_settings(runtime.agent, settings)
                return runtime.agent.model_info()
        return spec.info(settings.get("effort") or spec.default_effort)

    def visible_messages(self, conversation_id: str, include_internal: bool = False, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return [m.to_dict(include_internal=include_internal) for m in self.repository.list_messages(conversation_id, include_internal, limit, offset)]

    def emit(self, conversation_id: str, event: str, data: dict[str, Any], run_id: str | None = None, persist: bool = True) -> dict[str, Any]:
        payload = None
        # ``persist=False`` is used for streamed token deltas: storing each one
        # cost a SQLite transaction (~14 ms) on the Agent worker thread, and
        # ``assistant.stream_ended`` still persists the full text for replay.
        if persist:
            try:
                payload = self.repository.append_event(conversation_id, event, data, run_id)
            except Exception:
                # Event persistence must not turn a successful Agent operation
                # into a failed run. The event is still delivered to live clients
                # when possible, but the failure is logged for diagnosis.
                log.exception("Could not persist gateway event %s", event)
        if payload is None:
            payload = {"event_id": None, "event": event, "conversation_id": conversation_id, "run_id": run_id, "created_at": None, "data": data}
        with self._subscribers_lock:
            subscribers = list(self._subscribers.get(conversation_id, {}).values())
        for loop, callback in subscribers:
            try:
                future = asyncio.run_coroutine_threadsafe(callback(payload), loop)
                future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            except Exception:
                log.debug("Subscriber delivery failed", exc_info=True)
        return payload

    def subscribe(self, conversation_id: str, subscriber_id: str, loop: asyncio.AbstractEventLoop, callback: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        with self._subscribers_lock:
            self._subscribers.setdefault(conversation_id, {})[subscriber_id] = (loop, callback)

    def unsubscribe(self, conversation_id: str, subscriber_id: str) -> None:
        with self._subscribers_lock:
            subscribers = self._subscribers.get(conversation_id)
            if subscribers:
                subscribers.pop(subscriber_id, None)
                if not subscribers:
                    self._subscribers.pop(conversation_id, None)

    def events_after(self, conversation_id: str, after_event_id: int = 0) -> list[dict[str, Any]]:
        return self.repository.list_events(conversation_id, after_event_id)

    def _enqueue_run(
        self,
        runtime: AgentRuntime,
        content: str,
        image_url: str | None,
        source: str,
        client_id: str | None,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None,
        allow_control: bool = False,
        origin: str = "user",
    ) -> Run:
        """Persist and schedule one run without yielding to the event loop.

        The lifecycle check, queue insertion, task creation, and task registry
        insertion share the run-task lock. This prevents shutdown from taking
        its snapshot between queueing a run and registering its asyncio task.
        """
        with runtime.state_lock:
            if runtime.closing:
                raise RuntimeError("Conversation runtime is shutting down.")
            if runtime.control_busy and not allow_control:
                raise RuntimeError("Conversation runtime is busy.")
            with self._run_tasks_lock:
                if self._closed:
                    raise RuntimeError("Gateway is shutting down.")
                run = self.repository.create_run(
                    runtime.conversation_id,
                    content,
                    image_url,
                    source,
                    client_id,
                    origin,
                )
                pending = self._pending_runs.setdefault(
                    runtime.conversation_id, deque()
                )
                pending.append(run.id)
                active_id = runtime.active_run_id or pending[0]
                conversation_status = "running" if runtime.is_running else "queued"
                task = asyncio.create_task(
                    self._run_message(runtime, run, content, image_url, on_event)
                )
                self._run_tasks[run.id] = task

        try:
            self.repository.update_conversation(
                runtime.conversation_id,
                status=conversation_status,
                active_run_id=active_id,
            )
            self.emit(
                runtime.conversation_id,
                "run.queued",
                {"run": run.to_dict(), "origin": run.origin},
                run.id,
            )
        except Exception:
            # The durable run and asyncio task already exist. Keep the worker
            # alive if metadata/event publication has a transient failure.
            log.exception("Could not publish queued run %s", run.id)
        task.add_done_callback(
            lambda done, run_id=run.id: self._forget_task(run_id, done)
        )
        return run

    async def submit_message(
        self,
        conversation_id: str,
        content: str,
        image_url: str | None = None,
        source: str = "api",
        client_id: str | None = None,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> Run:
        if not self.repository.get_conversation(conversation_id):
            raise KeyError(f"Conversation not found: {conversation_id}")
        runtime = await self.load_runtime(conversation_id)
        if runtime.scheduler:
            runtime.scheduler.user_message_sent()
        return self._enqueue_run(
            runtime, content, image_url, source, client_id, on_event
        )

    def _forget_task(self, run_id: str, task: asyncio.Task) -> None:
        with self._run_tasks_lock:
            self._run_tasks.pop(run_id, None)
        if not task.cancelled():
            try:
                task.exception()
            except Exception:
                pass

    async def _run_message(
        self,
        runtime: AgentRuntime,
        run: Run,
        content: str,
        image_url: str | None,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        async with runtime.lock:
            # Check closing and publish the active state while holding the same
            # state lock used by unload_runtime. This closes the small window in
            # which unload could mark a runtime closing after this task checked
            # the flag but before it became visible as running.
            with runtime.state_lock:
                closing = runtime.closing
                if not closing:
                    runtime.is_running = True
                    runtime.active_run_id = run.id
                    runtime.last_activity = time.time()
            if closing:
                # A forced unload may be waiting for this queued task to reach
                # the lock. Cancel it at the boundary rather than starting new
                # work while resources are being torn down.
                cancelled = self.repository.update_run(
                    run.id,
                    "cancelled",
                    "Runtime unloaded before the run started.",
                    expected_status="queued",
                )
                with self._run_tasks_lock:
                    pending = self._pending_runs.get(runtime.conversation_id)
                    if pending and run.id in pending:
                        pending.remove(run.id)
                    if pending is not None and not pending:
                        self._pending_runs.pop(runtime.conversation_id, None)
                    next_id = pending[0] if pending else None
                current = self.repository.get_conversation(runtime.conversation_id)
                if current and current.active_run_id == run.id:
                    self.repository.update_conversation(
                        runtime.conversation_id,
                        status="queued" if next_id else "idle",
                        active_run_id=next_id,
                    )
                if cancelled:
                    self.emit(
                        runtime.conversation_id,
                        "run.cancelled",
                        {"run": cancelled.to_dict(), "origin": run.origin},
                        run.id,
                    )
                return

            with self._run_tasks_lock:
                pending = self._pending_runs.get(runtime.conversation_id)
                if pending and run.id in pending:
                    pending.remove(run.id)
                if pending is not None and not pending:
                    self._pending_runs.pop(runtime.conversation_id, None)
                next_id = pending[0] if pending else None
            started = self.repository.update_run(run.id, "running", expected_status="queued")
            if not started:
                with runtime.state_lock:
                    runtime.is_running = False
                    runtime.active_run_id = None
                return
            self.repository.update_conversation(runtime.conversation_id, status="running", active_run_id=run.id)
            self.emit(runtime.conversation_id, "run.started", {"run": started.to_dict(), "origin": run.origin}, run.id)
            loop = asyncio.get_running_loop()

            def send_event(event: str, data: dict[str, Any], persist: bool = True) -> None:
                payload = self.emit(runtime.conversation_id, event, data, run.id, persist)
                # The manager-level subscriber registry handles live fan-out.
                # Keep this callback for compatibility with direct callers, but
                # protect the Agent worker from disconnected client failures.
                if on_event:
                    try:
                        future = asyncio.run_coroutine_threadsafe(on_event(payload), loop)
                        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                    except Exception:
                        log.debug("Direct event observer failed", exc_info=True)

            def token_data(tokens: Any) -> dict[str, Any]:
                return status_payload(tokens)

            def configure_callbacks() -> None:
                agent = runtime.agent
                # Use setattr/getattr rather than requiring every injectable
                # test Agent to predeclare the full production callback set.
                setattr(agent, "on_stream_start", lambda: send_event("assistant.stream_started", {}))
                setattr(agent, "on_stream_token", lambda token: send_event("assistant.token", {"delta": token}, persist=False))
                setattr(agent, "on_stream_end", lambda text, has_tools: send_event("assistant.stream_ended", {"content": text, "has_tool_calls": has_tools}))
                setattr(agent, "on_tool_call", lambda name, args: send_event("tool.started", {"name": name, "summary": Agent._tool_summary(name, args)}))
                setattr(agent, "on_tool_result", lambda name, result, raw: send_event("tool.finished", {"name": name, "success": bool(result.get("success")), "error": result.get("error")}))
                setattr(agent, "on_token_update", lambda tokens: send_event("token.updated", token_data(tokens)))
                setattr(agent, "on_compact", lambda message: send_event("conversation.compacted", {"message": message}))

            configure_callbacks()
            # Notifications that arrived while this run was queued ride along
            # with it (after its input) instead of starting a separate wake.
            pending = runtime.scheduler.take_pending() if runtime.scheduler else []
            if pending:
                runtime.agent.add_background_events(pending)
            try:
                response = await loop.run_in_executor(
                    self._executor,
                    lambda: runtime.agent.run(content, image_url=image_url),
                )
                with runtime.state_lock:
                    history = copy.deepcopy(runtime.agent.get_history())
                self.repository.replace_messages(runtime.conversation_id, history, run.id)
                # A stop requested before the run started returns "[Stopped — ...]".
                final_status = "interrupted" if response == "[Interrupted]" or (response or "").startswith("[Stopped") else "completed"
                self.repository.update_run(run.id, final_status, expected_status="running")
                current = self.repository.get_conversation(runtime.conversation_id)
                if current and current.active_run_id == run.id:
                    with self._run_tasks_lock:
                        pending = self._pending_runs.get(runtime.conversation_id)
                        next_id = pending[0] if pending else None
                    self.repository.update_conversation(
                        runtime.conversation_id,
                        status="queued" if next_id else "idle",
                        active_run_id=next_id,
                    )
                send_event("run.interrupted" if final_status == "interrupted" else "run.completed", {"response": response, "run": self.repository.get_run(run.id).to_dict(), "origin": run.origin})
            except Exception as exc:
                log.exception("Run %s failed", run.id)
                self.repository.update_run(run.id, "failed", str(exc), expected_status="running")
                current = self.repository.get_conversation(runtime.conversation_id)
                if current and current.active_run_id == run.id:
                    with self._run_tasks_lock:
                        pending = self._pending_runs.get(runtime.conversation_id)
                        next_id = pending[0] if pending else None
                    self.repository.update_conversation(
                        runtime.conversation_id,
                        status="queued" if next_id else "idle",
                        active_run_id=next_id,
                    )
                send_event("run.failed", {"error": str(exc), "run": self.repository.get_run(run.id).to_dict(), "origin": run.origin})
            finally:
                with runtime.state_lock:
                    runtime.is_running = False
                    runtime.active_run_id = None
                    runtime.last_activity = time.time()
                # Do not retain callbacks to a disconnected client.
                for callback_name in (
                    "on_stream_start",
                    "on_stream_token",
                    "on_stream_end",
                    "on_tool_call",
                    "on_tool_result",
                    "on_token_update",
                    "on_compact",
                ):
                    if hasattr(runtime.agent, callback_name):
                        setattr(runtime.agent, callback_name, None)

    async def interrupt_run(self, conversation_id: str, run_id: str | None = None) -> bool:
        runtime = self.get_runtime(conversation_id)
        if not runtime:
            return False
        with runtime.state_lock:
            is_running = runtime.is_running
            active_run_id = runtime.active_run_id
        if not is_running:
            return False
        if run_id and active_run_id != run_id:
            return False
        runtime.agent.interrupt()
        return True

    async def cancel_run(self, run_id: str) -> bool:
        run = self.repository.get_run(run_id)
        if not run:
            raise KeyError(f"Run not found: {run_id}")
        if run.status == "queued":
            cancelled = self.repository.update_run(run_id, "cancelled", expected_status="queued")
            if not cancelled:
                return False
            with self._run_tasks_lock:
                task = self._run_tasks.get(run_id)
                pending = self._pending_runs.get(run.conversation_id)
                if pending and run_id in pending:
                    pending.remove(run_id)
                if pending is not None and not pending:
                    self._pending_runs.pop(run.conversation_id, None)
                next_id = pending[0] if pending else None
            if task and not task.done():
                task.cancel()
            current = self.repository.get_conversation(run.conversation_id)
            if current and current.active_run_id == run_id:
                self.repository.update_conversation(
                    run.conversation_id,
                    status="queued" if next_id else "idle",
                    active_run_id=next_id,
                )
            self.emit(run.conversation_id, "run.cancelled", {"run": cancelled.to_dict(), "origin": run.origin}, run_id)
            return True
        if run.status == "running":
            return await self.interrupt_run(run.conversation_id, run_id)
        return False

    async def compact(self, conversation_id: str) -> str:
        runtime = await self.load_runtime(conversation_id)
        self._reserve_control(runtime)
        try:
            async with runtime.lock:
                with runtime.state_lock:
                    if runtime.closing:
                        raise RuntimeError("Conversation runtime is shutting down.")
                result = await asyncio.get_running_loop().run_in_executor(
                    self._executor, runtime.agent.compact
                )
                self._persist_runtime(runtime)
                self.emit(conversation_id, "conversation.compacted", {"message": result})
                return result
        finally:
            with runtime.state_lock:
                runtime.control_busy = False

    async def continue_conversation(
        self, conversation_id: str, instruction: str = "Continue where you left off.", source: str = "api", client_id: str | None = None,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> Run:
        if not self.repository.get_conversation(conversation_id):
            raise KeyError(f"Conversation not found: {conversation_id}")
        runtime = await self.load_runtime(conversation_id)
        self._reserve_control(runtime)
        if runtime.scheduler:
            runtime.scheduler.user_message_sent()
        try:
            # Reload the durable raw history rather than using the already-
            # sanitized runtime history. Normal runtime loading intentionally
            # discards a dirty suffix; explicit continuation must still be able
            # to repair that suffix after a gateway restart.
            history = self.repository.load_history(conversation_id)
            if not history:
                with runtime.state_lock:
                    history = copy.deepcopy(runtime.agent.get_history())
            sanitized, _needs_continue = sanitize_for_resume(
                history or [], prefer_incomplete=True
            )
            if sanitized is None:
                raise ValueError("Conversation has no resumable history.")
            async with runtime.lock:
                with runtime.state_lock:
                    if runtime.closing:
                        raise RuntimeError("Conversation runtime is shutting down.")
                    runtime.agent.conversation_history = sanitized
                return self._enqueue_run(
                    runtime,
                    instruction,
                    None,
                    source,
                    client_id,
                    on_event,
                    allow_control=True,
                )
        finally:
            with runtime.state_lock:
                runtime.control_busy = False

    async def cleanup_idle(self, max_idle_seconds: int | None = None) -> None:
        limit = Config.KAIROS_RUNTIME_IDLE_SECONDS() if max_idle_seconds is None else max_idle_seconds
        while not self._closed:
            await asyncio.sleep(60)
            if limit <= 0:
                continue
            now = time.time()
            with self._runtimes_lock:
                candidates = [
                    sid
                    for sid, runtime in self._runtimes.items()
                    if not self._runtime_is_running(runtime)
                    and now - runtime.last_activity > limit
                    # Unloading would kill running sub-agents/background commands.
                    and not self._has_background_work(runtime)
                ]
            for sid in candidates:
                try:
                    await self.unload_runtime(sid)
                except Exception:
                    log.exception("Failed to unload idle runtime %s", sid)

    async def shutdown(self) -> None:
        # Close the admission gate before taking the task snapshot. Submission
        # checks this flag while holding _run_tasks_lock, so no new run can be
        # inserted after shutdown begins.
        with self._run_tasks_lock:
            if self._closed:
                return
            self._closed = True

        # Do not cancel an active _run_message task while its Agent is running
        # in the executor: cancelling the asyncio wrapper does not stop the
        # underlying worker thread, and unloading resources at that point would
        # race the still-running Agent. Queued tasks can be cancelled safely;
        # active Agents are interrupted and then awaited to a stable boundary.
        with self._run_tasks_lock:
            run_ids = list(self._run_tasks)
        for run_id in run_ids:
            run = self.repository.get_run(run_id)
            if not run:
                continue
            try:
                if run.status == "queued":
                    await self.cancel_run(run_id)
                elif run.status == "running":
                    runtime = self.get_runtime(run.conversation_id)
                    if runtime:
                        runtime.agent.interrupt()
            except Exception:
                log.exception("Failed to stop run %s during shutdown", run_id)

        while True:
            with self._run_tasks_lock:
                tasks = list(self._run_tasks.items())
            if not tasks:
                break
            # Active runs return [Interrupted] after observing the event;
            # queued tasks were cancelled above. Re-snapshot after each wait so
            # a task created just before the shutdown lock was acquired cannot
            # escape resource teardown.
            await asyncio.gather(
                *(task for _run_id, task in tasks), return_exceptions=True
            )
            # A completed asyncio Task's done callbacks run on the next event
            # loop turn. If gather receives only already-completed tasks it may
            # return without yielding, so remove completed entries explicitly
            # instead of spinning forever waiting for _forget_task callbacks.
            with self._run_tasks_lock:
                for run_id, task in tasks:
                    if task.done():
                        self._run_tasks.pop(run_id, None)

        with self._lifecycle_lock, self._runtimes_lock:
            ids = list(self._runtimes)
        for sid in ids:
            try:
                await self.unload_runtime(sid, force=True)
            except Exception:
                log.exception("Failed to unload %s", sid)
        self._executor.shutdown(wait=True, cancel_futures=True)
