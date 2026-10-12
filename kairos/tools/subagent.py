import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import ToolResult


@dataclass
class _SubAgent:
    id: str
    prompt: str
    agent: Any = None
    # running | idle (finished, context kept) | failed | terminated | lost
    state: str = "running"
    current_tool: Optional[str] = None
    # Last tool-call summaries, oldest first: progressing vs. stuck at a glance.
    recent_actions: deque = field(default_factory=lambda: deque(maxlen=3))
    started_at: float = field(default_factory=time.monotonic)
    finished_at: Optional[float] = None
    # The thread of the latest run. After terminate it may still be finishing
    # (a tool call in flight); the sub-agent is "stopping" until it exits.
    thread: Optional[threading.Thread] = None


class SubAgentTool:
    """Background sub-agents: autonomous copies of the parent agent that share
    its current model, effort and workspace but have their own context.

    Every sub-agent runs on its own thread; the tools return immediately.
    When a run ends, ``on_event`` receives a background event
    (``subagent_finished`` / ``subagent_failed``) that wakes the parent.
    ``on_status`` receives a status dict on spawn, each tool call, finish,
    failure, termination and loss.
    """

    MAX_RUNNING = 5
    MAX_RESULT_CHARS = 12_000
    PROMPT_PREVIEW_CHARS = 100
    STOP_WAIT_SECONDS = 2.0  # message() waits this long for a stopping run

    def __init__(
        self,
        workspace: str,
        model_config: Callable[[], Tuple[Any, Optional[str]]],
        on_event: Callable[[dict], None],
        on_status: Callable[[dict], None],
    ):
        self.workspace = workspace
        # Returns the parent's current (ModelSpec, effort), read at spawn time
        # so a model switch on the parent applies to later sub-agents.
        self.model_config = model_config
        self._on_event = on_event
        self._on_status = on_status
        self._subagents: Dict[str, _SubAgent] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    #  Tools (called from Agent._execute_tool)                             #
    # ------------------------------------------------------------------ #

    def spawn(self, prompt: str) -> ToolResult:
        with self._lock:
            if self._running_count() >= self.MAX_RUNNING:
                return ToolResult(False, "", self._cap_error())
            sub = _SubAgent(id=uuid.uuid4().hex[:12], prompt=prompt)
            sub.agent = self._create_sub_agent(sub)
            self._subagents[sub.id] = sub
        self._start(sub, prompt)
        return ToolResult(True, (
            f"Sub-agent {sub.id} started in the background. End your turn or keep "
            "working on something else; you will be woken with its result when it "
            "finishes. Do not poll or wait for it."
        ))

    def message(self, subagent_id: str, message: str) -> ToolResult:
        with self._lock:
            sub = self._subagents.get(subagent_id)
            thread = sub.thread if sub is not None and sub.state != "running" else None
        if thread is not None:
            # A terminated run may still be finishing; give it a moment.
            thread.join(self.STOP_WAIT_SECONDS)
        with self._lock:
            if sub is None:
                return ToolResult(False, "", f"Sub-agent {subagent_id} not found.")
            if sub.state == "running":
                return ToolResult(False, "", (
                    f"Sub-agent {subagent_id} is still running. You will be woken "
                    "when it finishes; message it after that."
                ))
            if sub.thread is not None and sub.thread.is_alive():
                # Starting now would run two threads on one Agent: the old run
                # would report as the reply and the new one would be lost.
                return ToolResult(False, "", (
                    f"Sub-agent {subagent_id} is still stopping (its terminated run "
                    "has not finished its current step). Message it again in a moment."
                ))
            if self._running_count() >= self.MAX_RUNNING:
                return ToolResult(False, "", self._cap_error())
            sub.state = "running"  # reserve the slot before releasing the lock
        self._start(sub, message)
        return ToolResult(True, (
            f"Sub-agent {subagent_id} is working on your message in the background "
            "with its previous context. You will be woken with its reply."
        ))

    def terminate(self, subagent_id: str) -> ToolResult:
        with self._lock:
            sub = self._subagents.get(subagent_id)
            if sub is None:
                return ToolResult(False, "", f"Sub-agent {subagent_id} not found.")
            if sub.state != "running":
                return ToolResult(False, "", f"Sub-agent {subagent_id} is not running (state: {sub.state}).")
            self._end(sub, "terminated")
        sub.agent.interrupt()
        self._emit_status(sub)
        return ToolResult(True, (
            f"Sub-agent {subagent_id} terminated. Its context is kept; "
            "message_subagent continues it."
        ))

    def list(self) -> ToolResult:
        statuses = self.statuses()
        if not statuses:
            return ToolResult(True, "No sub-agents.")
        lines = []
        for s in statuses:
            line = f"{s['subagent_id']}  {s['state']}  {s['elapsed_s']}s"
            if s.get("current_tool"):
                line += f"  tool: {s['current_tool']}"
            line += f"  task: {s['prompt']}"
            if s["recent_actions"]:
                line += "  recent: " + " | ".join(s["recent_actions"])
            lines.append(line)
        return ToolResult(True, "\n".join(lines))

    # ------------------------------------------------------------------ #
    #  Host API                                                            #
    # ------------------------------------------------------------------ #

    def statuses(self) -> List[dict]:
        with self._lock:
            subs = list(self._subagents.values())
        return [self._status(sub) for sub in subs]

    def running_count(self) -> int:
        with self._lock:
            return self._running_count()

    def child_agents(self) -> List[Any]:
        with self._lock:
            return [sub.agent for sub in self._subagents.values()]

    def stop_all(self) -> int:
        """Terminate every running sub-agent (the user's /stop)."""
        return self._end_running("terminated")

    def mark_lost(self) -> int:
        """The host is going away: stop running sub-agents, mark them lost and
        queue a notification so the parent's next run learns about it."""
        return self._end_running("lost")

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    def _running_count(self) -> int:
        return sum(1 for sub in self._subagents.values() if sub.state == "running")

    def _cap_error(self) -> str:
        return (
            f"{self.MAX_RUNNING} sub-agents are already running, the maximum. "
            "Wait for one to finish (you will be woken) or terminate one."
        )

    def _create_sub_agent(self, sub: _SubAgent):
        """Create an Agent with the same LLM config but without sub-agent tools."""
        from kairos.agent import Agent  # local import to avoid circular deps

        agent = Agent(self.workspace)
        spec, effort = self.model_config()
        agent._use_spec(spec, effort)
        agent._is_subagent = True
        agent.subagent_tool = None
        # Rebuild the prompt now that it is a sub-agent: it describes only
        # the tools it has (no browser, no sub-agents).
        agent._setup_system_prompt()

        def on_tool_call(name: str, args: dict) -> None:
            summary = Agent._tool_summary(name, args)
            with self._lock:
                sub.current_tool = summary
                sub.recent_actions.append(summary)
            self._emit_status(sub)

        agent.on_tool_call = on_tool_call
        return agent

    def _start(self, sub: _SubAgent, prompt: str) -> None:
        thread = threading.Thread(
            target=self._run, args=(sub, prompt), daemon=True, name=f"subagent-{sub.id}"
        )
        with self._lock:
            sub.state = "running"
            sub.current_tool = None
            sub.started_at = time.monotonic()
            sub.finished_at = None
            sub.thread = thread
        # A terminate that raced the end of the previous run can leave the
        # stop flag set; it would make this run return at once. The previous
        # thread has exited (message() checks), so clearing it is safe.
        sub.agent._interrupt_event.clear()
        self._emit_status(sub)
        thread.start()

    def _run(self, sub: _SubAgent, prompt: str) -> None:
        result, error = None, None
        try:
            result = sub.agent.run(prompt)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        with self._lock:
            if sub.state != "running":
                return  # terminated or lost meanwhile; already reported
            self._end(sub, "failed" if error else "idle")
        self._emit_status(sub)
        elapsed = sub.finished_at - sub.started_at
        if error:
            text = (
                f"Sub-agent {sub.id} failed after {elapsed:.1f}s.\n"
                f"Task: {self._preview(sub.prompt)}\nError: {error}"
            )
            self._on_event(self._event(sub, "subagent_failed", text))
            return
        result = result or ""
        if len(result) > self.MAX_RESULT_CHARS:
            result = result[: self.MAX_RESULT_CHARS] + "\n... [result truncated]"
        text = (
            f"Sub-agent {sub.id} finished in {elapsed:.1f}s.\n"
            f"Task: {self._preview(sub.prompt)}\nResult:\n{result}\n"
            f"(Its context is kept: message_subagent(subagent_id=\"{sub.id}\", ...) continues it.)"
        )
        self._on_event(self._event(sub, "subagent_finished", text))

    def _end(self, sub: _SubAgent, state: str) -> None:
        """Record a final state; the caller holds the lock."""
        sub.state = state
        sub.current_tool = None
        sub.finished_at = time.monotonic()

    def _end_running(self, state: str) -> int:
        with self._lock:
            subs = [sub for sub in self._subagents.values() if sub.state == "running"]
            for sub in subs:
                self._end(sub, state)
        for sub in subs:
            sub.agent.interrupt()
            self._emit_status(sub)
            if state == "lost":
                self._on_event(self._event(sub, "subagent_failed", (
                    f"Sub-agent {sub.id} was lost: Kairos shut down while it was running.\n"
                    f"Task: {self._preview(sub.prompt)}\n"
                    "Its work may be incomplete; check the workspace before continuing."
                )))
        return len(subs)

    def _preview(self, text: str) -> str:
        text = " ".join(text.split())
        if len(text) <= self.PROMPT_PREVIEW_CHARS:
            return text
        return text[: self.PROMPT_PREVIEW_CHARS - 3] + "..."

    @staticmethod
    def _event(sub: _SubAgent, kind: str, text: str) -> dict:
        return {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "subagent_id": sub.id,
            "text": text,
            "created_at": time.time(),
        }

    def _status(self, sub: _SubAgent) -> dict:
        """Snapshot of one sub-agent; never called with the lock held."""
        with self._lock:
            state, current_tool = sub.state, sub.current_tool
            started_at, finished_at = sub.started_at, sub.finished_at
            recent_actions = list(sub.recent_actions)
        end = finished_at if finished_at is not None else time.monotonic()
        status = {
            "subagent_id": sub.id,
            "state": state,
            "elapsed_s": round(end - started_at, 1),
            "prompt": self._preview(sub.prompt),
            "recent_actions": recent_actions,
        }
        if current_tool:
            status["current_tool"] = current_tool
        tokens = getattr(sub.agent, "tokens", None)
        if tokens is not None:
            status["tokens"] = {
                "session_input": tokens.session_input,
                "session_output": tokens.session_output,
                "context_pct": round(tokens.context_pct, 2),
                "turn_input": tokens.turn_input,
                "turn_output": tokens.turn_output,
            }
        return status

    def _emit_status(self, sub: _SubAgent) -> None:
        try:
            self._on_status(self._status(sub))
        except Exception:
            pass  # a display callback must never break a sub-agent
