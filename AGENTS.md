# Kairos Architecture Documentation

**MANDATORY: Whenever you make any code change (edit, add, or remove code), you MUST also update this AGENTS.md file AND README.md to reflect the change. This ensures the documentation stays in sync with the code. Failure to update documentation after a code change is not acceptable.**

## Overview

Kairos is a minimal coding agent written in Python. It uses the OpenAI chat completions API with streaming and function calling to autonomously execute tasks through 40 tools. All file operations use absolute paths — no workspace containment.

## Project Structure

```
Agent2/
├── main.py                 # Root entry point (imports from kairos.main)
├── .env                    # Environment configuration (API keys)
├── .env.example            # Template for .env file
├── requirements.txt        # Python dependencies
├── pyproject.toml          # Project metadata and build configuration
├── README.md               # User-facing documentation
├── AGENTS.md               # This file - architecture documentation
├── kairos.bat              # Windows shortcut (py main.py)
├── chats/                  # Saved chat sessions (gitignored)
│   └── chats.json          # All chat history in one file
└── kairos/
    ├── __init__.py         # Exports: Config, Agent, ToolResult, SessionManager, SkillManager, TerminalManager, BrowserManager
    ├── main.py             # CLI REPL loop, signal handlers, auto-save, paste resolution
    ├── config.py           # Lazy .env loading via lru_cache
    ├── agent.py            # Core agent: streaming, tool dispatch, compaction, error handling
    ├── cli.py              # Terminal UI: streaming panels, thinking dots, paste handling
    ├── tokens.py           # TokenCounter using tiktoken
    ├── terminal_manager.py # Terminal lifecycle (background + blocking)
    ├── browser_manager.py  # Playwright/CloakBrowser in dedicated worker thread + CDP cross-origin iframe support
    ├── cdp_manager.py      # CDPManager — low-level Chrome DevTools Protocol access (a11y tree, frame detection)
    └── tools/
        ├── __init__.py     # Imports and re-exports all tools
        ├── base.py         # ToolResult(success, output, error?, image_url?)
        ├── read.py         # ReadTool — text + image files
        ├── write.py        # WriteTool — create/overwrite files
        ├── edit.py         # EditTool — strict find-and-replace (must match exactly once)
        ├── search.py       # SearchTool — regex file search (ripgrep-like)
        ├── git.py          # GitTool — status, diff, log, commit, branch
        ├── terminal.py     # 5 terminal tool wrappers
        ├── subagent.py     # SubAgentTool — spawn/track child agents
        ├── browser.py      # 26 browser tool wrappers (scroll, wait, send_keys, search, find, think, index-based, etc.)
        ├── skills.py       # SkillManager — list, load, write skills
        └── session.py      # SessionManager — save/load chats to chats/chats.json
```

---

## File-by-File Reference

### `main.py` (root)

Simple entry point:
```python
from kairos.main import main
if __name__ == "__main__":
    main()
```

### `kairos/__init__.py`

Exports: `Config`, `Agent`, `ToolResult`, `SessionManager`, `SkillManager`, `TerminalManager`, `BrowserManager`.

### `kairos/config.py` — Config

**Class**: `Config` (all static/class methods, no instances)

Lazy-loads `.env` on first access via `python-dotenv`. Uses `@lru_cache(maxsize=1)` on each getter.

| Method | Returns | Default |
|--------|---------|---------|
| `Config.OPENAI_API_KEY()` | `str` | *(required)* |
| `Config.OPENAI_BASE_URL()` | `str` | `http://127.0.0.1:8082/v1` |
| `Config.OPENAI_MODEL()` | `str` | `gpt-4o` |
| `Config.validate()` | `bool` | Raises `ValueError` if API key missing |
| `Config.reload()` | `None` | Clears all caches, re-reads `.env` |

**Key detail**: `OPENAI_API_KEY()` clears its own cache if the key is missing (so `validate()` can re-check after `.env` is created).

### `kairos/main.py` — CLI Entry Point

**Function**: `main()` — orchestrates the entire application lifecycle.

**Global state**: `_session_mgr` and `_agent` are module-level globals shared with signal handlers.

**Signal handlers** (installed at startup):
- `SIGINT` → `_save_now()` + `sys.exit(0)`
- `SIGTERM` → `_save_now()` + `sys.exit(0)`
- `SIGHUP` (Unix only) → `_save_now()` + `sys.exit(0)`

**Auto-save**: `_start_auto_save(agent, interval_seconds=60)` runs a daemon thread that saves every 60 seconds.

**`process_request(cli, agent, user_input, image_url?)`**:
1. Starts an Escape key listener (via `cli.start_escape_listener`)
2. Runs `agent.run(user_input, image_url)` in a background thread
3. Main thread polls `t.join(timeout=0.15)` — catches `KeyboardInterrupt` to call `agent.interrupt()`
4. Returns the agent's response or `"[Interrupted]"`

