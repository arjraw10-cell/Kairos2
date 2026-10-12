# Kairos

A minimal personal coding agent in Python.

## Features

- **48 Tools**: File operations, terminal management, search, git, sub-agents, skills, a comprehensive browser automation suite (including console/network monitoring), and optional native desktop automation through Cua Driver
- **Absolute Paths**: All file operations use absolute paths — no workspace restrictions
- **Streaming First**: Tokens print as they arrive; no waiting for full responses
- **Token Aware**: Session, context window, and per-turn token counts displayed after every exchange. Estimates include message metadata, tool-call arguments, and function-tool schemas; uses ground-truth counts from the API when available (`stream_options={"include_usage": True}`), with tiktoken estimates as fallback
- **Auto-Compaction**: Conversation history is automatically summarized when the configured context budget exceeds 80%; estimates include tool schemas and tool-call metadata, and compaction input is bounded so large results cannot cause a second breach
- **Sub-Agents**: Spawn autonomous child agents to work on tasks in parallel
- **Browser Automation**: CloakBrowser stealth Chromium, always headed (visible) and always humanized, launched lazily on first use. Accessibility snapshots with element refs, diff snapshots after actions, dialog handling, iframes, tabs, persistent profiles with pinned fingerprints, and console/network inspection.
- **Native Desktop Automation**: Optional Cua Driver integration exposes native window discovery, accessibility-tree snapshots, screenshots, app launching, clicking, typing, keyboard, scrolling, dragging, and window actions. Accessibility element indices are the primary addressing mode; pixel coordinates are available as a fallback. Desktop tools are available to sub-agents and are loaded lazily when Cua Driver is installed.
- **Skills**: Self-extensible skill system — agent can create/load skills stored as `SKILL.md` files in `skills/` directory
- **Paste System**: Text pastes use explicit prompt-toolkit paste events: bracketed-paste events carry their own payload, and Ctrl+V reads the clipboard only when that key is pressed. Alt+V pastes images from the clipboard. Creates visible tokens like `(Pasted Text #1)` or `(Pasted Image #1)`. Backspace removes the entire token and its content. Clipboard changes from copying are never mistaken for paste actions.
- **Chat Persistence**: Standard and Textual CLI sessions are saved by workspace to `~/.kairos/chats/<directory>--<stable-path-id>/chats.json`. The readable directory name plus deterministic absolute-path ID prevents collisions between workspaces with the same basename, while the JSON envelope records the absolute `workspace` path. Existing `<workspace>/chats/chats.json` files remain loadable for resume and are never deleted; their sessions are copied into the new store on the next save. Each session is tracked by a unique ID — no fuzzy matching that could clobber different sessions. **Atomic writes** via temp-file + rename prevent corruption from interrupted saves. **Corruption recovery** auto-heals damaged files by parsing up to the last valid JSON boundary.
- **`/resume`**: Load previous chats via numbered picker, either interactively (`/resume`, then `1`) or inline (`/resume 1`). The standard picker consumes buffered handoff input and keeps prompting after invalid choices. The Textual picker extracts the session ID correctly and keeps selection mode active until a chat loads. Completed chats resume normally; interrupted chats resume mid-execution by identifying the latest request, repairing incomplete tool calls with synthetic failed results, and automatically continuing with "Continue where you left off". Both interactive frontends share this logic and workspace-isolated chat storage.
- **Animated Thinking**: "Thinking..." indicator with cycling dots
- **Streaming Display**: Real-time tokens in a live-updating panel (grey for thinking, green for final response)
- **Tool Summaries**: One-line display when tools are called (e.g., `read file: /path`)
- **Immediate Stop**: Ctrl+C and Escape share one hard-stop path. They close the active stream, cancel running blocking terminal commands and the in-flight browser call, stop before the next tool/API step, and return control to the prompt as soon as cleanup is complete. Interrupted assistant/tool turns remain repairable so the next request can continue them rather than silently losing context. Input typed while a response is handing back to the prompt is buffered, so commands such as `/exit` are not silently lost.
- **Responsive Terminals**: Blocking commands require a finite positive timeout capped at 20 seconds; background commands return immediately, preserve shell state, stay alive after completion, and notify the CLI/agent asynchronously with capped output
- **Cua Driver Configuration**: Set `KAIROS_CUA_DRIVER_PATH` to an explicit `cua-driver` executable or installation directory, or leave it empty for PATH discovery. On Windows, Kairos also checks the official per-user install location under `%LOCALAPPDATA%\Programs\Cua\cua-driver\bin` when a long-lived gateway has an older inherited PATH. Kairos starts the Cua daemon lazily when a desktop tool is first used.
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

