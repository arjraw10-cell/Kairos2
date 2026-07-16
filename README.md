# Kairos

A minimal personal coding agent in Python.

## Features

- **40 Tools**: File operations, terminal management, search, git, sub-agents, skills, and a comprehensive browser automation suite (30 browser tools including hover, drag, wait_for, select_index, scroll, wait, send_keys, search_page, find_elements, index-based click/type, go_back/go_forward/reload, and CDP cross-origin iframe support)
- **Absolute Paths**: All file operations use absolute paths — no workspace restrictions
- **Streaming First**: Tokens print as they arrive; no waiting for full responses
- **Token Aware**: Session, context window, and per-turn token counts displayed after every exchange. Uses ground-truth counts from the API when available (`stream_options={"include_usage": True}`), with tiktoken estimates as fallback
- **Auto-Compaction**: Conversation history is automatically summarized when context usage exceeds 80%
- **Sub-Agents**: Spawn autonomous child agents to work on tasks in parallel
- **Browser Automation**: Full Playwright/CloakBrowser integration with stealth mode, persistent profiles, multi-tab, and CDP support
- **Image Support (UI)**: Paste images with Ctrl+V, attach via file picker button, or drag-and-drop directly into the chat. Images are sent to the agent as vision data and displayed in the message history.
- **Markdown Rendering (UI)**: Full markdown support via `react-markdown` with GFM (tables, blockquotes, task lists, strikethrough), syntax highlighting (highlight.js/GitHub Dark), and copy-to-clipboard buttons on code blocks.
- **Background Session Notifications (UI)**: When a background session finishes while you're focused on another, a toast notification appears showing which session completed.
- **Skeleton Loading (UI)**: When loading a conversation from the sidebar, animated grey skeleton placeholders appear in the message area while the history is being fetched — giving instant visual feedback instead of a blank screen.
- **Unlocked Sidebar (UI)**: Sessions appear in the sidebar immediately after creation (even while the agent is running). You can switch between sessions without interrupting running agents — they continue in the background. Streaming indicators (pulsing dots) show on ALL concurrently running sessions, not just the focused one. Close button (×) to explicitly unload a session.
- **Chrome-Style Tabs (UI)**: Horizontal tab bar above the main content area shows currently loaded sessions, just like Chrome browser tabs. Each tab displays the session's first message as a label, with streaming indicators on active sessions. Close button on each tab unloads the session; "+" button opens a new thread without closing existing tabs. Middle-click to close a tab. Tabs auto-scroll to keep the active tab visible.
- **Skills**: Self-extensible skill system — agent can create/load skills stored as `SKILL.md` files in `skills/` directory
- **Paste System**: Text pastes are detected automatically via bracketed paste (modern terminals wrap pasted text in escape sequences, making it arrive as one atomic chunk). Alt+V pastes images from the clipboard. Creates visible tokens like `(Pasted Text #1)` or `(Pasted Image #1)`. Backspace removes the entire token and its content.
- **Chat Persistence**: All sessions saved to `chats/chats.json` with auto-save every 60 seconds and on window close. Each session is tracked by a unique ID — no fuzzy matching that could clobber different sessions. **Atomic writes** via temp-file + rename prevent corruption from interrupted saves. **Corruption recovery** auto-heals damaged files by parsing up to the last valid JSON boundary.
- **`/resume`**: Load previous chats via numbered picker — only resumes from completed agent responses (mid-execution chats with incomplete tool calls are rejected with a warning)
- **Animated Thinking**: "Thinking..." indicator with cycling dots
- **Streaming Display**: Real-time tokens in a live-updating panel (grey for thinking, green for final response)
- **Tool Summaries**: One-line display when tools are called (e.g., `read file: /path`)
- **Instant Interrupt**: Both Escape and Ctrl+C hard-interrupt immediately — including browser waits, navigations, and terminal commands. The agent aborts within 50ms at any checkpoint.
- **OpenAI Compatible**: Works with any OpenAI-compatible API (OpenRouter, local models, etc.)
- **Clean CLI**: Terminal UI with `rich` and `prompt_toolkit`
- **Gateway Architecture**: WebSocket server that hosts multiple Agent instances across different workspaces simultaneously. UI clients can run multiple concurrent sessions — switch focus without interrupting background agents. Streaming events are routed per-session so each conversation streams independently.

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

### Windows Shortcuts

