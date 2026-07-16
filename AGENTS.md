# Kairos Architecture Documentation

**MANDATORY: Whenever you make any code change (edit, add, or remove code), you MUST also update this AGENTS.md file AND README.md to reflect the change. This ensures the documentation stays in sync with the code. Failure to update documentation after a code change is not acceptable.**

## Overview

Kairos is a minimal coding agent written in Python. It uses the OpenAI chat completions API with streaming and function calling to autonomously execute tasks through 40 tools. All file operations use absolute paths — no workspace containment.

## Project Structure

```
Agent2/
├── main.py                 # Root entry point (imports from kairos.main)
├── temp.py                 # Headless agent runner — run_agent(prompt) with no CLI
├── run_temp_cli.py         # Textual frontend launcher
├── .env                    # Environment configuration (API keys)
├── .env.example            # Template for .env file
├── requirements.txt        # Python dependencies
├── pyproject.toml          # Project metadata and build configuration
├── README.md               # User-facing documentation
├── AGENTS.md               # This file - architecture documentation
├── gateway_main.py         # Gateway server shim
├── kairos_cli_new.bat      # Standard CLI launcher; starts the gateway if health is unavailable
├── kairos_old.bat          # Windows legacy CLI launcher (runs Agent2 source while preserving caller CWD)
├── kairos.bat              # Windows shortcut (py main.py)
├── chats/                  # Legacy workspace-local/direct-caller chat sessions (gitignored)
│   └── chats.json          # Read-compatible fallback; new saves use ~/.kairos/chats/<workspace>--<path-id>/chats.json
├── skills/                 # Skills directory (gitignored, stays local)
│   └── moodle-quiz/        # Example: Moodle quiz skill
│       └── SKILL.md
├── tests/                  # Local regression tests
│   ├── test_compaction.py  # Context accounting, compaction, and legacy-result tests
│   └── test_sessions.py    # Workspace-isolated home chat storage and legacy loading tests
└── kairos/
    ├── __init__.py         # Exports: Config, Agent, ToolResult, SessionManager, SkillManager, TerminalManager, BrowserManager
    ├── main.py             # CLI REPL loop, signal handlers, auto-save, paste resolution
    ├── gateway_main.py     # `python -m kairos.gateway_main`
    ├── main_gateway.py     # Compatibility gateway entry point
    ├── resume.py           # Shared saved-history repair and mid-execution resume logic
    ├── gateway/             # SQLite-backed runtime manager and FastAPI protocol
    ├── config.py           # Lazy .env loading via lru_cache
    ├── agent.py            # Core agent: streaming, tool dispatch, compaction, error handling
    ├── cli.py              # Terminal UI: streaming panels, thinking dots, paste handling, session picker, KairosMarkdown for enhanced table rendering
    ├── temp_cli.py         # Textual frontend, workspace-aware numbered resume picker
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
        └── session.py      # SessionManager — isolated ~/.kairos chat stores with legacy fallback loading
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

`kairos_old.bat` is a PATH-safe legacy launcher. It invokes this root entry point by absolute path while preserving the caller's current directory, so the current directory remains the Agent workspace even when the launcher is found through `PATH`.

`kairos_cli_new.bat` is the standard CLI launcher. It reads the gateway host/port through `Config`, checks `/healthz`, starts `python -m kairos.gateway_main` in a minimized window when needed, waits up to 30 seconds for readiness, and then invokes the standard `main.py` while preserving the caller's workspace CWD. An optional first argument selects an explicit workspace.

### `kairos/__init__.py`

Exports: `Config`, `Agent`, `ToolResult`, `SessionManager`, `SkillManager`, `TerminalManager`, `BrowserManager`.

### `temp.py` — Headless Agent Runner

Run one or many agents without the CLI. Supports single runs and concurrent execution via `ThreadPoolExecutor`:

```python
from temp import run_agent, run_agents

# Single agent
response = run_agent("read and summarize /path/to/file.py")

