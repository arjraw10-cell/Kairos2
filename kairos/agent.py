import json
import time
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable, Tuple, Generator
from openai import OpenAI
from openai import APIStatusError, APIConnectionError, RateLimitError, AuthenticationError

from .config import Config
from .terminal_manager import TerminalManager
from .browser_manager import BrowserManager
from .tokens import TokenCounter
from .tools import (
    ReadTool, WriteTool, EditTool,
    NewTerminalTool, ExecuteCommandTool, ReadLogsTool, CloseTerminalTool, GetTerminalInfoTool,
    SearchTool, GitTool, SubAgentTool,
    BrowserLaunchTool, BrowserNavigateTool, BrowserClickTool, BrowserClickIndexTool,
    BrowserTypeTool, BrowserTypeIndexTool, BrowserSelectTool, BrowserSelectIndexTool,
    BrowserSnapshotTool, BrowserScreenshotTool, BrowserScrollTool, BrowserWaitTool,
    BrowserSendKeysTool, BrowserSearchPageTool, BrowserFindElementsTool,
    BrowserTabListTool, BrowserTabSwitchTool, BrowserTabOpenTool, BrowserEvaluateTool,
    BrowserGoBackTool, BrowserGoForwardTool, BrowserReloadTool, BrowserCloseTool,
    BrowserClickXYTool, BrowserSwitchFrameTool,
    BrowserHoverTool, BrowserHoverIndexTool, BrowserDragTool, BrowserDragXYTool, BrowserWaitForTool,
)
from .tools.skills import SkillManager


class APIRequestError(Exception):
    """A concise, user-facing error raised after an API request is exhausted."""