| File | Description |
|------|-------------|
| `kairos.bat` | Starts the gateway + launches the **Electron UI** |
| `kairos_cli.bat` | Starts the gateway (if not already running) + launches the **CLI** |

`kairos_cli.bat` checks if the gateway is already listening on port 8765 before starting it. If the gateway is already running (e.g., from a previous `kairos.bat` launch), it skips startup and goes straight to the CLI. Any arguments are forwarded to the CLI (e.g., a workspace path).

### REPL Commands

| Command | Description |
|---------|-------------|
| `Escape` | Instant interrupt (aborts mid-step, mid-browser-wait, etc.) |
| `Ctrl+C` | Hard-interrupt (same as Escape — aborts immediately) |
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

Sub-agents have access to file, search, git, terminal, and skill tools but cannot spawn further sub-agents or use browser tools.

### Skill Tools

| Tool | Description |
|------|-------------|
| `list_skills()` | List all available skill names (from `skills/` directory) |
| `load_skill(skill_name)` | Load and return a skill's full `SKILL.md` content |
| `write_skill(skill_name, content, overwrite?)` | Create or update a skill's `SKILL.md` (default: refuses if exists; `overwrite=true` to replace) |

Skills are stored in `skills/<skill-name>/SKILL.md`. Only skill names are injected into the system prompt — full content is loaded on demand via `load_skill`. The agent can write its own skills with `write_skill`.

### Browser Tools

| Tool | Description |
|------|-------------|
| `browser_launch(profile?, proxy?, humanize?, chrome_profile?, connect_cdp?)` | Launch a browser (Playwright or CloakBrowser stealth) |
| `browser_navigate(url)` | Navigate to a URL. Always auto-screenshots + auto-snapshots (navigation = significant change). |
| `browser_go_back()` | Navigate back in browser history. Auto-snapshots if page changed significantly. |
| `browser_go_forward()` | Navigate forward in browser history. Auto-snapshots if page changed significantly. |
| `browser_reload()` | Reload the current page. Auto-snapshots on reload. |
| `browser_click(selector)` | Click an element (CSS selector, text, or label — auto-fallback chain). Verifies post-click. Auto-detects new tabs. Smart auto-snapshot on significant DOM changes. |
| `browser_click_index(index)` | **Click by snapshot index [0],[1]...** — PREFERRED, most reliable method. Smart auto-snapshot. |
| `browser_type(selector, text, press_enter?)` | Type into an input field. Verifies by reading back value. Smart auto-snapshot. |
| `browser_type_index(index, text, press_enter?)` | **Type by snapshot index** — PREFERRED over selector-based. Smart auto-snapshot. |
| `browser_select(selector, value)` | Select a dropdown option (by value, label, index, or JS fallback). Verifies. Smart auto-snapshot. |
| `browser_select_index(index, value)` | **Select by snapshot index** — PREFERRED over selector-based. Validates target is `<select>`. Smart auto-snapshot. |
| `browser_scroll(direction?, pages?)` | Scroll up/down by viewport heights (default 1.0 = full viewport). Smart auto-snapshot. |
| `browser_wait(seconds?)` | Wait for animations/AJAX to complete (max 30s). Smart auto-snapshot. |
| `browser_wait_for(selector?, text?, timeout?)` | Wait for a specific element to become visible or text to appear — more efficient than blind waiting. Smart auto-snapshot. |
| `browser_send_keys(keys)` | Send keyboard shortcuts (Enter, Tab, Control+a, ArrowDown, etc.). Smart auto-snapshot. |
| `browser_search_page(pattern, regex?, case_sensitive?, max_results?)` | **Grep the live page** for text patterns — zero LLM cost, instant results |
| `browser_find_elements(selector, max_results?)` | **Query DOM by CSS selector** — zero LLM cost, instant element listing |
| `browser_snapshot()` | Get a compact text representation of the page with element indices, CSS selectors, headings, text, form state, and cross-origin iframe content |
| `browser_screenshot(full_page?)` | Capture a screenshot (saved to `~/.kairos/screenshots/` and returned as vision data) |
| `browser_tab_list()` | List all open tabs |
| `browser_tab_switch(index?, url_pattern?)` | Switch tabs by index or URL pattern |
| `browser_tab_open(url?)` | Open a new tab |
| `browser_evaluate(expression)` | Execute JavaScript in the page |
| `browser_close()` | Close the browser and clean up |
| `browser_click_xy(x, y)` | Click at absolute viewport coordinates (vision-based fallback). Smart auto-snapshot. |
| `browser_hover(selector)` | Hover over an element to trigger hover states (dropdowns, tooltips, hover cards). Smart auto-snapshot. |
| `browser_hover_index(index)` | **Hover by snapshot index** — PREFERRED. Smart auto-snapshot. |
| `browser_drag(selector_from, selector_to)` | Drag an element to another element (for file uploads, sortable lists, Kanban boards). Smart auto-snapshot. |
| `browser_drag_xy(x1, y1, x2, y2)` | Drag from one coordinate to another. Smart auto-snapshot. |
| `browser_switch_frame(frame_selector?)` | Switch into an iframe (including cross-origin via CDP), or back to top-level |

