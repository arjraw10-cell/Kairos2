import json
import platform
import re
import sys
import threading
import time
from collections import deque
from datetime import date
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable, Tuple
import httpx
from openai import APIStatusError, APIConnectionError, RateLimitError, AuthenticationError

from . import providers
from .config import Config
from .terminal_manager import TerminalManager
from .browser_manager import BrowserManager
from .desktop_manager import DesktopManager
from .tokens import TokenCounter
from .tools import (
    ReadTool, WriteTool, EditTool,
    NewTerminalTool, ExecuteCommandTool, ReadLogsTool, CloseTerminalTool, GetTerminalInfoTool,
    StopCommandTool, SearchTool, GitTool, SubAgentTool, WebSearchTool,
    BrowserTool, BrowserScreenshotTool,
    DesktopDoctorTool, DesktopListWindowsTool, DesktopWindowStateTool, DesktopScreenshotTool,
    DesktopLaunchAppTool, DesktopClickTool, DesktopTypeTool, DesktopPressKeyTool,
    DesktopHotkeyTool, DesktopScrollTool, DesktopMoveTool, DesktopDragTool, DesktopWindowActionTool,
)
from .tools.base import ToolResult
from .tools.skills import SkillManager
from .resume import (
    DATE_NOTE, _is_synthetic_user_message, _repair_trailing_tool_chain, date_note, repair_tool_chains,
)
from .wake import format_notifications

# Sub-agents never get these (schema and dispatch both refuse them).
SUBAGENT_TOOL_NAMES = frozenset(
    ("spawn_subagent", "message_subagent", "terminate_subagent", "list_subagents")
)

# Optional tool groups (name prefix of their tools). They are off until
# enable_tools (or a direct call to one of their tools) turns them on, so a
# request does not carry ~33 browser/desktop definitions it rarely needs.
TOOL_GROUPS = {"browser": "browser_", "desktop": "desktop_"}


class APIRequestError(Exception):
    """A concise, user-facing error raised after an API request is exhausted."""