class Agent:
    MAX_HISTORY_MESSAGES = 10000000
    API_MAX_RETRIES = 2  # Two retries after the initial request.
    API_RETRY_BASE_DELAY = 1.0
    MAX_TOOL_RESULT_CHARS = Config.DEFAULT_MAX_TOOL_RESULT_CHARS

    def __init__(self, workspace: str):
        self.client = OpenAI(
            api_key=Config.OPENAI_API_KEY(),
            base_url=Config.OPENAI_BASE_URL(),
        )
        self.model = Config.OPENAI_MODEL()
        self.max_tool_result_chars = Config.MAX_TOOL_RESULT_CHARS()
        self.terminal_manager = TerminalManager()
        self._interrupt_event = threading.Event()
        self._stop_requested = False
        self._processing_lock = threading.Lock()
        self._is_processing = False
        self._background_ui_lock = threading.Lock()
        self._background_ui_notified_ids = set()

        # Token counter
        self.tokens = TokenCounter(self.model)

        # Working directory (used by git and search defaults)
        self.cwd = Path(workspace).resolve()

        # Individual tools
        self.read_tool = ReadTool()
        self.write_tool = WriteTool()
        self.edit_tool = EditTool()
        self.new_terminal_tool = NewTerminalTool(self.terminal_manager)
        self.execute_command_tool = ExecuteCommandTool(self.terminal_manager)
        self.read_logs_tool = ReadLogsTool(self.terminal_manager)
        self.close_terminal_tool = CloseTerminalTool(self.terminal_manager)
        self.get_terminal_info_tool = GetTerminalInfoTool(self.terminal_manager)
        self.search_tool = SearchTool()
        self.git_tool = GitTool()
        self.git_tool.set_workspace(str(self.cwd))

        # Sub-agent support (disabled for sub-agents to prevent recursion)
        self._is_subagent = False
        self.subagent_tool: Optional[SubAgentTool] = SubAgentTool(
            workspace=str(self.cwd),
            client=self.client,
            model=self.model,
        )

        # Browser tools
        self.browser_manager = BrowserManager()
        self.browser_launch_tool = BrowserLaunchTool(self.browser_manager)
        self.browser_navigate_tool = BrowserNavigateTool(self.browser_manager)
        self.browser_click_tool = BrowserClickTool(self.browser_manager)
        self.browser_click_index_tool = BrowserClickIndexTool(self.browser_manager)
        self.browser_type_tool = BrowserTypeTool(self.browser_manager)
        self.browser_type_index_tool = BrowserTypeIndexTool(self.browser_manager)
        self.browser_select_tool = BrowserSelectTool(self.browser_manager)
        self.browser_select_index_tool = BrowserSelectIndexTool(self.browser_manager)
        self.browser_snapshot_tool = BrowserSnapshotTool(self.browser_manager)
        self.browser_screenshot_tool = BrowserScreenshotTool(self.browser_manager)
        self.browser_scroll_tool = BrowserScrollTool(self.browser_manager)
        self.browser_wait_tool = BrowserWaitTool(self.browser_manager)
        self.browser_send_keys_tool = BrowserSendKeysTool(self.browser_manager)
        self.browser_search_page_tool = BrowserSearchPageTool(self.browser_manager)
        self.browser_find_elements_tool = BrowserFindElementsTool(self.browser_manager)

        self.browser_tab_list_tool = BrowserTabListTool(self.browser_manager)
        self.browser_tab_switch_tool = BrowserTabSwitchTool(self.browser_manager)
        self.browser_tab_open_tool = BrowserTabOpenTool(self.browser_manager)
        self.browser_evaluate_tool = BrowserEvaluateTool(self.browser_manager)
        self.browser_go_back_tool = BrowserGoBackTool(self.browser_manager)
        self.browser_go_forward_tool = BrowserGoForwardTool(self.browser_manager)
        self.browser_reload_tool = BrowserReloadTool(self.browser_manager)
        self.browser_close_tool = BrowserCloseTool(self.browser_manager)
        self.browser_click_xy_tool = BrowserClickXYTool(self.browser_manager)
        self.browser_switch_frame_tool = BrowserSwitchFrameTool(self.browser_manager)
        self.browser_hover_tool = BrowserHoverTool(self.browser_manager)
        self.browser_hover_index_tool = BrowserHoverIndexTool(self.browser_manager)
        self.browser_drag_tool = BrowserDragTool(self.browser_manager)
        self.browser_drag_xy_tool = BrowserDragXYTool(self.browser_manager)
        self.browser_wait_for_tool = BrowserWaitForTool(self.browser_manager)

        # Skills
        self.skill_manager = SkillManager(str(self.cwd / "skills"))

        # Callbacks wired from CLI
        self.on_tool_call: Optional[Callable[[str, dict], None]] = None
        self.on_stream_start: Optional[Callable[[], None]] = None
        self.on_stream_token: Optional[Callable[[str], None]] = None
        self.on_stream_end: Optional[Callable[[str, bool], None]] = None
        self.on_tool_result: Optional[Callable[[str, dict, str], None]] = None
        self.on_token_update: Optional[Callable[[TokenCounter], None]] = None
        self.on_compact: Optional[Callable[[str], None]] = None
        self.on_background_notification: Optional[Callable[[str], None]] = None

        # Keep this callback installed for the lifetime of the agent. It only
        # prints a visible notification; completed events are retained until
        # the next API turn drains them into the conversation.
        self.terminal_manager.set_completion_callback(self._on_background_completion)

        self.conversation_history: List[Dict[str, Any]] = []
        self._setup_system_prompt()

    def _on_background_completion(self, event: dict) -> None:
        """Show active completions; leave idle completions queued for next input."""
        if not self.is_processing or not self.on_background_notification:
            return

        completion_id = event.get("completion_id")
        with self._background_ui_lock:
            if completion_id and completion_id in self._background_ui_notified_ids:
                return
            if completion_id:
                self._background_ui_notified_ids.add(completion_id)
        notification = self.terminal_manager.format_background_completion(event)
        if self.on_background_notification:
            self.on_background_notification(notification)

    def _set_processing(self, value: bool) -> None:
        """Track whether a user request is actively running."""
        with self._processing_lock:
            self._is_processing = value

    @property
    def is_processing(self) -> bool:
        with self._processing_lock:
            return self._is_processing

    def _drain_background_notifications(self) -> Optional[str]:
        """Format completed background commands for insertion into the API turn."""
        completions = self.terminal_manager.drain_completed_background_commands()
        if not completions:
            return None
        # An idle completion was intentionally not displayed by the callback;
        # display it now when the next request drains the queue.
        for event in completions:
            self._on_background_completion(event)
        return "\n\n".join(
            self.terminal_manager.format_background_completion(event)
            for event in completions
        )

    def _setup_system_prompt(self):
        base = (
            "You are Kairos, a coding agent. You operate in a filesystem and can read, write, and edit files, execute terminal commands, search codebases, inspect version control, and browse the web.\n\n"
            "You think step-by-step. Before making changes, you read the relevant files to understand the current state. After making changes, you verify they work. When something fails, you read the error carefully and adjust.\n\n"
            "You have absolute access to the filesystem. All file paths must be absolute (e.g., C:/Users/me/project/main.py or /home/me/project/main.py). You are not sandboxed \u2014 you can read any file you have permission to, and write to any location you have permission to.\n\n"
            "You have 40 tools. Each tool either succeeds and returns output, or fails and returns an error message. When a tool fails, the error tells you exactly what went wrong \u2014 use that information to fix your approach. Never retry the exact same call that just failed without changing something.\n\n"
            "Whenever the user asks you to look at a project, it usually has an AGENTS.md file and a README.md file. You should use these files to understand the project and the codebase, and ALWAYS follow the instructions mentioned in the AGENTS.md files. Make sure to look for this file in any projects the user points you towards. The AGENTS.md will automatically be injected into your system prompt in the directory the user starts in, but if they point you towards a different directory, it will not automatically be injected, so you will have to look for the AGENTS.md file in that directory. Note that the AGENTS.md does not always exist.\n\n"
            "## Terminal Tools\n"
            "Use a blocking terminal for short commands whose result you need immediately. Its execute_command call requires a finite positive timeout; values above 20 seconds are capped at 20 seconds. Use a background terminal for servers, watchers, builds, or other long-running commands; timeout is ignored for background terminals. Background commands return immediately, the persistent shell stays alive, and a completion notification containing capped output is delivered while the agent is active or queued for the next user message when the agent is idle. The full output remains available through read_logs.\n\n"
            f"## Tool Result Limits\nText returned by tools is capped before it is added to conversation history: up to {self.max_tool_result_chars:,} characters per tool result. Oversized results contain a truncation marker and preserve both the beginning and end. Use narrower commands or queries; for complete background-terminal output, use read_logs.\n\n"
            "## Browser Tools\n"
            "You can browse the web using browser tools. The workflow is:\n"
            "1. `browser_launch` \u2014 start the browser (optionally with a named profile for persistent sessions)\n"
            "2. `browser_navigate` \u2014 go to a URL\n"
            "3. `browser_snapshot` \u2014 observe the page (shows elements with indices [0],[1]... and CSS selectors)\n"
            "4. `browser_click_index` / `browser_type_index` \u2014 interact by element index (PREFERRED)\n"
            "   Also: `browser_click` / `browser_type` with CSS selectors (fallback)\n"
            "5. `browser_scroll` \u2014 scroll up/down by viewport heights\n"
            "6. `browser_search_page` \u2014 grep the live page for text (zero LLM cost)\n"
            "7. `browser_find_elements` \u2014 query DOM by CSS selector (zero LLM cost)\n"
            "8. `browser_send_keys` \u2014 keyboard shortcuts (Enter, Tab, Control+a, etc.)\n"
            "9. `browser_screenshot` \u2014 capture visual screenshot (saves to ~/.kairos/screenshots/)\n"
            "10. `browser_tab_open` / `browser_tab_switch` / `browser_tab_list` \u2014 manage multiple tabs\n"
            "11. `browser_go_back` / `browser_go_forward` / `browser_reload` \u2014 navigation history\n"
            "12. `browser_wait` \u2014 wait for animations/AJAX to complete\n"
            "13. `browser_wait_for` \u2014 wait for a specific element or text to appear (more efficient than blind waiting)\n"
            "14. `browser_switch_frame` \u2014 enter/exit iframes (including cross-origin via CDP)\n"
            "15. `browser_hover` / `browser_hover_index` \u2014 hover over elements (for dropdowns, tooltips)\n"
            "16. `browser_drag` / `browser_drag_xy` \u2014 drag and drop (for file uploads, sortable lists)\n"
            "17. `browser_close` \u2014 shut down when done\n\n"
            "PREFER index-based tools (browser_click_index, browser_type_index, browser_select_index, browser_hover_index) over selector-based ones.\n"
            "All interaction tools automatically detect significant page changes (popups, navigation, big DOM shifts) and snapshot+screen when needed.\n"
            "You can still use browser_snapshot and browser_screenshot explicitly when you want to observe the page.\n"
            "Use named profiles (e.g. profile=\"Kairos\") to keep logins and cookies across sessions.\n\n"
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

    def _get_tool_schema(self) -> List[Dict[str, Any]]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read the contents of a file at the given absolute path.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Absolute file path"}
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
                            "path": {"type": "string", "description": "Absolute file path"},
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
                            "path": {"type": "string", "description": "Absolute file path"},
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
                            "path": {"type": "string", "description": "Directory to search in (defaults to cwd)"},
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
                    "description": "Execute a shell command in a terminal.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {"type": "integer", "description": "Terminal ID from new_terminal"},
                            "command": {"type": "string", "description": "Shell command to execute"},
                            "timeout": {"type": "number", "description": "Required finite positive timeout for blocking terminals; values above 20 seconds are capped at 20. Ignored for background terminals."},
                            "is_background": {"type": "boolean", "description": "Must match terminal type"},
                        },
                        "required": ["terminal_id", "command", "is_background"],
                        "oneOf": [
                            {
                                "properties": {"is_background": {"const": True}},
                            },
                            {
                                "properties": {"is_background": {"const": False}},
                                "required": ["timeout"],
                            },
                        ],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_logs",
                    "description": "Read output from a background terminal by line number range.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {"type": "integer", "description": "Background terminal ID"},
                            "start_line": {"type": "integer", "description": "First line to read (1-indexed)"},
                            "end_line": {"type": "integer", "description": "Last line (optional)"},
                        },
                        "required": ["terminal_id", "start_line"],
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
                    "description": "Spawn a sub-agent to work on a task autonomously.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "The task / instruction to give the sub-agent"},
                            "mode": {"type": "string", "description": "'blocking' (default) or 'non-blocking'"},
                        },
                        "required": ["prompt"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_subagent_result",
                    "description": "Retrieve the result of a non-blocking sub-agent.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "subagent_id": {"type": "string", "description": "The ID returned by spawn_subagent"},
                        },
                        "required": ["subagent_id"],
                    },
                },
            },
            # ---- Browser Tools ----
            {
                "type": "function",
                "function": {
                    "name": "browser_launch",
                    "description": "Launch a stealth browser.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "profile": {"type": "string", "description": "Named persistent profile (e.g. 'Arjun', 'work')."},
                            "proxy": {"type": "string", "description": "Proxy server URL"},
                            "humanize": {"type": "boolean", "description": "Enable human-like mouse/keyboard behavior"},
                            "headless": {"type": "boolean", "description": "Run in headless mode with no visible window (default: false)"},
                            "chrome_profile": {"type": "string", "description": "Path to Chrome user data directory to copy"},
                            "connect_cdp": {"type": "string", "description": "Connect to running Chrome via CDP"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_navigate",
                    "description": "Navigate the current tab to a URL.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "URL to navigate to (include https://)"},
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_go_back",
                    "description": "Navigate back in browser history.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_go_forward",
                    "description": "Navigate forward in browser history.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_reload",
                    "description": "Reload the current page.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_click",
                    "description": "Click an element on the page. TIP: Use browser_click_index instead for more reliable clicks.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector, text selector, or visible text to click"},
                        },
                        "required": ["selector"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_click_index",
                    "description": "Click an element by its snapshot index number. PREFERRED over browser_click for reliability.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer", "description": "Element index from browser_snapshot"},
                        },
                        "required": ["index"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_type",
                    "description": "Type text into an input field. TIP: Use browser_type_index instead for more reliable input.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector or placeholder text of the input field"},
                            "text": {"type": "string", "description": "Text to type"},
                            "press_enter": {"type": "boolean", "description": "Press Enter after typing"},
                        },
                        "required": ["selector", "text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_type_index",
                    "description": "Type text into an input element by its snapshot index number. PREFERRED over browser_type.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer", "description": "Element index from browser_snapshot"},
                            "text": {"type": "string", "description": "Text to type"},
                            "press_enter": {"type": "boolean", "description": "Press Enter after typing"},
                        },
                        "required": ["index", "text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_select",
                    "description": "Select an option from a <select> dropdown by its value attribute.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector of the <select> element"},
                            "value": {"type": "string", "description": "Value attribute of the option to select"},
                        },
                        "required": ["selector", "value"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_select_index",
                    "description": "Select an option from a <select> dropdown by snapshot index. PREFERRED over browser_select.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer", "description": "Element index from browser_snapshot (must be a <select>)"},
                            "value": {"type": "string", "description": "Value, label text, or numeric index of the option to select"},
                        },
                        "required": ["index", "value"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_snapshot",
                    "description": "Get a compact text representation of the current page. PRIMARY way to observe a page.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_screenshot",
                    "description": "Capture a visual screenshot of the current page.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "full_page": {"type": "boolean", "description": "Capture the entire scrollable page (default false)"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_tab_list",
                    "description": "List all open browser tabs with their index, title, and URL.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_tab_switch",
                    "description": "Switch to a different tab by index number or URL pattern match.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer", "description": "Tab index (0-based, from browser_tab_list)"},
                            "url_pattern": {"type": "string", "description": "Switch to the first tab whose URL contains this text"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_tab_open",
                    "description": "Open a new browser tab. Optionally navigate it to a URL immediately.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "URL to open in the new tab (optional)"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_evaluate",
                    "description": "Execute JavaScript in the current page and return the result.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "expression": {"type": "string", "description": "JavaScript expression or function body to evaluate"},
                        },
                        "required": ["expression"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_close",
                    "description": "Close the browser and clean up all resources. Always close when done browsing.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_click_xy",
                    "description": "Click at absolute viewport coordinates (x, y). Useful for vision-based interaction.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "number", "description": "Horizontal pixel coordinate (left=0)"},
                            "y": {"type": "number", "description": "Vertical pixel coordinate (top=0)"},
                        },
                        "required": ["x", "y"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_scroll",
                    "description": "Scroll the page up or down.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "direction": {"type": "string", "enum": ["down", "up"], "description": "Scroll direction"},
                            "pages": {"type": "number", "description": "Number of viewport heights to scroll (default 1.0)"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_wait",
                    "description": "Wait for a specified number of seconds (max 30). Useful for letting animations/AJAX complete.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "seconds": {"type": "integer", "description": "Seconds to wait (default 3, max 30)"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_wait_for",
                    "description": "Wait for a specific element to become visible or text to appear on the page. More efficient than browser_wait for AJAX/dynamic content.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector of element to wait for (provide selector OR text, not both)"},
                            "text": {"type": "string", "description": "Text to wait for appearing on page (provide selector OR text, not both)"},
                            "timeout": {"type": "integer", "description": "Max seconds to wait (default 10, max 30)"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_send_keys",
                    "description": "Send a keyboard key or shortcut. Examples: 'Enter', 'Tab', 'Escape', 'Control+a'.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keys": {"type": "string", "description": "Key name or shortcut (e.g. 'Enter', 'Control+a', 'Tab')"},
                        },
                        "required": ["keys"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_search_page",
                    "description": "Search for text on the current page (like grep on the live DOM). Zero LLM cost.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "description": "Text or regex pattern to search for"},
                            "regex": {"type": "boolean", "description": "Treat pattern as regex (default false)"},
                            "case_sensitive": {"type": "boolean", "description": "Case-sensitive search (default false)"},
                            "max_results": {"type": "integer", "description": "Max matches to return (default 20)"},
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_find_elements",
                    "description": "Query DOM elements by CSS selector. Returns matching elements with index, tag, and text. Zero LLM cost.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector query"},
                            "max_results": {"type": "integer", "description": "Max elements to return (default 50)"},
                        },
                        "required": ["selector"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_switch_frame",
                    "description": "Switch the active context into an iframe, or back to the top-level page.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "frame_selector": {"type": "string", "description": "CSS selector, frame name, or URL fragment to match the iframe. Empty/null to return to top-level."},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_hover",
                    "description": "Hover over an element to trigger hover states (dropdown menus, tooltips, hover cards).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector or text to hover over"},
                        },
                        "required": ["selector"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_hover_index",
                    "description": "Hover over an element by its snapshot index number. PREFERRED over browser_hover.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer", "description": "Element index from browser_snapshot"},
                        },
                        "required": ["index"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_drag",
                    "description": "Drag an element to another element. Use for file uploads, sortable lists, Kanban boards.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector_from": {"type": "string", "description": "CSS selector of the element to drag"},
                            "selector_to": {"type": "string", "description": "CSS selector of the destination element"},
                        },
                        "required": ["selector_from", "selector_to"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_drag_xy",
                    "description": "Drag from one viewport coordinate to another.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "x1": {"type": "number", "description": "Start X coordinate"},
                            "y1": {"type": "number", "description": "Start Y coordinate"},
                            "x2": {"type": "number", "description": "End X coordinate"},
                            "y2": {"type": "number", "description": "End Y coordinate"},
                        },
                        "required": ["x1", "y1", "x2", "y2"],
                    },
                },
            },
            # ---- Skill Tools ----
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

        if self._is_subagent:
            tools = [t for t in tools if t["function"]["name"] not in (
                "spawn_subagent", "get_subagent_result",
                "browser_launch", "browser_navigate", "browser_click", "browser_click_index",
                "browser_type", "browser_type_index", "browser_select", "browser_select_index",
                "browser_snapshot", "browser_screenshot", "browser_scroll", "browser_wait", "browser_wait_for",
                "browser_send_keys", "browser_search_page", "browser_find_elements",
                "browser_tab_list", "browser_tab_switch", "browser_tab_open",
                "browser_evaluate", "browser_close", "browser_click_xy",
                "browser_switch_frame", "browser_go_back", "browser_go_forward", "browser_reload",
                "browser_hover", "browser_hover_index",
                "browser_drag", "browser_drag_xy",
            )]
        return tools

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
            "git": lambda a: self.git_tool(a["command"], path=a.get("path"), count=a.get("count", 10), message=a.get("message", "")).to_dict(),
            "new_terminal": lambda a: self.new_terminal_tool(a["background"]).to_dict(),
            "execute_command": lambda a: self.execute_command_tool(a["terminal_id"], a["command"], a.get("timeout"), a.get("is_background")).to_dict(),
            "read_logs": lambda a: self.read_logs_tool(a["terminal_id"], a["start_line"], a.get("end_line")).to_dict(),
            "close_terminal": lambda a: self.close_terminal_tool(a["terminal_id"]).to_dict(),
            "get_terminal_info": lambda a: self.get_terminal_info_tool(a["terminal_id"]).to_dict(),
            "spawn_subagent": lambda a: self.subagent_tool.spawn(a["prompt"], a.get("mode", "blocking")).to_dict(),
            "get_subagent_result": lambda a: self.subagent_tool.get_result(a["subagent_id"]).to_dict(),
            # Browser tools
            "browser_launch": lambda a: self.browser_launch_tool(
                profile=a.get("profile"), headless=a.get("headless", False), proxy=a.get("proxy"),
                humanize=a.get("humanize", True), chrome_profile=a.get("chrome_profile"), connect_cdp=a.get("connect_cdp"),
            ).to_dict(),
            "browser_navigate": lambda a: self.browser_navigate_tool(a["url"]).to_dict(),
            "browser_go_back": lambda a: self.browser_go_back_tool().to_dict(),
            "browser_go_forward": lambda a: self.browser_go_forward_tool().to_dict(),
            "browser_reload": lambda a: self.browser_reload_tool().to_dict(),
            "browser_click": lambda a: self.browser_click_tool(a["selector"]).to_dict(),
            "browser_click_index": lambda a: self.browser_click_index_tool(a["index"]).to_dict(),
            "browser_type": lambda a: self.browser_type_tool(a["selector"], a["text"], press_enter=a.get("press_enter", False)).to_dict(),
            "browser_type_index": lambda a: self.browser_type_index_tool(a["index"], a["text"], press_enter=a.get("press_enter", False)).to_dict(),
            "browser_select": lambda a: self.browser_select_tool(a["selector"], a["value"]).to_dict(),
            "browser_select_index": lambda a: self.browser_select_index_tool(a["index"], a["value"]).to_dict(),
            "browser_snapshot": lambda a: self.browser_snapshot_tool().to_dict(),
            "browser_screenshot": lambda a: self.browser_screenshot_tool(full_page=a.get("full_page", False)).to_dict(),
            "browser_scroll": lambda a: self.browser_scroll_tool(direction=a.get("direction", "down"), pages=a.get("pages", 1.0)).to_dict(),
            "browser_wait": lambda a: self.browser_wait_tool(seconds=a.get("seconds", 3)).to_dict(),
            "browser_wait_for": lambda a: self.browser_wait_for_tool(selector=a.get("selector"), text=a.get("text"), timeout=a.get("timeout", 10)).to_dict(),
            "browser_send_keys": lambda a: self.browser_send_keys_tool(keys=a["keys"]).to_dict(),
            "browser_search_page": lambda a: self.browser_search_page_tool(
                a["pattern"], regex=a.get("regex", False), case_sensitive=a.get("case_sensitive", False), max_results=a.get("max_results", 20),
            ).to_dict(),
            "browser_find_elements": lambda a: self.browser_find_elements_tool(a["selector"], max_results=a.get("max_results", 50)).to_dict(),
            "browser_tab_list": lambda a: self.browser_tab_list_tool().to_dict(),
            "browser_tab_switch": lambda a: self.browser_tab_switch_tool(index=a.get("index"), url_pattern=a.get("url_pattern")).to_dict(),
            "browser_tab_open": lambda a: self.browser_tab_open_tool(url=a.get("url")).to_dict(),
            "browser_evaluate": lambda a: self.browser_evaluate_tool(a["expression"]).to_dict(),
            "browser_close": lambda a: self.browser_close_tool().to_dict(),
            "browser_click_xy": lambda a: self.browser_click_xy_tool(a["x"], a["y"]).to_dict(),
            "browser_switch_frame": lambda a: self.browser_switch_frame_tool(a.get("frame_selector")).to_dict(),
            "browser_hover": lambda a: self.browser_hover_tool(a["selector"]).to_dict(),
            "browser_hover_index": lambda a: self.browser_hover_index_tool(a["index"]).to_dict(),
            "browser_drag": lambda a: self.browser_drag_tool(a["selector_from"], a["selector_to"]).to_dict(),
            "browser_drag_xy": lambda a: self.browser_drag_xy_tool(a["x1"], a["y1"], a["x2"], a["y2"]).to_dict(),
            # Skill tools
            "list_skills": lambda a: self.skill_manager.list_skills().to_dict(),
            "load_skill": lambda a: self.skill_manager.load_skill(a["skill_name"]).to_dict(),
            "write_skill": lambda a: self.skill_manager.write_skill(a["skill_name"], a["content"], overwrite=a.get("overwrite", False)).to_dict(),
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

        try:
            result = dispatch[name](args)
            return json.dumps(
                self._cap_tool_result(result, max_chars=max_chars),
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps(
                self._cap_tool_result(
                    {"success": False, "output": "", "error": str(e)},
                    max_chars=max_chars,
                ),
                ensure_ascii=False,
            )

    @staticmethod
    def _tool_summary(name: str, args: Dict[str, Any]) -> str:
        if name == "read": return f"read file: {args.get('path', '?')}"
        if name == "write": return f"wrote file: {args.get('path', '?')}"
        if name == "edit": return f"edited file: {args.get('path', '?')}"
        if name == "search":
            timeout = args.get("timeout", SearchTool.DEFAULT_TIMEOUT)
            return f"search: '{args.get('pattern', '?')}' in {args.get('path', 'cwd')} (timeout: {timeout}s)"
        if name == "git":
            cmd = args.get("command", "?")
            if cmd == "commit": return f"git commit: {args.get('message', '')[:40]}"
            return f"git {cmd}"
        if name == "new_terminal": return f"opened {'background' if args.get('background') else 'blocking'} terminal"
        if name == "execute_command": return f"terminal {args.get('terminal_id', '?')}: {args.get('command', '?')}"
        if name == "read_logs": return f"read terminal {args.get('terminal_id', '?')} logs"
        if name == "close_terminal": return f"closed terminal {args.get('terminal_id', '?')}"
        if name == "get_terminal_info": return f"info on terminal {args.get('terminal_id', '?')}"
        if name == "spawn_subagent":
            mode = args.get("mode", "blocking")
            prompt_preview = args.get("prompt", "")[:40]
            return f"spawn {mode} subagent: {prompt_preview}"
        if name == "get_subagent_result": return f"get subagent result: {args.get('subagent_id', '?')}"
        # Browser tool summaries
        if name == "browser_launch":
            profile = args.get("profile")
            chrome = args.get("chrome_profile")
            cdp = args.get("connect_cdp")
            if cdp: return f"connect to Chrome via CDP: {cdp}"
            if chrome: return f"launch with Chrome profile: {chrome}"
            return f"launch browser" + (f" (profile: {profile})" if profile else "")
        if name == "browser_navigate": return f"navigate to: {args.get('url', '?')}"
        if name == "browser_go_back": return "go back"
        if name == "browser_go_forward": return "go forward"
        if name == "browser_reload": return "reload page"
        if name == "browser_click": return f"click: {args.get('selector', '?')}"
        if name == "browser_click_index": return f"click element [{args.get('index', '?')}]"
        if name == "browser_type":
            text_preview = (args.get("text", "") or "")[:30]
            return f"type into {args.get('selector', '?')}: \"{text_preview}\""
        if name == "browser_type_index":
            text_preview = (args.get("text", "") or "")[:30]
            return f"type into [{args.get('index', '?')}]: \"{text_preview}\""
        if name == "browser_select": return f"select '{args.get('value', '?')}' in {args.get('selector', '?')}"
        if name == "browser_select_index": return f"select '{args.get('value', '?')}' in [{args.get('index', '?')}]"
        if name == "browser_snapshot": return "snapshot current page"
        if name == "browser_screenshot":
            kind = "full page" if args.get("full_page") else "viewport"
            return f"screenshot ({kind})"
        if name == "browser_scroll":
            d = args.get("direction", "down")
            p = args.get("pages", 1.0)
            return f"scroll {d} {p} pages"
        if name == "browser_wait": return f"wait {args.get('seconds', 3)}s"
        if name == "browser_wait_for":
            target = args.get("selector") or args.get("text", "?")
            return f"wait for: {target}"
        if name == "browser_send_keys": return f"send keys: {args.get('keys', '?')}"
        if name == "browser_search_page": return f"search page for: '{args.get('pattern', '?')}'"
        if name == "browser_find_elements": return f"find elements: {args.get('selector', '?')}"
        if name == "browser_tab_list": return "list tabs"
        if name == "browser_tab_switch":
            idx = args.get("index")
            pattern = args.get("url_pattern")
            if idx is not None: return f"switch to tab {idx}"
            return f"switch to tab matching: {pattern or '?'}"
        if name == "browser_tab_open":
            url = args.get("url")
            return f"open new tab" + (f" -> {url}" if url else "")
        if name == "browser_evaluate":
            expr_preview = (args.get("expression", "") or "")[:40]
            return f"evaluate JS: {expr_preview}"
        if name == "browser_close": return "close browser"
        if name == "browser_click_xy": return f"click at ({args.get('x', '?')}, {args.get('y', '?')})"
        if name == "browser_switch_frame":
            sel = args.get("frame_selector", "")
            return f"switch to frame: {sel}" if sel else "switch to top-level page"
        if name == "browser_hover": return f"hover: {args.get('selector', '?')}"
        if name == "browser_hover_index": return f"hover element [{args.get('index', '?')}]"
        if name == "browser_drag": return f"drag '{args.get('selector_from', '?')}' -> '{args.get('selector_to', '?')}'"
        if name == "browser_drag_xy": return f"drag ({args.get('x1', '?')}, {args.get('y1', '?')}) -> ({args.get('x2', '?')}, {args.get('y2', '?')})"
        # Skill tool summaries
        if name == "list_skills": return "list skills"
        if name == "load_skill": return f"load skill: {args.get('skill_name', '?')}"
        if name == "write_skill":
            skill = args.get("skill_name", "?")
            ow = " (overwrite)" if args.get("overwrite") else ""
            return f"write skill: {skill}{ow}"
        return name

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

    def _truncate_history_if_needed(self):
        if len(self.conversation_history) >= 1 + self.MAX_HISTORY_MESSAGES:
            system = self.conversation_history[0]
            tail = self.conversation_history[-(self.MAX_HISTORY_MESSAGES):]
            has_user = any(m.get("role") == "user" for m in tail)
            if not has_user:
                for i in range(len(self.conversation_history) - 1, 0, -1):
                    if self.conversation_history[i].get("role") == "user":
                        tail = self.conversation_history[i:]
                        break
            self.conversation_history = [system] + tail

    def _validate_history_before_api(self) -> Optional[str]:
        history = self.conversation_history
        if len(history) <= 2:
            return None
        has_user = any(m.get("role") == "user" for m in history)
        if not has_user:
            return self.compact()
        # Only inspect a trailing tool chain. A normal user message (including
        # the synthetic background-notification user message) is a valid end
        # of history and must not be mistaken for an orphaned tool result.
        if history[-1].get("role") != "tool":
            return None
        i = len(history) - 1
        while i > 0 and history[i].get("role") == "tool":
            i -= 1
        if i > 0 and history[i].get("role") != "assistant":
            orphan_count = len(history) - 1 - i
            self.conversation_history = history[:i + 1]
            return f"Removed {orphan_count} orphaned tool message(s) to fix history ordering."
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

    COMPACT_SUMMARY_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish?]

