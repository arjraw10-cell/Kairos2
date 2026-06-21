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
    ReadTool,
    WriteTool,
    EditTool,
    NewTerminalTool,
    ExecuteCommandTool,
    ReadLogsTool,
    CloseTerminalTool,
    GetTerminalInfoTool,
    SearchTool,
    GitTool,
    SubAgentTool,
    BrowserLaunchTool,
    BrowserNavigateTool,
    BrowserClickTool,
    BrowserTypeTool,
    BrowserSelectTool,
    BrowserSnapshotTool,
    BrowserScreenshotTool,
    BrowserTabListTool,
    BrowserTabSwitchTool,
    BrowserTabOpenTool,
    BrowserEvaluateTool,
    BrowserCloseTool,
)


class Agent:
    MAX_HISTORY_MESSAGES = 100

    def __init__(self, workspace: str):
        self.client = OpenAI(
            api_key=Config.OPENAI_API_KEY(),
            base_url=Config.OPENAI_BASE_URL(),
        )
        self.model = Config.OPENAI_MODEL()
        self.terminal_manager = TerminalManager()
        self._interrupt_event = threading.Event()
        self._stop_requested = False

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
        self.browser_type_tool = BrowserTypeTool(self.browser_manager)
        self.browser_select_tool = BrowserSelectTool(self.browser_manager)
        self.browser_snapshot_tool = BrowserSnapshotTool(self.browser_manager)
        self.browser_screenshot_tool = BrowserScreenshotTool(self.browser_manager)
        self.browser_tab_list_tool = BrowserTabListTool(self.browser_manager)
        self.browser_tab_switch_tool = BrowserTabSwitchTool(self.browser_manager)
        self.browser_tab_open_tool = BrowserTabOpenTool(self.browser_manager)
        self.browser_evaluate_tool = BrowserEvaluateTool(self.browser_manager)
        self.browser_close_tool = BrowserCloseTool(self.browser_manager)

        # Callbacks wired from CLI
        self.on_tool_call: Optional[Callable[[str, dict], None]] = None
        self.on_stream_start: Optional[Callable[[], None]] = None
        self.on_stream_token: Optional[Callable[[str], None]] = None
        self.on_stream_end: Optional[Callable[[str, bool], None]] = None
        self.on_token_update: Optional[Callable[[TokenCounter], None]] = None
        self.on_compact: Optional[Callable[[str], None]] = None

        self.conversation_history: List[Dict[str, Any]] = []
        self._setup_system_prompt()

    # ------------------------------------------------------------------ #
    #  System prompt                                                       #
    # ------------------------------------------------------------------ #

    def _setup_system_prompt(self):
        base = (
            "You are Kairos, a coding agent. You operate in a filesystem and can read, write, and edit files, execute terminal commands, search codebases, inspect version control, and browse the web.\n\n"
            "You think step-by-step. Before making changes, you read the relevant files to understand the current state. After making changes, you verify they work. When something fails, you read the error carefully and adjust.\n\n"
            "You have absolute access to the filesystem. All file paths must be absolute (e.g., C:/Users/me/project/main.py or /home/me/project/main.py). You are not sandboxed \u2014 you can read any file you have permission to, and write to any location you have permission to.\n\n"
            "You have 24 tools. Each tool either succeeds and returns output, or fails and returns an error message. When a tool fails, the error tells you exactly what went wrong \u2014 use that information to fix your approach. Never retry the exact same call that just failed without changing something.\n\n Whenver the user asks you to look at a project, it usually has an AGENTS.md file and a README.md file. You should use these files to understand the project and the codebase, and ALWAYS follow the instructions mentioned in the AGENTS.md files. Make sure to look for this file in any projects the user points you towards. The AGENTS.md will automatically be injected into your system prompt in the directory the user starts in, but if they point you towards a different directory, you should look for the AGENTS.md file in that directory."
            "## Browser Tools\n"
            "You can browse the web using browser tools. The workflow is:\n"
            "1. `browser_launch` \u2014 start the browser (optionally with a named profile for persistent sessions)\n"
            "2. `browser_navigate` \u2014 go to a URL\n"
            "3. `browser_snapshot` \u2014 observe the page (shows interactive elements with their CSS selectors)\n"
            "4. `browser_click` / `browser_type` / `browser_select` \u2014 interact with elements\n"
            "5. `browser_screenshot` \u2014 capture visual screenshot (saves to ~/.kairos/screenshots/, read the file to view)\n"
            "6. `browser_tab_open` / `browser_tab_switch` / `browser_tab_list` \u2014 manage multiple tabs\n"
            "7. `browser_close` \u2014 shut down when done\n\n"
            "Always use `browser_snapshot` after navigating or interacting to see the updated page state. "
            "Use `browser_screenshot` when you need visual verification. "
            "Use named profiles (e.g. profile=\"Arjun\") to keep logins and cookies across sessions.\n\n"
            f"## Workspace\nYour current workspace is: {self.cwd}"
        )

        # Auto-load AGENTS.md from the workspace root if present
        agents_md = ""
        agents_md_path = self.cwd / "AGENTS.md"
        try:
            if agents_md_path.is_file():
                agents_md = agents_md_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass  # best-effort; don't break the prompt if the file can't be read

        if agents_md:
            base += (
                "\n\n## AGENTS.md (Architecture Documentation)\n"
                "Below is the AGENTS.md file from your workspace root. This is your complete reference for "
                "the codebase you are working on. It contains the full project structure, file-by-file "
                "descriptions, class signatures, method details, and design patterns. "
                "Use this as your primary knowledge source \u2014 it is injected into your system prompt so you "
                "always have full context of the codebase without needing to read every file.\n\n"
                "Follow any instructions or conventions described in it. "
                "If you make code changes, remember to also update this AGENTS.md and README.md.\n\n"
                f"{agents_md}"
            )

        self.system_prompt = base
        self.conversation_history = [{"role": "system", "content": self.system_prompt}]

    # ------------------------------------------------------------------ #
    #  Tool schema for OpenAI                                              #
    # ------------------------------------------------------------------ #

    def _get_tool_schema(self) -> List[Dict[str, Any]]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": (
                        "Read the contents of a file at the given absolute path. "
                        "Returns the full text, or an error if the file doesn't exist, isn't a regular file, or is too large (text >100KB, images >20MB). "
                        "Supports image files (png, jpg, gif, webp, bmp, tiff, svg) — images are returned as visual data you can analyze. "
                        "Use this before editing to understand the current state. Use this after writing to verify correctness. "
                        "If you get 'File not found', check the path. If you get 'Not a file', you're pointing at a directory — use execute_command with 'ls' or 'dir'."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute file path",
                            }
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write",
                    "description": (
                        "Create or overwrite a file at the given absolute path with the provided content. "
                        "Parent directories are created automatically. Written as UTF-8. "
                        "Use this to create new files or replace entire contents. If you only need to change part of a file, use edit instead — it's safer because it won't accidentally delete content."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute file path",
                            },
                            "content": {
                                "type": "string",
                                "description": "File content",
                            },
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit",
                    "description": (
                        "Strict find-and-replace on a file. oldText must appear exactly ONCE. "
                        "If zero matches: fails with info about what was found. If multiple matches: fails with line numbers of each. "
                        "Before editing, read the file to find the exact text. Copy it precisely including whitespace. "
                        "After editing, read the file to verify. If edit fails because of multiple matches, use a longer snippet with surrounding context."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute file path",
                            },
                            "oldText": {
                                "type": "string",
                                "description": "Exact text to find (must appear exactly once)",
                            },
                            "newText": {
                                "type": "string",
                                "description": "Replacement text",
                            },
                        },
                        "required": ["path", "oldText", "newText"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": (
                        "Search file contents using regular expressions, like ripgrep. "
                        "Returns matches with file paths, line numbers, and matching lines. "
                        "Skips binary files and common non-source directories (.git, node_modules, __pycache__, .venv). "
                        "Use this to find where a function is defined, where a variable is used, or to understand codebase structure. "
                        "Search is your primary tool for discovery — use it before reading files when you don't know what to read yet."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "Regex pattern to match against file contents",
                            },
                            "path": {
                                "type": "string",
                                "description": "Directory to search in (defaults to cwd)",
                            },
                            "include": {
                                "type": "string",
                                "description": "Filename glob filter (e.g. '*.py', '*.{ts,tsx}')",
                            },
                            "max_results": {
                                "type": "integer",
                                "description": "Max matches to return (default 50)",
                            },
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "git",
                    "description": (
                        "Run git commands in the workspace. "
                        "Sub-commands: status (modified/staged files), diff (actual changes, optionally scoped to a file), "
                        "log (recent commits, default 10), commit (stage all + commit with message), branch (list branches). "
                        "Use status and diff before making changes. Use log to understand recent history."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "Git sub-command: status, diff, log, commit, branch",
                            },
                            "path": {
                                "type": "string",
                                "description": "File path for diff (optional)",
                            },
                            "count": {
                                "type": "integer",
                                "description": "Number of log entries (default 10)",
                            },
                            "message": {
                                "type": "string",
                                "description": "Commit message (required for commit)",
                            },
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "new_terminal",
                    "description": (
                        "Create a new terminal session. Returns a terminal ID. "
                        "Background (true): stays open, output accumulates in a buffer. Use for long-running processes or multiple sequential commands. "
                        "Blocking (false): runs one command and closes. You must provide a timeout to prevent hanging. Use for quick isolated commands."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "background": {
                                "type": "boolean",
                                "description": "True=persistent shell, False=one-shot",
                            }
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
                        "Execute a shell command in a terminal. "
                        "For background terminals: command is sent to stdin, output appears in log buffer (read with read_logs). "
                        "For blocking terminals: command runs and returns all output (stdout+stderr) when done or timed out. "
                        "is_background must match the terminal type. Timeout is required for blocking terminals."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {
                                "type": "integer",
                                "description": "Terminal ID from new_terminal",
                            },
                            "command": {
                                "type": "string",
                                "description": "Shell command to execute",
                            },
                            "timeout": {
                                "type": "integer",
                                "description": "Seconds before kill (required for blocking)",
                            },
                            "is_background": {
                                "type": "boolean",
                                "description": "Must match terminal type",
                            },
                        },
                        "required": ["terminal_id", "command", "is_background"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_logs",
                    "description": (
                        "Read output from a background terminal by line number range. "
                        "Lines accumulate across commands — use start_line to skip old output. "
                        "If start_line exceeds available lines, the terminal hasn't produced that much output yet."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {
                                "type": "integer",
                                "description": "Background terminal ID",
                            },
                            "start_line": {
                                "type": "integer",
                                "description": "First line to read (1-indexed)",
                            },
                            "end_line": {
                                "type": "integer",
                                "description": "Last line (optional, defaults to end)",
                            },
                        },
                        "required": ["terminal_id", "start_line"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "close_terminal",
                    "description": (
                        "Close a terminal and release its resources. The process is terminated. "
                        "Always close terminals you're done with to avoid resource leaks."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {
                                "type": "integer",
                                "description": "Terminal ID to close",
                            }
                        },
                        "required": ["terminal_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_terminal_info",
                    "description": (
                        "Get info about a terminal: ID, background/blocking, closed status, line count. "
                        "Use to check if a background terminal has output before reading logs."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {
                                "type": "integer",
                                "description": "Terminal ID",
                            }
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
                        "Spawn a sub-agent to work on a task autonomously. "
                        "The sub-agent has all the same tools (read, write, edit, search, git, terminal) "
                        "but operates in its own conversation context. "
                        "Blocking mode waits for completion; non-blocking mode returns an ID to poll later."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "description": "The task / instruction to give the sub-agent",
                            },
                            "mode": {
                                "type": "string",
                                "description": "'blocking' (default) or 'non-blocking'",
                            },
                        },
                        "required": ["prompt"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_subagent_result",
                    "description": (
                        "Retrieve the result of a non-blocking sub-agent. "
                        "Returns the sub-agent's response when done, or 'Running...' if still in progress."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "subagent_id": {
                                "type": "string",
                                "description": "The ID returned by spawn_subagent",
                            },
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
                    "description": (
                        "Launch a stealth browser. Returns an error if browser dependencies are not installed "
                        "(pip install playwright && playwright install chromium, or pip install cloakbrowser). "
                        "Use profile to launch with a persistent named profile (cookies, localStorage, cache survive across sessions). "
                        "Without profile, creates an ephemeral session. "
                        "Use humanize=True for human-like mouse/keyboard behavior to pass bot detection. "
                        "Use proxy for HTTP/SOCKS5 proxy. "
                        "Close the existing browser first if one is already running."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "profile": {
                                "type": "string",
                                "description": "Named persistent profile (e.g. 'Arjun', 'work'). Cookies and state persist across sessions under ~/.kairos/profiles/<name>. Omit for ephemeral session.",
                            },
                            "proxy": {
                                "type": "string",
                                "description": "Proxy server URL, e.g. 'http://user:pass@proxy:8080' or 'socks5://user:pass@proxy:1080'",
                            },
                            "humanize": {
                                "type": "boolean",
                                "description": "Enable human-like mouse curves, typing timing, and scroll behavior (requires cloakbrowser).",
                            },
                            "chrome_profile": {
                                "type": "string",
                                "description": "Path to a real Chrome user data directory or profile folder to copy (e.g. 'C:\\Users\\me\\AppData\\Local\\Google\\Chrome\\User Data\\Default'). Copies the profile so your real Chrome isn't affected. Chrome MUST be closed when using this.",
                            },
                            "connect_cdp": {
                                "type": "string",
                                "description": "Connect to an already-running Chrome via CDP. Pass the debugging URL, e.g. 'http://localhost:9222'. Launch Chrome yourself with: chrome --remote-debugging-port=9222. This uses your REAL browser with all logins, extensions, cookies — no copying needed.",
                            },
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
                            "url": {
                                "type": "string",
                                "description": "URL to navigate to (include https://)",
                            }
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_click",
                    "description": (
                        "Click an element on the page. Accepts CSS selectors (#id, .class, tag), "
                        "text selectors (text=Button Text), or plain text that will be matched as a fallback. "
                        "Use browser_snapshot first to see available elements and their selectors."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {
                                "type": "string",
                                "description": "CSS selector, text selector, or visible text to click",
                            }
                        },
                        "required": ["selector"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_type",
                    "description": (
                        "Type text into an input, textarea, or other editable element. "
                        "Clears existing content first, then types character by character with a small delay. "
                        "Use browser_snapshot first to find the input's selector."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {
                                "type": "string",
                                "description": "CSS selector or placeholder text of the input field",
                            },
                            "text": {
                                "type": "string",
                                "description": "Text to type",
                            },
                            "press_enter": {
                                "type": "boolean",
                                "description": "Press Enter after typing (useful for search boxes)",
                            },
                        },
                        "required": ["selector", "text"],
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
                            "selector": {
                                "type": "string",
                                "description": "CSS selector of the <select> element",
                            },
                            "value": {
                                "type": "string",
                                "description": "Value attribute of the option to select",
                            },
                        },
                        "required": ["selector", "value"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_snapshot",
                    "description": (
                        "Get a compact text representation of the current page. Shows the page title, URL, "
                        "all interactive elements (links, buttons, inputs, selects) with their text and CSS selectors, "
                        "headings, key text content, and current form state. "
                        "This is your PRIMARY way to observe a page \u2014 use it after navigating or interacting "
                        "to see what's on the page and get selectors for your next action."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_screenshot",
                    "description": (
                        "Capture a visual screenshot of the current page. "
                        "Saved to ~/.kairos/screenshots/ and the file path is returned. "
                        "Use when you need to verify visual layout, "
                        "see images, or when the text snapshot isn't enough. "
                        "NOTE: do NOT use this tool — it saves a file but does not return "
                        "image data to the model. Use browser_snapshot instead for page inspection."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "full_page": {
                                "type": "boolean",
                                "description": "Capture the entire scrollable page (default false: viewport only)",
                            }
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_tab_list",
                    "description": "List all open browser tabs with their index, title, and URL. The active tab is marked with *.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
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
                            "index": {
                                "type": "integer",
                                "description": "Tab index (0-based, from browser_tab_list)",
                            },
                            "url_pattern": {
                                "type": "string",
                                "description": "Switch to the first tab whose URL contains this text",
                            },
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
                            "url": {
                                "type": "string",
                                "description": "URL to open in the new tab (optional)",
                            }
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_evaluate",
                    "description": (
                        "Execute JavaScript in the current page and return the result. "
                        "Use for advanced interactions, reading page data, or anything not covered by the other browser tools."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "expression": {
                                "type": "string",
                                "description": "JavaScript expression or function body to evaluate",
                            }
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
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            },
        ]

        if self._is_subagent:
            tools = [t for t in tools if t["function"]["name"] not in (
                "spawn_subagent", "get_subagent_result",
                "browser_launch", "browser_navigate", "browser_click",
                "browser_type", "browser_select", "browser_snapshot",
                "browser_screenshot", "browser_tab_list", "browser_tab_switch",
                "browser_tab_open", "browser_evaluate", "browser_close",
            )]
        return tools

    # ------------------------------------------------------------------ #
    #  Tool execution                                                      #
    #  Tool execution                                                      #
    # ------------------------------------------------------------------ #

    def _execute_tool(self, name: str, args: Dict[str, Any]) -> str:
        """Execute a tool and return the result as a JSON string."""
        dispatch = {
            "read": lambda a: self.read_tool(a["path"]).to_dict(),
            "write": lambda a: self.write_tool(a["path"], a["content"]).to_dict(),
            "edit": lambda a: self.edit_tool(
                a["path"], a["oldText"], a["newText"]
            ).to_dict(),
            "search": lambda a: self.search_tool(
                a["pattern"], a.get("path"), a.get("include"), a.get("max_results", 50)
            ).to_dict(),
            "git": lambda a: self.git_tool(
                a["command"],
                path=a.get("path"),
                count=a.get("count", 10),
                message=a.get("message", ""),
            ).to_dict(),
            "new_terminal": lambda a: self.new_terminal_tool(a["background"]).to_dict(),
            "execute_command": lambda a: self.execute_command_tool(
                a["terminal_id"], a["command"], a.get("timeout"), a.get("is_background")
            ).to_dict(),
            "read_logs": lambda a: self.read_logs_tool(
                a["terminal_id"], a["start_line"], a.get("end_line")
            ).to_dict(),
            "close_terminal": lambda a: self.close_terminal_tool(
                a["terminal_id"]
            ).to_dict(),
            "get_terminal_info": lambda a: self.get_terminal_info_tool(
                a["terminal_id"]
            ).to_dict(),
            "spawn_subagent": lambda a: self.subagent_tool.spawn(
                a["prompt"], a.get("mode", "blocking")
            ).to_dict(),
            "get_subagent_result": lambda a: self.subagent_tool.get_result(
                a["subagent_id"]
            ).to_dict(),
            # Browser tools
            "browser_launch": lambda a: self.browser_launch_tool(
                profile=a.get("profile"),
                headless=False,
                proxy=a.get("proxy"),
                humanize=a.get("humanize", False),
                chrome_profile=a.get("chrome_profile"),
                connect_cdp=a.get("connect_cdp"),
            ).to_dict(),
            "browser_navigate": lambda a: self.browser_navigate_tool(a["url"]).to_dict(),
            "browser_click": lambda a: self.browser_click_tool(a["selector"]).to_dict(),
            "browser_type": lambda a: self.browser_type_tool(
                a["selector"], a["text"], press_enter=a.get("press_enter", False)
            ).to_dict(),
            "browser_select": lambda a: self.browser_select_tool(
                a["selector"], a["value"]
            ).to_dict(),
            "browser_snapshot": lambda a: self.browser_snapshot_tool().to_dict(),
            "browser_screenshot": lambda a: self.browser_screenshot_tool(
                full_page=a.get("full_page", False)
            ).to_dict(),
            "browser_tab_list": lambda a: self.browser_tab_list_tool().to_dict(),
            "browser_tab_switch": lambda a: self.browser_tab_switch_tool(
                index=a.get("index"), url_pattern=a.get("url_pattern")
            ).to_dict(),
            "browser_tab_open": lambda a: self.browser_tab_open_tool(
                url=a.get("url")
            ).to_dict(),
            "browser_evaluate": lambda a: self.browser_evaluate_tool(
                a["expression"]
            ).to_dict(),
            "browser_close": lambda a: self.browser_close_tool().to_dict(),
        }

        if name not in dispatch:
            return json.dumps(
                {"success": False, "output": "", "error": f"Unknown tool: {name}"}
            )

        try:
            result = dispatch[name](args)
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"success": False, "output": "", "error": str(e)})

    # ------------------------------------------------------------------ #
    #  Tool call summary for CLI                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _tool_summary(name: str, args: Dict[str, Any]) -> str:
        if name == "read":
            return f"read file: {args.get('path', '?')}"
        if name == "write":
            return f"wrote file: {args.get('path', '?')}"
        if name == "edit":
            return f"edited file: {args.get('path', '?')}"
        if name == "search":
            return f"search: '{args.get('pattern', '?')}' in {args.get('path', 'cwd')}"
        if name == "git":
            cmd = args.get("command", "?")
            if cmd == "commit":
                return f"git commit: {args.get('message', '')[:40]}"
            return f"git {cmd}"
        if name == "new_terminal":
            kind = "background" if args.get("background") else "blocking"
            return f"opened {kind} terminal"
        if name == "execute_command":
            return (
                f"terminal {args.get('terminal_id', '?')}: {args.get('command', '?')}"
            )
        if name == "read_logs":
            return f"read terminal {args.get('terminal_id', '?')} logs"
        if name == "close_terminal":
            return f"closed terminal {args.get('terminal_id', '?')}"
        if name == "get_terminal_info":
            return f"info on terminal {args.get('terminal_id', '?')}"
        if name == "spawn_subagent":
            mode = args.get("mode", "blocking")
            prompt_preview = args.get("prompt", "")[:40]
            return f"spawn {mode} subagent: {prompt_preview}"
        if name == "get_subagent_result":
            return f"get subagent result: {args.get('subagent_id', '?')}"
        # Browser tool summaries
        if name == "browser_launch":
            profile = args.get("profile")
            chrome = args.get("chrome_profile")
            cdp = args.get("connect_cdp")
            if cdp:
                return f"connect to Chrome via CDP: {cdp}"
            if chrome:
                return f"launch with Chrome profile: {chrome}"
            return f"launch browser" + (f" (profile: {profile})" if profile else "")
        if name == "browser_navigate":
            return f"navigate to: {args.get('url', '?')}"
        if name == "browser_click":
            return f"click: {args.get('selector', '?')}"
        if name == "browser_type":
            text_preview = (args.get("text", "") or "")[:30]
            return f"type into {args.get('selector', '?')}: \"{text_preview}\""
        if name == "browser_select":
            return f"select '{args.get('value', '?')}' in {args.get('selector', '?')}"
        if name == "browser_snapshot":
            return "snapshot current page"
        if name == "browser_screenshot":
            kind = "full page" if args.get("full_page") else "viewport"
            return f"screenshot ({kind})"
        if name == "browser_tab_list":
            return "list tabs"
        if name == "browser_tab_switch":
            idx = args.get("index")
            pattern = args.get("url_pattern")
            if idx is not None:
                return f"switch to tab {idx}"
            return f"switch to tab matching: {pattern or '?'}"
        if name == "browser_tab_open":
            url = args.get("url")
            return f"open new tab" + (f" -> {url}" if url else "")
        if name == "browser_evaluate":
            expr_preview = (args.get("expression", "") or "")[:40]
            return f"evaluate JS: {expr_preview}"
        if name == "browser_close":
            return "close browser"
        return name

    # ------------------------------------------------------------------ #
    #  Content helpers                                                     #
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    #  Conversation history                                                #
    # ------------------------------------------------------------------ #

    def _truncate_history_if_needed(self):
        if len(self.conversation_history) >= 1 + self.MAX_HISTORY_MESSAGES:
            system = self.conversation_history[0]
            self.conversation_history = [system] + self.conversation_history[
                -(self.MAX_HISTORY_MESSAGES) :
            ]

    # ------------------------------------------------------------------ #
    #  Compaction                                                          #
    # ------------------------------------------------------------------ #

    COMPACT_RESERVE_TOKENS = 16384  # tokens reserved for summary prompt + output
    COMPACT_KEEP_RECENT = 20000  # tokens of recent context to keep
    COMPACT_THRESHOLD_PCT = 80.0  # auto-compact when context exceeds this %

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

Keep each section concise. Preserve exact file paths, function names, and error messages."""

    COMPACT_UPDATE_PROMPT = """The messages above are NEW conversation messages to incorporate into the existing summary.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context
- UPDATE Progress: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages

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

Keep each section concise. Preserve exact file paths, function names, and error messages."""

    def _should_auto_compact(self) -> bool:
        """Check if context is getting too full and should be auto-compacted."""
        if self.tokens.context_window == 0:
            return False
        return self.tokens.context_pct >= self.COMPACT_THRESHOLD_PCT

    def _find_compact_boundary(self) -> int:
        """Find the message index where we should split: summarize [boundary:] and keep [:boundary].

        Walks backward from the end, accumulating token counts, and stops when
        we've kept approximately COMPACT_KEEP_RECENT tokens.
        """
        keep_tokens = self.COMPACT_KEEP_RECENT
        accumulated = 0

        # Walk backward from end, find a good cut point (must be at a user or assistant message)
        for i in range(len(self.conversation_history) - 1, 0, -1):
            msg = self.conversation_history[i]
            role = msg.get("role", "")

            # Count tokens for this message
            text = self._get_text_content(msg.get("content", "") or "")
            # Also count tool call arguments if present
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    text += func.get("name", "") + func.get("arguments", "")
            msg_tokens = (
                len(self.tokens._enc.encode(text)) + 4
            )  # +4 for message overhead
            accumulated += msg_tokens

            if accumulated >= keep_tokens:
                # Found our boundary — find the nearest user message at or after
                # this point.  The messages before it become the summary, and the
                # recent messages (starting with a user msg) stay in context.
                for j in range(i, len(self.conversation_history)):
                    if self.conversation_history[j].get("role") == "user":
                        return j
                # No user message at or after i — fall through to return min(2, ...)

        # Couldn't find a good boundary — keep first 2 messages at minimum
        return min(2, len(self.conversation_history) - 1)

    def _serialize_messages_for_summary(self, messages: list) -> str:
        """Serialize a slice of conversation history into readable text for summarization."""
        parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            raw_content = msg.get("content", "") or ""
            content = self._get_text_content(raw_content)

            if role == "system":
                continue  # Skip system prompts in summary input

            if role == "tool":
                # Tool results — include brief version
                tool_name = msg.get("name", "tool")
                # Truncate large outputs
                if len(content) > 500:
                    content = content[:500] + "... [truncated]"
                parts.append(f"[Tool result from {tool_name}]: {content}")
            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    calls_str = ", ".join(
                        tc.get("function", {}).get("name", "?") for tc in tool_calls
                    )
                    parts.append(
                        f"Assistant (called tools: {calls_str}): {content or '(no text)'}"
                    )
                else:
                    parts.append(f"Assistant: {content}")
            elif role == "user":
                parts.append(f"User: {content}")

        return "\n\n".join(parts)

    def _generate_summary(
        self, messages_to_summarize: list, previous_summary: Optional[str] = None
    ) -> str:
        """Use the LLM to generate a summary of old messages."""
        prompt = self._serialize_messages_for_summary(messages_to_summarize)

        if previous_summary:
            full_prompt = (
                f"<conversation>\n{prompt}\n</conversation>\n\n"
                f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
                f"{self.COMPACT_UPDATE_PROMPT}"
            )
        else:
            full_prompt = (
                f"<conversation>\n{prompt}\n</conversation>\n\n"
                f"{self.COMPACT_SUMMARY_PROMPT}"
            )

        # Use a separate, non-streaming call for summarization
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
        """Manually compact conversation history. Returns a status message."""
        history = self.conversation_history
        if len(history) <= 2:
            return "Nothing to compact — conversation is still short."

        boundary = self._find_compact_boundary()

        # Safety: ensure boundary always starts at a user message.
        # If _find_compact_boundary fell through, try finding any user msg.
        if boundary >= len(history) or history[boundary].get("role") != "user":
            for idx in range(2, len(history)):
                if history[idx].get("role") == "user":
                    boundary = idx
                    break
            else:
                return "Nothing to compact — couldn't find a user message boundary."

        to_summarize = history[1:boundary]  # Old messages to summarize (skip system at index 0)

        # Nothing old to summarize — boundary is right at the start
        if not to_summarize:
            return "Nothing to compact — all messages are recent."

        system_msg = history[0]

        # Check for existing compaction summary
        existing_summary = None
        if (
            len(history) > 1
            and history[1].get("role") == "system"
            and history[1].get("name") == "compaction"
        ):
            existing_summary = history[1].get("content")

        # Generate summary of the OLD messages
        summary = self._generate_summary(to_summarize, existing_summary)

        # Build new history: [system, compaction summary, recent messages]
        compaction_msg = {
            "role": "system",
            "name": "compaction",
            "content": f"[Conversation compacted — summary of prior history]\n\n{summary}",
        }

        recent = history[boundary:]  # Recent messages to keep as-is
        self.conversation_history = [system_msg, compaction_msg] + recent

        # Re-count tokens
        self.tokens.start_turn(self.conversation_history)
        self.tokens.finish_turn()

        return f"Compacted: summarized {len(to_summarize)} messages into checkpoint."

    def auto_compact_if_needed(self) -> Optional[str]:
        """Auto-compact if context is too full. Returns status message or None."""
        if not self._should_auto_compact():
            return None
        return self.compact()

    # ------------------------------------------------------------------ #
    #  Interrupt                                                           #
    # ------------------------------------------------------------------ #

    def interrupt(self):
        """Hard interrupt — aborts mid-step (Ctrl+C)."""
        self._interrupt_event.set()

    def _check_interrupt(self):
        if self._interrupt_event.is_set():
            self._interrupt_event.clear()
            raise InterruptedError("Interrupted by user")

    def request_stop(self):
        """Graceful stop — finishes current step, then stops (Escape).

        After the current API call + tool calls complete, run() will return
        instead of making another API call.
        """
        self._stop_requested = True

    def _should_stop(self) -> bool:
        """Check (and consume) the graceful-stop flag."""
        if self._stop_requested:
            self._stop_requested = False
            return True
        return False

    # ------------------------------------------------------------------ #
    #  Prepare messages for API (inject images into tool results)          #
    # ------------------------------------------------------------------ #

    def _prepare_messages_for_api(self) -> List[Dict[str, Any]]:
        """Convert conversation history to API format.

        IMPORTANT: Images are only sent in user messages (where OpenAI/OpenRouter
        support them). Tool messages with images have the image data stripped to
        avoid 400 errors from providers that don't support images in tool messages.
        """
        messages = []
        for msg in self.conversation_history:
            if msg.get("role") == "tool" and "_image_url" in msg:
                # Strip image data — tool messages must be text-only for
                # OpenRouter and most providers.  If the model needs to see
                # the image, it should come through a user message.
                messages.append(
                    {
                        "tool_call_id": msg["tool_call_id"],
                        "role": "tool",
                        "name": msg.get("name", ""),
                        "content": msg["content"],
                    }
                )
            else:
                messages.append(msg)
        return messages

    # ------------------------------------------------------------------ #
    #  Error formatting helpers                                            #
    # ------------------------------------------------------------------ #

    def _format_api_error(self, e: Exception) -> str:
        """Extract maximum detail from an OpenAI API exception."""
        lines = [f"OpenAI API Error: {type(e).__name__}\n"]

        if isinstance(e, APIStatusError):
            lines.append(f"  Status Code: {e.status_code}")
            lines.append(f"  Request ID:  {e.request_id or '(none)'}")

            # Response body — often contains the real error message
            body = getattr(e, "body", None)
            if body:
                if isinstance(body, dict):
                    # Pretty-print error details from the API
                    error_obj = body.get("error", body)
                    if isinstance(error_obj, dict):
                        lines.append(f"  Error Type:  {error_obj.get('type', '(none)')}")
                        lines.append(f"  Error Code:  {error_obj.get('code', '(none)')}")
                        lines.append(f"  Message:     {error_obj.get('message', str(body))}")
                        # Param is often useful
                        param = error_obj.get("param")
                        if param:
                            lines.append(f"  Param:       {param}")
                    else:
                        lines.append(f"  Body: {body}")
                else:
                    lines.append(f"  Body: {body}")

            # HTTP response text if available
            resp = getattr(e, "response", None)
            if resp is not None:
                text = getattr(resp, "text", None)
                if text and text != str(body):
                    lines.append(f"  Response:    {text[:500]}")

        elif isinstance(e, AuthenticationError):
            lines.append("  Hint: Check your OPENAI_API_KEY and OPENAI_BASE_URL in .env")
            body = getattr(e, "body", None)
            if body:
                lines.append(f"  Body: {body}")

        elif isinstance(e, RateLimitError):
            lines.append("  Hint: You are being rate-limited. Wait a moment and retry.")
            body = getattr(e, "body", None)
            if body:
                lines.append(f"  Body: {body}")

        elif isinstance(e, APIConnectionError):
            lines.append(f"  Hint: Could not connect to {self.client.base_url}")
            lines.append("  Check that the API server is running and reachable.")
            cause = getattr(e, "cause", None)
            if cause:
                lines.append(f"  Underlying: {cause}")

        else:
            # Generic exception — show everything we can
            lines.append(f"  Message: {str(e)}")
            body = getattr(e, "body", None)
            if body:
                lines.append(f"  Body: {body}")

        # Always show the model and base URL for context
        lines.append(f"\n  Config:")
        lines.append(f"    Model:    {self.model}")
        lines.append(f"    Base URL: {self.client.base_url}")

        return "\n".join(lines)

    @staticmethod
    def _is_retryable_error(e: Exception) -> bool:
        """Check if an error is transient and worth retrying."""
        if isinstance(e, (RateLimitError, APIConnectionError)):
            return True
        if isinstance(e, APIStatusError):
            return e.status_code in (500, 502, 503, 504)
        err = str(e).lower()
        return any(c in err for c in ["timeout", "connection", "network"])

    # ------------------------------------------------------------------ #
    #  OpenAI call with retry (non-streaming, for fallback)                #
    # ------------------------------------------------------------------ #

    def _call_openai_with_retry(self, max_retries: int = 3, base_delay: float = 1.0):
        last_exc = None
        for attempt in range(max_retries):
            try:
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=self._prepare_messages_for_api(),
                    tools=self._get_tool_schema(),
                    tool_choice="auto",
                )
            except Exception as e:
                last_exc = e
                if not self._is_retryable_error(e) or attempt == max_retries - 1:
                    break
                time.sleep(base_delay * (2**attempt) + 0.5 * attempt)

        # All retries failed — wrap with full details
        raise Exception(self._format_api_error(last_exc)) from last_exc

    # ------------------------------------------------------------------ #
    #  Streaming API call                                                  #
    # ------------------------------------------------------------------ #

    def _stream_response(
        self,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Stream a response from OpenAI. Yields content tokens via on_stream
        callback as they arrive. Returns (full_content, assembled_tool_calls).

        Each tool call is: {"id": str, "name": str, "arguments": dict}
        """
        max_retries = 3
        base_delay = 1.0
        last_exc = None

        stream = None
        for attempt in range(max_retries):
            try:
                stream = self.client.chat.completions.create(
                    model=self.model,
                    messages=self._prepare_messages_for_api(),
                    tools=self._get_tool_schema(),
                    tool_choice="auto",
                    stream=True,
                )
                break
            except Exception as e:
                last_exc = e
                if not self._is_retryable_error(e) or attempt == max_retries - 1:
                    break
                time.sleep(base_delay * (2**attempt) + 0.5 * attempt)

        if stream is None:
            # All retries failed — wrap with full details
            raise Exception(self._format_api_error(last_exc)) from last_exc

        # Notify that streaming has begun
        if self.on_stream_start:
            self.on_stream_start()

        content = ""
        tool_calls: Dict[int, Dict[str, str]] = {}  # index -> {id, name, arguments}

        for chunk in stream:
            self._check_interrupt()

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            if not delta:
                continue

            # Text content
            if delta.content:
                content += delta.content
                if self.on_stream_token:
                    self.on_stream_token(delta.content)

            # Tool call deltas
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

        # Assemble tool calls into the format the rest of the code expects
        assembled = []
        for idx in sorted(tool_calls.keys()):
            tc = tool_calls[idx]
            try:
                args = json.loads(tc["arguments"])
            except json.JSONDecodeError:
                args = {}
            assembled.append(
                {
                    "id": tc["id"],
                    "name": tc["name"],
                    "arguments": args,
                }
            )

        return content, assembled

    # ------------------------------------------------------------------ #
    #  Agent loop                                                          #
    # ------------------------------------------------------------------ #

    def step(
        self, user_message: Optional[str] = None, image_url: Optional[str] = None
    ) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """Run one step. Returns (response_text | None, tool_calls_made).

        If image_url is provided, the user message is sent as a vision content
        array so the model can see the image.
        """
        self._check_interrupt()

        if user_message is not None:
            if image_url:
                # Vision message: content array with text + image
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

        # Count input tokens for this turn
        self.tokens.start_turn(self.conversation_history)

        # Stream the response
        content, assembled_tool_calls = self._stream_response()

        # Accumulate output tokens from content
        self.tokens.add_output_tokens(content)

        # Build the assistant message for history
        assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": content or None,
        }

        # Notify stream end (handles display for both thinking and final response)
        if self.on_stream_end:
            self.on_stream_end(content, bool(assembled_tool_calls))

        if assembled_tool_calls:
            # Format tool calls for the API message
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]),
                    },
                }
                for tc in assembled_tool_calls
            ]

        self.conversation_history.append(assistant_msg)

        if not assembled_tool_calls:
            self.tokens.finish_turn()
            if self.on_token_update:
                self.on_token_update(self.tokens)
            return content, []

        # Execute tool calls
        tool_results = []
        for tc in assembled_tool_calls:
            self._check_interrupt()

            func_name = tc["name"]
            func_args = tc["arguments"]

            if self.on_tool_call:
                self.on_tool_call(func_name, func_args)

            result_json = self._execute_tool(func_name, func_args)

            # Parse result — discard image data since tool messages with
            # images cause 400 errors on OpenRouter and most providers.
            result_dict = json.loads(result_json)
            result_dict.pop("image_url", None)  # Discard — never safe in tool messages
            text_content = json.dumps(result_dict)

            tool_msg = {
                "tool_call_id": tc["id"],
                "role": "tool",
                "name": func_name,
                "content": text_content,
            }

            tool_results.append(tool_msg)

        self.conversation_history.extend(tool_results)
        self._truncate_history_if_needed()

        # Count output tokens from tool results (text only, not image data)
        for tr in tool_results:
            self.tokens.add_output_tokens(tr["content"])

        self.tokens.finish_turn()
        if self.on_token_update:
            self.on_token_update(self.tokens)

        return None, assembled_tool_calls

    def run(
        self,
        user_message: str,
        image_url: Optional[str] = None,
    ) -> Optional[str]:
        """Run the full loop until a final response, interrupt, or graceful stop.

        If image_url is provided, it is attached to the first user message as
        vision content (only the initial message, not follow-up tool loops).
        """
        self._interrupt_event.clear()
        current = user_message
        _first_image = image_url  # Only attach to the very first step

        max_empty_retries = 2  # Retry up to 2 times if agent returns no response
        empty_retry_count = 0

        try:
            while True:
                # Check for graceful stop (Escape) — only between steps,
                # not mid-step.  This lets any in-progress tool calls finish.
                if self._should_stop():
                    return "[Stopped — waiting for your input]"

                # Auto-compact if context is getting full
                if current is not None:
                    compact_msg = self.auto_compact_if_needed()
                    if compact_msg and self.on_compact:
                        self.on_compact(compact_msg)

                response, tool_calls = self.step(current, image_url=_first_image)
                _first_image = None  # Don't re-attach on subsequent steps
                current = None

                if response:
                    return response
                if not tool_calls:
                    # No response and no tool calls — retry the API call.
                    # Remove the empty assistant message so the history still
                    # ends with the user message (avoids the "Cannot have 2 or
                    # more assistant messages at the end of the list" 400 error).
                    if self.conversation_history and self.conversation_history[-1].get("role") == "assistant":
                        self.conversation_history.pop()
                    if empty_retry_count < max_empty_retries:
                        empty_retry_count += 1
                        if self.on_compact:
                            self.on_compact(
                                f"No response from API — retrying ({empty_retry_count}/{max_empty_retries})..."
                            )
                        continue
                    return "Agent returned without a response."
        except InterruptedError:
            return "[Interrupted]"

    def reset(self):
        self._setup_system_prompt()
        self.tokens = TokenCounter(self.model)
        # Close any open browser session
        if self.browser_manager.is_open:
            try:
                self.browser_manager.close()
            except Exception:
                pass

    def get_history(self) -> List[Dict[str, Any]]:
        return list(self.conversation_history)