class Agent:
    API_MAX_RETRIES = 2  # Two retries after the initial request.
    API_RETRY_BASE_DELAY = 1.0
    MAX_TOOL_RESULT_CHARS = Config.DEFAULT_MAX_TOOL_RESULT_CHARS
    # The same tool call (name and arguments) failing this many times in a row
    # gets a warning in its result, then stops the run.
    REPEATED_FAILURE_WARN = 3
    REPEATED_FAILURE_STOP = 5

    def __init__(self, workspace: str, setup: Optional[str] = None):
        # Model access: a provider.json setup (default_setup when omitted).
        # ``spec``/``adapter``/``model``/``effort`` are replaced by set_model().
        self.spec = providers.resolve(setup)
        self.adapter = providers.create_adapter(self.spec)
        self.model = self.spec.model
        self.effort = self.spec.default_effort
        self.max_tool_result_chars = Config.MAX_TOOL_RESULT_CHARS()
        # Terminals start in the workspace (not the gateway's CWD).
        self.terminal_manager = TerminalManager(cwd=str(Path(workspace).resolve()))
        # A hard interrupt is shared by the UI thread and the worker running
        # the request.  It stays set until ``run`` reaches its cleanup so a
        # stop cannot be lost between API steps.
        self._interrupt_event = threading.Event()
        self._processing_lock = threading.Lock()
        self._is_processing = False
        self._active_stream_lock = threading.Lock()
        self._active_stream = None
        self._interrupted_pending = False
        # (call key, consecutive failures) and the message that ends a run
        # stuck on one failing call; both are reset when a run starts.
        self._repeated_failure: Tuple[Optional[str], int] = (None, 0)
        self._failure_stop_message: Optional[str] = None
        # Background events (sub-agents, terminals) owned by the Agent: those
        # that arrive during a run (injected before its next API step) and,
        # without a host callback, those waiting for the next run. Guarded by
        # _processing_lock so ownership flips atomically when a run ends.
        self._background_events: deque = deque()

        # Token counter
        self.tokens = TokenCounter(self.model, self.spec.context_window, self.spec.pricing)
        # (prompt_tokens, history length) of the last API request that
        # reported usage; auto-compaction builds on it (see
        # _refresh_context_tokens). None after compaction or reset.
        self._api_context: Optional[Tuple[int, int]] = None

        # Working directory (used by git and search defaults)
        self.cwd = Path(workspace).resolve()

        # Individual tools
        # Relative paths resolve against the workspace.
        self.read_tool = ReadTool(str(self.cwd))
        self.write_tool = WriteTool(str(self.cwd))
        self.edit_tool = EditTool(str(self.cwd))
        self.new_terminal_tool = NewTerminalTool(self.terminal_manager)
        self.execute_command_tool = ExecuteCommandTool(self.terminal_manager)
        self.read_logs_tool = ReadLogsTool(self.terminal_manager)
        self.close_terminal_tool = CloseTerminalTool(self.terminal_manager)
        self.get_terminal_info_tool = GetTerminalInfoTool(self.terminal_manager)
        self.stop_command_tool = StopCommandTool(self.terminal_manager)
        self.search_tool = SearchTool(str(self.cwd))
        self.web_search_tool = WebSearchTool()
        self.git_tool = GitTool()
        self.git_tool.set_workspace(str(self.cwd))

        # Sub-agent support (disabled for sub-agents to prevent recursion)
        self._is_subagent = False
        self.subagent_tool: Optional[SubAgentTool] = SubAgentTool(
            workspace=str(self.cwd),
            model_config=lambda: (self.spec, self.effort),
            on_event=self._push_background_event,
            on_status=self._emit_subagent_status,
        )

        # Browser tools: each forwards to one BrowserManager method
        self.browser_manager = BrowserManager()
        bm = self.browser_manager
        self.browser_tools = {
            "browser_navigate": BrowserTool(bm.navigate, "Navigation"),
            "browser_snapshot": BrowserTool(bm.snapshot, "Snapshot"),
            "browser_click": BrowserTool(bm.click, "Click"),
            "browser_type": BrowserTool(bm.type, "Type"),
            "browser_fill_form": BrowserTool(bm.fill_form, "Fill form"),
            "browser_select": BrowserTool(bm.select, "Select"),
            "browser_hover": BrowserTool(bm.hover, "Hover"),
            "browser_drag": BrowserTool(bm.drag, "Drag"),
            "browser_press_key": BrowserTool(bm.press_key, "Key press"),
            "browser_scroll": BrowserTool(bm.scroll, "Scroll"),
            "browser_wait_for": BrowserTool(bm.wait_for, "Wait"),
            "browser_evaluate": BrowserTool(bm.evaluate, "JavaScript"),
            "browser_search_page": BrowserTool(bm.search_page, "Search page"),
            "browser_tabs": BrowserTool(bm.tabs, "Tabs"),
            "browser_handle_dialog": BrowserTool(bm.handle_dialog, "Dialog"),
            "browser_console": BrowserTool(bm.console, "Console"),
            "browser_network": BrowserTool(bm.network, "Network"),
            "browser_profile": BrowserTool(bm.profile, "Profile"),
            "browser_close": BrowserTool(bm.close, "Close"),
        }
        self.browser_screenshot_tool = BrowserScreenshotTool(bm)

        # Native desktop tools backed by Cua Driver. Desktop access is also
        # intentionally available to sub-agents; each Agent owns its own Cua
        # session while the driver daemon preserves platform state.
        self.desktop_manager = DesktopManager()
        self.desktop_doctor_tool = DesktopDoctorTool(self.desktop_manager)
        self.desktop_list_windows_tool = DesktopListWindowsTool(self.desktop_manager)
        self.desktop_window_state_tool = DesktopWindowStateTool(self.desktop_manager)
        self.desktop_screenshot_tool = DesktopScreenshotTool(self.desktop_manager)
        self.desktop_launch_app_tool = DesktopLaunchAppTool(self.desktop_manager)
        self.desktop_click_tool = DesktopClickTool(self.desktop_manager)
        self.desktop_type_tool = DesktopTypeTool(self.desktop_manager)
        self.desktop_press_key_tool = DesktopPressKeyTool(self.desktop_manager)
        self.desktop_hotkey_tool = DesktopHotkeyTool(self.desktop_manager)
        self.desktop_scroll_tool = DesktopScrollTool(self.desktop_manager)
        self.desktop_move_tool = DesktopMoveTool(self.desktop_manager)
        self.desktop_drag_tool = DesktopDragTool(self.desktop_manager)
        self.desktop_window_actions = {
            action: DesktopWindowActionTool(self.desktop_manager, action)
            for action in ("activate_window", "minimize_window", "maximize_window", "close_window")
        }

        # Skills
        self.skill_manager = SkillManager(str(self.cwd / "skills"))

        # TOOL_GROUPS turned on in this conversation (see _enabled_tool_groups).
        self.enabled_tool_groups: set = set()

        # Callbacks wired from CLI
        self.on_tool_call: Optional[Callable[[str, dict], None]] = None
        self.on_stream_start: Optional[Callable[[], None]] = None
        self.on_stream_token: Optional[Callable[[str], None]] = None
        self.on_stream_end: Optional[Callable[[str, bool], None]] = None
        self.on_tool_result: Optional[Callable[[str, dict, str], None]] = None
        self.on_token_update: Optional[Callable[[TokenCounter], None]] = None
        self.on_compact: Optional[Callable[[str], None]] = None
        # Hosts set these; they fire whether or not a run is active.
        # on_background_event(event): {"id", "kind", "subagent_id"?,
        # "terminal_id"?, "text", "created_at", "in_run"?}. With in_run the
        # active run receives the event itself; otherwise the host owns its
        # delivery (WakeScheduler: wake run or the next user message).
        self.on_background_event: Optional[Callable[[dict], None]] = None
        self.on_subagent_status: Optional[Callable[[dict], None]] = None

        self.terminal_manager.set_completion_callback(self._on_terminal_event)

        self.conversation_history: List[Dict[str, Any]] = []
        self._setup_system_prompt()

    # ------------------------------------------------------------------ #
    #  Background events                                                   #
    # ------------------------------------------------------------------ #

    def _on_terminal_event(self, event: dict) -> None:
        self._push_background_event({
            "id": event["completion_id"],
            "kind": event.get("kind", "terminal_finished"),
            "terminal_id": event.get("terminal_id"),
            "text": TerminalManager.format_background_completion(event),
            "created_at": time.time(),
        })

    def _push_background_event(self, event: dict) -> None:
        """Route one event: to the active run, or to the host when idle."""
        with self._processing_lock:
            in_run = self._is_processing
            if in_run or self.on_background_event is None:
                # The active run (or, with no host, the next run) delivers it.
                self._background_events.append(event)
        callback = self.on_background_event
        if callback:
            try:
                callback({**event, "in_run": True} if in_run else event)
            except Exception:
                pass  # a host callback must never break a terminal reader or sub-agent

    def add_background_events(self, events: List[dict]) -> None:
        """Hand events to the next run (hosts: ``WakeScheduler.take_pending()``)."""
        with self._processing_lock:
            self._background_events.extend(events)

    def drain_background_events(self) -> List[dict]:
        """Remove and return the events the Agent still holds."""
        with self._processing_lock:
            events = list(self._background_events)
            self._background_events.clear()
        return events

    def has_background_work(self) -> bool:
        """True while sub-agents or background terminal commands are running."""
        if self.subagent_tool and self.subagent_tool.running_count():
            return True
        return self.terminal_manager.running_background_commands() > 0

    def stop_subagents(self) -> int:
        """Terminate every running sub-agent (the user's /stop); returns the count."""
        return self.subagent_tool.stop_all() if self.subagent_tool else 0

    def subagent_statuses(self) -> List[dict]:
        return self.subagent_tool.statuses() if self.subagent_tool else []

    def _emit_subagent_status(self, status: dict) -> None:
        if self.on_subagent_status:
            self.on_subagent_status(status)

    def _set_processing(self, value: bool) -> None:
        """Track whether a user request is actively running."""
        with self._processing_lock:
            self._is_processing = value

    @property
    def is_processing(self) -> bool:
        with self._processing_lock:
            return self._is_processing

    def _drain_background_notifications(self) -> Optional[str]:
        """Format held background events for insertion into the API turn."""
        events = self.drain_background_events()
        return format_notifications(events) if events else None

    def _finish_run_unless_background(self) -> bool:
        """End the run unless events arrived during its final step.

        Checking the queue and clearing the processing flag under one lock
        makes the hand-off to the host atomic: an event is either held here
        (and the run continues to show it to the model) or goes to the host.
        """
        with self._processing_lock:
            if self._background_events:
                return False
            self._is_processing = False
            return True

    def _end_run(self) -> None:
        """Clear the processing flag; hand events the run did not show to the
        model (it ended by error, stop or an empty response) back to the host.

        The host ignored them while they were marked ``in_run``, so without
        this they would wait for the next user message instead of waking the
        agent. Without a host callback they stay queued for the next run.
        """
        with self._processing_lock:
            self._is_processing = False
            callback = self.on_background_event
            leftovers = list(self._background_events) if callback else []
            if callback:
                self._background_events.clear()
        for event in leftovers:
            try:
                callback(event)
            except Exception:
                pass

    BROWSER_PROMPT = (
        "## Browser Tools\n"
        "The browser is a headed, humanized CloakBrowser. It launches automatically on the first browser tool call.\n"
        "It passes bot detection (Cloudflare, reCAPTCHA, fingerprinting checks): its Chromium is patched at the source and every click, keystroke and scroll is humanized. Use it on sites that block scripts or plain HTTP fetches instead of giving up.\n"
        "1. `browser_navigate` opens a URL (or back / forward / reload) and returns an accessibility snapshot.\n"
        "2. Elements in the snapshot carry refs like [ref=e12]; refs inside iframes or after the page navigates look like f1e12. Pass them as `ref` to click/type/select/hover/drag. Always use refs from the latest snapshot.\n"
        "3. Every action returns the resulting page state: on the same page only the changed snapshot lines, after navigation the full snapshot. Call `browser_snapshot` only to see the whole page again.\n"
        "4. If an action opens an alert/confirm/prompt, the result says so; answer it with `browser_handle_dialog` before anything else.\n"
        "5. `browser_search_page` finds text when a snapshot is truncated; `browser_evaluate` reads anything else.\n"
        "6. `browser_screenshot` is for visual checks only; prefer snapshots for reading and acting.\n"
        "7. Use `browser_profile use <name>` only when a task needs a login (profiles keep cookies and a fixed fingerprint, and are shared with the user's Claude Code browser, so a profile open there cannot be opened here); return with `browser_profile ephemeral`.\n\n"
    )

    DESKTOP_PROMPT = (
        "## Native Desktop Tools\n"
        "Native desktop tools use Cua Driver. Start with `desktop_doctor`, then `desktop_list_windows`, and use `desktop_window_state(pid, window_id)` before every interaction. Prefer `element_index` values from the latest accessibility snapshot for click/type/key/scroll actions. Use x/y coordinates only as a fallback when the accessibility tree is missing, degraded, stale, or the action did not land; never infer coordinates from the tree without checking the matching screenshot. Re-snapshot after every state-changing action to verify the result. Desktop state is session-scoped and available to sub-agents.\n\n"
    )

    def _setup_system_prompt(self):
        # A sub-agent has no browser and no sub-agent tools, and its final
        # message goes to the parent: its prompt describes only what it has.
        sub = self._is_subagent
        # Terminals use cmd.exe on Windows (blocking: shell=True; background:
        # cmd /Q /k); flash models otherwise write bash there.
        if sys.platform == "win32":
            shell = (
                "terminals run cmd.exe, not bash. Use cmd syntax (`dir`, `type`, `copy`, `del`, `set NAME=value`, `%NAME%`, `cd /d`), "
                "not `ls`, `cat`, `rm`, `export` or `$NAME`; for PowerShell, run `powershell -NoProfile -Command \"...\"`."
            )
        else:
            shell = "background terminals run bash; blocking terminals run /bin/sh."
        base = (
            "You are Kairos, a coding agent. You operate in a filesystem and can read, write, and edit files, execute terminal commands, search codebases, "
            + ("and inspect version control.\n\n" if sub else "inspect version control, and browse the web.\n\n") +
            "You think step-by-step. Before making changes, you read the relevant files to understand the current state. After making changes, you verify they work. When something fails, you read the error carefully and adjust.\n\n"
            "You have full access to the filesystem. Relative file paths resolve against your workspace (below), and terminals start there; use absolute paths (e.g., C:/Users/me/project/main.py or /home/me/project/main.py) to reach anywhere else. You are not sandboxed \u2014 you can read any file you have permission to, and write to any location you have permission to.\n\n"
            "Each tool either succeeds and returns output, or fails and returns an error message. When a tool fails, the error tells you exactly what went wrong \u2014 use that information to fix your approach. Never retry the exact same call that just failed without changing something.\n\n"
            "Whenever the user asks you to look at a project, it usually has an AGENTS.md file and a README.md file. You should use these files to understand the project and the codebase, and ALWAYS follow the instructions mentioned in the AGENTS.md files. Make sure to look for this file in any projects the user points you towards. The AGENTS.md will automatically be injected into your system prompt in the directory the user starts in, but if they point you towards a different directory, it will not automatically be injected, so you will have to look for the AGENTS.md file in that directory. Note that the AGENTS.md does not always exist.\n\n"
            + ("When you finish your task, your final message (that has no tool calls) is returned as your result to the agent that started you.\n\n" if sub else
               "When you finish a task, the final message (that has no tool calls) will be showm to user as your message to them.\n\n") +
            "## Terminal Tools\n"
            "Use a blocking terminal for short commands whose result you need immediately. Its execute_command call waits up to `timeout` seconds (default and maximum 20). Use a background terminal for servers, watchers, builds, or other long-running commands; timeout is ignored for background terminals. Background commands return immediately and the persistent shell stays alive. The full output remains available through read_logs.\n\n"
            "## Background Work\n"
            + ("Background terminal commands report back with a `[Background notification]` message when a command finishes (or its `until` pattern matches), delivered before your next step while you are still working. Your final message ends your task; notifications that arrive after it reach you only if you are messaged again, so get the background results you need (read_logs has the output) before finishing.\n\n" if sub else
               "Background terminal commands and sub-agents report back with a `[Background notification]` message: when a command finishes (or its `until` pattern matches) and when a sub-agent finishes or fails. If your turn has already ended, you are woken with the notification. Never poll, sleep or wait for background work: start it, tell the user what is running, and end your turn or keep working on something else.\n\n") +
            f"## Tool Result Limits\nText returned by tools is capped before it is added to conversation history: up to {self.max_tool_result_chars:,} characters per tool result. Oversized results contain a truncation marker and preserve both the beginning and end. Use narrower commands or queries; for complete background-terminal output, use read_logs.\n\n"
            "## Web Search\n"
            + ("Use `web_search` for quick facts and current events. You have no browser: if web_search is unavailable, say so in your result.\n\n" if sub else
               "Use `web_search` for quick facts and current events. Use the browser for pages that need interaction, or when web_search is unavailable.\n\n") +
            "## Optional Tools\n"
            + ("Desktop tools (control native desktop apps through their accessibility tree and screenshots) are off until you call `enable_tools` with group \"desktop\"; its result explains how to use them.\n\n" if sub else
               "Two tool groups are off until you call `enable_tools`, whose result explains how to use them: \"browser\" (a stealth, headed web browser for pages that need interaction or logins, and sites that block scripts) and \"desktop\" (control native desktop apps through their accessibility tree and screenshots).\n\n") +
            "## Environment\n"
            f"- OS: {platform.system()} {platform.release()}\n"
            f"- Shell: {shell}\n"
            "- Terminals start in your workspace (below).\n"
            "- A user message ends with [Current date: YYYY-MM-DD (weekday)] when the date has changed since you last saw it.\n\n"
            f"## Workspace\nYour current workspace is: {self.cwd}"
        )

        # Auto-load AGENTS.md from the workspace root if present
        agents_md = ""
        agents_md_path = self.cwd / "AGENTS.md"
        try:
            if agents_md_path.is_file():
                agents_md = agents_md_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

        if agents_md:
            base += (
                "\n\n## AGENTS.md (Architecture Documentation)\n"
                "Below is the AGENTS.md file from your workspace root. This is your complete reference for "
                "the codebase you are working on. It contains the instructions for any agents working in that directory, including you. "
                "It may contain instructions (which should be followed) information about the project, and conventions for the project. "
                f"{agents_md}"
            )

        # Auto-inject available skill names
        skill_names = self.skill_manager._discover_skills()
        if skill_names:
            base += (
                "\n\n## Skills\n"
                "Available skills: " + ", ".join(skill_names) + "\n"
                "Use load_skill(skill_name) to read a skill's full content when needed.\n"
                "Use write_skill(skill_name, content) to create a new skill, "
                "or write_skill(skill_name, content, overwrite=true) to update an existing one."
            )
        else:
            base += (
                "\n\n## Skills\n"
                "No skills available yet. Use write_skill(skill_name, content) to create one."
            )

        self.system_prompt = base
        self.conversation_history = [{"role": "system", "content": self.system_prompt}]

    def _get_tool_schema(self, all_groups: bool = False) -> List[Dict[str, Any]]:
        """Function tools for the next request: TOOL_GROUPS that are off are
        left out unless ``all_groups``."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": (
                        "Read a file (absolute, or relative to the workspace). Supports UTF-8 text files "
                        "(SVG is read as text) and PNG, JPEG, GIF and WebP images up to about 3.9 MB, "
                        "which are returned as vision data for visual analysis."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path (absolute, or relative to the workspace)"}
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write",
                    "description": "Create or overwrite a file at the given absolute path.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path (absolute, or relative to the workspace)"},
                            "content": {"type": "string", "description": "File content"},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit",
                    "description": "Strict find-and-replace on a file. oldText must appear exactly ONCE.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path (absolute, or relative to the workspace)"},
                            "oldText": {"type": "string", "description": "Exact text to find (must appear exactly once)"},
                            "newText": {"type": "string", "description": "Replacement text"},
                        },
                        "required": ["path", "oldText", "newText"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search file contents using regular expressions, like ripgrep.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "description": "Regex pattern to match against file contents"},
                            "path": {"type": "string", "description": "Directory to search in (defaults to the workspace)"},
                            "include": {"type": "string", "description": "Filename glob filter"},
                            "max_results": {"type": "integer", "description": "Max matches to return (default 50)"},
                            "timeout": {"type": "number", "description": "Maximum search time in seconds (default 10; non-negative)"},
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web (Tavily). Returns a short answer when available, then title, URL and snippet per result.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"},
                            "max_results": {"type": "integer", "description": "Number of results, 1-10 (default 5)"},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "git",
                    "description": "Run git commands in the workspace. Sub-commands: status, diff, log, commit, branch.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Git sub-command: status, diff, log, commit, branch"},
                            "path": {"type": "string", "description": "File path for diff (optional)"},
                            "count": {"type": "integer", "description": "Number of log entries (default 10)"},
                            "message": {"type": "string", "description": "Commit message (required for commit)"},
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "new_terminal",
                    "description": "Create a new terminal session. Returns a terminal ID.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "background": {"type": "boolean", "description": "True=persistent shell, False=one-shot"},
                        },
                        "required": ["background"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "execute_command",
                    "description": (
                        "Execute a shell command in a terminal. In a background terminal the call returns "
                        "immediately and you will be woken with a notification when the command finishes, "
                        "or when a line of its output matches `until`; end your turn or keep working instead "
                        "of polling."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {"type": "integer", "description": "Terminal ID from new_terminal"},
                            "command": {"type": "string", "description": "Shell command to execute"},
                            "timeout": {"type": "number", "description": "Blocking terminals: seconds to wait, default and maximum 20. Ignored for background terminals."},
                            "until": {"type": "string", "description": "Background terminals only: a regular expression. The first output line that matches notifies you once (with that line and recent output) while the command keeps running, e.g. 'Listening on' for a dev server."},
                        },
                        # No oneOf/if-then here: with it, GLM-5.3-flash on OpenRouter's
                        # Wafer host (schema-constrained decoding) produced only
                        # {"is_background": false, "is_background": false, ...} for 110k
                        # characters, in every call. The terminal ID already says whether it
                        # is a background terminal, so is_background is not asked for either
                        # (an is_background argument from older histories is still checked).
                        "required": ["terminal_id", "command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_logs",
                    "description": "Read a background terminal's output. With no range, returns the last 100 lines; tail sets the number of last lines; start_line/end_line select a 1-indexed inclusive range. Output starts with a [lines A-B of N] header.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {"type": "integer", "description": "Background terminal ID"},
                            "tail": {"type": "integer", "description": "Return the last N lines (do not combine with start_line/end_line)"},
                            "start_line": {"type": "integer", "description": "First line to read (1-indexed)"},
                            "end_line": {"type": "integer", "description": "Last line to read, inclusive (default: the last line)"},
                        },
                        "required": ["terminal_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "stop_command",
                    "description": "Stop the command running in a background terminal (kills its process tree) without closing the terminal. You then get a notification that it was stopped.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {"type": "integer", "description": "Background terminal ID"},
                        },
                        "required": ["terminal_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "close_terminal",
                    "description": "Close a terminal and release its resources.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {"type": "integer", "description": "Terminal ID to close"},
                        },
                        "required": ["terminal_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_terminal_info",
                    "description": "Get info about a terminal: ID, background/blocking, closed status, line count.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {"type": "integer", "description": "Terminal ID"},
                        },
                        "required": ["terminal_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "spawn_subagent",
                    "description": (
                        "Start a sub-agent on a task. It runs in the background with its own context and your "
                        "tools (except browser and sub-agent tools), and this call returns its ID immediately. "
                        "Then end your turn or keep working on something else: you will be woken with its result "
                        "when it finishes. Do not poll or wait for it. Sub-agents share your workspace, so give "
                        "parallel sub-agents separate files or tasks. At most 5 run at once."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "Complete, self-contained instructions; the sub-agent sees nothing of this conversation"},
                        },
                        "required": ["prompt"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "message_subagent",
                    "description": (
                        "Send a follow-up message to a sub-agent that has finished; it continues with its previous "
                        "context, in the background, and you will be woken with its reply. Refused while it is "
                        "still running."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "subagent_id": {"type": "string", "description": "ID returned by spawn_subagent"},
                            "message": {"type": "string", "description": "The follow-up instruction"},
                        },
                        "required": ["subagent_id", "message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "terminate_subagent",
                    "description": "Stop a running sub-agent. Its context is kept, so message_subagent can continue it later.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "subagent_id": {"type": "string", "description": "ID returned by spawn_subagent"},
                        },
                        "required": ["subagent_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_subagents",
                    "description": "List sub-agents: ID, state (running, idle, failed, terminated, lost), current tool, elapsed time, task and their last 3 tool calls (to tell progress from a stuck sub-agent). Use it to check on them, not to wait: results arrive as notifications.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            # ---- Browser Tools ----
            {"type": "function", "function": {
                "name": "browser_navigate",
                "description": "Open a URL in the current tab (launches the browser if needed), or pass back / forward / reload. Returns the page snapshot with [ref=eN] handles.",
                "parameters": {"type": "object", "properties": {
                    "url": {"type": "string", "description": "Absolute URL, or back | forward | reload"},
                }, "required": ["url"]},
            }},
            {"type": "function", "function": {
                "name": "browser_snapshot",
                "description": "Full accessibility snapshot of the current page with [ref=eN] handles. Actions already return page state; use this to see the whole page again.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }},
            {"type": "function", "function": {
                "name": "browser_click",
                "description": "Click an element by ref. Pass x/y instead of ref to click viewport coordinates (canvas, captchas, elements missing from the snapshot).",
                "parameters": {"type": "object", "properties": {
                    "ref": {"type": "string", "description": "Ref from the latest snapshot, e.g. e12 or f1e12"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "double": {"type": "boolean"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                }, "required": []},
            }},
            {"type": "function", "function": {
                "name": "browser_type",
                "description": "Replace the content of a text field by typing like a human. submit presses Enter afterwards.",
                "parameters": {"type": "object", "properties": {
                    "ref": {"type": "string"},
                    "text": {"type": "string"},
                    "submit": {"type": "boolean"},
                }, "required": ["ref", "text"]},
            }},
            {"type": "function", "function": {
                "name": "browser_fill_form",
                "description": "Fill several fields in one call. checkbox/radio take \"true\" or \"false\"; combobox takes an option label or value.",
                "parameters": {"type": "object", "properties": {
                    "fields": {"type": "array", "items": {"type": "object", "properties": {
                        "ref": {"type": "string"},
                        "type": {"type": "string", "enum": ["textbox", "checkbox", "radio", "combobox"]},
                        "value": {"type": "string"},
                    }, "required": ["ref", "type", "value"]}},
                }, "required": ["fields"]},
            }},
            {"type": "function", "function": {
                "name": "browser_select",
                "description": "Select option(s) in a <select> by label or value.",
                "parameters": {"type": "object", "properties": {
                    "ref": {"type": "string"},
                    "values": {"type": "array", "items": {"type": "string"}},
                }, "required": ["ref", "values"]},
            }},
            {"type": "function", "function": {
                "name": "browser_hover",
                "description": "Move the mouse over an element (menus, tooltips).",
                "parameters": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]},
            }},
            {"type": "function", "function": {
                "name": "browser_drag",
                "description": "Drag one element onto another (from_ref, to_ref), or between viewport coordinates (x1, y1, x2, y2).",
                "parameters": {"type": "object", "properties": {
                    "from_ref": {"type": "string"}, "to_ref": {"type": "string"},
                    "x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"}, "y2": {"type": "number"},
                }, "required": []},
            }},
            {"type": "function", "function": {
                "name": "browser_press_key",
                "description": "Press a key or combination on the focused element, e.g. Enter, Escape, Tab, Control+A.",
                "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
            }},
            {"type": "function", "function": {
                "name": "browser_scroll",
                "description": "Scroll by viewport heights. With ref, scroll the area under that element (inner scroll panes).",
                "parameters": {"type": "object", "properties": {
                    "direction": {"type": "string", "enum": ["down", "up"]},
                    "pages": {"type": "number", "description": "Viewport heights, default 1"},
                    "ref": {"type": "string"},
                }, "required": []},
            }},
            {"type": "function", "function": {
                "name": "browser_wait_for",
                "description": "Wait for text to appear, text to disappear, or a number of seconds (max 60).",
                "parameters": {"type": "object", "properties": {
                    "text": {"type": "string"}, "text_gone": {"type": "string"}, "seconds": {"type": "number"},
                    "timeout": {"type": "number", "description": "Seconds to wait for text, default 30"},
                }, "required": []},
            }},
            {"type": "function", "function": {
                "name": "browser_screenshot",
                "description": "Screenshot of the viewport, the full page, or one element (ref), returned as an image. For visual checks; prefer snapshots for reading and acting.",
                "parameters": {"type": "object", "properties": {
                    "full_page": {"type": "boolean"}, "ref": {"type": "string"},
                }, "required": []},
            }},
            {"type": "function", "function": {
                "name": "browser_evaluate",
                "description": "Run JavaScript in the page and return the JSON result: an expression (document.title) or a function (() => ...). With ref, pass a function that receives the element (el => el.value).",
                "parameters": {"type": "object", "properties": {
                    "expression": {"type": "string"}, "ref": {"type": "string"},
                }, "required": ["expression"]},
            }},
            {"type": "function", "function": {
                "name": "browser_search_page",
                "description": "Find text in the page's visible text and return matches with context. Useful when a snapshot is truncated.",
                "parameters": {"type": "object", "properties": {
                    "pattern": {"type": "string"},
                    "regex": {"type": "boolean"},
                    "case_sensitive": {"type": "boolean"},
                    "max_results": {"type": "integer"},
                }, "required": ["pattern"]},
            }},
            {"type": "function", "function": {
                "name": "browser_tabs",
                "description": "List, open, select or close tabs. index is 0-based; close without index closes the current tab.",
                "parameters": {"type": "object", "properties": {
                    "action": {"type": "string", "enum": ["list", "new", "select", "close"]},
                    "index": {"type": "integer"},
                    "url": {"type": "string", "description": "URL to open with action=new"},
                }, "required": ["action"]},
            }},
            {"type": "function", "function": {
                "name": "browser_handle_dialog",
                "description": "Accept or dismiss the open alert/confirm/prompt dialog.",
                "parameters": {"type": "object", "properties": {
                    "accept": {"type": "boolean"}, "prompt_text": {"type": "string"},
                }, "required": ["accept"]},
            }},
            {"type": "function", "function": {
                "name": "browser_console",
                "description": "Console messages and uncaught errors of the current page (since it loaded). Without index: a list. With index: one message in full.",
                "parameters": {"type": "object", "properties": {
                    "index": {"type": "integer"}, "errors_only": {"type": "boolean"}, "max_results": {"type": "integer"},
                }, "required": []},
            }},
            {"type": "function", "function": {
                "name": "browser_network",
                "description": "Network requests for the current tab. Without index: a list (filter = URL substring). With index: one request with headers.",
                "parameters": {"type": "object", "properties": {
                    "index": {"type": "integer"}, "filter": {"type": "string"}, "max_results": {"type": "integer"},
                }, "required": []},
            }},
            {"type": "function", "function": {
                "name": "browser_profile",
                "description": "Persistent profiles keep logins, cookies and a fixed fingerprint, shared with Claude Code's cloakbrowser MCP (one browser per profile at a time). list shows them; use relaunches the browser in a profile (created if new); ephemeral relaunches with a throwaway session. proxy applies to the relaunch.",
                "parameters": {"type": "object", "properties": {
                    "action": {"type": "string", "enum": ["list", "use", "ephemeral"]},
                    "name": {"type": "string"}, "proxy": {"type": "string"},
                }, "required": ["action"]},
            }},
            {"type": "function", "function": {
                "name": "browser_close",
                "description": "Close the browser. The next browser tool relaunches it.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }},
            # ---- Skill Tools ----
            # Native desktop tools (Cua Driver). Accessibility element indices
            # are the primary addressing mode; x/y coordinates are an explicit
            # fallback for surfaces without a usable accessibility tree.
            {
                "type": "function",
                "function": {
                    "name": "desktop_doctor",
                    "description": "Check Cua Driver installation, daemon, desktop session, and platform permissions.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_list_windows",
                    "description": "List native desktop windows, optionally filtered by process ID.",
                    "parameters": {"type": "object", "properties": {"pid": {"type": "integer"}}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_window_state",
                    "description": "Get a native window accessibility tree and grounding screenshot. Use before desktop actions and again to verify them.",
                    "parameters": {
                        "type": "object",
                        "properties": {"pid": {"type": "integer"}, "window_id": {"type": "integer"}, "include_screenshot": {"type": "boolean"}},
                        "required": ["pid", "window_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_screenshot",
                    "description": "Capture a native desktop window screenshot for visual grounding. The current driver returns this through get_window_state.",
                    "parameters": {"type": "object", "properties": {"pid": {"type": "integer"}, "window_id": {"type": "integer"}}, "required": ["pid", "window_id"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_launch_app",
                    "description": "Launch or connect to a native desktop application. Arguments depend on the platform, such as name, bundle_id, or args.",
                    "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "bundle_id": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_click",
                    "description": "Click a native desktop element. Prefer pid, window_id, and element_index from the latest desktop_window_state; use x/y only as a fallback.",
                    "parameters": {"type": "object", "properties": {"pid": {"type": "integer"}, "window_id": {"type": "integer"}, "element_index": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"}, "count": {"type": "integer"}, "modifier": {"type": "array", "items": {"type": "string"}}}, "required": ["pid"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_type",
                    "description": "Type text into a native desktop element. Prefer element_index from the latest desktop_window_state; use x/y only as a fallback.",
                    "parameters": {"type": "object", "properties": {"pid": {"type": "integer"}, "text": {"type": "string"}, "window_id": {"type": "integer"}, "element_index": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"}}, "required": ["pid", "text"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_press_key",
                    "description": "Press a key in a native desktop application, optionally targeting an accessibility element or pixel coordinate.",
                    "parameters": {"type": "object", "properties": {"pid": {"type": "integer"}, "key": {"type": "string"}, "window_id": {"type": "integer"}, "element_index": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"}, "modifiers": {"type": "array", "items": {"type": "string"}}}, "required": ["pid", "key"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_hotkey",
                    "description": "Send a keyboard shortcut to a native desktop application.",
                    "parameters": {"type": "object", "properties": {"pid": {"type": "integer"}, "keys": {"type": "array", "items": {"type": "string"}}, "window_id": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"}}, "required": ["pid", "keys"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_scroll",
                    "description": "Scroll a native desktop window. Prefer a fresh accessibility window state first.",
                    "parameters": {"type": "object", "properties": {"pid": {"type": "integer"}, "direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "window_id": {"type": "integer"}, "element_index": {"type": "integer"}, "amount": {"type": "integer"}}, "required": ["pid", "direction"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_move",
                    "description": "Move the Cua agent cursor to native desktop pixel coordinates. This action is pixel-only; use desktop_window_state first to ground coordinates.",
                    "parameters": {"type": "object", "properties": {"pid": {"type": "integer"}, "window_id": {"type": "integer"}, "x": {"type": "number"}, "y": {"type": "number"}}, "required": ["pid", "x", "y"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_drag",
                    "description": "Drag in a native desktop window using grounded window-local pixel coordinates. Cua Driver requires x1/y1/x2/y2; use desktop_window_state screenshot coordinates.",
                    "parameters": {"type": "object", "properties": {"pid": {"type": "integer"}, "window_id": {"type": "integer"}, "x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"}, "y2": {"type": "number"}}, "required": ["pid", "x1", "y1", "x2", "y2"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_window_action",
                    "description": "Activate a native window. The current Cua Driver supports activate_window through bring_to_front; minimize, maximize, and close are reported as unsupported until the driver exposes them.",
                    "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["activate_window", "minimize_window", "maximize_window", "close_window"]}, "pid": {"type": "integer"}, "window_id": {"type": "integer"}}, "required": ["action", "pid"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_skills",
                    "description": "List all available skills by name.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "load_skill",
                    "description": "Load a skill by name and return its full SKILL.md content.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_name": {"type": "string", "description": "Name of the skill (folder name under skills/)"},
                        },
                        "required": ["skill_name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_skill",
                    "description": "Create or update a skill's SKILL.md file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_name": {"type": "string", "description": "Name for the skill (folder name under skills/)"},
                            "content": {"type": "string", "description": "Full content for the SKILL.md file"},
                            "overwrite": {"type": "boolean", "description": "Set to true to overwrite an existing skill (default: false)"},
                        },
                        "required": ["skill_name", "content"],
                    },
                },
            },
        ]
        tools.append({
            "type": "function",
            "function": {
                "name": "enable_tools",
                "description": (
                    "Turn on an optional tool group for the rest of the session; returns how to use it. "
                    + ("desktop: native desktop apps." if self._is_subagent else
                       "browser: a headed web browser for pages that need interaction or logins. desktop: native desktop apps.")
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "group": {"type": "string", "enum": ["desktop"] if self._is_subagent else list(TOOL_GROUPS),
                                  "description": "Tool group to turn on"},
                    },
                    "required": ["group"],
                },
            },
        })

        if self._is_subagent:
            tools = [t for t in tools if t["function"]["name"] not in SUBAGENT_TOOL_NAMES
                     and not t["function"]["name"].startswith("browser_")]
        if not all_groups:
            enabled = self._enabled_tool_groups()
            tools = [t for t in tools if _tool_group(t["function"]["name"]) in (None, *enabled)]
        return tools

    def _available_tool_groups(self) -> List[str]:
        return ["desktop"] if self._is_subagent else list(TOOL_GROUPS)

    def _enabled_tool_groups(self) -> set:
        """Enabled TOOL_GROUPS: those in ``enabled_tool_groups`` plus any whose
        tools or enable_tools calls appear in history (a resumed or reloaded
        conversation) or that a compaction summary lists. Groups only turn on."""
        for message in self.conversation_history:
            role = message.get("role")
            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    group = _tool_group(function.get("name") or "")
                    if function.get("name") == "enable_tools":
                        try:
                            group = json.loads(function.get("arguments") or "{}").get("group")
                        except (ValueError, AttributeError):
                            group = None
                    if group in TOOL_GROUPS:
                        self.enabled_tool_groups.add(group)
            elif role == "user" and self._is_compaction_summary(message):
                match = re.search(r"^\[Enabled tool groups: (.*)\]$", self._get_text_content(message.get("content")), re.M)
                if match:
                    self.enabled_tool_groups.update(g for g in match.group(1).split(", ") if g in TOOL_GROUPS)
        return self.enabled_tool_groups & set(self._available_tool_groups())

    def _enable_tools(self, group: str) -> ToolResult:
        available = self._available_tool_groups()
        if group not in available:
            return ToolResult(False, "", f"Unknown tool group: {group!r}. Available: {', '.join(available)}.")
        self.enabled_tool_groups.add(group)
        guide = self.BROWSER_PROMPT if group == "browser" else self.DESKTOP_PROMPT
        return ToolResult(True, f"The {group} tools are enabled for the rest of this session.\n\n{guide.strip()}")

    TOOL_ERROR_RESERVE_CHARS = 4_000

    @staticmethod
    def _truncate_text(text: str, max_chars: int) -> str:
        """Keep both ends of oversized text while preserving Unicode safely."""
        if len(text) <= max_chars:
            return text
        marker = (
            f"\n... [tool output truncated: original length {len(text):,} characters; "
            f"showing the first and last portions] ...\n"
        )
        if max_chars <= len(marker):
            return marker[:max_chars]
        available = max_chars - len(marker)
        head_chars = (available + 1) // 2
        tail_chars = available - head_chars
        tail = text[-tail_chars:] if tail_chars else ""
        return text[:head_chars] + marker + tail

    def _cap_tool_result(
        self, result: Dict[str, Any], max_chars: Optional[int] = None
    ) -> Dict[str, Any]:
        """Cap textual result fields while never copying inline image data.

        The configured budget applies to textual output/error fields.  Image
        URLs are removed from the model-facing tool message and re-injected as
        a separate user vision message by ``step()``; counting them as text
        here was the primary source of multi-million-token context estimates.
        """
        capped = dict(result)
        configured_limit = (
            self.max_tool_result_chars if max_chars is None else max_chars
        )
        limit = max(1, int(configured_limit))
        output = capped.get("output")
        error = capped.get("error")

        if isinstance(error, str):
            error_limit = min(limit, self.TOOL_ERROR_RESERVE_CHARS)
            capped["error"] = self._truncate_text(error, error_limit)
        error_length = len(capped["error"]) if isinstance(capped.get("error"), str) else 0

        if isinstance(output, str):
            output_limit = max(0, limit - error_length)
            capped["output"] = (
                self._truncate_text(output, output_limit)
                if output_limit
                else ""
            )

        # A malformed/custom tool may return an inline image in another field;
        # preserve the normal image_url contract but do not include it in the
        # textual budget. Non-text metadata remains untouched.
        return capped

    def _execute_tool(self, name: str, args: Dict[str, Any], max_chars: Optional[int] = None) -> str:
        """Execute a tool and return a capped JSON result for the model."""
        dispatch = {
            "read": lambda a: self.read_tool(a["path"]).to_dict(),
            "write": lambda a: self.write_tool(a["path"], a["content"]).to_dict(),
            "edit": lambda a: self.edit_tool(a["path"], a["oldText"], a["newText"]).to_dict(),
            "search": lambda a: self.search_tool(
                a["pattern"],
                a.get("path"),
                a.get("include"),
                a.get("max_results", 50),
                a.get("timeout", SearchTool.DEFAULT_TIMEOUT),
            ).to_dict(),
            "web_search": lambda a: self.web_search_tool(a["query"], a.get("max_results", 5)).to_dict(),
            "git": lambda a: self.git_tool(a["command"], path=a.get("path"), count=a.get("count", 10), message=a.get("message", "")).to_dict(),
            "new_terminal": lambda a: self.new_terminal_tool(a["background"]).to_dict(),
            "execute_command": lambda a: self.execute_command_tool(a["terminal_id"], a["command"], a.get("timeout"), a.get("is_background"), a.get("until")).to_dict(),
            "read_logs": lambda a: self.read_logs_tool(a["terminal_id"], a.get("start_line"), a.get("end_line"), a.get("tail")).to_dict(),
            "stop_command": lambda a: self.stop_command_tool(a["terminal_id"]).to_dict(),
            "close_terminal": lambda a: self.close_terminal_tool(a["terminal_id"]).to_dict(),
            "get_terminal_info": lambda a: self.get_terminal_info_tool(a["terminal_id"]).to_dict(),
            "spawn_subagent": lambda a: self.subagent_tool.spawn(a["prompt"]).to_dict(),
            "message_subagent": lambda a: self.subagent_tool.message(a["subagent_id"], a["message"]).to_dict(),
            "terminate_subagent": lambda a: self.subagent_tool.terminate(a["subagent_id"]).to_dict(),
            "list_subagents": lambda a: self.subagent_tool.list().to_dict(),
            # Browser tools: schema parameter names match BrowserManager keyword arguments
            **{name: (lambda a, tool=tool: tool(**a).to_dict()) for name, tool in self.browser_tools.items()},
            "browser_screenshot": lambda a: self.browser_screenshot_tool(**a).to_dict(),
            # Native desktop tools
            "desktop_doctor": lambda a: self.desktop_doctor_tool().to_dict(),
            "desktop_list_windows": lambda a: self.desktop_list_windows_tool(a.get("pid")).to_dict(),
            "desktop_window_state": lambda a: self.desktop_window_state_tool(a["pid"], a["window_id"], a.get("include_screenshot", True)).to_dict(),
            "desktop_screenshot": lambda a: self.desktop_screenshot_tool(a["pid"], a["window_id"]).to_dict(),
            "desktop_launch_app": lambda a: self.desktop_launch_app_tool(**a).to_dict(),
            "desktop_click": lambda a: self.desktop_click_tool(**a).to_dict(),
            "desktop_type": lambda a: self.desktop_type_tool(a["pid"], a["text"], **{k: v for k, v in a.items() if k not in ("pid", "text")}).to_dict(),
            "desktop_press_key": lambda a: self.desktop_press_key_tool(a["pid"], a["key"], **{k: v for k, v in a.items() if k not in ("pid", "key")}).to_dict(),
            "desktop_hotkey": lambda a: self.desktop_hotkey_tool(a["pid"], a["keys"], **{k: v for k, v in a.items() if k not in ("pid", "keys")}).to_dict(),
            "desktop_scroll": lambda a: self.desktop_scroll_tool(a["pid"], a["direction"], **{k: v for k, v in a.items() if k not in ("pid", "direction")}).to_dict(),
            "desktop_move": lambda a: self.desktop_move_tool(a["pid"], a["x"], a["y"], **{k: v for k, v in a.items() if k not in ("pid", "x", "y")}).to_dict(),
            "desktop_drag": lambda a: self.desktop_drag_tool(a["pid"], **{k: v for k, v in a.items() if k != "pid"}).to_dict(),
            "desktop_window_action": lambda a: self.desktop_window_actions[a["action"]](a["pid"], **{k: v for k, v in a.items() if k not in ("action", "pid")}).to_dict(),
            # Skill tools
            "list_skills": lambda a: self.skill_manager.list_skills().to_dict(),
            "load_skill": lambda a: self.skill_manager.load_skill(a["skill_name"]).to_dict(),
            "write_skill": lambda a: self.skill_manager.write_skill(a["skill_name"], a["content"], overwrite=a.get("overwrite", False)).to_dict(),
            "enable_tools": lambda a: self._enable_tools(a["group"]).to_dict(),
        }

        if name not in dispatch:
            return json.dumps(
                self._cap_tool_result({
                    "success": False,
                    "output": "",
                    "error": f"Unknown tool: {name}",
                }, max_chars=max_chars),
                ensure_ascii=False,
            )
        if name in SUBAGENT_TOOL_NAMES and (self._is_subagent or self.subagent_tool is None):
            return json.dumps({
                "success": False,
                "output": "",
                "error": "Sub-agents cannot spawn, message, terminate or list sub-agents.",
            })
        # A call to a tool of a group that is off turns the group on and runs.
        group = _tool_group(name)
        if group in self._available_tool_groups():
            self.enabled_tool_groups.add(group)

        try:
            result = dispatch[name](args)
            return json.dumps(
                self._cap_tool_result(result, max_chars=max_chars),
                ensure_ascii=False,
            )
        except Exception as e:
            # Dispatch lambdas read required arguments as a["x"]; a bare "'x'"
            # gives the model nothing to fix, so name the missing argument.
            # A KeyError can also come from a bad value (an unknown action),
            # so only an absent parameter of this tool counts as missing.
            if isinstance(e, KeyError) and e.args and isinstance(args, dict) and e.args[0] not in args \
                    and e.args[0] in self._tool_parameters(name):
                error = f"Missing required argument {e} for tool {name}. Call it again with that argument."
            elif isinstance(e, KeyError):
                error = f"KeyError: {e}"
            else:
                error = str(e)
            return json.dumps(
                self._cap_tool_result(
                    {"success": False, "output": "", "error": error},
                    max_chars=max_chars,
                ),
                ensure_ascii=False,
            )

    def _tool_parameters(self, name: str) -> set:
        """Parameter names the tool's schema declares."""
        for tool in self._get_tool_schema(all_groups=True):
            if tool["function"]["name"] == name:
                return set(tool["function"].get("parameters", {}).get("properties", {}))
        return set()

    @staticmethod
    def _tool_summary(name: str, args: Any) -> str:
        """One-line summary of a tool call. Never raises: the arguments are
        whatever JSON the model sent (null values, a list instead of an
        object, ...), and hosts call this while the tool loop is running."""
        try:
            return _summarize_tool_call(name, args if isinstance(args, dict) else {})
        except Exception:
            return str(name)

    @staticmethod
    def _get_text_content(content) -> str:
        """Extract plain text from message content (string or vision content array)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "\n".join(parts)
        return ""

    def _normalize_history_for_context(self) -> int:
        """Cap legacy tool messages before they reach an API request.

        Older saved sessions may contain multi-megabyte search/read results,
        including image data accidentally embedded inside a JSON tool result.
        Current execution paths cap results before insertion, but resumed
        sessions need the same protection before compaction can be effective.
        """
        changed = 0
        for message in self.conversation_history:
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            normalized = content
            try:
                parsed = json.loads(content)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                # Image data belongs in the separate vision user message. A
                # legacy tool message containing it cannot be sent as text.
                legacy_image_url = parsed.pop("image_url", None)
                if legacy_image_url:
                    # We cannot safely reconstruct the original tool-to-image
                    # association in a saved history, but retaining a short
                    # marker makes the lost vision context explicit without
                    # putting base64 data back into the prompt.
                    parsed["output"] = (
                        f"{parsed.get('output', '')}\n[legacy image omitted; "
                        "use a fresh screenshot/read-image call if needed]"
                    )
                parsed = self._cap_tool_result(parsed)
                normalized = json.dumps(parsed, ensure_ascii=False)

            elif len(content) > self.max_tool_result_chars:
                normalized = self._truncate_text(content, self.max_tool_result_chars)
            if normalized != content:
                message["content"] = normalized
                changed += 1
        return changed

    def _validate_history_before_api(self) -> Optional[str]:
        history = self.conversation_history
        if len(history) <= 2:
            return None
        has_user = any(m.get("role") == "user" for m in history)
        if not has_user:
            return self.compact()
        # Every assistant tool call needs its result right after it, anywhere
        # in history (a failed tool loop can leave a gap before a later user
        # message); providers reject the request otherwise.
        repaired = repair_tool_chains(history)
        if repaired:
            return f"Repaired {repaired} tool-call chain(s) or orphaned tool message(s) to fix history ordering."
        return None

    COMPACT_RESERVE_TOKENS = 16384
    COMPACT_KEEP_RECENT_PCT = 0.20  # 20% of context window
    COMPACT_THRESHOLD_PCT = 80.0

    COMPACT_SYSTEM_PROMPT = (
        "You are a context summarization assistant. "
        "Your task is to read a conversation between a user and an AI coding assistant, "
        "then produce a structured summary following the exact format specified.\n\n"
        "Do NOT continue the conversation. Do NOT respond to any questions. "
        "ONLY output the structured summary."
    )

    COMPACT_SUMMARY_PROMPT = """The messages above are a conversation to summarize. Write a handoff summary that another LLM will use to continue the work. The user's messages are kept verbatim next to your summary, so do not repeat them: record what the work produced.