## Constraints & Preferences
- [Any constraints or preferences]
- [Or "(none)" if none]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Data, file paths, function names, error messages needed to continue]
- [Or "(none)" if not applicable]

## User Messages & Agent-to-User Messages
### User Messages
- [Chronological, concise summary of every user message in the conversation being summarized]
- [Preserve each request, correction, constraint, preference, and explicit question; do not omit messages]
- [Or "(none)" if not applicable]

### Agent-to-User Messages
- [Chronological, concise summary of every substantive assistant response intended for the user]
- [Preserve answers, explanations, status updates, decisions, and commitments; do not omit messages]
- [Do not treat tool-call-only messages or tool results as agent-to-user messages]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages. The User Messages and Agent-to-User Messages subsections must cover every applicable message, in chronological order."""

    COMPACT_UPDATE_PROMPT = """The messages above are NEW conversation messages to incorporate into the existing summary.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context
- UPDATE Progress: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- PRESERVE the complete chronological User Messages and Agent-to-User Messages record
- APPEND or update that record for every applicable new user message and substantive assistant response; do not silently omit any

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones]

## Constraints & Preferences
- [Preserve existing, add new]

## Progress
### Done
- [x] [All completed items]

### In Progress
- [ ] [Current work]

## Key Decisions
- **[Decision]**: [Rationale]

