# Kairos

A minimal personal coding agent in Python.

## Features

- **24 Tools**: File operations, terminal management, search, git, sub-agents, and a full browser automation suite
- **Absolute Paths**: All file operations use absolute paths — no workspace restrictions
- **Streaming First**: Tokens print as they arrive; no waiting for full responses
- **Token Aware**: Session, context window, and per-turn token counts displayed after every exchange
- **Auto-Compaction**: Conversation history is automatically summarized when context usage exceeds 80%
- **Sub-Agents**: Spawn autonomous child agents to work on tasks in parallel
- **Browser Automation**: Full Playwright/CloakBrowser integration with stealth mode, persistent profiles, multi-tab, and CDP support
- **Paste System**: Ctrl+V pastes text, Alt+V pastes images. Creates visible tokens like `(Pasted Text #1)` or `(Pasted Image #1)`. Backspace removes the entire token and its content. No background polling or auto-detection — images are pasted explicitly via Alt+V.
- **Chat Persistence**: All sessions saved to `chats/chats.json` with auto-save every 60 seconds and on window close. Each session is tracked by a unique ID — no fuzzy matching that could clobber different sessions.
- **`/resume`**: Load previous chats via numbered picker
- **Animated Thinking**: "Thinking..." indicator with cycling dots
- **Streaming Display**: Real-time tokens in a live-updating panel (grey for thinking, green for final response)
- **Tool Summaries**: One-line display when tools are called (e.g., `read file: /path`)
- **Dual Interrupt**: Ctrl+C hard-interrupts mid-step; Escape gracefully stops after the current step finishes
- **OpenAI Compatible**: Works with any OpenAI-compatible API (OpenRouter, local models, etc.)
- **Clean CLI**: Terminal UI with `rich` and `prompt_toolkit`

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate    # Windows
# source .venv/bin/activate  # Linux/Mac