Use this EXACT format:

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Constraints & Preferences
- [Constraints and preferences stated or discovered, or "(none)"]

## Current State
- [Where things stand: what works, what fails, open errors]

## Next Steps
1. [Ordered list of what should happen next]

## Key Files & Context
- [File paths, function names, commands, error messages and data needed to continue, or "(none)"]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

    COMPACT_UPDATE_PROMPT = """The messages above are NEW conversation messages to incorporate into the existing handoff summary. The user's messages are kept verbatim next to the summary, so do not repeat them.

Update the existing summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, constraints and context
- UPDATE Progress: move items from "In Progress" to "Done" when completed
- UPDATE "Current State" and "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages

Use this EXACT format:

## Progress
### Done
- [x] [All completed items]

### In Progress
- [ ] [Current work]

## Key Decisions
- **[Decision]**: [Rationale]

## Constraints & Preferences
- [Preserve existing, add new]

## Current State
- [Where things stand now]

## Next Steps
1. [Updated next steps]

## Key Files & Context
- [Preserve and add important context]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

    # Compacted history (as in Codex): the real user messages of the
    # summarized part, verbatim and newest first within this budget, then
    # the summary. The budget is capped at a fifth of the context window.
    COMPACT_USER_MESSAGES_TOKENS = 20_000
    COMPACT_TOOL_RESULT_CHARS = 2_000  # per tool result sent to the summarizer
    COMPACT_SUMMARY_HEADER = (
        "[Conversation compacted — handoff summary]\n"
        "Another model began this task; below is its handoff summary. Build on this work; don't restart."
    )

    def _make_user_message(
        self, user_message: Optional[str], image_url: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Build the same user message shape used by ``step()`` for preflight."""
        if user_message is None:
            return None
        if image_url:
            return {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_message},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        return {"role": "user", "content": user_message}

    def _refresh_context_tokens(
        self, pending_user_message: Optional[Dict[str, Any]] = None
    ) -> None:
        """Refresh context, including tools and a user message about to append.

        The tiktoken estimate can be far below what the provider counts
        (screenshots, provider framing), so when the last request reported
        usage, its prompt tokens plus an estimate of the messages added since
        then are used if larger.
        """
        self._normalize_history_for_context()
        messages = self.conversation_history
        if pending_user_message is not None:
            messages = [*messages, pending_user_message]
        context = self.tokens.count_request(messages, self._get_tool_schema())
        if self._api_context is not None:
            prompt_tokens, length = self._api_context
            if length <= len(self.conversation_history):
                context = max(context, prompt_tokens + self.tokens.count_history(messages[length:]))
        self.tokens.context_tokens = context

    def _should_auto_compact(self) -> bool:
        if self.tokens.context_window == 0:
            return False
        return self.tokens.context_pct >= self.COMPACT_THRESHOLD_PCT

    def _is_safe_compact_boundary(self, index: int) -> bool:
        """Return whether ``index`` can begin the preserved API message tail."""
        history = self.conversation_history
        if index == len(history):
            return True  # Summarize the entire non-system history.
        if index <= 1 or index >= len(history):
            return False

        message = history[index]
        role = message.get("role")
        if role == "user":
            return True
        if role != "assistant":
            return False

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return True

        # An assistant tool-call message is only valid when every requested
        # call has its matching result immediately after it. Never cut in the
        # middle of that chain, or the next API request will be rejected.
        expected_ids = [tc.get("id") for tc in tool_calls]
        if not expected_ids or any(not call_id for call_id in expected_ids):
            return False
        found_ids = set()
        result_index = index + 1
        while result_index < len(history) and history[result_index].get("role") == "tool":
            tool_call_id = history[result_index].get("tool_call_id")
            if tool_call_id:
                found_ids.add(tool_call_id)
            result_index += 1
        return set(expected_ids).issubset(found_ids)

    def _find_compact_boundary(self) -> int:
        """Find a safe boundary even when the only user turn is still running.

        Previously this method could only return a later ``user`` message.
        An active request commonly has ``user -> assistant tool_calls -> tool``
        messages and no later user message, so compaction fell through with an
        empty summary range. The checkpoint inserted by :meth:`compact` is a
        user message, which means the preserved tail may safely begin at a
        complete assistant/tool chain instead.
        """
        history_length = len(self.conversation_history)
        keep_tokens = max(int(self.tokens.context_window * self.COMPACT_KEEP_RECENT_PCT), 1000)
        accumulated = 0
        candidate = 2 if history_length > 2 else 1
        for index in range(history_length - 1, 0, -1):
            message = self.conversation_history[index]
            # Use the same field-aware counter as the preflight decision so
            # the retention target cannot be reached by an estimate that
            # silently omits tool IDs, names, arguments, or vision metadata.
            accumulated += self.tokens.count_message(message)
            if accumulated >= keep_tokens:
                candidate = index
                break

        # Prefer the latest safe boundary at or before the token target. If
        # the target is inside a tool result, this backs up to its assistant
        # call. If the target is the only user message, the earliest later
        # safe boundary summarizes that request while preserving the live
        # execution context. ``len(history)`` is the last-resort valid choice
        # for an incomplete chain: summarize it rather than send orphaned
        # messages.
        for index in range(min(candidate, history_length - 1), 1, -1):
            if self._is_safe_compact_boundary(index):
                return index

        # No safe boundary exists before the target. Move forward to the
        # first complete assistant/tool chain or user message. If the history
        # ends in an incomplete chain, keeping the full chain in the summary
        # is safer than leaving orphaned tool messages in the preserved tail.
        for index in range(max(candidate + 1, 2), history_length + 1):
            if self._is_safe_compact_boundary(index):
                return index
        return history_length

    def _serialize_messages_for_summary(self, messages: list) -> str:
        """Serialize old messages with hard bounds for the summary request.

        A compaction request is itself an API request. Passing an entire large
        tool result (or an inline image URL) to the summarizer can exceed the
        provider before compaction has a chance to reduce the main history.
        """
        parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            raw_content = msg.get("content", "") or ""
            content = self._get_text_content(raw_content)
            if role == "system":
                continue
            if role == "tool":
                tool_name = msg.get("name", "tool")
                if len(content) > self.COMPACT_TOOL_RESULT_CHARS:
                    content = content[:self.COMPACT_TOOL_RESULT_CHARS] + "... [truncated]"
                parts.append(f"[Tool result from {tool_name}]: {content}")
            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    calls_str = ", ".join(tc.get("function", {}).get("name", "?") for tc in tool_calls)
                    parts.append(f"Assistant (called tools: {calls_str}): {content or '(no text)'}")
                else:
                    parts.append(f"Assistant: {content}")
            elif role == "user":
                parts.append(f"User: {content}")
        return "\n\n".join(parts)

    def _cap_compaction_prompt(self, prompt: str, previous_summary: Optional[str]) -> tuple[str, Optional[str]]:
        """Bound summarizer input while retaining room for its instructions."""
        # Reserve room for the summarizer's requested completion and leave a
        # safety margin for provider message framing/hidden system overhead.
        available = max(
            1_000,
            self.tokens.context_window - self.COMPACT_RESERVE_TOKENS - 1_024,
        )
        instruction = self.COMPACT_UPDATE_PROMPT if previous_summary else self.COMPACT_SUMMARY_PROMPT
        fixed = len(self.tokens._enc.encode(
            f"<conversation>\n\n</conversation>\n\n{instruction}"
        ))
        previous = previous_summary or ""
        previous_tokens = len(self.tokens._enc.encode(previous)) if previous else 0
        conversation_budget = max(1_000, available - fixed - previous_tokens)
        prompt_tokens = self.tokens._enc.encode(prompt)
        if len(prompt_tokens) > conversation_budget:
            # Keep the beginning and end; both often contain the user's goal
            # and the latest active tool result/error respectively.
            marker = "\n[older summary input truncated]\n"
            marker_tokens = self.tokens._enc.encode(marker)
            if len(marker_tokens) >= conversation_budget:
                prompt = self.tokens._enc.decode(prompt_tokens[:conversation_budget])
            else:
                usable = conversation_budget - len(marker_tokens)
                head = max(1, usable // 2)
                tail = max(1, usable - head)
                prompt = (
                    self.tokens._enc.decode(prompt_tokens[:head])
                    + marker
                    + self.tokens._enc.decode(prompt_tokens[-tail:])
                )
        return prompt, previous_summary

    def _generate_summary(self, messages_to_summarize: list, previous_summary: Optional[str] = None) -> str:
        prompt = self._serialize_messages_for_summary(messages_to_summarize)
        prompt, previous_summary = self._cap_compaction_prompt(prompt, previous_summary)
        if previous_summary:
            full_prompt = f"<conversation>\n{prompt}\n</conversation>\n\n<previous-summary>\n{previous_summary}\n</previous-summary>\n\n{self.COMPACT_UPDATE_PROMPT}"
        else:
            full_prompt = f"<conversation>\n{prompt}\n</conversation>\n\n{self.COMPACT_SUMMARY_PROMPT}"
        messages = [
            {"role": "system", "content": self.COMPACT_SYSTEM_PROMPT},
            {"role": "user", "content": full_prompt},
        ]

        def attempt() -> str:
            stream = self._open_stream(messages, None)
            try:
                summary = ""
                for event in stream:
                    self._check_interrupt()
                    if event[0] == "text":
                        summary += event[1]
                self._check_interrupt()  # a stream closed by interrupt() may just end
                return summary
            finally:
                self._close_stream(stream)

        # Same retry policy as a normal request; interrupt() closes the stream.
        summary = self._with_retries(attempt)
        return summary or "(compaction produced no summary)"

    @classmethod
    def _is_compaction_summary(cls, message: Dict[str, Any]) -> bool:
        """A compaction summary message (also the older "summary of prior
        history" form); the omitted-messages note is not one."""
        return message.get("role") == "user" and cls._get_text_content(
            message.get("content")).startswith("[Conversation compacted —")

    def _compacted_user_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The real user messages among ``messages``, verbatim (text only),
        newest first within the budget; the oldest that do not fit become one
        "[Conversation compacted: N earlier user messages omitted]" note."""
        omitted = 0
        real = []
        for message in messages:
            if message.get("role") != "user":
                continue
            text = self._get_text_content(message.get("content"))
            note = re.match(r"\[Conversation compacted: (\d+) earlier user message", text)
            if note:
                omitted += int(note.group(1))  # dropped by an earlier compaction
            elif text.strip() and not _is_synthetic_user_message(message):
                real.append({"role": "user", "content": text})
        budget = min(self.COMPACT_USER_MESSAGES_TOKENS, self.tokens.context_window // 5)
        kept = []
        for message in reversed(real):
            budget -= self.tokens.count_message(message)
            if budget < 0:
                break
            kept.append(message)
        omitted += len(real) - len(kept)
        kept.reverse()
        if omitted:
            plural = "s" if omitted != 1 else ""
            kept.insert(0, {"role": "user", "content": f"[Conversation compacted: {omitted} earlier user message{plural} omitted]"})
        return kept

    def compact(self) -> str:
        history = self.conversation_history
        if len(history) <= 2:
            return "Nothing to compact \u2014 conversation is still short."
        boundary = self._find_compact_boundary()
        if not self._is_safe_compact_boundary(boundary):
            return "Nothing to compact \u2014 couldn't find a safe message boundary."
        system_msg = history[0]
        to_summarize = history[1:boundary]
        if not to_summarize:
            return "Nothing to compact \u2014 all messages are recent."
        # The previous summary is sent once, as <previous-summary>; the user
        # messages kept by the previous compaction are collected again.
        summaries = [m for m in to_summarize if self._is_compaction_summary(m)]
        existing_summary = self._get_text_content(summaries[-1]["content"]) if summaries else None
        summary = self._generate_summary(
            [m for m in to_summarize if not self._is_compaction_summary(m)], existing_summary
        )
        content = f"{self.COMPACT_SUMMARY_HEADER}\n\n{summary}"
        # A reloaded history must still turn on the groups whose calls are
        # summarized away (see _enabled_tool_groups).
        groups = sorted(self._enabled_tool_groups())
        if groups:
            content += f"\n\n[Enabled tool groups: {', '.join(groups)}]"
        compaction_msg = {"role": "user", "content": content}
        recent = history[boundary:]
        self.conversation_history = (
            [system_msg] + self._compacted_user_messages(to_summarize) + [compaction_msg] + recent
        )
        self._api_context = None  # the last request's usage no longer applies
        # Recount the compacted request for display/trigger decisions, but do
        # not add it to cumulative session input: those messages were already
        # accounted for on their original turns.
        self.tokens.context_tokens = self.tokens.count_request(
            self.conversation_history,
            self._get_tool_schema(),
        )
        self.tokens.turn_input = self.tokens.context_tokens
        self.tokens.turn_output = 0
        return f"Compacted: summarized {len(to_summarize)} messages into checkpoint."

    def auto_compact_if_needed(self) -> Optional[str]:
        if not self._should_auto_compact():
            return None
        return self.compact()

    def clear_stop(self) -> None:
        """Drop a stop that arrived after the run it targeted had finished.

        Only for hosts that know no run is executing (the gateway calls it
        between runs); a stop meant for a running request must stay set.
        """
        self._interrupt_event.clear()

    def interrupt(self):
        """Request an immediate hard stop of the current request.

        The event is deliberately not cleared by the checker. Clearing it
        there allowed a stop observed in one layer to be lost before the run
        loop noticed it in another layer. The run cleanup clears it only once
        the worker has stopped, making it safe for the next prompt.
        """
        self._interrupt_event.set()
        # Closing an OpenAI stream releases a blocked iterator as well as
        # stopping further token delivery. The worker still owns history and
        # is joined by process_request before the next prompt is accepted.
        with self._active_stream_lock:
            stream = self._active_stream
        if stream is not None:
            try:
                close = getattr(stream, "close", None)
                if close:
                    close()
            except Exception:
                pass
        # A blocking terminal is cancellable independently of the Python
        # worker, so stop it without waiting for the tool to return.
        try:
            self.terminal_manager.cancel_active_commands()
        except Exception:
            pass
        try:
            self.browser_manager.cancel_active_operation()
        except Exception:
            pass
        try:
            self.desktop_manager.cancel_active_operation()
        except Exception:
            pass

    def _check_interrupt(self):
        if self._interrupt_event.is_set():
            raise InterruptedError("Interrupted by user")

    def _format_api_error(self, e: Exception) -> str:
        """Return concise API diagnostics without an exception traceback.

        Gateway/proxy failures often arrive as low-level ``httpx`` or
        ``httpcore`` exceptions.  Their ``str`` value is useful, but their
        chained traceback is not actionable for a normal CLI response, so
        only the exception type and one-line message are surfaced here.
        """
        error_name = type(e).__name__
        message = " ".join(str(e).split()) or "No additional details provided."
        lines = [f"API Error: {error_name}", f"  Message: {message}"]

        status = getattr(e, "status_code", None)
        if isinstance(e, (APIStatusError, providers.ProviderError)) and status:
            lines.insert(1, f"  Status Code: {status}")
            request_id = getattr(e, "request_id", None)
            if request_id:
                lines.insert(2, f"  Request ID:  {request_id}")
        # AuthenticationError and RateLimitError are APIStatusErrors; the
        # other adapters raise ProviderError with the same statuses.
        if isinstance(e, AuthenticationError) or status == 401:
            lines.append(f"  Hint: Check the '{self.spec.provider}' API keys in provider.json")
        elif isinstance(e, RateLimitError) or status == 429:
            lines.append("  Hint: You are being rate-limited. Wait a moment and retry.")
        elif isinstance(e, (APIConnectionError, httpx.TransportError)):
            lines.append(f"  Hint: Could not connect to {self.spec.base_url}")
            lines.append("  Check that the API server is running and reachable.")

        lines.extend([
            "  Config:",
            f"    Setup:    {self.spec.setup or '(ad-hoc)'} ({self.spec.provider}, {self.spec.format})",
            f"    Model:    {self.model}",
            f"    Base URL: {self.spec.base_url}",
        ])
        return "\n".join(lines)

    @staticmethod
    def _is_retryable_error(e: Exception) -> bool:
        if isinstance(e, (RateLimitError, APIConnectionError, httpx.TransportError)):
            return True
        if isinstance(e, APIStatusError) or (isinstance(e, providers.ProviderError) and e.status_code):
            return e.status_code in (429, 500, 502, 503, 504, 529)
        err = str(e).lower()
        return any(c in err for c in ["timeout", "connection", "network", "remoteprotocolerror", "incomplete chunked read"])

    def _retry_delay(self, attempt: int, base_delay: Optional[float] = None) -> float:
        """Return a short exponential backoff with small deterministic jitter."""
        delay = self.API_RETRY_BASE_DELAY if base_delay is None else base_delay
        return delay * (2 ** attempt) + 0.5 * attempt

    def _stream_response(self) -> Tuple[str, List[Dict[str, Any]], Optional[Dict[str, int]]]:
        """Stream a response, retrying transient connection failures twice.

        Retries cover both request creation and iteration: a gateway can close
        a chunked response after streaming has already started.  Partial data
        is discarded before retrying so content/tool calls are never mixed
        across attempts.
        """
        def close_panel() -> None:
            # The UI may hold a partial stream from the failed attempt: close
            # it rather than appending the next attempt (or leaving a live
            # partial response on screen after a stop).
            if self.on_stream_end:
                self.on_stream_end("", False)

        # Never turn a partial assistant stream into a completed assistant
        # message. Resume repair will continue the latest user request on the
        # next explicit prompt.
        return self._with_retries(self._stream_once, on_retry=close_panel, on_interrupt=close_panel)

    def _stream_once(self) -> Tuple[str, List[Dict[str, Any]], Optional[Dict[str, int]]]:
        """One streaming attempt of :meth:`_stream_response`."""
        stream = None
        try:
            stream = self._open_stream(list(self.conversation_history), self._get_tool_schema())
            if self.on_stream_start:
                self.on_stream_start()
            content = ""
            tool_calls: Dict[int, Dict[str, str]] = {}
            api_usage = None
            extras: Dict[str, Any] = {}
            # Adapters normalize every wire format into the same events
            # (see kairos/providers/base.py).
            for event in stream:
                self._check_interrupt()
                kind = event[0]
                if kind == "text":
                    content += event[1]
                    if self.on_stream_token:
                        self.on_stream_token(event[1])
                elif kind == "tool_call":
                    _, idx, call_id, name, arguments = event
                    if idx not in tool_calls:
                        tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if call_id:
                        tool_calls[idx]["id"] = call_id
                    if name:
                        tool_calls[idx]["name"] = name
                    tool_calls[idx]["arguments"] += arguments
                elif kind == "usage":
                    api_usage = {"prompt_tokens": event[1], "completion_tokens": event[2],
                                 "cached_tokens": event[3] if len(event) > 3 else 0}
                elif kind == "message_extra":
                    extras[event[1]] = event[2]
            self._check_interrupt()  # a stream closed by interrupt() may just end

            assembled = []
            for idx in sorted(tool_calls.keys()):
                tc = tool_calls[idx]
                try:
                    args = json.loads(tc["arguments"])
                except json.JSONDecodeError:
                    args = {}
                assembled.append({"id": tc["id"], "name": tc["name"], "arguments": args})
            # Provider state for the assistant message (step() attaches it).
            self._stream_extras = extras
            return content, assembled, api_usage
        finally:
            self._close_stream(stream)

    def _open_stream(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]):
        """Open a provider stream that interrupt() can close."""
        stream = self.adapter.stream(messages, tools, self.effort)
        with self._active_stream_lock:
            self._active_stream = stream
        if self._interrupt_event.is_set():  # the stop came while it was opening
            self._close_stream(stream)
            raise InterruptedError("Interrupted by user")
        return stream

    def _close_stream(self, stream) -> None:
        if stream is None:
            return
        try:
            close = getattr(stream, "close", None)
            if close:
                close()
        except Exception:
            pass
        with self._active_stream_lock:
            if self._active_stream is stream:
                self._active_stream = None

    def _with_retries(
        self,
        attempt: Callable[[], Any],
        on_retry: Optional[Callable[[], None]] = None,
        on_interrupt: Optional[Callable[[], None]] = None,
    ) -> Any:
        """Run one API request (``attempt``) with the retry policy: an initial
        try plus ``API_MAX_RETRIES`` retries with backoff for retryable errors.
        A stop raises ``InterruptedError``; exhausted or non-retryable errors
        raise ``APIRequestError`` with concise diagnostics."""
        attempts = self.API_MAX_RETRIES + 1
        last_exc = None
        for n in range(attempts):
            try:
                self._check_interrupt()
                return attempt()
            except InterruptedError:
                if on_interrupt:
                    on_interrupt()
                raise
            except Exception as e:
                if self._interrupt_event.is_set():
                    if on_interrupt:
                        on_interrupt()
                    raise InterruptedError("Interrupted by user")
                last_exc = e
                if not self._is_retryable_error(e) or n == attempts - 1:
                    break
                if on_retry:
                    on_retry()
                if self._interrupt_event.wait(self._retry_delay(n)):
                    raise InterruptedError("Interrupted by user")

        raise APIRequestError(self._format_api_error(last_exc)) from None

    def _track_repeated_failure(self, name: str, args: Any, result: Dict[str, Any]) -> Optional[str]:
        """Count identical failing calls in a row (any other call resets the
        count). Returns a warning for the model at REPEATED_FAILURE_WARN; at
        REPEATED_FAILURE_STOP it sets ``_failure_stop_message`` instead."""
        if result.get("success", True):
            self._repeated_failure = (None, 0)
            return None
        key = name + json.dumps(args, sort_keys=True, default=str)
        last_key, count = self._repeated_failure
        count = count + 1 if key == last_key else 1
        self._repeated_failure = (key, count)
        if count >= self.REPEATED_FAILURE_STOP:
            error = str(result.get("error") or "")[:300]
            self._failure_stop_message = (
                f"[Stopped: {name} failed {count} times in a row with the same arguments. "
                f"Last error: {error}]"
            )
            return None
        if count == self.REPEATED_FAILURE_WARN:
            message = (
                f"{name} failed {count} times in a row with the same arguments; "
                f"the run stops after {self.REPEATED_FAILURE_STOP - count} more identical failures."
            )
            if self.on_compact:
                self.on_compact(message)
            return (
                f"This exact call has now failed {count} times in a row. Do not repeat it: "
                "read the error, then fix the arguments or try a different approach. "
                f"After {self.REPEATED_FAILURE_STOP - count} more identical failures the run is stopped."
            )
        return None

    def step(self, user_message: Optional[str] = None, image_url: Optional[str] = None) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        # No stop check before the user message is appended: run() checks
        # just before, and a stop arriving now is caught before the request
        # with the message safely in history.
        background_notifications = self._drain_background_notifications()
        if user_message is not None:
            if image_url:
                user_msg: Dict[str, Any] = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_message},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            else:
                user_msg = {"role": "user", "content": user_message}
            self.conversation_history.append(user_msg)
        # Notifications go in their own "[Background notification]" user
        # message: after the real user message when there is one, otherwise
        # before the next API request of a tool-call loop.
        if background_notifications:
            self.conversation_history.append({"role": "user", "content": background_notifications})
        normalized_count = self._normalize_history_for_context()
        recovery = self._validate_history_before_api()
        if (normalized_count or recovery) and self.on_compact:
            details = []
            if normalized_count:
                details.append(f"normalized {normalized_count} oversized legacy tool result(s)")
            if recovery:
                details.append(recovery)
            self.on_compact("; ".join(details) + ".")
        self.tokens.start_turn(
            self.conversation_history,
            extra_tokens=self.tokens.count_tools(self._get_tool_schema()),
        )
        request_length = len(self.conversation_history)
        content, assembled_tool_calls, api_usage = self._stream_response()
        if api_usage and api_usage.get("prompt_tokens"):
            self._api_context = (api_usage["prompt_tokens"], request_length)
        if api_usage:
            self.tokens.set_turn_from_api(api_usage["prompt_tokens"], api_usage["completion_tokens"],
                                          api_usage.get("cached_tokens", 0))
        else:
            self.tokens.add_output_tokens(content)
        assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content or None}
        assistant_msg.update(getattr(self, "_stream_extras", None) or {})
        if self.on_stream_end:
            self.on_stream_end(content, bool(assembled_tool_calls))
        if assembled_tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                }
                for tc in assembled_tool_calls
            ]
            if not api_usage:
                for tc in assembled_tool_calls:
                    self.tokens.add_output_tokens(tc["name"] + json.dumps(tc["arguments"]))
        self.conversation_history.append(assistant_msg)
        if not assembled_tool_calls:
            self.tokens.finish_turn()
            if self.on_token_update:
                self.on_token_update(self.tokens)
            return content, []
        tool_image_data_urls = []
        for tc in assembled_tool_calls:
            self._check_interrupt()
            func_name = tc["name"]
            func_args = tc["arguments"]
            if self.on_tool_call:
                try:
                    self.on_tool_call(func_name, func_args)
                except Exception:
                    pass  # like on_tool_result: observers must never break tool execution
            result_json = self._execute_tool(func_name, func_args)
            result_dict = json.loads(result_json)
            warning = self._track_repeated_failure(func_name, func_args, result_dict)
            if warning:
                result_dict["warning"] = warning
                result_json = json.dumps(result_dict, ensure_ascii=False)
            if self.on_tool_result:
                try:
                    self.on_tool_result(func_name, result_dict, result_json)
                except Exception:
                    # Client observers must never break tool execution.
                    pass
            image_data_url = result_dict.pop("image_url", None)
            if image_data_url:
                tool_image_data_urls.append(image_data_url)
            text_content = json.dumps(result_dict, ensure_ascii=False)
            # Checkpoint each result before looking for another interrupt. If
            # stop arrives while a tool is running, completed tools remain in
            # history and only the unfinished suffix is repaired on resume.
            self.conversation_history.append({
                "tool_call_id": tc["id"],
                "role": "tool",
                "name": func_name,
                "content": text_content,
            })
            if self._failure_stop_message:
                break  # run() repairs the calls left without a result
            self._check_interrupt()
        if tool_image_data_urls:
            image_parts = [{"type": "text", "text": "[Screenshot captured \u2014 the image below shows the current browser page]"}]
            for img_url in tool_image_data_urls:
                image_parts.append({"type": "image_url", "image_url": {"url": img_url}})
            self.conversation_history.append({"role": "user", "content": image_parts})
        self.tokens.finish_turn()
        if self.on_token_update:
            self.on_token_update(self.tokens)
        return None, assembled_tool_calls

    def _with_date_note(self, text: str) -> str:
        """Append today's date when it differs from the last date note in
        history (or there is none). The date lives in history, not in the
        system prompt, so the prompt stays cacheable for long sessions."""
        today = date.today()
        for message in reversed(self.conversation_history):
            if message.get("role") == "user":
                match = DATE_NOTE.search(self._get_text_content(message.get("content")))
                if match:
                    if match.group(1) == today.isoformat():
                        return text
                    break
        return text + date_note(today)

    def run(self, user_message: str, image_url: Optional[str] = None) -> Optional[str]:
        self._set_processing(True)
        # A request may start after background commands completed while idle.
        # Mark it processing before draining so those queued notices are shown.
        # Do not clear a stop here: an interrupt can arrive between thread
        # creation and this function's first instruction.
        if self._interrupt_event.is_set():
            self._end_run()
            self._interrupt_event.clear()
            return "[Stopped — waiting for your input]"
        current = user_message
        _first_image = image_url
        if self._interrupted_pending:
            # Make the next prompt a continuation boundary. Repair any
            # incomplete assistant/tool suffix before appending the new user
            # message; missing tool results then cause the model to reconsider
            # and re-call the interrupted tool instead of producing an invalid
            # API message sequence.
            try:
                _repair_trailing_tool_chain(self.conversation_history)
            except Exception:
                pass
            current = (
                "The previous request was interrupted before it finished. "
                "Continue that work, taking this new instruction into account:\n\n"
                f"{user_message}"
            )
            self._interrupted_pending = False
        current = self._with_date_note(current)
        max_empty_retries = 2
        empty_retry_count = 0
        self._repeated_failure = (None, 0)
        self._failure_stop_message = None
        try:
            while True:
                # Check before every API step, not only before the initial
                # user message. A tool-call loop sets ``current`` to None
                # after its first step, so restricting this check to
                # ``current is not None`` delayed compaction until the next
                # user request.
                pending_user = self._make_user_message(current, _first_image)
                try:
                    self._check_interrupt()
                    self._refresh_context_tokens(pending_user)
                    compact_msg = self.auto_compact_if_needed()
                except Exception:
                    # A stop (or failure) before step() appended the user's
                    # message, e.g. during auto-compaction: keep the message
                    # so the next request continues it instead of losing it.
                    if pending_user is not None:
                        self.conversation_history.append(pending_user)
                    raise
                if compact_msg and self.on_compact:
                    self.on_compact(compact_msg)
                response, tool_calls = self.step(current, image_url=_first_image)
                _first_image = None
                current = None
                if self._failure_stop_message:
                    # Stuck on one failing call: end the run with a reply in
                    # history, so a resume does not continue the loop.
                    _repair_trailing_tool_chain(self.conversation_history)
                    self.conversation_history.append(
                        {"role": "assistant", "content": self._failure_stop_message})
                    return self._failure_stop_message
                word_count = len(response.split()) if response else 0
                if not tool_calls and word_count == 0:
                    # Empty response with no tool calls — retry
                    if self.conversation_history and self.conversation_history[-1].get("role") == "assistant":
                        self.conversation_history.pop()
                    if empty_retry_count < max_empty_retries:
                        empty_retry_count += 1
                        reason = "No response from API" if not response else f"Response too short ({word_count} word{'s' if word_count != 1 else ''})"
                        if self.on_compact:
                            self.on_compact(f"{reason} \u2014 retrying ({empty_retry_count}/{max_empty_retries})...")
                        continue
                    return response if response else "Agent returned without a response."
                if response:
                    if self._finish_run_unless_background():
                        return response
                    # Background events arrived during the final step: one
                    # more step shows them to the model instead of a wake.
        except InterruptedError:
            # Convert an interrupted in-memory suffix into the same valid
            # repairable shape used by /resume. This is especially important
            # when a stream was cut off before the assistant message existed.
            try:
                _repair_trailing_tool_chain(self.conversation_history)
            except Exception:
                pass
            self._interrupted_pending = True
            return "[Interrupted]"
        except Exception:
            # Any other failure (a provider error, a host callback, a bug)
            # must not leave an assistant tool call without its results: the
            # next request would be rejected and the conversation wedged.
            try:
                _repair_trailing_tool_chain(self.conversation_history)
            except Exception:
                pass
            raise
        finally:
            self._end_run()
            # This is the stable handoff point: no new prompt may reuse the
            # agent until this worker has reached here.
            self._interrupt_event.clear()

    def reset(self):
        self._interrupt_event.clear()
        self._interrupted_pending = False
        self._api_context = None
        # Notifications of the old conversation must not reach the new one.
        self.drain_background_events()
        self.enabled_tool_groups.clear()
        self._setup_system_prompt()
        self.tokens = TokenCounter(self.model, self.spec.context_window, self.spec.pricing)
        # Unconditionally: the browser worker can be alive without an open
        # window (failed launch, window closed by hand).
        try:
            self.browser_manager.close()
        except Exception:
            pass
        try:
            self.desktop_manager.reset()
        except Exception:
            pass

    def get_history(self) -> List[Dict[str, Any]]:
        return list(self.conversation_history)

    # ------------------------------------------------------------------ #
    #  Model selection                                                     #
    # ------------------------------------------------------------------ #

    def _use_spec(self, spec: "providers.ModelSpec", effort: Optional[str]) -> None:
        """Switch model access; history and token totals carry over."""
        self.spec = spec
        self.adapter = providers.create_adapter(spec)
        self.model = spec.model
        self.effort = effort
        self.tokens.context_window = spec.context_window or Config.CONTEXT_WINDOW()
        self.tokens.pricing = spec.pricing

    def set_model(self, setup: Optional[str] = None, model: Optional[str] = None, provider: Optional[str] = None) -> str:
        """Switch to a provider.json setup (the default setup when all are
        None) or to an ad-hoc ``model`` on ``provider``. Effort resets to the
        new spec's default."""
        spec = providers.resolve(setup, model=model, provider=provider)
        self._use_spec(spec, spec.default_effort)
        return f"Model: {spec.setup or spec.model} ({spec.provider}, {spec.format}), effort: {self.effort or 'default'}"

    def set_effort(self, effort: str) -> str:
        """Set the reasoning effort, validated when the spec lists efforts."""
        self.effort = providers.validate_effort(self.spec, effort)
        return f"Effort: {self.effort}"

    def model_info(self) -> Dict[str, Any]:
        return self.spec.info(self.effort)


def _tool_group(name: str) -> Optional[str]:
    """The TOOL_GROUPS group a tool belongs to (None for core tools)."""
    return next((group for group, prefix in TOOL_GROUPS.items() if name.startswith(prefix)), None)


def _summarize_tool_call(name: str, args: Dict[str, Any]) -> str:
    if name == "read": return f"read file: {args.get('path', '?')}"
    if name == "enable_tools": return f"enable {args.get('group', '?')} tools"
    if name == "write": return f"wrote file: {args.get('path', '?')}"
    if name == "edit": return f"edited file: {args.get('path', '?')}"
    if name == "search":
        timeout = args.get("timeout", SearchTool.DEFAULT_TIMEOUT)
        return f"search: '{args.get('pattern', '?')}' in {args.get('path', 'cwd')} (timeout: {timeout}s)"
    if name == "web_search": return f"web search: '{args.get('query', '?')}'"
    if name == "git":
        cmd = args.get("command", "?")
        if cmd == "commit": return f"git commit: {str(args.get('message') or '')[:40]}"
        return f"git {cmd}"
    if name == "new_terminal": return f"opened {'background' if args.get('background') else 'blocking'} terminal"
    if name == "execute_command": return f"terminal {args.get('terminal_id', '?')}: {args.get('command', '?')}"
    if name == "read_logs": return f"read terminal {args.get('terminal_id', '?')} logs"
    if name == "close_terminal": return f"closed terminal {args.get('terminal_id', '?')}"
    if name == "get_terminal_info": return f"info on terminal {args.get('terminal_id', '?')}"
    if name == "stop_command": return f"stop command in terminal {args.get('terminal_id', '?')}"
    if name == "spawn_subagent": return f"spawn subagent: {str(args.get('prompt') or '')[:40]}"
    if name == "message_subagent": return f"message subagent {args.get('subagent_id', '?')}: {str(args.get('message') or '')[:40]}"
    if name == "terminate_subagent": return f"terminate subagent {args.get('subagent_id', '?')}"
    if name == "list_subagents": return "list subagents"
    # Browser tool summaries
    if name == "desktop_doctor": return "check native desktop automation"
    if name == "desktop_list_windows": return "list native desktop windows"
    if name == "desktop_window_state": return f"inspect native window {args.get('pid', '?')}:{args.get('window_id', '?')}"
    if name == "desktop_screenshot": return f"screenshot native window {args.get('pid', '?')}:{args.get('window_id', '?')}"
    if name == "desktop_launch_app": return f"launch native app: {args.get('name') or args.get('bundle_id') or '?'}"
    if name == "desktop_click": return "click native desktop element" if "element_index" in args else "click native desktop pixel"
    if name == "desktop_type": return f"type into native desktop: {(args.get('text') or '')[:30]}"
    if name == "desktop_press_key": return f"press native desktop key: {args.get('key', '?')}"
    if name == "desktop_hotkey": return f"native desktop hotkey: {args.get('keys', '?')}"
    if name == "desktop_scroll": return f"scroll native desktop {args.get('direction', '?')}"
    if name == "desktop_move": return "move native desktop cursor"
    if name == "desktop_drag": return "drag native desktop"
    if name == "desktop_window_action": return f"{args.get('action', 'act')} native window"
    if name == "browser_navigate": return f"navigate: {args.get('url', '?')}"
    if name == "browser_snapshot": return "snapshot current page"
    if name == "browser_click":
        if args.get("ref"): return f"click {args['ref']}" + (" (double)" if args.get("double") else "")
        return f"click at ({args.get('x', '?')}, {args.get('y', '?')})"
    if name == "browser_type":
        text = str(args.get("text") or "")
        return f"type into {args.get('ref', '?')}: \"{text[:30]}{'...' if len(text) > 30 else ''}\""
    if name == "browser_fill_form": return f"fill {len(args.get('fields', []))} form field(s)"
    if name == "browser_select": return f"select {args.get('values', '?')} in {args.get('ref', '?')}"
    if name == "browser_hover": return f"hover {args.get('ref', '?')}"
    if name == "browser_drag": return f"drag {args.get('from_ref', 'xy')} -> {args.get('to_ref', 'xy')}"
    if name == "browser_press_key": return f"press key: {args.get('key', '?')}"
    if name == "browser_scroll": return f"scroll {args.get('direction', 'down')} {args.get('pages', 1)} page(s)"
    if name == "browser_wait_for":
        if args.get("text"): return f"wait for text: '{args['text']}'"
        if args.get("text_gone"): return f"wait for text to disappear: '{args['text_gone']}'"
        return f"wait {args.get('seconds', '?')}s"
    if name == "browser_screenshot": return "screenshot (" + ("full page" if args.get("full_page") else args.get("ref") or "viewport") + ")"
    if name == "browser_evaluate":
        expr = str(args.get("expression") or "")
        return f"run JS: {expr[:40]}{'...' if len(expr) > 40 else ''}"
    if name == "browser_search_page": return f"search page for: '{args.get('pattern', '?')}'"
    if name == "browser_tabs": return f"tabs: {args.get('action', 'list')}" + (f" [{args['index']}]" if args.get("index") is not None else "")
    if name == "browser_handle_dialog": return ("accept" if args.get("accept") else "dismiss") + " dialog"
    if name == "browser_console": return "console messages" + (f" [{args['index']}]" if args.get("index") is not None else "")
    if name == "browser_network": return "network requests" + (f" [{args['index']}]" if args.get("index") is not None else "")
    if name == "browser_profile": return f"browser profile: {args.get('action', '?')} {args.get('name') or ''}".rstrip()
    if name == "browser_close": return "close browser"
    # Skill tool summaries
    if name == "list_skills": return "list skills"
    if name == "load_skill": return f"load skill: {args.get('skill_name', '?')}"
    if name == "write_skill":
        skill = args.get("skill_name", "?")
        ow = " (overwrite)" if args.get("overwrite") else ""
        return f"write skill: {skill}{ow}"
    return name