# Multiple agents running concurrently
prompts = ["task 1", "task 2", "task 3"]
responses = run_agents(prompts, max_workers=5)  # returns list in same order
```

**Functions**:
- `run_agent(prompt, workspace=r"C:\Users\arjra") -> str` — single agent, blocks until done
- `run_agents(prompts, max_workers=5) -> list[str]` — runs all prompts in parallel threads, returns responses in input order

Each agent gets its own `Agent` instance (separate conversation history, terminal, browser). Can also be run directly: `python temp.py "your prompt"` or `python temp.py` to run the built-in template loop.

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

| `Config.MAX_TOOL_RESULT_CHARS()` | `int` | 20,000 |
| `Config.CONTEXT_WINDOW()` | `int` | 262,000 |

`Config.MAX_TOOL_RESULT_CHARS()` reads the optional `KAIROS_MAX_TOOL_RESULT_CHARS` setting. Invalid or non-positive values fall back to the 20,000-character default.

`Config.CONTEXT_WINDOW()` reads the optional `KAIROS_CONTEXT_WINDOW` setting. It is the conservative prompt budget used by `TokenCounter`, dynamic keep-recent compaction, and compaction-prompt budgeting; invalid or non-positive values fall back to 262,000. Set it to the actual context limit accepted by the configured gateway/model.

### `kairos/main.py` — CLI Entry Point

**Function**: `main()` — orchestrates the entire application lifecycle.

**Resume flow**: `/resume` delegates to `kairos.resume.sanitize_history_for_resume()`. It anchors resume decisions at the latest real user request, repairs incomplete assistant/tool chains with synthetic failed results, and automatically sends `Continue where you left off. Pick up the next step.` for mid-execution sessions. The standard CLI accepts both `/resume` followed by a numbered picker entry and `/resume 1`; its picker consumes buffered handoff input and retries invalid choices instead of returning to the agent prompt. The Textual frontend uses the active agent workspace's isolated `~/.kairos/chats/<directory>--<stable-path-id>/chats.json` store (with legacy workspace-local loading), extracts the selected metadata dictionary's string `id`, and keeps selection mode active until a valid chat is loaded.

**Global state**: `_session_mgr` and `_agent` are module-level globals shared with signal handlers.

**Signal handlers** (installed at startup):
- `SIGINT` → `_save_now()` + `sys.exit(0)`
- `SIGTERM` → `_save_now()` + `sys.exit(0)`
- `SIGHUP` (Unix only) → `_save_now()` + `sys.exit(0)`

**Auto-save**: `_start_auto_save(agent, interval_seconds=60)` runs a daemon thread that saves every 60 seconds.

**`process_request(cli, agent, user_input, image_url?)`**:
1. Starts an Escape key listener (via `cli.start_escape_listener`); Escape invokes the same immediate hard-stop path as Ctrl+C
2. Runs `agent.run(user_input, image_url)` in a background thread
3. Main thread polls `t.join(timeout=0.15)` — catches `KeyboardInterrupt` to call `agent.interrupt()`
4. Hard stop closes the active stream and cancels active blocking terminal commands; the worker is joined fully before stdin is handed back, preventing history/callback races
5. Stops the Escape listener completely before the next prompt owns stdin, and returns the agent's response or `"[Interrupted]"`

The Escape listener shares the terminal with `PromptSession`, so it buffers ordinary characters (including a complete command typed during the response-to-prompt handoff) instead of discarding them. Escape is an immediate hard stop, not a graceful end-of-step request. The listener uses short polling waits and no terminal-input flush, preventing a command such as `/exit` from being lost and requiring a second entry.

**REPL loop** (inside `main()`):
1. `cli.get_user_input()` → returns prompt text and handles any buffered handoff input
2. `_resolve_paste_input()` resolves text/image tokens before command dispatch, so pasted aliases such as `/exit` are handled locally
3. Command dispatch: `exit`, `clear`, `reset`, `/resume`, `/compact`, `/paste`
4. Clipboard image auto-detection on empty input or alongside text
5. `cli.start_thinking()` → `process_request()` → `cli.stop_thinking()`
6. Response display (streaming panel handles it; `_skip_print_response` prevents double-print)
7. Auto-save after each exchange (all saves go through `_save_now()` which holds `_auto_save_lock` to prevent race conditions with the auto-save thread)

**Resume sanitization** (`kairos.resume.sanitize_history_for_resume(history)`):
- Anchors the decision at the latest real user request, so an older clean response cannot hide a newer interrupted request
- Ignores internally generated screenshot, compaction, and background-notification user messages when identifying the latest request
- Repairs incomplete tool chains by preserving matching results in call order, adding synthetic failed results for missing calls, and removing orphaned results
- Returns `(sanitized_history, last_agent_content, is_mid_execution)` — `(None, "", False)` if no resumable state exists
- The CLI displays the last clean response on normal resume and auto-continues mid-execution sessions with `Continue where you left off. Pick up the next step.`

The Textual frontend (`kairos/temp_cli.py`) uses the same shared sanitizer and continuation behavior. It uses `SessionManager(agent.cwd)` so both frontends list the same workspace-isolated sessions, and its numbered picker passes `sessions[index]["id"]` to `load_session()` while retaining selection mode after invalid or unloadable entries.

`kairos.main._sanitize_history_for_resume` remains a compatibility alias to the shared function for existing callers.

**Wiring** (in `main()`): The agent's callbacks are wired to CLI methods:
```python
agent.on_tool_call = lambda name, args: cli.print_tool_summary(agent._tool_summary(name, args))
agent.on_stream_start = lambda: cli.start_stream()
agent.on_stream_token = cli.on_stream_token
agent.on_stream_end = _on_stream_end  # Finalizes as green response or grey thinking trace
agent.on_token_update = lambda tc: cli.print_token_status(tc)
agent.on_compact = lambda msg: cli.print_info(msg)
agent.on_background_notification = cli.print_background_notification
# Sub-agent visibility:
agent.subagent_tool._tool_printer = lambda summary: cli.console.print(f"  ↓ subagent: {summary}")
agent.subagent_tool._stream_start = lambda: cli.start_stream()
agent.subagent_tool._stream_token = cli.on_stream_token
agent.subagent_tool._token_update = lambda subagent_id, tc: cli.print_subagent_token_status(subagent_id, tc)
agent.subagent_tool._stream_end = lambda _content, _has_tools: cli.finish_stream()
```

### `kairos/resume.py` — Saved-history Repair

`sanitize_history_for_resume(history)` is the shared repair helper used by both interactive frontends. It returns `(history_or_none, last_agent_content, is_mid_execution)`, recognizes agent-generated user messages, removes trailing screenshot injections from unfinished turns, makes incomplete tool-call chains valid before continuation, and the Textual frontend saves the resumed continuation back into the selected session.

### `kairos/agent.py` — Agent (Core)

**Immediate stop**: `interrupt()` and `request_stop()` share a hard-stop event. The active OpenAI stream is closed when possible, blocking terminal commands are killed through `TerminalManager.cancel_active_commands()`, and the worker checks the event before/during streaming and between tools. Tool results are checkpointed individually so completed calls survive an interruption. `process_request()` waits for the worker's stable cleanup boundary before reopening the prompt; the next request can then use the shared resume repair for a cut-off stream or incomplete tool chain.

**Background terminal notifications**: `Agent` retains completed background-terminal events in the manager queue. While `Agent.run()` is processing, completions display immediately through `on_background_notification`; while idle they stay quiet and are shown when the next request drains them. They are inserted as a separate user message after the next real user message (or before the next API step if the agent is still working). Notification output is capped by the terminal manager.

**Tool-result limits**: `_execute_tool()` caps textual `output` and `error` fields before serialization and history insertion. The default per-result limit is 20,000 characters. Truncation preserves the beginning and end, records the original length in a marker, and never copies inline image URLs into the textual tool message. Screenshot/read images are stripped from the tool result and re-injected as a separate user vision message. Configure the text limit with `KAIROS_MAX_TOOL_RESULT_CHARS`. Full background-terminal output remains available through `read_logs`.

**Class**: `Agent`

**Constructor**: `Agent(workspace: str)`
- Creates `OpenAI` client from config
- Sets `self.cwd = Path(workspace).resolve()`
- Initializes all 40 tool instances
- Calls `_setup_system_prompt()` which builds the system prompt and initializes `conversation_history`

**Key attributes**:
- `self.client` — `OpenAI` instance
- `self.model` — model name string
- `self.cwd` — workspace path (`Path`)
- `self.tokens` — `TokenCounter` instance
- `self.conversation_history` — `List[Dict]` (starts with system prompt)
- `self._interrupt_event` — `threading.Event` for Ctrl+C
- `self._stop_requested` — `bool` for Escape
- `self._is_processing` — guarded request-state flag used to decide whether background completions are displayed immediately
- `self._is_subagent` — `bool` (True = no browser/subagent tools)
- `self.terminal_manager` — `TerminalManager` instance
- `self.browser_manager` — `BrowserManager` instance
- `self.subagent_tool` — `SubAgentTool` instance (None if sub-agent)
- `self.skill_manager` — `SkillManager` instance

**Callbacks** (set by `main.py`; the background notification callback is optional):
- `on_tool_call(name: str, args: dict) -> None`
- `on_stream_start() -> None`
- `on_stream_token(token: str) -> None`
- `on_stream_end(content: str, has_tool_calls: bool) -> None`
- `on_token_update(counter: TokenCounter) -> None` — parent-agent token status after each turn
- `on_compact(status_msg: str) -> None`
- `on_background_notification(message: str) -> None` — visible completion notice while processing; idle notices remain queued until the next request

`SubAgentTool._token_update(subagent_id, counter)` is an optional frontend callback. Each child agent installs it as its `on_token_update` callback, so blocking and non-blocking subagents report their own session/context/turn token status without mutating the parent's `TokenCounter`. The legacy CLI and Textual frontend display these updates with the child ID.

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

Dispatch dict mapping tool names to lambdas. Each calls the corresponding tool instance and returns capped JSON. Text output and errors are centrally limited before entering `conversation_history`: the default is 20,000 characters per result. Oversized text preserves its beginning and end and includes the original character count. `KAIROS_MAX_TOOL_RESULT_CHARS` can override the default. Image URLs are not text-truncated. Catches all exceptions and returns capped error JSON.

#### Tool Summaries (`_tool_summary(name, args)`)

Static method. Returns a one-line human-readable summary string for each tool call (used by CLI display).

#### Streaming (`_stream_response()`)

Returns `(full_content: str, assembled_tool_calls: List[Dict], api_usage: Dict | None)`.

**Retry logic**: API requests make an initial attempt plus two retries for retryable errors (rate limits, connection errors, timeouts, network errors, interrupted chunked streams such as `RemoteProtocolError`, and 500/502/503/504 responses). Exponential backoff is used between attempts. Retries cover both request creation and streaming response iteration, and partial stream content is discarded before retrying.

When all attempts fail, `APIRequestError` carries concise one-line diagnostics (exception type/message, relevant status/request ID, hints, model, and base URL) without an exception traceback. The CLI suppresses traceback output for normal request failures so gateway errors do not dump `httpx`/`httpcore` internals.

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
8. If tool calls: when using tiktoken fallback, counts tool call argument tokens via `add_output_tokens()` (API counts already include these); executes each via `_execute_tool()`, checkpoints each completed tool result immediately, truncates history if >10,000,000 messages (effectively disabled), calls `tokens.finish_turn()`

**Important**: Tool results have `image_url` stripped before appending to history; each result is checkpointed immediately so an interrupted batch preserves completed calls. Screenshot images are re-injected as a user vision message (with `[Screenshot captured]` prefix) so the model can actually see them, since tool messages can't carry images on most providers. Textual tool results are centrally capped before appending; image URLs are preserved separately and counted with a bounded vision estimate. Legacy saved tool results are normalized before the next API request; an old embedded image is replaced with a short omission marker because its original vision message cannot be safely reconstructed. Tool results are NOT counted as output tokens — they become input tokens in the next turn via `start_turn()`.

#### Run (`run(user_message, image_url?)`)

After a hard stop, the next request is wrapped as a continuation instruction so the model can resume the unfinished work while incorporating the new request. The main agent loop:
1. Preserves an interrupt arriving during startup rather than clearing it
2. Loops indefinitely until one of the termination conditions is met:
   - Checks the shared hard-stop event before and during streaming, between tools, and before each API step; both Escape and Ctrl+C use this path
   - Recounts the current in-memory history and checks auto-compaction before **every** API step, including steps inside a tool-call loop. This is necessary because API prompt usage is measured before tool results are appended; the next step must see the newly added history rather than waiting for another user request.
   - Auto-compacts if context > 80%, using a safe user boundary or a complete assistant/tool-call chain so active execution can continue with valid API message ordering.
   - Calls `step()`
   - **Empty response retry**: If `step()` returns no content AND no tool calls, removes the assistant message from history and retries up to 2 times. This handles transient API issues where the model returns empty responses.
   - Returns when: final response received (3+ words or tool calls), no tool calls (after retries exhausted), interrupt, or graceful stop (Escape)
3. Returns `"[Interrupted]"` on `InterruptedError`

#### Compaction

**Constants**:
- `COMPACT_RESERVE_TOKENS = 16384` — tokens for summary prompt + output
- `COMPACT_KEEP_RECENT_PCT = 0.20` — keeps 20% of the model's context window as recent context
- `COMPACT_THRESHOLD_PCT = 80.0` — auto-compact threshold

**`compact()`**:
1. `_find_compact_boundary()` — walks backward from end, accumulates the same field-aware message tokens used by preflight (including tool-call metadata and bounded vision estimates), and chooses a safe user boundary or complete assistant/tool-call chain while keeping 20% of the context window recent
2. Serializes old messages into readable text (`_serialize_messages_for_summary()`)
3. `_generate_summary()` — non-streaming API call with structured prompt
4. If existing compaction summary exists, passes it as `<previous-summary>` for incremental update
5. Rebuilds history: `[system_prompt, compaction_summary, recent_messages]`
   - Compaction message is `role: "user"` so it flows naturally in conversation ordering
   - Existing compaction summaries are detected by content prefix `"[Conversation compacted"`
6. Re-counts tokens

`auto_compact_if_needed()` is evaluated before every API step in `run()`, not just the first step of a user request. `_refresh_context_tokens()` recounts the in-memory history after tool results are appended, includes the function-tool schema, and preflights the user message about to be appended, because API prompt usage from the previous step does not include those results or the next request. Message counting includes assistant tool-call metadata/arguments and tool IDs/names; the old visible-text-only estimate could under-report the request substantially. Compaction also bounds the separate summarizer prompt to the configured context budget minus `COMPACT_RESERVE_TOKENS` and a 1,024-token framing margin, so a huge tool result cannot make the compaction request breach the same window.

**Summary format** (structured checkpoint):
```
## Goal
## Constraints & Preferences
## Progress (Done / In Progress)
## Key Decisions
## Next Steps
## Critical Context
## User Messages & Agent-to-User Messages
### User Messages
### Agent-to-User Messages
```

The compaction prompt requires the final two subsections to preserve a concise, chronological record of every user message and every substantive agent-to-user response represented by the summarized history. Tool-call-only assistant messages and tool results are excluded from the agent-to-user record. Incremental compaction must preserve and extend both records.

#### Error Handling

**`APIRequestError`**: User-facing exception raised after the initial API attempt and two retries are exhausted. It intentionally has no traceback chaining at the display boundary.

**`_format_api_error(e)`**: Produces concise diagnostics from OpenAI and low-level gateway exceptions — one-line exception type/message, status/request ID where available, a targeted hint, and config (model + base URL). It does not include response bodies or traceback text.

**`_is_retryable_error(e)`**: Returns True for `RateLimitError`, `APIConnectionError`, `APIStatusError` with 500/502/503/504, and matching timeout/connection/network/`RemoteProtocolError`/incomplete-chunk messages.

**`_stream_response()`** retries errors raised both when opening the stream and while iterating its chunks. A failed partial stream is closed in the UI and discarded before retrying.

#### Reset (`reset()`)

Rebuilds system prompt, resets token counter, closes browser if open.

#### History Truncation (`_truncate_history_if_needed()`)

Keeps `system + last MAX_HISTORY_MESSAGES (10,000,000)`. After truncation, verifies at least one `role: "user"` message survives — if not, expands the window backward to include the most recent user message. This prevents the "No user query found in messages" 400 error that occurs during long tool-call chains. The limit is effectively disabled (set to 10 million) so that history is never truncated by message count — context management is handled entirely by token-based compaction.

#### History Validation (`_validate_history_before_api()`)

Called before every API request in `step()`. `_normalize_history_for_context()` runs first so legacy saved tool results are capped and embedded `image_url` fields are removed before the request is counted. Handles two structural problems that cause 400 errors:
1. **No user message**: If the conversation history has no user message (from truncation or compaction), triggers a `compact()` to restore a valid state.
2. **Orphaned tool messages**: If trailing tool messages lack a preceding assistant message (from truncation cutting at a bad point), they are trimmed to restore valid ordering.

### `kairos/cli.py` — CLI (Terminal UI)

The saved-chat picker accepts an optional inline choice for `/resume N`, consumes complete lines buffered by the Escape listener before opening its prompt, and loops after invalid input so a numeric selection cannot fall through to normal agent processing.

**Classes**: `CLI`, `KairosMarkdown`, `_EnhancedTableElement`

**`KairosMarkdown`** (subclass of `rich.markdown.Markdown`):
Replaces the default Rich markdown table rendering with an enhanced version. Uses a custom `_EnhancedTableElement` registered via `elements` dict override to swap in `box.ROUNDED` borders, bold white headers on a subtle dark background (`grey15`), alternating row shading (`dim` on even rows), and cyan border styling. All other markdown elements (headings, code blocks, lists, etc.) fall through to Rich's default renderer unchanged.

**`_EnhancedTableElement`** (module-level, replaces `TableElement` for `"table_open"` tokens):
A `MarkdownElement` subclass that yields a `rich.table.Table` with `box.ROUNDED`, `border_style="cyan"`, `header_style="bold bright_white on grey15"`, `row_styles=["", "dim"]`, and `show_edge=True`. Implements the full `MarkdownElement` protocol (`create`, `on_child_close`, `on_enter`, `on_leave`, `__rich_console__`) so it integrates cleanly with Rich 15's `markdown-it-py` based parser.

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
- `finalize_stream_as_response()` — upgrades live panel to green border with `KairosMarkdown` rendering, sets `_skip_print_response = True`

**Paste system** (module-level):
- `_paste_registry: Dict[str, dict]` — maps token strings to `{type: "text"|"image", ...}`
- `_make_image_token()` / `_make_text_token()` — creates numbered tokens like `(Pasted Image #1)`
- `_reset_paste_counters()` — resets token counters at the start of each prompt
- `_insert_text_paste(buf, text)` — stores pasted text and inserts a visible `(Pasted Text #N)` token
- `_bracketed_paste_handler(event)` — handles `Keys.BracketedPaste` events and normalizes line endings before token insertion
- `_paste_handler(event)` — handles Ctrl+V when the terminal passes the key through; reads the clipboard only in response to that explicit key event
- `_alt_v_handler(event)` — Alt+V key binding: image paste only (reads clipboard image, inserts image token; shows `[no image on clipboard]` if none)
- `_backspace_handler(event)` — deletes the entire paste token if the cursor is inside or immediately after one
- Paste handling is event-based. It does not watch `Buffer.on_text_changed` or use the Windows clipboard sequence number, because copying new content changes that number without meaning that a paste occurred.

**Clipboard helpers** (cross-platform):
- `_check_clipboard_has_image()` — Windows: PowerShell + `System.Windows.Forms.Clipboard`, macOS: `pngpaste`, Linux: `xclip`
- `_read_system_clipboard()` — same platforms; called for explicit Ctrl+V handling
- `_detect_mime(data)` — detects PNG/JPEG/GIF/WEBP/BMP/TIFF from magic bytes
- `_image_data_to_url(data)` — converts to base64 data URL

**Escape key listener**:
- `start_escape_listener(on_escape)` — spawns a thread listening for Escape while buffering ordinary input captured during the handoff
- `stop_escape_listener()` — signals the thread and waits for it to finish before the prompt resumes ownership of stdin (no timed handoff)
- `_take_pending_input_for_prompt()` — returns a complete buffered line or pre-fills a partial line in the next prompt
- Windows: `msvcrt.kbhit()` + `msvcrt.getwch()`
- Unix: `tty.setcbreak()` + `select.select()` + `os.read()`; restores terminal settings without flushing queued input

### `kairos/tokens.py` — TokenCounter

**Class**: `TokenCounter`

**Constructor**: `TokenCounter(model: str = "gpt-4o", context_window: int | None = None)` — loads tiktoken encoding for model (falls back to `cl100k_base`) and reads `Config.CONTEXT_WINDOW()` when no explicit budget is supplied

**Attributes**:
- `session_input` / `session_output` — cumulative across all turns
- `context_tokens` — tokens in current conversation_history
- `turn_input` / `turn_output` — per-turn counters
- `context_window` — configured prompt budget (default 262,000; override with `KAIROS_CONTEXT_WINDOW`)

**Methods**:
- `start_turn(messages, extra_tokens=0)` — counts conversation history plus optional tool-schema tokens, sets `context_tokens`
- `count_request(messages, tools?)` — counts message fields, assistant tool-call metadata/arguments, and optional function-tool definitions
- `count_tools(tools)` — estimates the tokens added by the function-tool schema
- `add_output_tokens(text)` — encodes text and adds to `turn_output` (tiktoken estimate)
- `set_turn_from_api(prompt_tokens, completion_tokens)` — overrides turn counters with ground-truth values from the API's `stream_options={"include_usage": True}` response
- `finish_turn()` — adds turn totals to session totals
- `context_pct` — property: `(context_tokens / context_window) * 100`
- `format_status()` — `"Session: X in / Y out  |  Context: Z%  |  Turn: A in / B out"`

**Counting strategy**: `count_message()` includes assistant tool-call IDs/types/names/arguments and tool message IDs/names, because all of those fields are sent in the next API prompt. API-reported usage replaces estimates for completed turns, so these fields are not double-counted in session totals. Image tokens on vision content blocks are estimated with a bounded vision estimate (85 low-detail / 765 high-detail tokens), never by treating the raw base64 URL as ordinary text.

### `kairos/terminal_manager.py` — TerminalManager

Blocking commands are registered in `_active_blocking` while running. `cancel_active_commands()` kills their process trees so hard stop does not wait for a normal command timeout; background terminals remain persistent unless explicitly closed. Browser waits/polls also honor the agent's cancellation event through `cancel_active_operation()`.

**Class**: `TerminalManager`

**`create_terminal(background: bool) -> int`**:
- Background: persistent shell (`cmd /Q /k` on Windows, `bash --login` on Unix), reader thread, shell state preserved; command echo is suppressed while each wrapped command runs and its exit code is captured before wrapper commands alter it
- Blocking: no persistent process; each command gets its own subprocess
- Returns an auto-incrementing terminal ID

**`execute_command(terminal_id, command, timeout?, is_background?)`** (schema requires `timeout` whenever `is_background` is false):
- Background: submits immediately with an internal completion marker; timeout is ignored
- Blocking: requires a finite positive timeout, caps it at 20 seconds, rejects invalid values before spawning, captures output, and kills the process tree on timeout
- Timeout errors report the effective capped value; process-tree cleanup prevents child processes from keeping pipes open after a timeout
- Non-zero blocking exit codes are failures
- Validates `is_background` matches the terminal type

**Background completion notifications**:
- Completion events stay in a thread-safe FIFO queue via `drain_completed_background_commands()` for the next agent API turn, including when no UI callback is configured
- `set_completion_callback()` can notify the CLI immediately without consuming the queue
- Notifications include terminal ID, command, exit status, duration, and output capped at 12,000 characters; `read_logs()` retains complete output
- The persistent terminal stays open after command completion

**`read_logs(terminal_id, start_line, end_line?)`**: Returns background output by 1-indexed line range.

**`close_terminal(terminal_id)`**: Terminates the background process tree, marks pending commands failed, and removes the terminal.

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
- `wait_for(selector?, text?, timeout?)` — wait for a specific element to become visible or text to appear (uses Playwright's built-in wait mechanisms for selectors; for text, polls every 0.5s until timeout). Much more efficient than blind waiting.
- `send_keys(keys)` — send keyboard shortcut via `page.keyboard.press()` (e.g. "Enter", "Tab", "Control+a")
- `hover(selector)` — hover over an element to trigger hover states (dropdown menus, tooltips, hover cards). Uses Playwright's `locator.hover()` with text fallback.
- `hover_by_index(index)` — hover by snapshot index (PREFERRED)
- `click(selector)` — tries CSS selector, visible text, label/aria-labelledby click for hidden elements, then JS `getElementById` force-click. Auto-screenshots. Detects new tabs and auto-switches.
- `click_by_index(index)` — click element by its snapshot index `[0]`, `[1]`, etc. PREFERRED over `click(selector)` for reliability.
- `click_xy(x, y)` — `page.mouse.click(x, y)` for coordinate-based clicking (vision fallback)
- `drag(selector_from, selector_to)` — drag-and-drop between elements via bounding box calculation with smooth 10-step mouse movement (uses `current_page` for mouse operations)
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
- Snapshot elements are cached in `_last_snapshot_elements` after each `snapshot()` call. Cache is invalidated on `navigate()`, `go_back()`, `go_forward()`, `reload()`, and `switch_tab()` to prevent stale element references across page changes.
- CDP support via `self._cdp` (CDPManager instance): used in `snapshot()` for cross-origin iframe a11y content, in `switch_frame()` for cross-origin frame detection, and in `close_tab()` to invalidate stale sessions.

**Snapshot JS** (`_SNAPSHOT_JS`): Extracts:
- **Shadow DOM piercing**: Uses `queryShadow()` to recursively walk into `.shadowRoot` on every element, making web components inside Shadow DOM visible
- **Ancestor visibility checking**: `isVisible()` now walks up ALL ancestors (not just the element itself) — detects CSS-hidden parents, `display:none`, `visibility:hidden`, `opacity:0` at any level. Prevents selecting elements that are invisible due to hidden parent containers.
- **Off-viewport detection**: `isInViewport()` checks if the element intersects the viewport (with 1000px margin below for scroll-reachable elements). Elements flagged with `_offscreen: true` to help the model distinguish "hidden because scrolled past" vs "hidden because off-screen".
- Interactive elements with computed CSS selectors (id > name > data-testid > class path)
- Hidden radio/checkbox inputs (always included, even when CSS-hidden — commonly used in quiz forms)
- Associated `<label>` text for radio/checkbox inputs (via `label[for]` and wrapping `<label>`)
- **Question context** for radio/checkbox/select elements: walks up the DOM via `findQuestionContext()` to find the nearest question text (Moodle `.qtext`, `.formulation`, headings, "Question N" patterns). Included as `context` field on element entries. For matching questions (Moodle dropdown tables), also extracts the left-side label text (e.g. "el fósforo") and appends it to the context as `"label → question text"`.
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

`to_dict()` returns `{"success": bool, "output": str, "error": str|None, "image_url": str|None}`. The optional `image_url` field is only included when present (used by `ReadTool` and browser screenshots for vision); image data is kept out of the textual tool message and re-injected as a user vision message.

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
    DEFAULT_TIMEOUT = 10.0
    SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox",
                 ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs"}
    
    def __call__(self, pattern: str, path: Optional[str] = None,
                 include: Optional[str] = None, max_results: int = 50,
                 timeout: Optional[float] = DEFAULT_TIMEOUT) -> ToolResult:
```

- Compiles `pattern` as `re.IGNORECASE` regex
- Uses `os.walk` with `SKIP_DIRS` pruning
- `include` glob converted via `fnmatch.translate()`
- Binary detection: reads first 512 bytes, skips if contains `\x00`
- Enforces a finite, non-negative timeout (default 10 seconds) using a monotonic deadline
- Checks the deadline between files and while reading lines; timed-out searches return a failed result with any partial matches found so far
- Validates `max_results` as a positive integer
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
3. Wires the optional `_token_update(subagent_id, counter)` callback to the child's `on_token_update`
4. **Blocking**: Calls `sub_agent.run(prompt)` synchronously, returns result
5. **Non-blocking**: Starts daemon thread, returns ID for polling

**`get_result()`**: Returns `"Running..."` if still going, otherwise pops and returns the result.

Child token reports use the child's independent `TokenCounter`; they include session totals, current context percentage, and the latest turn input/output counts. They are emitted after every completed child API turn, including turns inside tool-call loops.

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

Each returns `ToolResult`. `BrowserLaunchTool` catches `ImportError` specifically to give installation instructions. The `headless` parameter is exposed in the tool schema (default: `False`). `humanize` defaults to `True`. Skills directory (`skills/`) is gitignored — skills stay local and are never pushed to GitHub.

### `kairos/tools/session.py` — SessionManager

Interactive frontends pass their active workspace to `SessionManager`, so standard and Textual `/resume` commands use the same isolated store at `~/.kairos/chats/<directory>--<stable-path-id>/chats.json`. The directory basename remains readable, while the deterministic SHA-256 path prefix prevents collisions between workspaces with the same basename. The canonical JSON envelope stores the absolute workspace path in `workspace` for verification. Direct callers that omit a workspace use the current working directory.

`<workspace>/chats/chats.json` remains a read-compatible legacy source for that workspace. Legacy-only sessions appear in the picker and are saved into the canonical store on the next save; the legacy file is never deleted or modified.

```python
class SessionManager:
    def __init__(self, workspace: str | os.PathLike[str] | None = None):
        # Canonical: ~/.kairos/chats/<directory>--<stable-path-id>/chats.json
        # Legacy fallback read: <workspace>/chats/chats.json
        self._current_session_id: Optional[str] = None
    
    def save_chat(self, conversation_history: List[Dict]): ...
    def new_session(self): ...
    def set_current_session(self, session_id: str): ...
    def list_sessions(self) -> List[Dict[str, str]]: ...
    def load_session(self, session_id: str) -> Optional[List[Dict]]: ...
```

**`_load_all()`** — loads canonical sessions first with canonical IDs winning, then merges sessions from the workspace-local legacy file. It accepts both the new envelope and the old flat session format, verifies the canonical `workspace` metadata, and recovers damaged JSON by parsing valid JSON boundaries.

**`_save_all()`** — atomic-writes the canonical envelope to `~/.kairos` through a same-directory temp file, `fsync()`, and `os.replace()` with a 5-attempt retry loop. If all replacements fail due to a transient Windows lock, it falls back to direct writing while leaving the complete temp file available.

**`save_chat()`** deduplication strategy:
1. If `_current_session_id` set and exists in the merged workspace-specific sessions → update it
2. Otherwise → create a new entry keyed by `chat_<timestamp>`

No fuzzy/prefix matching — each session is tracked by its ID. This prevents different sessions from accidentally overwriting each other.

**Session data** (in `~/.kairos/chats/<directory>--<stable-path-id>/chats.json`):
```json
{
  "workspace": "C:/Users/arja/Documents/App",
  "sessions": {
    "chat_2024-01-15 10:30:00": {
      "timestamp": "2024-01-15 10:30:00",
      "preview": "Fix the login bug",
      "messages": [...]
    }
  }
}
```

---

## Tests

`tests/test_compaction.py` contains focused regression coverage for function-tool/schema accounting, message metadata, bounded vision estimates, pending-user preflight, safe tool-chain boundaries, compaction-prompt limits, and normalization of oversized legacy saved results. `tests/test_sessions.py` covers deterministic workspace IDs, home-directory storage, JSON workspace metadata, workspace isolation, and loading/migrating legacy workspace-local chats. Run the suite with `python -m unittest discover -s tests -v`.

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

## Gateway Architecture (feature/gateway-v2)

The gateway layer under `kairos/gateway/` provides durable SQLite-backed conversations, resumable histories, Agent runtime lifecycle management, per-conversation serialization, cross-conversation worker concurrency, REST/WebSocket APIs, authentication, event fan-out, and event replay. The root `gateway_main.py` shim, `python -m kairos.gateway_main`, and `python -m kairos.main_gateway` start the FastAPI gateway. `kairos_cli_new.bat` is the Windows standard-CLI launcher: it probes `/healthz`, starts the gateway only when unavailable, waits for readiness, and preserves the caller's workspace CWD. CLI, Electron, and Telegram clients use the gateway rather than creating Agents or owning browser/terminal resources.

Gateway configuration is exposed through `KAIROS_GATEWAY_HOST`, `KAIROS_GATEWAY_PORT`, `KAIROS_DEFAULT_WORKSPACE`, `KAIROS_DATA_DIR`, `KAIROS_MAX_CONCURRENT_RUNS`, `KAIROS_RUNTIME_IDLE_SECONDS`, `KAIROS_AUTH_TOKEN`, and `KAIROS_LEGACY_CHAT_FILE`. Workspace paths remain context rather than a security boundary. `Config.MAX_TOOL_RESULT_CHARS()` and `Config.CONTEXT_WINDOW()` configure model-facing tool-result limits and conservative context accounting.

`GatewayManager` accepts an injectable `agent_factory` for tests. Runtime operations use state/lifecycle locks, FIFO run queues, stable persistence boundaries, interruption, queued cancellation, manual compaction, continuation repair, idle unloading, and shutdown cleanup. Resource cleanup tolerates minimal Agents without optional token/browser/terminal attributes.

The gateway repository stores workspaces, conversations, messages, runs, and monotonic events in SQLite WAL mode. REST routes cover conversation/runtime/message/run operations; WebSocket commands support loading, sending, interruption, cancellation, compaction, subscriptions, replay, and live streaming. Agent callbacks also expose tool-finished and background-terminal completion events. Replay initializes its cursor before live subscription, serializes replay/live handoff, and deduplicates event IDs. The gateway's manager is compatible with the main branch's background-terminal and tool-result callback additions.

Blocking terminal commands require a finite positive timeout and clamp it to 20 seconds; background terminals remain persistent and report asynchronous completion events with bounded notifications while retaining full logs. Timeout validation is case-insensitive in the regression checks.

Dependency-light gateway validation is available with `python tests/run_gateway_tests.py`; compile checks use `python -m compileall -q kairos gateway_main.py tests`. Full pytest tests require installing `pytest`. Every code change must update this file and `README.md`.
