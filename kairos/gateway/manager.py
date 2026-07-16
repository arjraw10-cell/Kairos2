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

from ..agent import Agent
from ..config import Config
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


class GatewayManager:
    """Own persistent conversations and transient Agent runtimes."""

    def __init__(
        self,
        repository: GatewayRepository | None = None,
        default_workspace: str | None = None,
        max_concurrent_runs: int | None = None,
        agent_factory: AgentFactory | None = None,
    ):
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
        value = workspace or self.default_workspace
        if not value:
            raise ValueError("No workspace specified. Provide an absolute workspace path.")
        # Workspace paths are context, not a security boundary.  Do not apply
        # an allowlist or containment check here.
        return str(Path(value).expanduser().resolve())

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
        return AgentRuntime(conversation.id, conversation.workspace_path, agent)

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
                self._persist_runtime(runtime)
                self._close_agent_resources(runtime.agent)
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

    @staticmethod
    def _close_agent_resources(agent: Agent) -> None:
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

    def visible_messages(self, conversation_id: str, include_internal: bool = False, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return [m.to_dict(include_internal=include_internal) for m in self.repository.list_messages(conversation_id, include_internal, limit, offset)]

    def emit(self, conversation_id: str, event: str, data: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
        try:
            payload = self.repository.append_event(conversation_id, event, data, run_id)
        except Exception:
            # Event persistence must not turn a successful Agent operation
            # into a failed run. The event is still delivered to live clients
            # when possible, but the failure is logged for diagnosis.
            log.exception("Could not persist gateway event %s", event)
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
                {"run": run.to_dict()},
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
                        {"run": cancelled.to_dict()},
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
            self.emit(runtime.conversation_id, "run.started", {"run": started.to_dict()}, run.id)
            loop = asyncio.get_running_loop()

            def send_event(event: str, data: dict[str, Any]) -> None:
                payload = self.emit(runtime.conversation_id, event, data, run.id)
                # The manager-level subscriber registry handles live fan-out.
                # Keep this callback for compatibility with direct callers, but
                # protect the Agent worker from disconnected client failures.
                if on_event:
                    try:
                        future = asyncio.run_coroutine_threadsafe(on_event(payload), loop)
                        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                    except Exception:
                        log.debug("Direct event observer failed", exc_info=True)

            def configure_callbacks() -> None:
                agent = runtime.agent
                # Use setattr/getattr rather than requiring every injectable
                # test Agent to predeclare the full production callback set.
                setattr(agent, "on_stream_start", lambda: send_event("assistant.stream_started", {}))
                setattr(agent, "on_stream_token", lambda token: send_event("assistant.token", {"delta": token}))
                setattr(agent, "on_stream_end", lambda text, has_tools: send_event("assistant.stream_ended", {"content": text, "has_tool_calls": has_tools}))
                setattr(agent, "on_tool_call", lambda name, args: send_event("tool.started", {"name": name, "summary": Agent._tool_summary(name, args)}))
                setattr(agent, "on_tool_result", lambda name, result, raw: send_event("tool.finished", {"name": name, "success": bool(result.get("success")), "error": result.get("error")}))
                setattr(agent, "on_token_update", lambda tokens: send_event("token.updated", {"session_input": tokens.session_input, "session_output": tokens.session_output, "context_pct": round(tokens.context_pct, 2), "turn_input": tokens.turn_input, "turn_output": tokens.turn_output}))
                setattr(agent, "on_compact", lambda message: send_event("conversation.compacted", {"message": message}))
                setattr(agent, "on_background_notification", lambda message: send_event("terminal.completed", {"message": message}))
                subagent_tool = getattr(agent, "subagent_tool", None)
                if subagent_tool:
                    subagent_tool._tool_printer = lambda summary: send_event("tool.started", {"name": "subagent", "summary": summary})
                    subagent_tool._stream_start = lambda: send_event("assistant.stream_started", {"subagent": True})
                    subagent_tool._stream_token = lambda token: send_event("assistant.token", {"delta": token, "subagent": True})
                    subagent_tool._stream_end = lambda text, has_tools: send_event("assistant.stream_ended", {"content": text, "has_tool_calls": has_tools, "subagent": True})

            configure_callbacks()
            try:
                response = await loop.run_in_executor(
                    self._executor,
                    lambda: runtime.agent.run(content, image_url=image_url),
                )
                with runtime.state_lock:
                    history = copy.deepcopy(runtime.agent.get_history())
                self.repository.replace_messages(runtime.conversation_id, history, run.id)
                final_status = "interrupted" if response == "[Interrupted]" else "completed"
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
                send_event("run.interrupted" if final_status == "interrupted" else "run.completed", {"response": response, "run": self.repository.get_run(run.id).to_dict()})
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
                send_event("run.failed", {"error": str(exc), "run": self.repository.get_run(run.id).to_dict()})
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
                    "on_background_notification",
                ):
                    if hasattr(runtime.agent, callback_name):
                        setattr(runtime.agent, callback_name, None)
                subagent_tool = getattr(runtime.agent, "subagent_tool", None)
                if subagent_tool:
                    subagent_tool._tool_printer = None
                    subagent_tool._stream_start = None
                    subagent_tool._stream_token = None
                    subagent_tool._stream_end = None

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
            self.emit(run.conversation_id, "run.cancelled", {"run": cancelled.to_dict()}, run_id)
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