Browser automation needs Playwright 1.63+ (for AI accessibility snapshots) and CloakBrowser 0.6+, both in `requirements.txt`. CloakBrowser downloads its Chromium binary (~200 MB) on first use; to fetch it ahead of time:

```bash
python -m cloakbrowser install
```

Kairos always launches the browser headed and humanized; a browser tool fails with an installation message if CloakBrowser or its humanization layer is unavailable.

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

`kairos_cli_new.bat` launches the standard Rich/prompt-toolkit CLI (not the Textual frontend). It checks the configured gateway `/healthz` endpoint first and starts `python -m kairos.gateway_main` in a minimized window only when the gateway is unavailable. The caller's current directory remains the CLI workspace; an optional first argument can explicitly select the workspace, matching `python main.py /path/to/project`.

The standard CLI resolves paste placeholders before command dispatch, so pasted aliases such as `/exit` work like typed commands. Its Escape listener also buffers ordinary terminal input captured during the response-to-prompt handoff; this prevents a quickly typed command from being consumed and forcing a second `/exit`.

### REPL Commands

| Command | Description |
|---------|-------------|
| `Escape` | Immediate hard stop: cut off the stream/tool and return to input |
| `Ctrl+C` | Immediate hard stop: cut off the stream/tool and return to input |
| `Ctrl+V` | Explicitly paste text from clipboard when the terminal passes the key through; terminals that emit bracketed-paste events use that event automatically |
| `Alt+V` | Paste image from clipboard |
| `/resume` / `/resume 1` | Load a saved chat with the numbered picker, or select chat 1 inline |
| `/compact` | Manually compact conversation history |
| `/paste` | Info about paste functionality |
| `clear` | Clear the screen |
| `reset` | Save and reset conversation history |
| `exit` / `quit` / `q` | Save and exit |

### Resume behavior

`kairos/resume.py` is shared by the standard and Textual frontends. It anchors the decision at the latest real user request, ignores/removes internally generated screenshot/compaction/background-notification messages, repairs partial or orphaned tool chains, automatically continues an interrupted turn, and saves the Textual continuation back to the selected session. Both frontends use the workspace-isolated `~/.kairos/chats/<directory>--<stable-path-id>/chats.json` store and also read legacy `<workspace>/chats/chats.json` sessions; the standard picker accepts `/resume` followed by a number or `/resume 1`, while the Textual picker keeps waiting on invalid selections and passes the selected metadata's string ID to the loader.

The standard CLI synchronizes input handoff with its Escape listener: complete lines captured while an agent is finishing are consumed by the next prompt and by the session picker, and partial lines are prefilled for editing instead of dropped.

## Tools

### File Tools

| Tool | Description |
|------|-------------|
| `read(path)` | Read UTF-8 text or supported images (png, jpg, jpeg, gif, webp, bmp, tiff, tif, svg); images are returned as vision data for visual analysis |
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

The browser is a headed, humanized CloakBrowser that launches automatically on the first browser tool call. Snapshots are Playwright's AI accessibility snapshots: elements carry refs like `[ref=e12]` (`f1e12` inside iframes and after the page navigates), and tools take those refs. Every action returns the resulting page state: only the changed snapshot lines while the URL stays the same, the full snapshot after navigation, and a fresh full snapshot with any error.

| Tool | Description |
|------|-------------|
| `browser_navigate(url)` | Open a URL, or `back` / `forward` / `reload`. Returns the full snapshot. |
| `browser_snapshot()` | Full accessibility snapshot of the current page |
| `browser_click(ref?, button?, double?, x?, y?)` | Click an element by ref, or viewport coordinates (canvas, captchas) |
| `browser_type(ref, text, submit?)` | Replace a field's content by typing like a human; `submit` presses Enter |
| `browser_fill_form(fields)` | Fill textboxes, checkboxes, radios and selects in one call |
| `browser_select(ref, values)` | Select `<select>` options by label or value |
| `browser_hover(ref)` | Hover (menus, tooltips) |
| `browser_drag(from_ref?, to_ref?, x1?, y1?, x2?, y2?)` | Drag between elements or coordinates |
| `browser_press_key(key)` | Press a key or combination (Enter, Escape, Control+A) |
| `browser_scroll(direction?, pages?, ref?)` | Scroll by viewport heights, optionally inside the pane under `ref` |
| `browser_wait_for(text?, text_gone?, seconds?, timeout?)` | Wait for text to appear or disappear, or for a number of seconds |
| `browser_screenshot(full_page?, ref?)` | Screenshot returned as vision data and saved to `~/.kairos/screenshots/` |
| `browser_evaluate(expression, ref?)` | Run JavaScript; an expression or a function (with `ref`, the function receives the element) |
| `browser_search_page(pattern, regex?, case_sensitive?, max_results?)` | Find text in the page's visible text with context |
| `browser_tabs(action, index?, url?)` | `list` / `new` / `select` / `close` tabs |
| `browser_handle_dialog(accept, prompt_text?)` | Answer an open alert/confirm/prompt |
| `browser_console(index?, errors_only?, max_results?)` | Console messages and uncaught errors of the current page |
| `browser_network(index?, filter?, max_results?)` | Network requests of the current tab; `index` shows headers and timing |
| `browser_profile(action, name?, proxy?)` | `list` / `use <name>` / `ephemeral`; switching relaunches the browser |
| `browser_close()` | Close the browser; the next browser tool relaunches it |

