import uuid
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from .base import ToolResult


class SubAgentTool:
    """Manages spawning and tracking of subagents — autonomous copies of the
    parent agent that share its current model, effort and workspace but run in
    their own conversation context.

    Two tool-style methods are exposed:

    * ``spawn_subagent(prompt, mode)``  – create and optionally run a subagent.
    * ``get_subagent_result(subagent_id)`` – poll a non-blocking subagent.
    """

    def __init__(
        self,
        workspace: str,
        model_config: Callable[[], Tuple[Any, Optional[str]]],
    ):
        self.workspace = workspace
        # Returns the parent's current (ModelSpec, effort), read at spawn time
        # so a model switch on the parent applies to later sub-agents.
        self.model_config = model_config

        # Registry: subagent_id -> {"status", "result", "thread"}
        self._subagents: Dict[str, Dict[str, Any]] = {}

        # Optional callbacks (set externally, e.g. from main.py)
        # _tool_printer: Callable[[str], None] — prints a tool-call summary line
        self._tool_printer: Any = None
        # Streaming callbacks forwarded to subagents so their text is visible
        self._stream_start: Any = None   # Callable[[], None]
        self._stream_token: Any = None   # Callable[[str], None]
        self._stream_end: Any = None     # Callable[[str, bool], None]
        # Receives (subagent_id, TokenCounter) after each child turn so
        # frontends can display usage separately from the parent.
        self._token_update: Any = None   # Callable[[str, Any], None]

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
        sub_agent = self._create_sub_agent(sub_id)
        # Streaming and token callbacks are wired by _create_sub_agent()
        # (tool calls, streaming text, and token status are forwarded to the
        # parent's display).

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

    def _create_sub_agent(self, subagent_id: str):
        """Create an Agent with the same LLM config but without the sub-agent tool."""
        from kairos.agent import Agent  # local import to avoid circular deps

        agent = Agent(self.workspace)
        # Use the parent's current model setup and reasoning effort
        spec, effort = self.model_config()
        agent._use_spec(spec, effort)
        # Subagents cannot spawn further subagents
        agent._is_subagent = True
        agent.subagent_tool = None  # prevent recursive spawning

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
        if self._token_update:
            agent.on_token_update = lambda counter: self._token_update(
                subagent_id, counter
            )

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