**REPL loop** (inside `main()`):
1. `cli.get_user_input()` → handles paste token resolution (text + images from clipboard)
2. Command dispatch: `exit`, `clear`, `reset`, `/resume`, `/compact`, `/paste`
3. Clipboard image auto-detection on empty input or alongside text
4. `cli.start_thinking()` → `process_request()` → `cli.stop_thinking()`
5. Response display (streaming panel handles it; `_skip_print_response` prevents double-print)
6. Auto-save after each exchange (all saves go through `_save_now()` which holds `_auto_save_lock` to prevent race conditions with the auto-save thread)

**Resume sanitization** (`_sanitize_history_for_resume(history)`):
- Walks backward through saved history to find the last clean agent response (an `assistant` message *without* `tool_calls`)
- Skips dirty messages: `tool` results, `assistant` messages with `tool_calls` (incomplete execution), and user screenshot injection messages (`[Screenshot captured ...]`)
- Returns `(sanitized_history, last_agent_content)` on success, or `(None, "")` if no clean response exists
- On resume, the last agent message is displayed in a green panel so the user can see where the conversation left off
- If a chat was interrupted mid-execution (no clean agent response), a warning is shown and the chat is skipped

**Helper**: `_is_screenshot_injection(msg)` — detects user messages that are agent-injected screenshots (content array starting with `[Screenshot captured ...`) vs real user messages.

**Wiring** (in `main()`): The agent's callbacks are wired to CLI methods:
```python
agent.on_tool_call = lambda name, args: cli.print_tool_summary(agent._tool_summary(name, args))
agent.on_stream_start = lambda: cli.start_stream()
agent.on_stream_token = cli.on_stream_token
agent.on_stream_end = _on_stream_end  # Finalizes as green response or grey thinking trace
agent.on_token_update = lambda tc: cli.print_token_status(tc)
agent.on_compact = lambda msg: cli.print_info(msg)
# Sub-agent visibility:
agent.subagent_tool._tool_printer = lambda summary: cli.console.print(f"  ↓ subagent: {summary}")
agent.subagent_tool._stream_start = lambda: cli.start_stream()
agent.subagent_tool._stream_token = cli.on_stream_token
agent.subagent_tool._stream_end = lambda _content, _has_tools: cli.finish_stream()
```

### `kairos/agent.py` — Agent (Core)

**Class**: `Agent`

**Constructor**: `Agent(workspace: str)`
- Creates `OpenAI` client from config
- Sets `self.cwd = Path(workspace).resolve()`
- Initializes all 29 tool instances
- Calls `_setup_system_prompt()` which builds the system prompt and initializes `conversation_history`

**Key attributes**:
- `self.client` — `OpenAI` instance
- `self.model` — model name string
- `self.cwd` — workspace path (`Path`)
- `self.tokens` — `TokenCounter` instance
- `self.conversation_history` — `List[Dict]` (starts with system prompt)
- `self._interrupt_event` — `threading.Event` for Ctrl+C
- `self._stop_requested` — `bool` for Escape
- `self._is_subagent` — `bool` (True = no browser/subagent tools)
- `self.terminal_manager` — `TerminalManager` instance
- `self.browser_manager` — `BrowserManager` instance
- `self.subagent_tool` — `SubAgentTool` instance (None if sub-agent)
- `self.skill_manager` — `SkillManager` instance

**Callbacks** (set by `main.py`):
- `on_tool_call(name: str, args: dict) -> None`
- `on_stream_start() -> None`
- `on_stream_token(token: str) -> None`
- `on_stream_end(content: str, has_tool_calls: bool) -> None`
- `on_token_update(counter: TokenCounter) -> None`
- `on_compact(status_msg: str) -> None`

#### System Prompt (`_setup_system_prompt`)

Builds a system prompt containing:
1. Base instructions (role, file access, tool usage philosophy)
2. Browser tool workflow instructions
3. Workspace path: `f"## Workspace\nYour current workspace is: {self.cwd}"`
4. **AGENTS.md** (auto-loaded from `self.cwd / "AGENTS.md"` if it exists):
   ```
   ## AGENTS.md
   The following is an AGENTS.md file found in the workspace. Follow any instructions or conventions described in it.
   
   {contents of AGENTS.md}
   ```
5. **Skills** (auto-injected skill names from `skills/` directory):
   - If skills exist: lists available skill names with instructions to use `load_skill` and `write_skill`
   - If no skills: tells agent to use `write_skill` to create the first one

This means the AGENTS.md content is injected directly into every API call's system message. The agent sees it as authoritative project context.

#### Tool Schema (`_get_tool_schema()`)

Returns a list of 40 OpenAI function tool definitions. If `self._is_subagent` is True, removes browser tools (19) and sub-agent tools (2), leaving 13 tools (includes skill tools).

#### Tool Execution (`_execute_tool(name, args)`)

Dispatch dict mapping tool names to lambdas. Each calls the corresponding tool instance and returns `json.dumps(result.to_dict())`. Catches all exceptions and returns error JSON.

#### Tool Summaries (`_tool_summary(name, args)`)

Static method. Returns a one-line human-readable summary string for each tool call (used by CLI display).

#### Streaming (`_stream_response()`)

Returns `(full_content: str, assembled_tool_calls: List[Dict], api_usage: Dict | None)`.

**Retry logic**: Up to 3 attempts for retryable errors (rate limits, connection errors, 500/502/503/504). Exponential backoff with jitter.

