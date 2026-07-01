import uuid
import threading
from typing import Any, Dict, Optional

from .base import ToolResult


class SubAgentTool:
    """Manages spawning and tracking of subagents — autonomous copies of the
    parent agent that share the same LLM client and workspace but run in
    their own conversation context.

    Two tool-style methods are exposed:

    * ``spawn_subagent(prompt, mode)``  – create and optionally run a subagent.
    * ``get_subagent_result(subagent_id)`` – poll a non-blocking subagent.
    """

    def __init__(
        self,
        workspace: str,
        client: Any,
        model: str,
        interrupt_event: Any = None,
    ):
        self.workspace = workspace
        self.client = client
        self.model = model
        self._interrupt_event = interrupt_event

        # Registry: subagent_id -> {"status", "result", "thread"}
        self._subagents: Dict[str, Dict[str, Any]] = {}

        # Optional callbacks (set externally, e.g. from main.py)
        # _tool_printer: Callable[[str], None] — prints a tool-call summary line
        self._tool_printer: Any = None
        # Streaming callbacks forwarded to subagents so their text is visible
        self._stream_start: Any = None   # Callable[[], None]
        self._stream_token: Any = None   # Callable[[str], None]
        self._stream_end: Any = None     # Callable[[str, bool], None]

    # ------------------------------------------------------------------ #
    #  Public helpers (called from Agent._execute_tool)                     #
    # ------------------------------------------------------------------ #

    def spawn(self, prompt: str, mode: str = "blocking") -> ToolResult:
        """Spawn a subagent and run it.

        ``mode``:
          * ``"blocking"``   – block until the subagent finishes.
          * ``"non-blocking"`` – start in background; poll with
            ``get_subagent_result``.
        """
        sub_id = self._generate_id()

        # Build a fresh Agent that has every tool *except* the sub-agent tool.
        sub_agent = self._create_sub_agent()
        # Streaming callbacks are already wired by _create_sub_agent()
        # (tool calls, streaming text, etc. are forwarded to the parent's display)

        if mode == "blocking":
            result = self._run_blocking(sub_agent, prompt)
            return result
        elif mode == "non-blocking":
            self._run_non_blocking(sub_id, sub_agent, prompt)
            return ToolResult(
                success=True,
                output=f"Subagent {sub_id} spawned in background. Use get_subagent_result(subagent_id=\"{sub_id}\") to check its status.",
            )
        else:
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid mode: {mode}. Use 'blocking' or 'non-blocking'.",
            )

    def get_result(self, subagent_id: str) -> ToolResult:
        """Return the result of a (possibly still-running) non-blocking subagent."""
        if subagent_id not in self._subagents:
            return ToolResult(
                success=False,
                output="",
                error=f"Subagent {subagent_id} not found.",
            )

        info = self._subagents[subagent_id]

        if info["status"] == "running":
            return ToolResult(success=True, output="Running…")

        # Done or error — clean up thread ref and return stored result
        self._subagents.pop(subagent_id, None)
        return info["result"]

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _generate_id() -> str:
        return uuid.uuid4().hex[:12]

    def _create_sub_agent(self):
        """Create an Agent with the same LLM config but without the sub-agent tool."""
        from kairos.agent import Agent  # local import to avoid circular deps

        agent = Agent(self.workspace)
        # Re-use parent's OpenAI client and model so keys / base-url are shared
        agent.client = self.client
        agent.model = self.model
        # Subagents cannot spawn further subagents
        agent._is_subagent = True
        agent.subagent_tool = None  # prevent recursive spawning
        # Share parent's interrupt event so user can stop subagents too
        if self._interrupt_event:
            agent._interrupt_event = self._interrupt_event
            agent.terminal_manager._interrupt_event = self._interrupt_event

        # Wire tool-call logging so the user can see what the subagent is doing
        if self._tool_printer:
            printer = self._tool_printer

            def _subagent_on_tool_call(name: str, args: dict) -> None:
                from kairos.agent import Agent as _Agent  # noqa: F811
                summary = _Agent._tool_summary(name, args)
                printer(summary)

            agent.on_tool_call = _subagent_on_tool_call

        # Wire streaming callbacks so the subagent's thinking text is visible
        if self._stream_start:
            agent.on_stream_start = self._stream_start
        if self._stream_token:
            agent.on_stream_token = self._stream_token
        if self._stream_end:
            agent.on_stream_end = self._stream_end

        return agent

    def _run_blocking(self, sub_agent, prompt: str) -> ToolResult:
        try:
            result_text = sub_agent.run(prompt)
            return ToolResult(
                success=True,
                output=f"Subagent completed:\n{result_text}",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Subagent failed: {e}",
            )

    def _run_non_blocking(
        self, sub_id: str, sub_agent, prompt: str
    ) -> None:
        self._subagents[sub_id] = {
            "status": "running",
            "result": None,
            "thread": None,
        }

        def _target():
            try:
                result_text = sub_agent.run(prompt)
                self._subagents[sub_id] = {
                    "status": "done",
                    "result": ToolResult(
                        success=True,
                        output=f"Subagent completed:\n{result_text}",
                    ),
                    "thread": None,
                }
            except Exception as e:
                self._subagents[sub_id] = {
                    "status": "error",
                    "result": ToolResult(
                        success=False,
                        output="",
                        error=f"Subagent failed: {e}",
                    ),
                    "thread": None,
                }

        t = threading.Thread(target=_target, daemon=True)
        self._subagents[sub_id]["thread"] = t
        t.start()