## Next Steps
1. [Updated next steps]

## Critical Context
- [Preserve and add important context]

## User Messages & Agent-to-User Messages
### User Messages
- [Chronological, concise summary of every user message represented by the existing summary or new messages]
- [Preserve each request, correction, constraint, preference, and explicit question]
- [Or "(none)" if not applicable]

### Agent-to-User Messages
- [Chronological, concise summary of every substantive assistant response represented by the existing summary or new messages]
- [Preserve answers, explanations, status updates, decisions, and commitments]
- [Do not treat tool-call-only messages or tool results as agent-to-user messages]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages. The User Messages and Agent-to-User Messages subsections must cover every applicable message, in chronological order."""

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
        """Refresh context, including tools and a user message about to append."""
        self._normalize_history_for_context()
        messages = self.conversation_history
        if pending_user_message is not None:
            messages = [*messages, pending_user_message]
        self.tokens.context_tokens = self.tokens.count_request(
            messages,
            self._get_tool_schema(),
        )

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
                if len(content) > 500:
                    content = content[:500] + "... [truncated]"
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
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.COMPACT_SYSTEM_PROMPT},
                    {"role": "user", "content": full_prompt},
                ],
                max_tokens=min(self.COMPACT_RESERVE_TOKENS, 8192),
            )
        except Exception as e:
            raise Exception(self._format_api_error(e)) from e
        return response.choices[0].message.content or "(compaction produced no summary)"

    def compact(self) -> str:
        history = self.conversation_history
        if len(history) <= 2:
            return "Nothing to compact \u2014 conversation is still short."
        boundary = self._find_compact_boundary()
        if not self._is_safe_compact_boundary(boundary):
            return "Nothing to compact \u2014 couldn't find a safe message boundary."
        to_summarize = history[1:boundary]
        if not to_summarize:
            return "Nothing to compact \u2014 all messages are recent."
        system_msg = history[0]
        existing_summary = None
        if (
            len(history) > 1
            and history[1].get("role") == "user"
            and isinstance(history[1].get("content"), str)
            and history[1]["content"].startswith("[Conversation compacted")
        ):
            existing_summary = history[1].get("content")
        summary = self._generate_summary(to_summarize, existing_summary)
        compaction_msg = {
            "role": "user",
            "content": f"[Conversation compacted — summary of prior history]\n\n{summary}",
        }
        recent = history[boundary:]
        self.conversation_history = [system_msg, compaction_msg] + recent
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

    def interrupt(self):
        self._interrupt_event.set()

    def _check_interrupt(self):
        if self._interrupt_event.is_set():
            self._interrupt_event.clear()
            raise InterruptedError("Interrupted by user")

    def request_stop(self):
        self._stop_requested = True

    def _should_stop(self) -> bool:
        if self._stop_requested:
            self._stop_requested = False
            return True
        return False

    def _prepare_messages_for_api(self) -> List[Dict[str, Any]]:
        messages = []
        for msg in self.conversation_history:
            if msg.get("role") == "tool" and "_image_url" in msg:
                messages.append({
                    "tool_call_id": msg["tool_call_id"],
                    "role": "tool",
                    "name": msg.get("name", ""),
                    "content": msg["content"],
                })
            else:
                messages.append(msg)
        return messages

    def _format_api_error(self, e: Exception) -> str:
        """Return concise API diagnostics without an exception traceback.

        Gateway/proxy failures often arrive as low-level ``httpx`` or
        ``httpcore`` exceptions.  Their ``str`` value is useful, but their
        chained traceback is not actionable for a normal CLI response, so
        only the exception type and one-line message are surfaced here.
        """
        error_name = type(e).__name__
        message = " ".join(str(e).split()) or "No additional details provided."
        lines = [f"OpenAI API Error: {error_name}", f"  Message: {message}"]

        if isinstance(e, APIStatusError):
            lines.insert(1, f"  Status Code: {e.status_code}")
            request_id = getattr(e, "request_id", None)
            if request_id:
                lines.insert(2, f"  Request ID:  {request_id}")
        elif isinstance(e, AuthenticationError):
            lines.append("  Hint: Check your OPENAI_API_KEY and OPENAI_BASE_URL in .env")
        elif isinstance(e, RateLimitError):
            lines.append("  Hint: You are being rate-limited. Wait a moment and retry.")
        elif isinstance(e, APIConnectionError):
            lines.append(f"  Hint: Could not connect to {self.client.base_url}")
            lines.append("  Check that the API server is running and reachable.")

        lines.extend([
            "  Config:",
            f"    Model:    {self.model}",
            f"    Base URL: {self.client.base_url}",
        ])
        return "\n".join(lines)

    @staticmethod
    def _is_retryable_error(e: Exception) -> bool:
        if isinstance(e, (RateLimitError, APIConnectionError)):
            return True
        if isinstance(e, APIStatusError):
            return e.status_code in (500, 502, 503, 504)
        err = str(e).lower()
        return any(c in err for c in ["timeout", "connection", "network", "remoteprotocolerror", "incomplete chunked read"])

    def _retry_delay(self, attempt: int, base_delay: Optional[float] = None) -> float:
        """Return a short exponential backoff with small deterministic jitter."""
        delay = self.API_RETRY_BASE_DELAY if base_delay is None else base_delay
        return delay * (2 ** attempt) + 0.5 * attempt

    def _call_openai_with_retry(self, max_retries: Optional[int] = None, base_delay: Optional[float] = None):
        """Call the non-streaming endpoint, retrying transient failures twice."""
        retries = self.API_MAX_RETRIES if max_retries is None else max_retries
        attempts = retries + 1
        last_exc = None
        for attempt in range(attempts):
            try:
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=self._prepare_messages_for_api(),
                    tools=self._get_tool_schema(),
                    tool_choice="auto",
                )
            except Exception as e:
                last_exc = e
                if not self._is_retryable_error(e) or attempt == attempts - 1:
                    break
                time.sleep(self._retry_delay(attempt, base_delay))
        raise APIRequestError(self._format_api_error(last_exc)) from None

    def _stream_response(self) -> Tuple[str, List[Dict[str, Any]], Optional[Dict[str, int]]]:
        """Stream a response, retrying transient connection failures twice.

        Retries cover both request creation and iteration: a gateway can close
        a chunked response after streaming has already started.  Partial data
        is discarded before retrying so content/tool calls are never mixed
        across attempts.
        """
        retries = self.API_MAX_RETRIES
        attempts = retries + 1
        last_exc = None

        for attempt in range(attempts):
            stream = None
            try:
                stream = self.client.chat.completions.create(
                    model=self.model,
                    messages=self._prepare_messages_for_api(),
                    tools=self._get_tool_schema(),
                    tool_choice="auto",
                    stream=True,
                    stream_options={"include_usage": True},
                )

                if self.on_stream_start:
                    self.on_stream_start()
                content = ""
                tool_calls: Dict[int, Dict[str, str]] = {}
                api_usage = None
                for chunk in stream:
                    self._check_interrupt()
                    if not chunk.choices:
                        if chunk.usage:
                            api_usage = {
                                "prompt_tokens": chunk.usage.prompt_tokens or 0,
                                "completion_tokens": chunk.usage.completion_tokens or 0,
                            }
                        continue
                    delta = chunk.choices[0].delta
                    if not delta:
                        continue
                    if delta.content:
                        content += delta.content
                        if self.on_stream_token:
                            self.on_stream_token(delta.content)
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls:
                                tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                            if tc.id:
                                tool_calls[idx]["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    tool_calls[idx]["name"] = tc.function.name
                                if tc.function.arguments:
                                    tool_calls[idx]["arguments"] += tc.function.arguments

                assembled = []
                for idx in sorted(tool_calls.keys()):
                    tc = tool_calls[idx]
                    try:
                        args = json.loads(tc["arguments"])
                    except json.JSONDecodeError:
                        args = {}
                    assembled.append({"id": tc["id"], "name": tc["name"], "arguments": args})
                return content, assembled, api_usage
            except InterruptedError:
                raise
            except Exception as e:
                last_exc = e
                if not self._is_retryable_error(e) or attempt == attempts - 1:
                    break
                # The UI may still hold a partial stream from this attempt;
                # close it before the next request rather than appending a
                # second response to the same panel.
                if self.on_stream_end:
                    self.on_stream_end("", False)
                time.sleep(self._retry_delay(attempt))

        raise APIRequestError(self._format_api_error(last_exc)) from None

    def step(self, user_message: Optional[str] = None, image_url: Optional[str] = None) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        self._check_interrupt()
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
            # Keep completions as a separate user message immediately after
            # the real user message, preserving both the request and the
            # asynchronous notification in their natural order.
            if background_notifications:
                self.conversation_history.append({
                    "role": "user",
                    "content": (
                        "Background terminal notifications arrived since the previous turn. "
                        "Process them as context for the user's request:\n\n"
                        f"{background_notifications}"
                    ),
                })
        elif background_notifications:
            # If the agent is still in a tool-call loop, deliver completions
            # before the next API request as a synthetic user event.
            self.conversation_history.append({
                "role": "user",
                "content": (
                    "Background terminal notifications arrived while you were working. "
                    "Process them before taking your next step:\n\n"
                    f"{background_notifications}"
                ),
            })
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
        content, assembled_tool_calls, api_usage = self._stream_response()
        if api_usage:
            self.tokens.set_turn_from_api(api_usage["prompt_tokens"], api_usage["completion_tokens"])
        else:
            self.tokens.add_output_tokens(content)
        assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content or None}
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
        tool_results = []
        tool_image_data_urls = []
        for tc in assembled_tool_calls:
            self._check_interrupt()
            func_name = tc["name"]
            func_args = tc["arguments"]
            if self.on_tool_call:
                self.on_tool_call(func_name, func_args)
            result_json = self._execute_tool(func_name, func_args)
            result_dict = json.loads(result_json)
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
            tool_msg = {
                "tool_call_id": tc["id"],
                "role": "tool",
                "name": func_name,
                "content": text_content,
            }
            tool_results.append(tool_msg)
        self.conversation_history.extend(tool_results)
        if tool_image_data_urls:
            image_parts = [{"type": "text", "text": "[Screenshot captured \u2014 the image below shows the current browser page]"}]
            for img_url in tool_image_data_urls:
                image_parts.append({"type": "image_url", "image_url": {"url": img_url}})
            self.conversation_history.append({"role": "user", "content": image_parts})
        self._truncate_history_if_needed()
        self.tokens.finish_turn()
        if self.on_token_update:
            self.on_token_update(self.tokens)
        return None, assembled_tool_calls

    def run(self, user_message: str, image_url: Optional[str] = None) -> Optional[str]:
        self._set_processing(True)
        # A request may start after background commands completed while idle.
        # Mark it processing before draining so those queued notices are shown.
        self._interrupt_event.clear()
        current = user_message
        _first_image = image_url
        max_empty_retries = 2
        empty_retry_count = 0
        try:
            while True:
                if self._should_stop():
                    return "[Stopped \u2014 waiting for your input]"
                # Check before every API step, not only before the initial
                # user message. A tool-call loop sets ``current`` to None
                # after its first step, so restricting this check to
                # ``current is not None`` delayed compaction until the next
                # user request.
                pending_user = self._make_user_message(current, _first_image)
                self._refresh_context_tokens(pending_user)
                compact_msg = self.auto_compact_if_needed()
                if compact_msg and self.on_compact:
                    self.on_compact(compact_msg)
                response, tool_calls = self.step(current, image_url=_first_image)
                _first_image = None
                current = None
                word_count = len(response.split()) if response else 0
                if not tool_calls and word_count == 0:
                    # Response is empty or too short (≤2 words) with no tool calls — retry
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
                    return response
        except InterruptedError:
            return "[Interrupted]"
        finally:
            self._set_processing(False)

    def reset(self):
        self._setup_system_prompt()
        self.tokens = TokenCounter(self.model)
        if self.browser_manager.is_open:
            try:
                self.browser_manager.close()
            except Exception:
                pass

    def get_history(self) -> List[Dict[str, Any]]:
        return list(self.conversation_history)