**Streaming loop**:
1. Calls `on_stream_start()` callback
2. Iterates over chunks from `client.chat.completions.create(..., stream=True, stream_options={"include_usage": True})`
3. Accumulates `delta.content` → calls `on_stream_token(token)` for each chunk
4. Accumulates `delta.tool_calls` by index (id, name, arguments deltas)
5. Captures `chunk.usage` from the final chunk (prompt_tokens, completion_tokens) as ground-truth token counts
6. Assembles tool calls: parses JSON arguments, returns list of `{id, name, arguments}` dicts plus the API usage

**Interrupt checking**: `_check_interrupt()` is called per-chunk — raises `InterruptedError` if Ctrl+C was pressed.

#### Step (`step(user_message?, image_url?)`)

Returns `(response_text | None, tool_calls_made: List[Dict])`.

1. Appends user message to `conversation_history` (as vision content array if `image_url` provided)
2. `tokens.start_turn()` — counts input tokens via tiktoken (estimate)
3. `_stream_response()` — streams response, captures API usage from final chunk
4. If API usage available: `tokens.set_turn_from_api()` replaces tiktoken estimates with ground-truth counts. Otherwise falls back to `tokens.add_output_tokens()` for tiktoken estimates
5. Builds assistant message (with `tool_calls` if present)
6. Calls `on_stream_end()` — this is where the CLI finalizes the display panel
7. If no tool calls: calls `tokens.finish_turn()`, returns response
8. If tool calls: when using tiktoken fallback, counts tool call argument tokens via `add_output_tokens()` (API counts already include these); executes each via `_execute_tool()`, appends tool results to history, truncates history if >100 messages, calls `tokens.finish_turn()`

**Important**: Tool results have `image_url` stripped before appending to history. Screenshot images are re-injected as a user vision message (with `[Screenshot captured]` prefix) so the model can actually see them, since tool messages can't carry images on most providers. Tool results are NOT counted as output tokens — they become input tokens in the next turn via `start_turn()`.

#### Run (`run(user_message, image_url?)`)

The main agent loop:
1. Clears interrupt event
2. Loops indefinitely until one of the termination conditions is met:
   - Checks `_should_stop()` (Escape) between steps
   - Auto-compacts if context > 80%
   - Calls `step()`
   - **Empty response retry**: If `step()` returns no content and no tool calls (API returned nothing), removes the empty assistant message from history (to prevent consecutive assistant messages), then retries the same call up to 2 times with a status message before giving up. This handles transient API issues where the model returns an empty response.
   - Returns when: final response received, no tool calls (after retries exhausted), interrupt, or graceful stop (Escape)
3. Returns `"[Interrupted]"` on `InterruptedError`

#### Compaction

**Constants**:
- `COMPACT_RESERVE_TOKENS = 16384` — tokens for summary prompt + output
- `COMPACT_KEEP_RECENT = 20000` — tokens of recent context to preserve
- `COMPACT_THRESHOLD_PCT = 80.0` — auto-compact threshold

**`compact()`**:
1. `_find_compact_boundary()` — walks backward from end, accumulates tokens, finds cut point keeping ~20k tokens
2. Serializes old messages into readable text (`_serialize_messages_for_summary()`)
3. `_generate_summary()` — non-streaming API call with structured prompt
4. If existing compaction summary exists, passes it as `<previous-summary>` for incremental update
5. Rebuilds history: `[system_prompt, compaction_summary, recent_messages]`
6. Re-counts tokens

**Summary format** (structured checkpoint):
```
## Goal
## Constraints & Preferences
## Progress (Done / In Progress)
## Key Decisions
## Next Steps
## Critical Context
```

#### Error Handling

**`_format_api_error(e)`**: Extracts maximum detail from OpenAI exceptions — status code, request ID, error type/code/message, response body, config (model + base URL). Returns formatted string.

**`_is_retryable_error(e)`**: Returns True for `RateLimitError`, `APIConnectionError`, and `APIStatusError` with 500/502/503/504.

#### Reset (`reset()`)

Rebuilds system prompt, resets token counter, closes browser if open.

#### History Truncation (`_truncate_history_if_needed()`)

Keeps `system + last MAX_HISTORY_MESSAGES (100)`. After truncation, verifies at least one `role: "user"` message survives — if not, expands the window backward to include the most recent user message. This prevents the "No user query found in messages" 400 error that occurs during long tool-call chains.

#### History Validation (`_validate_history_before_api()`)

Called before every API request in `step()`. Handles two structural problems that cause 400 errors:
1. **No user message**: If the conversation history has no user message (from truncation or compaction), triggers a `compact()` to restore a valid state.
2. **Orphaned tool messages**: If trailing tool messages lack a preceding assistant message (from truncation cutting at a bad point), they are trimmed to restore valid ordering.

### `kairos/cli.py` — CLI (Terminal UI)

**Class**: `CLI`

**Constructor**: Creates `Console` (from rich), `PromptSession` (from prompt_toolkit), initializes thinking/stream state.