Browser features:
- **Persistent profiles**: Cookies, localStorage, and cache survive across sessions (`~/.kairos/profiles/`)
- **CloakBrowser**: Stealth Chromium with fingerprint patches when installed (`pip install cloakbrowser`)
- **CDP mode**: Connect to an already-running Chrome instance (`chrome --remote-debugging-port=9222`)
- **Chrome profile copy**: Import your real Chrome profile (cookies, logins, history)
- **Human-like mode**: Realistic mouse/keyboard/scroll behavior for bot detection
- **Index-based interaction**: Snapshot shows element indices [0],[1],[2]... use `browser_click_index`/`browser_type_index` for reliable, selector-free interaction
- **Auto new-tab detection**: When a click opens a new tab (target="_blank"), automatically switches to it
- **Smart auto-snapshot**: All interaction tools automatically detect significant page changes via DOM fingerprinting (URL/title change, modals, new iframes, big DOM shifts) and append a snapshot + screenshot only when warranted — eliminating both the token waste of always-snapshotting and the blind spots of never-snapshotting
- **Smart form interaction**: Hidden radio/checkbox inputs are captured with their label text; click auto-falls back to label/JS for hidden elements; select dropdowns show available options with both display text and value attributes
- **Shadow DOM**: Snapshot pierces shadow roots to expose web component internals
- **Ancestor visibility**: Snapshot checks ALL ancestors for display/visibility/opacity — prevents selecting hidden elements
- **Off-viewport detection**: Elements flagged as offscreen vs truly hidden
- **Cross-origin iframes**: CDP-based a11y tree access for cross-origin iframe content (e.g., embedded Google Docs, payment forms)
- **In-page search**: `browser_search_page` greps the live DOM without LLM cost
- **DOM queries**: `browser_find_elements` queries by CSS selector for instant element discovery
- **Iframe support**: `browser_switch_frame` routes all interactions through a target iframe
- **Vision click**: `browser_click_xy` enables coordinate-based clicking from screenshots

## Architecture

```
main.py                     # Entry point
kairos/
├── main.py                 # CLI REPL — thin WebSocket client connected to the gateway
├── main_gateway.py         # Gateway entry point (kairos serve)
├── config.py               # Lazy .env loading (OPENAI_API_KEY, BASE_URL, MODEL)
├── agent.py                # Core agent loop, streaming, compaction, tool dispatch
├── cli.py                  # Terminal UI (streaming panels, thinking dots, paste handling)
├── tokens.py               # Token counting with tiktoken (session/context/turn)
├── terminal_manager.py     # Terminal lifecycle (background shells, blocking subprocesses)
├── browser_manager.py      # Playwright/CloakBrowser lifecycle in a dedicated worker thread + CDP cross-origin iframe support + smart auto-snapshot
├── cdp_manager.py          # CDPManager — low-level Chrome DevTools Protocol access (a11y tree, frame detection)
├── gateway/
│   ├── __init__.py         # Exports: GatewayManager, ManagedSession, create_app, ClientMsg, ServerMsg
│   ├── protocol.py         # Message type constants (ClientMsg, ServerMsg)
│   ├── manager.py          # GatewayManager + ManagedSession — owns conversations, routes to Agents
│   └── server.py           # FastAPI app — WebSocket + REST routes, thread-safe streaming
└── tools/
    ├── base.py             # ToolResult class
    ├── read.py             # Read file (text + images)
    ├── write.py            # Write/create file
    ├── edit.py             # Strict find-and-replace
    ├── search.py           # Regex file search (ripgrep-like)
    ├── git.py              # Git subcommand dispatcher
    ├── terminal.py         # Terminal tool wrappers
    ├── subagent.py         # Sub-agent spawn and tracking
    ├── browser.py          # Browser tool wrappers (30 tools, smart auto-snapshot)
    ├── skills.py           # Skill manager (list, load, write skills)
    └── session.py          # Chat save/load manager
```

