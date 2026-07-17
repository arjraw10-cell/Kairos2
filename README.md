# Kairos

A minimal personal coding agent in Python.

## Features

- **40 Tools**: File operations, terminal management, search, git, sub-agents, skills, and a comprehensive browser automation suite (30 browser tools including hover, drag, wait_for, select_index, scroll, wait, send_keys, search_page, find_elements, index-based click/type, go_back/go_forward/reload, and CDP cross-origin iframe support)
- **Absolute Paths**: All file operations use absolute paths — no workspace restrictions
- **Streaming First**: Tokens print as they arrive; no waiting for full responses
- **Token Aware**: Session, context window, and per-turn token counts displayed after every exchange. Estimates include message metadata, tool-call arguments, and function-tool schemas; uses ground-truth counts from the API when available (`stream_options={"include_usage": True}`), with tiktoken estimates as fallback
- **Auto-Compaction**: Conversation history is automatically summarized when the configured context budget exceeds 80%; estimates include tool schemas and tool-call metadata, and compaction input is bounded so large results cannot cause a second breach
- **Sub-Agents**: Spawn autonomous child agents to work on tasks in parallel
- **Browser Automation**: Full Playwright/CloakBrowser integration with stealth mode, persistent profiles, multi-tab, and CDP support
- **Skills**: Self-extensible skill system — agent can create/load skills stored as `SKILL.md` files in `skills/` directory
- **Paste System**: Text pastes use explicit prompt-toolkit paste events: bracketed-paste events carry their own payload, and Ctrl+V reads the clipboard only when that key is pressed. Alt+V pastes images from the clipboard. Creates visible tokens like `(Pasted Text #1)` or `(Pasted Image #1)`. Backspace removes the entire token and its content. Clipboard changes from copying are never mistaken for paste actions.
- **Chat Persistence**: Standard and Textual CLI sessions are saved to `<workspace>/chats/chats.json` (with the historical Agent2 location retained for direct library callers), so using either frontend from another project does not mix that project's chats with Agent2's chats. Each session is tracked by a unique ID — no fuzzy matching that could clobber different sessions. **Atomic writes** via temp-file + rename prevent corruption from interrupted saves. **Corruption recovery** auto-heals damaged files by parsing up to the last valid JSON boundary.
- **`/resume`**: Load previous chats via numbered picker, either interactively (`/resume`, then `1`) or inline (`/resume 1`). The standard picker consumes buffered handoff input and keeps prompting after invalid choices. The Textual picker extracts the session ID correctly and keeps selection mode active until a chat loads. Completed chats resume normally; interrupted chats resume mid-execution by identifying the latest request, repairing incomplete tool calls with synthetic failed results, and automatically continuing with "Continue where you left off". Both interactive frontends share this logic and workspace-specific chat storage.
- **Animated Thinking**: "Thinking..." indicator with cycling dots
- **Streaming Display**: Real-time tokens in a live-updating panel (grey for thinking, green for final response)
- **Tool Summaries**: One-line display when tools are called (e.g., `read file: /path`)
- **Dual Interrupt**: Ctrl+C hard-interrupts mid-step; Escape gracefully stops after the current step finishes. Input typed while a response is handing back to the prompt is buffered, so commands such as `/exit` are not silently lost.
- **Responsive Terminals**: Blocking commands require a finite positive timeout capped at 20 seconds; background commands return immediately, preserve shell state, stay alive after completion, and notify the CLI/agent asynchronously with capped output
- **Bounded Tool Results**: Text returned by tools is capped before it enters history or the API (20,000 characters per result by default), preserving both the beginning and end with a truncation marker; image data is kept out of textual tool messages and re-injected as vision context with bounded token estimates; the limit can be configured with `KAIROS_MAX_TOOL_RESULT_CHARS`
- **OpenAI Compatible**: Works with any OpenAI-compatible API (OpenRouter, local models, etc.)
- **Gateway Retry & Clean Errors**: Transient API/gateway failures, including interrupted chunked streams, are retried twice with backoff; exhausted requests show concise API diagnostics without dumping a traceback
- **Clean CLI**: Terminal UI with `rich` and `prompt_toolkit`, including enhanced table rendering with rounded boxes, bold headers, and alternating row shading

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

# Conservative prompt budget used by token display and auto-compaction
# Set this to the actual context limit accepted by your gateway/model.
KAIROS_CONTEXT_WINDOW=262000