**Key attributes**:
- `self.console` — `rich.console.Console`
- `self._live` — `rich.live.Live` (streaming panel, None when not streaming)
- `self._stream_text` — accumulated streaming text
- `self._skip_print_response` — bool to prevent double-printing final response
- `self._prompt_session` — `PromptSession` with paste key bindings

**Streaming display**:
- `start_stream()` — stops thinking, creates `Live` panel with grey border and italic dim text
- `on_stream_token(token)` — appends to `_stream_text`, updates live panel
- `finish_stream()` — stops live panel, returns text
- `finalize_stream_as_response()` — upgrades live panel to green border with Markdown rendering, sets `_skip_print_response = True`

**Paste system** (module-level):
- `_paste_registry: Dict[str, dict]` — maps token strings to `{type: "text"|"image", ...}`
- `_make_image_token()` / `_make_text_token()` — creates numbered tokens like `(Pasted Image #1)`
- `_reset_paste_counters()` — resets token counters at the start of each prompt
- `_paste_handler(event)` — Ctrl+V key binding: text paste only (reads clipboard, inserts text token)
- `_alt_v_handler(event)` — Alt+V key binding: image paste only (reads clipboard image, inserts image token; shows `[no image on clipboard]` if none)
- `_backspace_handler(event)` — deletes entire paste token if cursor is inside one
- `_on_text_changed(b)` — detects clipboard paste using `GetClipboardSequenceNumber()` (a single ctypes call on Windows, returns 0 on other platforms). Captures a baseline sequence number before each prompt starts. On each buffer change, if the sequence number hasn't changed since the baseline, the change is treated as normal typing and left alone. Only when the clipboard sequence number has advanced (indicating a real Ctrl+V or clipboard paste) does it extract the inserted text via `_diff_inserted_text()` and replace it with a `(Pasted Text #N)` token. This prevents false positives where every keystroke was being misdetected as a paste.
- Image pasting is explicit via Alt+V — no background polling or auto-detection

**Clipboard helpers** (cross-platform):
- `_check_clipboard_has_image()` — Windows: PowerShell + `System.Windows.Forms.Clipboard`, macOS: `pngpaste`, Linux: `xclip`
- `_read_system_clipboard()` — same platforms
- `_detect_mime(data)` — detects PNG/JPEG/GIF/WEBP/BMP/TIFF from magic bytes
- `_image_data_to_url(data)` — converts to base64 data URL
- `_get_clipboard_sequence_number()` — Windows: single ctypes call to `GetClipboardSequenceNumber()` (microseconds), returns 0 on other platforms
- `_get_clipboard_sequence_number()` — Windows: single ctypes call to `GetClipboardSequenceNumber()` (microseconds), returns 0 on other platforms

**Escape key listener**:
- `start_escape_listener(on_escape)` — spawns thread listening for raw Escape key
- Windows: `msvcrt.kbhit()` + `msvcrt.getwch()`
- Unix: `tty.setraw()` + `select.select()` + `os.read()`

### `kairos/tokens.py` — TokenCounter

**Class**: `TokenCounter`

**Constructor**: `TokenCounter(model: str = "gpt-4o")` — loads tiktoken encoding for model (falls back to `cl100k_base`)

**Attributes**:
- `session_input` / `session_output` — cumulative across all turns
- `context_tokens` — tokens in current conversation_history
- `turn_input` / `turn_output` — per-turn counters
- `context_window` — max context (default 999,000)

**Methods**:
- `start_turn(messages)` — counts all tokens in conversation_history via tiktoken, sets `context_tokens`
- `add_output_tokens(text)` — encodes text and adds to `turn_output` (tiktoken estimate)
- `set_turn_from_api(prompt_tokens, completion_tokens)` — overrides turn counters with ground-truth values from the API's `stream_options={"include_usage": True}` response
- `finish_turn()` — adds turn totals to session totals
- `context_pct` — property: `(context_tokens / context_window) * 100`
- `format_status()` — `"Session: X in / Y out  |  Context: Z%  |  Turn: A in / B out"`

**Counting strategy**: `count_message()` intentionally does NOT count tool call arguments on assistant messages — they were already counted as output tokens when generated via `add_output_tokens()`. This prevents double-counting the same bytes across turns. Image tokens on vision content blocks are estimated via `_estimate_image_tokens()` using data URL length as a proxy.

### `kairos/terminal_manager.py` — TerminalManager

**Class**: `TerminalManager`

**`create_terminal(background: bool) -> int`**:
- Background: spawns persistent shell (`cmd /k` on Windows, `bash --login` on Unix), starts reader thread
- Blocking: no process created (uses `subprocess.run` per command)
- Returns terminal ID (auto-incrementing int)

**`execute_command(terminal_id, command, timeout?, is_background?)`**:
- Background: writes command to process stdin, returns "Command sent to background terminal"
- Blocking: `subprocess.run(command, shell=True, timeout=timeout)`, returns stdout+stderr
- Validates `is_background` matches terminal type

**`read_logs(terminal_id, start_line, end_line?)`**: Returns lines from background terminal's output buffer (1-indexed).

**`close_terminal(terminal_id)`**: Terminates process (terminate → wait 5s → kill → wait 2s), removes from dict.

All shared state is protected by `threading.Lock`.