## Key Concepts

### Streaming

Tokens are streamed in real-time. During the agent's reasoning phase, a grey panel updates live. When a final response is reached, it transitions to a green panel rendered as Markdown. Tool call thinking stays as a grey trace.

### Compaction

When the conversation context reaches 80% of the context window, old messages are automatically summarized into a structured checkpoint (Goal, Progress, Key Decisions, Next Steps) and replaced. Recent context (20% of context window) is preserved. The check runs every loop iteration, so compaction triggers reliably even during long tool-call chains. You can also trigger this manually with `/compact`.

### Skills

Skills are self-contained knowledge modules stored in `skills/<skill-name>/SKILL.md`. At startup, only skill **names** are injected into the system prompt (lightweight). When the agent wants to use a skill, it calls `load_skill(skill_name)` to fetch the full content into context. The agent can also create new skills via `write_skill(skill_name, content)`.

### AGENTS.md

Kairos auto-loads an `AGENTS.md` file from the workspace root into the system prompt. Use this to give the agent project-specific conventions, instructions, or context.

### Auto-Save

Chat history is saved:
- After every exchange
- Every 60 seconds in the background
- On SIGTERM / SIGINT / SIGHUP (window close, task kill)

### Gateway (Multi-Workspace)

Kairos can run as a stateful WebSocket gateway that hosts multiple Agent instances across different workspaces simultaneously. Each conversation gets its own Agent with its own workspace, conversation history, and browser state.

```bash
# Start gateway with a default workspace (optional)
python -m kairos.main_gateway [default_workspace]

# CLI connects automatically and sends its cwd as workspace
python -m kairos.main [workspace]
```

**How workspaces work:**
- **CLI clients** send their current working directory (or a CLI argument) as the workspace when creating a new session. No UI picker needed.
- **UI clients** (Electron, etc.) receive a list of known workspaces on connect. They present a workspace picker and include the selected workspace path in `new_session` messages.
- If no `KAIROS_DEFAULT_WORKSPACE` env var is set and no CLI argument is given, the gateway requires every client to explicitly provide a workspace.
- Each session's workspace is persisted to `chats.json` so reloading a session always uses the correct workspace.

**Concurrent sessions:**
- A single UI client can have multiple sessions loaded simultaneously. Switching focus via the sidebar does NOT unload the old session — its agent keeps running in the background.
- Messages and session creation are handled **non-blocking**: `send_message` runs as a background task, so the WebSocket loop stays free to process interrupts, new-session requests, and session switches while an agent is streaming. When creating a new session, the server responds with `new_session_created` immediately and unloads old sessions in the background — so the workspace picker disappears instantly instead of blocking on `browser_manager.close()` or `save_chat()`.
- The **tab bar** at the top of the main content area shows all currently loaded sessions as Chrome-style tabs. Click a tab to switch focus, close a tab to unload it, or click "+" to open a new thread without closing existing tabs.
- All streaming events (`stream_start`, `stream_token`, `tool_call`, `stream_end`, `done`) include a `session_id` so the UI routes them to the correct conversation's display buffer.
- The sidebar shows streaming indicators (pulsing dots) on ALL currently running sessions, not just the focused one.
- Use the close button (×) on a tab or sidebar entry to explicitly unload a session and free its resources (browser, terminals, agent).

## Design Principles

1. **Streaming First** — Tokens print as they arrive; no waiting for full response
2. **Absolute Paths** — No workspace containment, just use full paths
3. **One Tool Per File** — Easy to add new tools
4. **Interruptible** — Ctrl+C hard-interrupts; Escape gracefully stops between steps
5. **Token Aware** — Session, context, and turn token counts displayed
6. **Minimal Dependencies** — `openai`, `python-dotenv`, `rich`, `prompt_toolkit`, `tiktoken`, `playwright`
7. **Loud by Default** — Tools always report what they did. Success includes specifics (e.g. "Wrote 42 lines to `main.py`"). Failure clearly states what went wrong. Nothing happens silently — overwrites, creations, and deletions are always announced.

## License

MIT