### Native desktop tools

When `cua-driver` is installed, Kairos also exposes native desktop automation through Cua Driver. Use `desktop_window_state(pid, window_id)` before interacting with a window and again afterward to verify the result. Accessibility `element_index` values from the latest state are the primary interaction method for supported actions; pixel coordinates are required for cursor movement and dragging. If both addressing forms are accidentally supplied, Kairos prefers the accessibility address to match Cua's mutually exclusive contract. Kairos maps drag coordinates to Cua's `from_x/from_y/to_x/to_y` fields, supplies scroll defaults, maps `activate_window` to `bring_to_front`, and reports minimize/maximize/close as unsupported because the current driver does not expose those tools. Desktop tools are available to sub-agents. Set `KAIROS_CUA_DRIVER_PATH` to override PATH discovery.

| Tool | Description |
|------|-------------|
| `desktop_doctor()` | Check Cua Driver and interactive desktop prerequisites |
| `desktop_list_windows(pid?)` | List native windows |
| `desktop_window_state(pid, window_id, include_screenshot?)` | Return accessibility state and grounding screenshot |
| `desktop_screenshot(pid, window_id)` | Capture a native window screenshot through the driver's window-state response |
| `desktop_launch_app(...)` | Launch/connect to a native application |
| `desktop_click(...)` | Click by accessibility index, with coordinate fallback |
| `desktop_type(pid, text, ...)` | Type by accessibility index, with coordinate fallback |
| `desktop_press_key(pid, key, ...)` | Press a key |
| `desktop_hotkey(pid, keys, ...)` | Send a shortcut |
| `desktop_scroll(pid, direction, ...)` | Scroll a native window |
| `desktop_move(...)` / `desktop_drag(...)` | Move or drag |
| `desktop_window_action(action, pid, ...)` | Activate, minimize, maximize, or close a window |

Browser features:
- **Stealth and humanized input**: CloakBrowser's patched Chromium, with every click, keystroke and mouse path humanized. Refs are resolved to element handles before acting, because CloakBrowser's humanized Locator methods cannot resolve `aria-ref` selectors; inside iframes, where its engine cannot map a handle, the humanized page mouse and keyboard act at the element's box.
- **Real accessibility snapshots**: `page.aria_snapshot(mode="ai")` gives real ARIA roles and names, iframe content inline, and refs for every element; snapshots over 15,000 characters are truncated with a pointer to `browser_search_page`.
- **Diff snapshots**: actions on the same page return only the snapshot lines that changed (refs are stable across snapshots), so a click on a large page returns a few hundred characters instead of the whole page.
- **Dialogs**: actions race against `dialog` events. An alert/confirm/prompt is reported to the agent and answered with `browser_handle_dialog`, instead of hanging the call or being silently dismissed.
- **Stale refs**: a ref that no longer exists fails immediately with a fresh snapshot.
- **Lazy launch and recovery**: the browser starts on the first call, a failed launch can be retried, and closing the window by hand makes the next call relaunch.
- **Persistent profiles**: `browser_profile use <name>` keeps cookies and logins under `~/.kairos/profiles/<name>` and pins a fingerprint seed in `.kairos-browser.json`, so a logged-in identity always presents the same device. A profile can be open in only one browser at a time; a second conversation gets a clear error.
- **New tabs**: when an action opens a tab, Kairos switches to it and says so.
- **Console and network**: network requests are captured from tab creation with status, timing and headers. CloakBrowser suppresses the live console stream (enabling the CDP Runtime domain is a bot signal), so `browser_console` briefly enables it on demand, which makes Chrome replay the current page's console messages and uncaught exceptions.

## Architecture