### `kairos/cdp_manager.py` — CDPManager (NEW)

**Class**: `CDPManager`

Low-level Chrome DevTools Protocol access via Playwright's CDP session API.

**Key attributes**:
- `self._sessions` — `Dict[int, Any]` mapping `id(page)` → CDP session

**Methods**:
| Method | Returns | Description |
|--------|---------|-------------|
| `get_session(page)` | `CDPSession` | Get/create CDP session for a page |
| `invalidate_session(page)` | `None` | Remove cached session |
| `invalidate_all()` | `None` | Clear all cached sessions |
| `get_frame_tree(page)` | `Dict` | Get frame hierarchy via `Page.getFrameTree` |
| `get_all_frame_ids(page)` | `List[Dict]` | Collect all frame IDs, URLs, names |
| `get_ax_tree(page, frame_id?)` | `List[Dict]` | Get accessibility tree (optionally per-frame) |
| `get_all_ax_trees(page)` | `Dict[str, List]` | A11y trees for all frames |
| `capture_dom_snapshot(page, computed_styles?)` | `Dict` | Full layout snapshot via `DOMSnapshot.captureSnapshot` |
| `get_layout_metrics(page)` | `Dict` | Viewport size and DPR |
| `get_viewport_size(page)` | `(float, float)` | CSS viewport width/height |
| `get_device_pixel_ratio(page)` | `float` | DPR value |
| `get_cross_origin_iframe_content(page, main_url?)` | `List[Dict]` | A11y content for cross-origin iframes |
| `evaluate_js(page, expression, frame_id?)` | `Any` | JS evaluation via CDP `Runtime.evaluate` |

**CDP session management**: Sessions are cached per-page and reused across operations. A session is tied to the page target, not the URL — navigation does not invalidate it.

### `kairos/browser_manager.py` — BrowserManager

**Class**: `BrowserManager`

Uses a dedicated `_WorkerThread` that keeps `sync_playwright()` alive for its entire lifetime (avoids greenlet errors).

**Worker Thread** (`_WorkerThread`):
- `start()` — spawns thread, initializes Playwright, signals `_started` event when ready
- `dispatch(fn, timeout)` — queues callable, blocks using `threading.Event` for zero-latency notification; raises `TimeoutError` if task never completes
- `stop()` — sends sentinel, joins thread

**Launch modes**:
1. **Ephemeral** (no profile): `pw.chromium.launch()` + `browser.new_context()`
2. **Named profile**: `pw.chromium.launch_persistent_context(profile_dir)` — stores at `~/.kairos/profiles/<name>`
3. **CDP**: `pw.chromium.connect_over_cdp(cdp_url)` — connects to running Chrome
4. **Chrome profile copy**: Copies real Chrome user data dir to `~/.kairos/profiles/_chrome_copy_<name>`, launches persistent context with copy

**CloakBrowser integration**: When `pip install cloakbrowser` is available, uses its stealth Chromium binary and `build_args()` for fingerprint patches. Falls back to standard Playwright Chromium.