pip install -r requirements.txt
```

For browser automation, also install Playwright browsers:

```bash
playwright install chromium
```

## Configuration

1. Copy `.env.example` to `.env`
2. Edit `.env` with your credentials:

```
OPENAI_API_KEY=your_api_key_here
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o
```

Any OpenAI-compatible endpoint works — just change `OPENAI_BASE_URL`.

## Usage

```bash
python main.py                   # cwd as workspace
python main.py /path/to/project  # specific workspace
```

### REPL Commands

| Command | Description |
|---------|-------------|
| `Escape` | Stop after current step (finish tool calls, then wait for input) |
| `Ctrl+C` | Hard-interrupt (abort mid-step) |
| `Ctrl+V` | Paste text from clipboard |
| `Alt+V` | Paste image from clipboard |
| `/resume` | Load a saved chat |
| `/compact` | Manually compact conversation history |
| `/paste` | Info about paste functionality |
| `clear` | Clear the screen |
| `reset` | Save and reset conversation history |
| `exit` / `quit` / `q` | Save and exit |

## Tools

### File Tools

| Tool | Description |
|------|-------------|
| `read(path)` | Read file contents — supports images (png, jpg, gif, webp, bmp, tiff, svg) returned as vision data |
| `write(path, content)` | Write or create a file (parent directories created automatically) |
| `edit(path, oldText, newText)` | Strict find-and-replace — must match exactly once |

### Search & Git

| Tool | Description |
|------|-------------|
| `search(pattern, path?, include?, max_results?)` | Regex file content search (like ripgrep) — skips binary files and non-source directories |
| `git(command, **kwargs)` | Git operations: `status`, `diff`, `log`, `commit`, `branch` |

### Terminal Tools

| Tool | Description |
|------|-------------|
| `new_terminal(background)` | Create a terminal — `true` for persistent shell, `false` for one-shot |
| `execute_command(terminal_id, command, timeout?, is_background?)` | Run a command in a terminal |
| `read_logs(terminal_id, start_line, end_line?)` | Read output from a background terminal by line numbers |
| `close_terminal(terminal_id)` | Close a terminal and release resources |
| `get_terminal_info(terminal_id)` | Get terminal status (ID, type, closed status, line count) |

### Sub-Agent Tools

| Tool | Description |
|------|-------------|
| `spawn_subagent(prompt, mode?)` | Spawn an autonomous child agent — `blocking` (waits) or `non-blocking` (returns ID) |
| `get_subagent_result(subagent_id)` | Poll a non-blocking sub-agent for its result |

Sub-agents have access to file, search, git, and terminal tools but cannot spawn further sub-agents or use browser tools.

### Browser Tools

| Tool | Description |
|------|-------------|
| `browser_launch(profile?, proxy?, humanize?, chrome_profile?, connect_cdp?)` | Launch a browser (Playwright or CloakBrowser stealth) |
| `browser_navigate(url)` | Navigate to a URL |
| `browser_click(selector)` | Click an element (CSS selector, text, or label — auto-fallback for hidden inputs like radio buttons) |
| `browser_type(selector, text, press_enter?)` | Type into an input field |
| `browser_select(selector, value)` | Select a dropdown option (tries by value, then visible label text, then index) |
| `browser_snapshot()` | Get a compact text representation of the page (interactive elements, select options, radio labels, form state) |
| `browser_screenshot(full_page?)` | Capture a screenshot (saved to `~/.kairos/screenshots/`) |
| `browser_tab_list()` | List all open tabs |
| `browser_tab_switch(index?, url_pattern?)` | Switch tabs by index or URL pattern |
| `browser_tab_open(url?)` | Open a new tab |
| `browser_evaluate(expression)` | Execute JavaScript in the page |
| `browser_close()` | Close the browser and clean up |

Browser features:
- **Persistent profiles**: Cookies, localStorage, and cache survive across sessions (`~/.kairos/profiles/`)
- **CloakBrowser**: Stealth Chromium with fingerprint patches when installed (`pip install cloakbrowser`)
- **CDP mode**: Connect to an already-running Chrome instance (`chrome --remote-debugging-port=9222`)
- **Chrome profile copy**: Import your real Chrome profile (cookies, logins, history)
- **Human-like mode**: Realistic mouse/keyboard/scroll behavior for bot detection
- **Smart form interaction**: Hidden radio/checkbox inputs are captured with their label text; click auto-falls back to label/JS for hidden elements; select dropdowns show available options with both display text and value attributes

## Architecture

```
main.py                     # Entry point
kairos/
├── main.py                 # CLI REPL loop, signal handlers, auto-save
├── config.py               # Lazy .env loading (OPENAI_API_KEY, BASE_URL, MODEL)
├── agent.py                # Core agent loop, streaming, compaction, tool dispatch
├── cli.py                  # Terminal UI (streaming panels, thinking dots, paste handling)
├── tokens.py               # Token counting with tiktoken (session/context/turn)
├── terminal_manager.py     # Terminal lifecycle (background shells, blocking subprocesses)
├── browser_manager.py      # Playwright/CloakBrowser lifecycle in a dedicated worker thread
└── tools/
    ├── base.py             # ToolResult class
    ├── read.py             # Read file (text + images)
    ├── write.py            # Write/create file
    ├── edit.py             # Strict find-and-replace
    ├── search.py           # Regex file search (ripgrep-like)
    ├── git.py              # Git subcommand dispatcher
    ├── terminal.py         # Terminal tool wrappers
    ├── subagent.py         # Sub-agent spawn and tracking
    ├── browser.py          # Browser tool wrappers
    └── session.py          # Chat save/load manager
```

## Key Concepts

### Streaming

Tokens are streamed in real-time. During the agent's reasoning phase, a grey panel updates live. When a final response is reached, it transitions to a green panel rendered as Markdown. Tool call thinking stays as a grey trace.

### Compaction

When the conversation context exceeds 80% of the context window, old messages are automatically summarized into a structured checkpoint (Goal, Progress, Key Decisions, Next Steps) and replaced. Recent context (~20k tokens) is preserved. You can also trigger this manually with `/compact`.

### AGENTS.md

Kairos auto-loads an `AGENTS.md` file from the workspace root into the system prompt. Use this to give the agent project-specific conventions, instructions, or context.

### Auto-Save

Chat history is saved:
- After every exchange
- Every 60 seconds in the background
- On SIGTERM / SIGINT / SIGHUP (window close, task kill)

## Design Principles

1. **Streaming First** — Tokens print as they arrive; no waiting for full response
2. **Absolute Paths** — No workspace containment, just use full paths
3. **One Tool Per File** — Easy to add new tools
4. **Interruptible** — Ctrl+C hard-interrupts; Escape gracefully stops between steps
5. **Token Aware** — Session, context, and turn token counts displayed
6. **Minimal Dependencies** — `openai`, `python-dotenv`, `rich`, `prompt_toolkit`, `tiktoken`, `playwright`

## License

MIT