```
main.py                     # Entry point
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
├── browser_manager.py      # CloakBrowser via async Playwright on a worker thread: AI snapshots, refs, diff snapshots, dialogs, profiles
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
    ├── browser.py          # BrowserTool (forwards one BrowserManager method) and BrowserScreenshotTool
    ├── skills.py           # Skill manager (list, load, write skills)
    └── session.py          # Chat save/load manager
```

## Key Concepts

### Streaming

Tokens are streamed in real-time. and Escape immediately close the active stream and return to the prompt after the worker has safely checkpointed its current history. A cut-off assistant response or partial tool-call chain remains repairable, so the next request can continue the unfinished turn.
 During the agent's reasoning phase, a grey panel updates live. When a final response is reached, it transitions to a green panel rendered as Markdown with enhanced table styling (rounded boxes, bold headers, alternating rows). Tool call thinking stays as a grey trace.

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
4. **Interruptible** — Ctrl+C and Escape hard-stop active streams/tools and safely hand control back to the prompt
5. **Token Aware** — Session, context, and turn token counts displayed; estimates include message metadata, tool calls, schemas, and bounded vision estimates
6. **Minimal Dependencies** — `openai`, `python-dotenv`, `rich`, `prompt_toolkit`, `tiktoken`, `playwright`, `cloakbrowser`
7. **Loud by Default** — Tools always report what they did. Success includes specifics (e.g. "Wrote 42 lines to `main.py`"). Failure clearly states what went wrong. Nothing happens silently — overwrites, creations, and deletions are always announced.

## Tests

Run the focused compaction/accounting regression suite with:

```bash
python -m unittest discover -s tests -v
```

The tests cover tool-schema and metadata accounting, bounded vision estimates, pending-user preflight, safe tool-chain boundaries, compaction-prompt limits, normalization of oversized legacy saved results, and workspace-isolated `~/.kairos` session storage with legacy chat loading.

## License

MIT

## Gateway Architecture (feature/gateway-v2)

The gateway layer under `kairos/gateway/` provides durable SQLite-backed conversations, resumable histories, Agent runtime lifecycle management, per-conversation serialization, cross-conversation worker concurrency, REST/WebSocket APIs, authentication, event fan-out, and event replay. The root `gateway_main.py` shim, `python -m kairos.gateway_main`, and `python -m kairos.main_gateway` start the FastAPI gateway. `kairos_cli_new.bat` is the Windows standard-CLI launcher: it probes `/healthz`, starts the gateway only when unavailable, waits for readiness, and preserves the caller's workspace CWD. CLI, Electron, and Telegram clients use the gateway rather than creating Agents or owning browser/terminal resources.

Gateway configuration is exposed through `KAIROS_GATEWAY_HOST`, `KAIROS_GATEWAY_PORT`, `KAIROS_DEFAULT_WORKSPACE`, `KAIROS_DATA_DIR`, `KAIROS_MAX_CONCURRENT_RUNS`, `KAIROS_RUNTIME_IDLE_SECONDS`, `KAIROS_AUTH_TOKEN`, and `KAIROS_LEGACY_CHAT_FILE`. Workspace paths remain context rather than a security boundary. `Config.MAX_TOOL_RESULT_CHARS()` and `Config.CONTEXT_WINDOW()` configure model-facing tool-result limits and conservative context accounting.

`GatewayManager` accepts an injectable `agent_factory` for tests. Runtime operations use state/lifecycle locks, FIFO run queues, stable persistence boundaries, interruption, queued cancellation, manual compaction, continuation repair, idle unloading, and shutdown cleanup. Resource cleanup tolerates minimal Agents without optional token/browser/terminal attributes.

The gateway repository stores workspaces, conversations, messages, runs, and monotonic events in SQLite WAL mode. REST routes cover conversation/runtime/message/run operations; WebSocket commands support loading, sending, interruption, cancellation, compaction, subscriptions, replay, and live streaming. Agent callbacks also expose tool-finished and background-terminal completion events. Replay initializes its cursor before live subscription, serializes replay/live handoff, and deduplicates event IDs. The gateway's manager is compatible with the main branch's background-terminal and tool-result callback additions.

Blocking terminal commands require a finite positive timeout and clamp it to 20 seconds; background terminals remain persistent and report asynchronous completion events with bounded notifications while retaining full logs. Timeout validation is case-insensitive in the regression checks.

Dependency-light gateway validation is available with `python tests/run_gateway_tests.py`; compile checks use `python -m compileall -q kairos gateway_main.py tests`. Full pytest tests require installing `pytest`. Every code change must update this file and `README.md`.