**Key operations** (all dispatched to worker thread):
- `navigate(url)` — `page.goto(url, wait_until="domcontentloaded")`, clears active frame; reports specific error types (DNS, connection, timeout) on failure. Auto-screenshots after navigate.
- `go_back()` / `go_forward()` / `reload()` — navigation history, clear active frame
- `scroll(direction?, pages?)` — scroll page using `page.mouse.wheel()` by viewport heights. `direction="down"|"up"`, `pages=1.0` (full viewport)
- `wait(seconds?)` — sleep for up to 30 seconds to let animations/AJAX complete
- `wait_for(selector?, text?, timeout?)` — wait for a specific element to become visible or text to appear (uses Playwright's built-in wait mechanisms, much more efficient than blind waiting)
- `send_keys(keys)` — send keyboard shortcut via `page.keyboard.press()` (e.g. "Enter", "Tab", "Control+a")
- `hover(selector)` — hover over an element to trigger hover states (dropdown menus, tooltips, hover cards). Uses Playwright's `locator.hover()` with text fallback.
- `hover_by_index(index)` — hover by snapshot index (PREFERRED)
- `click(selector)` — tries CSS selector, visible text, label/aria-labelledby click for hidden elements, then JS `getElementById` force-click. Auto-screenshots. Detects new tabs and auto-switches.
- `click_by_index(index)` — click element by its snapshot index `[0]`, `[1]`, etc. PREFERRED over `click(selector)` for reliability.
- `click_xy(x, y)` — `page.mouse.click(x, y)` for coordinate-based clicking (vision fallback)
- `drag(selector_from, selector_to)` — drag-and-drop between elements via bounding box calculation with smooth 10-step mouse movement
- `drag_xy(x1, y1, x2, y2)` — coordinate-based drag
- `type_text(selector, text, press_enter?)` — tries `fill()` first (fast, fires events), falls back to `click + type()` (keystroke simulation), then placeholder fallback. Verifies value after each attempt.
- `type_by_index(index, text, press_enter?)` — type into element by snapshot index. PREFERRED over `type_text`.
- `select_option(selector, value)` — tries by HTML `value` attribute, then visible `label` text, then numeric index, then JS fallback
- `select_option_by_index(index, value)` — select by snapshot index (PREFERRED). Validates that target is a `<select>`.
- `snapshot()` — executes `_SNAPSHOT_JS` (inline JS that extracts accessibility tree), formats into compact text. Caches elements in `_last_snapshot_elements` for index-based interactions. Appends cross-origin iframe a11y data via CDP. Question context lines (`↳ Q:`) shown below radio/checkbox/select elements.
- `screenshot(full_page?)` — saves PNG to `~/.kairos/screenshots/screenshot_<timestamp>.png`, returns file path + base64 data URL for vision injection
- `search_page(pattern, regex?, case_sensitive?, max_results?)` — in-page text search via `document.createTreeWalker` + regex. Returns matches with context. Zero LLM cost.
- `find_elements(selector, max_results?)` — CSS selector query returning matching elements with index, tag, text, attributes. Zero LLM cost.
- Tab management: `open_new_tab()`, `switch_tab()`, `list_tabs()`, `close_tab()` (invalidates CDP session on close)
- `evaluate(expression)` — tries expression directly first, wraps in arrow function on `SyntaxError` fallback; returns JSON-stringified result
- Frame management: `switch_frame(frame_selector?)` — uses CDP `get_all_frame_ids()` as fallback for cross-origin iframes, stores CDP frame info in `_active_frame_type="cdp"`

**Internal**:
- `_target()` — returns `_active_frame` if set and is a Playwright Frame; if `_active_frame_type == "cdp"`, falls back to `current_page` (cross-origin content handled by `_get_cross_origin_snapshot_section()`). Returns `current_page` if no frame active.
- Frame references are cleared on `navigate()`, `go_back()`, `go_forward()`, and `reload()` (both `_active_frame` and `_active_frame_type`).
- `_post_action(result, pre_fingerprint?, is_navigation?)` — common post-action hook; optionally takes screenshot+snapshot after any interaction.
- `_detect_new_tab(tabs_before_count)` — compares `len(self._pages)` before/after an action.
- Snapshot elements are cached in `_last_snapshot_elements` after each `snapshot()` call.
- CDP support via `self._cdp` (CDPManager instance): used in `snapshot()` for cross-origin iframe a11y content, in `switch_frame()` for cross-origin frame detection, and in `close_tab()` to invalidate stale sessions.

**Snapshot JS** (`_SNAPSHOT_JS`): Extracts:
- **Shadow DOM piercing**: Uses `queryShadow()` to recursively walk into `.shadowRoot` on every element, making web components inside Shadow DOM visible
- **Ancestor visibility checking**: `isVisible()` now walks up ALL ancestors (not just the element itself) — detects CSS-hidden parents, `display:none`, `visibility:hidden`, `opacity:0` at any level. Prevents selecting elements that are invisible due to hidden parent containers.
- **Off-viewport detection**: `isInViewport()` checks if the element intersects the viewport (with 1000px margin below for scroll-reachable elements). Elements flagged with `_offscreen: true` to help the model distinguish "hidden because scrolled past" vs "hidden because off-screen".
- Interactive elements with computed CSS selectors (id > name > data-testid > class path)
- Hidden radio/checkbox inputs (always included, even when CSS-hidden — commonly used in quiz forms)
- Associated `<label>` text for radio/checkbox inputs (via `label[for]` and wrapping `<label>`)
- **Question context** for radio/checkbox/select elements: walks up the DOM via `findQuestionContext()` to find the nearest question text (Moodle `.qtext`, `.formulation`, headings, "Question N" patterns). Included as `context` field on element entries.
- Headings (h1-h4)
- Text blocks (p, label, li, dt, dd) — up to 40 blocks (increased from 20)
- Form state (input values, textarea content, select options with visible text and value attributes)
- Additional ARIA roles: `radio`, `checkbox`, `option`, `listbox`, `combobox`, `menuitemcheckbox`, `menuitemradio`

**Click fallback chain** (with post-click verification):
1. CSS selector click via Playwright `locator(selector).click()`
2. `page.get_by_text()` — matches visible text
3. Label click — finds `<label for="id">`, `aria-labelledby` target (Moodle pattern), or wrapping `<label>` and clicks it (for hidden radio/checkbox)
4. JavaScript `el.click()` — last resort force-click: parses `[id="..."]` attribute selectors to extract raw ID, then uses `document.getElementById(id).click()` (immune to colons and special chars that break `querySelector`).

**Post-click verification**: Before clicking, snapshots URL and title. After clicking, checks for URL change, title change, modal/dropdown appearance, and radio/checkbox state. Reports what changed or warns "no visible page state change detected" so the agent knows the click may not have had the expected effect.

### `kairos/tools/base.py` — ToolResult

```python
class ToolResult:
    def __init__(self, success: bool, output: str, error: Optional[str] = None,
                 workspace_changed: Optional[str] = None, image_url: Optional[str] = None):
```

`to_dict()` returns `{"success": bool, "output": str, "error": str|None, "image_url": str|None}`. The `image_url` field is only included when present (used by `ReadTool` for images).

### `kairos/tools/read.py` — ReadTool

```python
class ReadTool:
    def __call__(self, path: str) -> ToolResult:
```

- **Text files**: UTF-8 decoded with `errors="replace"`, max 100KB
- **Image files** (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, `.tiff`, `.tif`, `.svg`): base64 data URL returned as `image_url` on ToolResult, max 20MB
- Returns `ToolResult(False, "", "File not found: ...")` or `"Not a file: ..."` on errors

### `kairos/tools/write.py` — WriteTool

```python
class WriteTool:
    def __call__(self, path: str, content: str) -> ToolResult:
```

Creates parent directories automatically (`mkdir(parents=True, exist_ok=True)`). Writes UTF-8.

### `kairos/tools/edit.py` — EditTool

```python
class EditTool:
    def __call__(self, path: str, oldText: str, newText: str) -> ToolResult:
```

- Finds ALL occurrences of `oldText` via `str.find()` loop
- **0 matches**: Error with line count + similar text locations (checks first 20 chars of `oldText` against each line)
- **Multiple matches**: Error with line numbers of each occurrence
- **1 match**: Replaces, writes file, reports line number

### `kairos/tools/search.py` — SearchTool

```python
class SearchTool:
    SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox",
                 ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs", "*.egg-info"}
    
    def __call__(self, pattern: str, path: Optional[str] = None,
                 include: Optional[str] = None, max_results: int = 50) -> ToolResult:
```

- Compiles `pattern` as `re.IGNORECASE` regex
- Uses `os.walk` with `SKIP_DIRS` pruning
- `include` glob converted via `fnmatch.translate()`
- Binary detection: reads first 512 bytes, skips if contains `\x00`
- Returns `"file:line: text"` format

### `kairos/tools/git.py` — GitTool

```python
class GitTool:
    def __init__(self):
        self._workspace = None  # Set via set_workspace()
    
    def set_workspace(self, path: str): ...
    def __call__(self, command: str, **kwargs) -> ToolResult:
```

Sub-commands:
- `status` → `git status --porcelain`
- `diff` → `git diff [-- path]`
- `log` → `git log --oneline -n N` (default N=10)
- `commit` → `git add -A && git commit -m "message"` (stages everything)
- `branch` → `git branch --list`

All run via `subprocess.run(["git"] + args, cwd=self._workspace)`.

### `kairos/tools/subagent.py` — SubAgentTool

```python
class SubAgentTool:
    def __init__(self, workspace: str, client: Any, model: str):
        self._subagents: Dict[str, Dict[str, Any]] = {}  # id -> {status, result, thread}
    
    def spawn(self, prompt: str, mode: str = "blocking") -> ToolResult:
    def get_result(self, subagent_id: str) -> ToolResult:
```

**`spawn()`**:
1. Generates UUID-based ID
2. Creates child `Agent` with `_is_subagent = True`, shares parent's `client` and `model`
3. **Blocking**: Calls `sub_agent.run(prompt)` synchronously, returns result
4. **Non-blocking**: Starts daemon thread, returns ID for polling

**`get_result()`**: Returns `"Running..."` if still going, otherwise pops and returns the result.

**Sub-agent restrictions** (enforced in `Agent._get_tool_schema()`):
- No `spawn_subagent` / `get_subagent_result`
- No browser tools (19 tools removed)
- Has: read, write, edit, search, git, all 5 terminal tools, and 3 skill tools (13 total)

### `kairos/tools/skills.py` — SkillManager

```python
class SkillManager:
    SKILL_FILENAME = "SKILL.md"
    
    def __init__(self, skills_dir: str): ...
    def list_skills(self) -> ToolResult: ...
    def load_skill(self, skill_name: str) -> ToolResult: ...
    def write_skill(self, skill_name: str, content: str, overwrite: bool = False) -> ToolResult: ...
```

Skills are stored under `<workspace>/skills/<skill_name>/SKILL.md`. Only skill names are loaded into the system prompt; full content is fetched on demand.

**`list_skills()`**: Scans skills directory for subdirectories containing `SKILL.md`, returns comma-separated names.

**`load_skill(skill_name)`**: Validates name, reads `skills/<skill_name>/SKILL.md`, returns full content. Lists available skills in error if not found.

**`write_skill(skill_name, content, overwrite=False)`**: Validates name (no `..`, `/`, `\`, special chars), creates folder if needed, writes `SKILL.md`. If skill exists and `overwrite=False`, returns error with instruction to set `overwrite=true`.

### `kairos/tools/browser.py` — Browser Tools

30 callable wrapper classes, each takes a `BrowserManager` instance:

| Class | Method Called |
|-------|-------------|
| `BrowserLaunchTool` | `bm.launch(profile, headless, proxy, humanize, chrome_profile, connect_cdp)` |
| `BrowserNavigateTool` | `bm.navigate(url)` |
| `BrowserGoBackTool` | `bm.go_back()` |
| `BrowserGoForwardTool` | `bm.go_forward()` |
| `BrowserReloadTool` | `bm.reload()` |
| `BrowserClickTool` | `bm.click(selector)` |
| `BrowserClickIndexTool` | `bm.click_by_index(index)` |
| `BrowserTypeTool` | `bm.type_text(selector, text, press_enter)` |
| `BrowserTypeIndexTool` | `bm.type_by_index(index, text, press_enter)` |
| `BrowserSelectTool` | `bm.select_option(selector, value)` |
| `BrowserSelectIndexTool` | `bm.select_option_by_index(index, value)` — PREFERRED |
| `BrowserScrollTool` | `bm.scroll(direction, pages)` |
| `BrowserWaitTool` | `bm.wait(seconds)` |
| `BrowserWaitForTool` | `bm.wait_for(selector?, text?, timeout?)` — wait for element/text condition |
| `BrowserSendKeysTool` | `bm.send_keys(keys)` |
| `BrowserSearchPageTool` | `bm.search_page(pattern, regex, case_sensitive, max_results)` |
| `BrowserFindElementsTool` | `bm.find_elements(selector, max_results)` |
| `BrowserSnapshotTool` | `bm.snapshot()` |
| `BrowserScreenshotTool` | `bm.screenshot(full_page)` |
| `BrowserTabListTool` | `bm.list_tabs()` |
| `BrowserTabSwitchTool` | `bm.switch_tab(index, url_pattern)` |
| `BrowserTabOpenTool` | `bm.open_new_tab(url)` |
| `BrowserEvaluateTool` | `bm.evaluate(expression)` |
| `BrowserCloseTool` | `bm.close()` |
| `BrowserClickXYTool` | `bm.click_xy(x, y)` — coordinate-based click for vision fallback |
| `BrowserHoverTool` | `bm.hover(selector)` — hover for dropdowns/tooltips |
| `BrowserHoverIndexTool` | `bm.hover_by_index(index)` — PREFERRED |
| `BrowserDragTool` | `bm.drag(selector_from, selector_to)` — drag-and-drop |
| `BrowserDragXYTool` | `bm.drag_xy(x1, y1, x2, y2)` — coordinate-based drag |
| `BrowserSwitchFrameTool` | `bm.switch_frame(frame_selector)` — switch into/out of iframes |

Each returns `ToolResult`. `BrowserLaunchTool` catches `ImportError` specifically to give installation instructions.

### `kairos/tools/session.py` — SessionManager

```python
class SessionManager:
    def __init__(self):
        CHATS_DIR.mkdir(exist_ok=True)  # CHATS_DIR = Agent2/chats/
        self._current_session_id: Optional[str] = None
    
    def save_chat(self, conversation_history: List[Dict]): ...
    def new_session(self): ...
    def set_current_session(self, session_id: str): ...
    def list_sessions(self) -> List[Dict[str, str]]: ...
    def load_session(self, session_id: str) -> Optional[List[Dict]]: ...
```

**`_load_all()`** — corruption-recovering loader:
- Tries `json.loads()` first
- On `JSONDecodeError`: uses `JSONDecoder.raw_decode()` to parse up to the last valid JSON boundary, re-saves the recovered data
- Last resort: iterates through JSON blocks and merges any that parse successfully
- Returns `{}` if completely unrecoverable

**`_save_all()`** — atomic writer:
- Writes to a temp file in the same directory, then `fsync()`s, then atomically replaces the target via `os.replace()` with a 3-attempt retry loop (handles transient PermissionError from antivirus/indexers on Windows)
- Prevents file corruption from interrupted writes (Ctrl+C, crash, power loss) — the target file is only replaced after the full write succeeds
- Cleans up the temp file on failure

**`save_chat()`** deduplication strategy:
1. If `_current_session_id` set and exists on disk → update it
2. Otherwise → create new entry keyed by `chat_<timestamp>`

No fuzzy/prefix matching — each session is tracked by its ID. This prevents different sessions from accidentally overwriting each other.

**Session data** (in `chats.json`):
```json
{
  "chat_2024-01-15 10:30:00": {
    "timestamp": "2024-01-15 10:30:00",
    "preview": "Fix the login bug",
    "messages": [...]
  }
}
```

---

## Design Principles

1. **Streaming First** — Tokens print as they arrive; no waiting for full response
2. **Absolute Paths** — No workspace containment
3. **One Tool Per File** — Easy to add new tools
4. **Interruptible** — Ctrl+C hard-interrupts; Escape gracefully stops between steps
5. **Token Aware** — Session, context, and turn token counts displayed
6. **Lazy Config** — Environment loaded on first access, not import
7. **Minimal Dependencies** — `openai`, `python-dotenv`, `rich`, `prompt_toolkit`, `tiktoken`, `playwright`

## Adding New Tools

1. Create `kairos/tools/your_tool.py` with a callable class returning `ToolResult`
2. Import in `kairos/tools/__init__.py` and add to `__all__`
3. Import in `kairos/agent.py` (top-level imports block)
4. Add tool schema to `Agent._get_tool_schema()` (OpenAI function definition)
5. Add dispatch entry to `Agent._execute_tool()` (lambda calling the tool)
6. Add summary case to `Agent._tool_summary()` (one-liner for CLI display)
7. If it should be excluded from sub-agents, add its name to the exclusion list in `_get_tool_schema()`
8. **Update this AGENTS.md and README.md**