# Optional model-facing tool result cap (characters)
KAIROS_MAX_TOOL_RESULT_CHARS=20000
```

Any OpenAI-compatible endpoint works — just change `OPENAI_BASE_URL`. `KAIROS_CONTEXT_WINDOW` defaults to 262,000 and should be set to the actual prompt limit of the configured gateway/model. Tool-result cap settings are optional; invalid or non-positive values fall back to the defaults.

## Usage

```bash
python main.py                   # cwd as workspace
python main.py /path/to/project  # specific workspace
```

`kairos_old.bat` is the PATH-safe legacy launcher. It invokes `main.py` from the batch file's own directory while preserving the current directory as the workspace, so running `cd Documents/Agent2Gateway; kairos_old` uses `Agent2Gateway` as workspace context but still executes the current Agent2 source.

The standard CLI resolves paste placeholders before command dispatch, so pasted aliases such as `/exit` work like typed commands. Its Escape listener also buffers ordinary terminal input captured during the response-to-prompt handoff; this prevents a quickly typed command from being consumed and forcing a second `/exit`.

### REPL Commands

| Command | Description |
|---------|-------------|
| `Escape` | Stop after current step (finish tool calls, then wait for input) |
| `Ctrl+C` | Hard-interrupt (abort mid-step) |
| `Ctrl+V` | Explicitly paste text from clipboard when the terminal passes the key through; terminals that emit bracketed-paste events use that event automatically |
| `Alt+V` | Paste image from clipboard |
| `/resume` / `/resume 1` | Load a saved chat with the numbered picker, or select chat 1 inline |
| `/compact` | Manually compact conversation history |
| `/paste` | Info about paste functionality |
| `clear` | Clear the screen |
| `reset` | Save and reset conversation history |
| `exit` / `quit` / `q` | Save and exit |

### Resume behavior

`kairos/resume.py` is shared by the standard and Textual frontends. It anchors the decision at the latest real user request, ignores/removes internally generated screenshot/compaction/background-notification messages, repairs partial or orphaned tool chains, automatically continues an interrupted turn, and saves the Textual continuation back to the selected session. Both frontends use `<workspace>/chats/chats.json`; the standard picker accepts `/resume` followed by a number or `/resume 1`, while the Textual picker keeps waiting on invalid selections and passes the selected metadata's string ID to the loader. The headless `temp.py` runner intentionally has no interactive `/resume` command.

The standard CLI synchronizes input handoff with its Escape listener: complete lines captured while an agent is finishing are consumed by the next prompt and by the session picker, and partial lines are prefilled for editing instead of dropped.

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
| `search(pattern, path?, include?, max_results?, timeout?)` | Regex file content search (like ripgrep) — skips binary files and non-source directories; defaults to a 10-second timeout and returns partial matches if the deadline is reached |
| `git(command, **kwargs)` | Git operations: `status`, `diff`, `log`, `commit`, `branch` |

Tool results are capped centrally before they are stored in conversation history or sent to the API. The default is 20,000 characters per result. Oversized text preserves the beginning and end and includes its original character count. Inline image data is removed from textual tool messages and re-injected as a separate vision user message with bounded image-token estimates. Legacy saved sessions are normalized before their next API request, so old multi-megabyte results cannot bypass the cap; embedded images from old tool messages become a short omission marker because their original vision message cannot be safely reconstructed. Set `KAIROS_MAX_TOOL_RESULT_CHARS` in `.env` to override the default. Background terminals retain their complete output for `read_logs`.

### Terminal Tools

| Tool | Description |
|------|-------------|
| `new_terminal(background)` | Create a terminal — `true` for persistent shell, `false` for one-shot |
| `execute_command(terminal_id, command, timeout?, is_background?)` | Run a command in a terminal. The schema requires `timeout` when `is_background` is false; blocking timeouts are finite positive values capped at 20 seconds, while background terminals return immediately, ignore timeout, and report completion asynchronously. |
| `read_logs(terminal_id, start_line, end_line?)` | Read output from a background terminal by line numbers |
| `close_terminal(terminal_id)` | Close a terminal and release resources |
| `get_terminal_info(terminal_id)` | Get terminal status (ID, type, closed status, line count) |

Blocking terminal commands are capped at a 20-second timeout and terminate their process tree on timeout; a missing, non-positive, non-finite, or invalid timeout is rejected immediately, before spawning the command process. Background terminal commands are submitted asynchronously to a persistent shell, preserving shell state between commands, suppressing wrapper command echo, and preserving each command's exit status. When a background command finishes while Kairos is processing, it shows a visible notification immediately and queues the capped output for the next API turn. The completion queue is retained even when no CLI callback is configured, so headless agents still receive notifications in their next API turn. If Kairos is idle, the completion is held quietly and shown after the next user message. The terminal remains open, and full output is still available through `read_logs`.

### Sub-Agent Tools

| Tool | Description |
|------|-------------|
| `spawn_subagent(prompt, mode?)` | Spawn an autonomous child agent — `blocking` (waits) or `non-blocking` (returns ID) |
| `get_subagent_result(subagent_id)` | Poll a non-blocking sub-agent for its result |

Sub-agents have access to file, search, git, terminal, and skill tools but cannot spawn further sub-agents or use browser tools. Their token usage is tracked independently and displayed as `Subagent <id>: Session ... | Context ... | Turn ...` after each child turn in the interactive frontends.

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
temp.py                     # Headless agent runner — run_agent(prompt) with no CLI
run_temp_cli.py              # Textual frontend launcher
kairos/
├── main.py                 # CLI REPL loop, signal handlers, auto-save
├── resume.py               # Shared saved-history repair and mid-execution resume logic
├── config.py               # Lazy .env loading (API settings, result cap, context budget)
├── agent.py                # Core agent loop, streaming, compaction, tool dispatch
├── cli.py                  # Terminal UI (streaming panels, thinking dots, paste handling, numbered resume picker, enhanced table rendering)
├── temp_cli.py             # Textual frontend with workspace-aware numbered resume picker
├── tokens.py               # Token counting with tiktoken (metadata/tools/session/context/turn)
├── terminal_manager.py     # Terminal lifecycle (background shells, blocking subprocesses)
├── browser_manager.py      # Playwright/CloakBrowser lifecycle in a dedicated worker thread + CDP cross-origin iframe support + smart auto-snapshot
├── cdp_manager.py          # CDPManager — low-level Chrome DevTools Protocol access (a11y tree, frame detection)
├── tests/
│   └── test_compaction.py  # Context accounting and compaction regression tests
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

Tokens are streamed in real-time. During the agent's reasoning phase, a grey panel updates live. When a final response is reached, it transitions to a green panel rendered as Markdown with enhanced table styling (rounded boxes, bold headers, alternating rows). Tool call thinking stays as a grey trace.

API streaming requests retry transient gateway failures twice after the initial attempt. Retries also cover failures raised while iterating the response body, such as `RemoteProtocolError` / incomplete chunked reads. If all attempts fail, Kairos reports a concise `OpenAI API Error` with the exception message and configuration, without printing the Python traceback.

### Compaction

When the conversation context exceeds 80% of the configured context budget, old messages are automatically summarized into a structured checkpoint (Goal, Progress, Key Decisions, Next Steps) and replaced. The checkpoint also includes chronological `User Messages` and `Agent-to-User Messages` subsections covering every applicable user message and substantive assistant response in the summarized history; tool-call-only messages and tool results are excluded from the latter. Incremental compaction preserves and extends both records. The check runs before every API step, including inside an ongoing tool-call loop, and recounts newly appended tool results, the function-tool schema, and the user message about to be sent. Token accounting includes assistant tool-call metadata/arguments and tool IDs/names, not just visible text. The separate compaction request is bounded to the configured budget minus its summary reserve and a 1,024-token framing margin, preventing a huge tool result from causing compaction itself to breach the window. Recent context (20% of context window) is preserved, with message boundaries kept valid for assistant/tool calls. You can also trigger this manually with `/compact`.

### Skills

Skills are self-contained knowledge modules stored in `skills/<skill-name>/SKILL.md`. At startup, only skill **names** are injected into the system prompt (lightweight). When the agent wants to use a skill, it calls `load_skill(skill_name)` to fetch the full content into context. The agent can also create new skills via `write_skill(skill_name, content)`.

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
5. **Token Aware** — Session, context, and turn token counts displayed; estimates include message metadata, tool calls, schemas, and bounded vision estimates
6. **Minimal Dependencies** — `openai`, `python-dotenv`, `rich`, `prompt_toolkit`, `tiktoken`, `playwright`
7. **Loud by Default** — Tools always report what they did. Success includes specifics (e.g. "Wrote 42 lines to `main.py`"). Failure clearly states what went wrong. Nothing happens silently — overwrites, creations, and deletions are always announced.

## Tests

Run the focused compaction/accounting regression suite with:

```bash
python -m unittest discover -s tests -v
```

The tests cover tool-schema and metadata accounting, bounded vision estimates, pending-user preflight, safe tool-chain boundaries, compaction-prompt limits, and normalization of oversized legacy saved results.

## License

MIT

## Browser launch defaults

Browser automation always launches in headed mode with human-like interaction behavior. The `browser_launch` arguments remain for protocol compatibility, but requests for headless mode or disabled humanization are ignored.
