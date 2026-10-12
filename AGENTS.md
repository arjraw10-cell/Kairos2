# Kairos Architecture Documentation

**MANDATORY: Whenever you make any code change (edit, add, or remove code), you MUST also update this AGENTS.md file AND README.md to reflect the change. This ensures the documentation stays in sync with the code. Failure to update documentation after a code change is not acceptable.**

## Vision

Kairos is meant to become my personal agent: like Claude Code in the terminal, but simpler, with everything I need built in and understood (tools, stealth browser, computer use, system prompt, long-term memory). It is a hybrid of agent and chatbot, used for coding and for everyday questions as I use the Claude app, and it should know me through memory. One gateway serves every surface: a good terminal CLI, a desktop app, and a personal phone app that connects to the gateway. The motivation: Claude Max 20x from YC Startup School runs for six months, after which I'll have API credits that the Claude app can't use. The big build is deliberately deferred until then, when models will be better, cheaper and faster. Capabilities are built as code, not MCP.

Read `VISION.md` for the full vision, the branch map, current status, and the ordered list of what's left. Work should move Kairos toward it: keep the gateway as the single owner of conversations, memory, tools and the browser, and keep clients thin.

## Overview

Kairos is a minimal coding agent written in Python. It talks to models directly through provider adapters (OpenAI Responses, OpenAI-compatible Chat Completions, Anthropic Messages) configured in `provider.json`, with streaming and function calling, and works through 52 tools (28 for sub-agents), including web search, a stealth browser and optional native desktop automation through Cua Driver. A FastAPI gateway owns conversations and Agent runtimes; the CLI is a WebSocket client of it by default, and `--offline` runs the Agent in the CLI process. Sub-agents and background terminal commands run in the background and wake the agent when they finish; the agent never polls. All file operations use absolute paths — no workspace containment.

## Project Structure

```
Kairos/
├── main.py                 # Root entry point (kairos.main.main)
├── gateway_main.py         # Gateway shim (kairos.gateway_main.main)
├── run_temp_cli.py         # Textual frontend launcher (legacy, in-process Agent)
├── kairos.bat              # CLI on the gateway; starts the gateway when /healthz does not answer
├── kairos_offline.bat      # CLI with the Agent in-process (--offline)
├── provider.example.json   # Template for provider.json
├── provider.json           # Providers, setups, API keys (gitignored; not in the repo)
├── requirements.txt        # Python dependencies
├── pyproject.toml          # Project metadata (console script: kairos = kairos.main:main)
├── README.md               # User-facing documentation
├── AGENTS.md               # This file - architecture documentation
├── VISION.md               # Vision, branch map, status, what's left
├── ui/index.html           # Static design mockup for the desktop app
├── chats/                  # Legacy workspace-local chat sessions (gitignored)
├── skills/                 # Skills directory (gitignored, stays local)
├── tests/                  # See Tests
│   └── fixtures/           # browser_page.html, browser_iframe.html
└── kairos/
    ├── __init__.py         # Lazy exports: Config, Agent, ToolResult, SessionManager, SkillManager, TerminalManager, BrowserManager, DesktopManager
    ├── main.py             # CLI: arguments, REPL, slash commands, GatewayBackend / OfflineBackend
    ├── cli.py              # Terminal UI: streaming panel, wake rendering above the prompt, sub-agent toolbar, paste, Esc listener
    ├── gateway_client.py   # Synchronous WebSocket client of the gateway (used by the CLI)
    ├── gateway_main.py     # `python -m kairos.gateway_main`
    ├── main_gateway.py     # Compatibility gateway entry point
    ├── gateway/            # SQLite-backed runtime manager and FastAPI REST/WebSocket protocol
    │   ├── protocol.py     # Command names, ack/error/event envelopes
    │   ├── models.py       # Conversation, Run, StoredMessage
    │   ├── repository.py   # SQLite persistence (schema v4)
    │   ├── manager.py      # GatewayManager: runtimes, runs, wake scheduling, model settings
    │   ├── server.py       # FastAPI app: REST routes + /api/v1/ws
    │   └── history.py      # Gateway history repair for load/continue
    ├── providers/          # Direct model access
    │   ├── config.py       # provider.json loading, ModelSpec, setup resolution, effort validation
    │   ├── base.py         # Normalized stream events, EventStream, KeyPool (key rotation)
    │   ├── completions.py  # OpenAI Chat Completions adapter (OpenAI, OpenRouter, compatible)
    │   ├── responses.py    # OpenAI Responses adapter
    │   └── anthropic.py    # Anthropic Messages adapter (raw httpx)
    ├── wake.py             # WakeScheduler and format_notifications
    ├── workspaces.py       # Fresh per-conversation workspace folders
    ├── resume.py           # Shared saved-history repair and mid-execution resume logic
    ├── config.py           # Environment settings (cached)
    ├── agent.py            # Core agent: streaming, tool dispatch, background events, compaction, model selection
    ├── temp_cli.py         # Textual frontend (legacy)
    ├── tokens.py           # TokenCounter using tiktoken
    ├── terminal_manager.py # Blocking and persistent background terminals, `until` matches, stop_command
    ├── browser_manager.py  # CloakBrowser via async Playwright on a worker thread: AI snapshots, refs, diff snapshots, dialogs, profiles
    ├── desktop_manager.py  # Cua Driver daemon/CLI wrapper
    └── tools/
        ├── __init__.py     # Imports and re-exports all tools
        ├── base.py         # ToolResult(success, output, error?, image_url?)
        ├── read.py         # ReadTool — text + image files
        ├── write.py        # WriteTool — create/overwrite files
        ├── edit.py         # EditTool — strict find-and-replace (must match exactly once)
        ├── search.py       # SearchTool — regex file search (ripgrep-like)
        ├── web_search.py   # WebSearchTool — Tavily search
        ├── git.py          # GitTool — status, diff, log, commit, branch
        ├── terminal.py     # 6 terminal tool wrappers
        ├── subagent.py     # SubAgentTool — background sub-agents
        ├── browser.py      # BrowserTool (forwards one BrowserManager method) + BrowserScreenshotTool
        ├── desktop.py      # Native desktop tool wrappers
        ├── skills.py       # SkillManager — list, load, write skills
        └── session.py      # SessionManager — ~/.kairos chat stores for the offline CLI, with legacy fallback loading
```

---

## File-by-File Reference

### `main.py` (root) and launchers

`main.py` calls `kairos.main.main()`; `gateway_main.py` calls `kairos.gateway_main.main()`.

`kairos.bat` is the standard launcher (its folder is on PATH). It keeps the caller's current folder and passes all arguments through (`kairos`, `kairos .`, `kairos <path>`). It reads `KAIROS_GATEWAY_HOST`/`KAIROS_GATEWAY_PORT` from the environment (defaults `127.0.0.1:8765`; `0.0.0.0`/`::` are probed as `127.0.0.1`), probes `/healthz` with `curl.exe`, and when that fails starts `py -m kairos.gateway_main` in a minimized "Kairos Gateway" window from the repository folder and waits for health (curl retries, about 40 s at most). It then runs `py main.py %*`.

`kairos_offline.bat` runs `py main.py --offline %*` (no gateway).

### `kairos/__init__.py`

Lazy exports through a module `__getattr__`, so the CLI in gateway mode does not import the Agent and its tools: `Config`, `Agent`, `ToolResult`, `SessionManager`, `SkillManager`, `TerminalManager`, `BrowserManager`, `DesktopManager`.

### `kairos/config.py` — Config

**Class**: `Config` (all class methods, no instances). Reads environment variables only (no `.env` file); each getter is cached with `@lru_cache(maxsize=1)`. Model providers and API keys live in `provider.json` (see `kairos/providers/`).

| Method | Returns | Default |
|--------|---------|---------|
| `MAX_TOOL_RESULT_CHARS()` | `int` | 20,000 (`KAIROS_MAX_TOOL_RESULT_CHARS`; invalid or < 1 falls back) |
| `CONTEXT_WINDOW()` | `int` | 300,000 (`KAIROS_CONTEXT_WINDOW`; invalid or < 1 falls back) |
| `KAIROS_GATEWAY_HOST()` | `str` | `127.0.0.1` |
| `KAIROS_GATEWAY_PORT()` | `int` | 8765 (raises `ValueError` if not an integer in 1-65535) |
| `KAIROS_DEFAULT_WORKSPACE()` | `str \| None` | None; only the target workspace of the legacy-chat import |
| `KAIROS_DATA_DIR()` | `str` | `""` (repository default `~/.kairos`) |
| `KAIROS_MAX_CONCURRENT_RUNS()` | `int` | 8 |
| `KAIROS_RUNTIME_IDLE_SECONDS()` | `int` | 1800 (0 disables idle unloading) |
| `KAIROS_AUTH_TOKEN()` | `str \| None` | None |
| `KAIROS_LEGACY_CHAT_FILE()` | `str \| None` | None |
| `KAIROS_CUA_DRIVER_PATH()` | `str \| None` | None |
| `validate()` | `bool` | Calls `providers.resolve()`; raises `ValueError` (`ProviderConfigError`) if `provider.json` is missing or its default setup is invalid |
| `reload()` | `None` | Clears all caches |

`CONTEXT_WINDOW()` is the context cap (default 300,000). Each model's effective window is `min(cap, max_context)`, where `max_context` is the model's real limit from `provider.json` (the older key `context_window` is read the same way). `TokenCounter`, keep-recent compaction and compaction-prompt budgeting use that window; auto-compaction runs at 80% of it.

### `kairos/providers/` — Model Providers

Kairos keeps its conversation history in Chat Completions shape. An adapter translates it to one wire format and normalizes the reply stream into tuples:

- `("text", delta)`
- `("tool_call", index, call_id, name, arguments_delta)` — id/name may be empty on continuation deltas
- `("usage", prompt_tokens, completion_tokens)`
- `("message_extra", key, value)` — provider state kept on the assistant message under a private `_` key

Each adapter has `stream(messages, tools, effort) -> EventStream`; `EventStream.close()` closes the HTTP stream from another thread (hard stop). `create_adapter(spec)` picks the adapter by `spec.format`.

**`config.py`**: `provider.json` sits at the repository root (`config_path()`), is gitignored, and has this shape (`provider.example.json` is the template):

```json
{
  "default_setup": "gpt-6-luna",
  "providers": {
    "openai": {"format": "responses", "base_url": "https://api.openai.com/v1", "api_keys": ["..."]},
    "openrouter": {"format": "completions", "base_url": "https://openrouter.ai/api/v1", "api_keys": ["...", "..."]},
    "anthropic": {"format": "anthropic", "base_url": "https://api.anthropic.com", "api_keys": []}
  },
  "setups": {
    "gpt-6-luna": {"provider": "openai", "model": "gpt-6-luna", "reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"], "default_effort": "high"}
  },
  "tavily_api_key": "..."
}
```

A setup names a `provider` and `model` plus `reasoning_efforts` and `default_effort`; it may override the provider's `format`, `base_url` and `api_keys`, and adds `max_context` (the model's real context limit) and `pricing` (`input`, `cached_input`, `output` in USD per 1M tokens). `resolve(setup=None, *, model=None, provider=None)` returns a frozen `ModelSpec` (setup, provider, model, format, base_url, api_keys (not in repr), reasoning_efforts, default_effort, context_window (= min(cap, max_context)), pricing) for a named setup, the default setup, or an ad-hoc `model` on a named `provider` (ad-hoc specs have `reasoning_efforts=None`, so any effort passes unvalidated). `list_setups()`, `default_setup()`, `validate_effort(spec, effort)` and `load_config()` complete the module; errors raise `ProviderConfigError` (a `ValueError`).

Setups in `provider.example.json` and their verified efforts:

| Setup | Provider / format | Efforts |
|-------|-------------------|---------|
| `gpt-6-luna`, `gpt-6-sol` | openai / responses | none, low, medium, high, xhigh, max |
| `gpt-6.1-sol`, `gpt-6-astra` | openai / responses | low, medium, high, xhigh, max |
| `glm-5.3-flash` (`z-ai/glm-5.3-flash`) | openrouter / completions | minimal, low, medium, high, xhigh, max (reasoning cannot be turned off) |
| `mimo-v2.5` (`xiaomi/mimo-v2.5`) | openrouter / completions | on, off |

**`base.py`**: `ProviderError(message, status_code)`, `EventStream`, `public_fields()` (drops `_` keys), and `KeyPool`. Keys are tried in order, skipping keys in cooldown; a 401, 402 or 429 puts the key in a 60-second cooldown and tries the next one; other errors are raised at once. If every key is cooling, the one that recovers first is tried. `key_pool(keys)` returns one process-wide pool per key list, so conversations share cooldowns.

**`completions.py`**: `CompletionsAdapter` streams `chat.completions.create` with `stream_options={"include_usage": True}` and `tool_choice="auto"`. Effort `on`/`off` becomes `extra_body={"reasoning": {"enabled": ...}}`; any other effort is sent as `reasoning_effort`. OpenAI SDK clients are cached per key; with more than one key the SDK's own retries are disabled so a rate-limited key rotates at once.

**`responses.py`**: `ResponsesAdapter` translates history to Responses input items (tool calls become `function_call` items whose `call_id` is the Chat `tool_call_id`, tool results become `function_call_output`, `text`/`image_url` parts become `input_text`/`input_image`), flattens tool definitions, sends effort as `reasoning={"effort": ...}`, and normalizes `response.*` stream events. `response.failed` and `error` events raise `ProviderError`.

**`anthropic.py`**: `AnthropicAdapter` posts to `<base_url>/v1/messages` with raw `httpx` (no SDK; `anthropic-version: 2023-06-01`, `max_tokens` 32,000). System messages move to `system`, tool results become `tool_result` blocks, consecutive same-role turns are merged, data-URL images become base64 image blocks. With an effort it sends `thinking={"type": "adaptive"}` and `output_config={"effort": ...}`; thinking blocks (with signatures) are returned as `message_extra` under `_thinking` and echoed back on the next request. `overloaded_error` maps to status 529. This adapter is unit-tested against recorded streams but has not been called against the live API.

### `kairos/main.py` — CLI Entry Point

`main(argv=None)` parses arguments, builds a backend, prints the banner and runs the REPL.

**Arguments** (`parse_args`): optional `workspace` (`.` = current folder; must be an existing folder) and `--offline`. Without a workspace the conversation gets a fresh folder from `kairos.workspaces.new_workspace()`.

**Commands** (`COMMANDS`): `/quit /clear /reset /compact /resume /model /effort /stop`. `parse_command()` treats only a first word made of letters as a command, so `/etc/hosts ...` goes to the agent; unknown commands are reported instead of being sent. Paste tokens are resolved before command dispatch (`_resolve_paste_input`), so a pasted `/quit` works. A message that is only an image is sent as "Describe this image."

- `/reset`: new conversation (same workspace if one was given, otherwise a new fresh folder); `/model`/`/effort` choices carry over.
- `/resume [N]`: numbered picker. With an explicit workspace it lists that workspace's conversations; without one it lists conversations in all fresh folders (they are siblings). If the chosen conversation ended mid-turn, it continues with `Continue where you left off. Pick up the next step.`
- `/model`: lists setups and asks for a number or name; `/model <N|name>` picks directly; `/model <model-id> <openai|anthropic|openrouter>` uses an ad-hoc model on that provider. Effort resets to the new model's default.
- `/effort`: shows the current effort and allowed values; `/effort <level>` sets it (validated against the setup).
- `/stop`: terminates all running sub-agents.

**`run_turn(cli, start)`**: starts the thinking dots and the Escape listener, runs `start(stop)` and prints the outcome. Esc or Ctrl+C sets `stop`; the backend returns `("interrupted", None)` at once and the prompt comes back while the stopped run winds down. Esc stops only the main agent; sub-agents keep running (`/stop` stops them).

**Backends** (same interface; both feed the same event names to `CLI.handle_event`):

- `GatewayBackend` (default): connects a `GatewayClient`, creates a conversation (`conversation.create` with or without `workspace`) and subscribes to its events. A message is `message.send` (source `cli`); its run's events are rendered until a `run.*` end event. Esc sends `run.cancel` and abandons the run; control commands (`/compact`, `/reset`, `/resume`, `/model`, `/effort`) first wait up to 30 s for abandoned runs to end (`_settle`). `/resume` uses `conversation.load` and its `needs_continue` flag, then `conversation.continue`. `/model` uses `model.list` and `conversation.configure`; `/stop` uses `subagent.stop_all`. A pump thread reads events while the CLI is not waiting on its own run: `background.notification` events (not marked `in_run`) are collected for the next wake header, a `run.started` with origin `wake` starts a wake turn, `subagent.status` and `wake.paused` are rendered. Esc at the prompt during a wake turn sends `run.interrupt` (`stop_wake`).
- `OfflineBackend` (`--offline`): builds `Agent(workspace)` in-process and maps its callbacks to the gateway's event names. Chats are saved with `SessionManager` (store keyed to the workspace, or to `~/Documents/kairos_workspace` when no path was given); auto-save every 60 s and on SIGTERM/SIGHUP. One agent run at a time (`_agent_lock`). It runs its own `WakeScheduler`: idle background events start a wake turn whose input is `wake.format_notifications(events)` (the same as gateway wake runs); a user message calls `user_message_sent()` and hands the scheduler's pending events to `agent.add_background_events()`. `/reset` and `/resume` drop pending results of the old conversation. On exit it stops sub-agents, closes terminals and the browser.

`_status_text()` shows `model · effort` at the right of the prompt (empty if the gateway lacks `conversation.configure`). In gateway mode a lost connection ends the REPL; failing to connect at startup suggests `kairos.bat` or `kairos_offline.bat`. Ctrl+C or Ctrl+D at the prompt exits.

`kairos.main._sanitize_history_for_resume` remains a compatibility alias of `kairos.resume.sanitize_history_for_resume`.

### `kairos/gateway_client.py` — GatewayClient

Synchronous WebSocket client built on `websockets.sync.client`. `gateway_url()` builds `ws://<host>:<port>/api/v1/ws` from `KAIROS_GATEWAY_HOST`/`KAIROS_GATEWAY_PORT`; `KAIROS_AUTH_TOKEN` is sent as `Authorization: Bearer`. `connect()` (5 s open timeout, no message size cap) sends `gateway.hello`. A reader thread completes `request(command, conversation_id?, payload?, timeout?)` calls by `request_id` and queues events; `next_event(timeout)` returns the next event or None and raises when the socket closed. Errors raise `GatewayError(message, code)` (`disconnected`, `timeout`, or the gateway's error code). It deliberately does not import `kairos.gateway` (that would load the server and Agent stack).

### `kairos/cli.py` — CLI (Terminal UI)

**Classes**: `CLI`, `KairosMarkdown`, `_EnhancedTableElement`, `_StreamView`, `_AbovePrompt`.

**`KairosMarkdown`** (subclass of `rich.markdown.Markdown`) swaps in `_EnhancedTableElement` for `table_open`: `box.ROUNDED`, cyan borders, `header_style="bold bright_white on grey15"`, alternating `dim` rows. Other elements use Rich's defaults.

**Output**: `print_banner(workspace, mode)` prints one line (`KAIROS  <mode>  <workspace>`). Info lines are dim; error text is printed as `Text` (not markup), in a red panel when multi-line. Tool calls print `→ <summary>`; `print_token_status(data)` prints `kairos.tokens.format_status_line(data)`: `Context Z% · N / W tokens · this call: O out · session: I in / O out · $cost` (`cost n/a` when the setup has no pricing).

**Streaming**: `start_stream()` opens a Rich `Live` with a `_StreamView`, which renders the grey panel from the CLI's text at render time and, while streaming, shows only the bottom lines that fit the terminal (long replies keep scrolling; text wraps at word boundaries). `finish_stream()` leaves the grey trace (tool-call preamble); `finalize_stream_as_response()` turns the panel into a green `KairosMarkdown` panel. `finish_turn(outcome, payload)` prints the response, an error, or "Interrupted."

**`handle_event(event, data)`** (thread-safe, shared by both modes): `assistant.stream_started`, `assistant.token`, `assistant.stream_ended`, `tool.started`, `token.updated`, `conversation.compacted`, `wake.started`, `wake.ended`, `wake.paused`, `subagent.status`, `subagent.list`.

**Background rendering while the prompt is open**: while a prompt is open, `console.file` is an `_AbovePrompt`, which prints each flush above the prompt with prompt_toolkit's `run_in_terminal`, so the user's half-typed text stays. A wake turn prints a header such as `» woke: sub-agent a3f2 finished` (`describe_notification`), streams its reply in a preview above the input (up to `PREVIEW_HEIGHT` lines) and ends with the usual green panel. Esc at the prompt during a wake turn calls `on_prompt_escape` (the backend's `stop_wake`). Running sub-agents are listed in the bottom toolbar, one line each (`● <id>  running  <current tool>  <elapsed>`, refreshed every second); finished ones drop off. `wake.paused` prints `paused auto-wake: N background results will be sent with your next message`.

**Prompt**: `get_user_input(prefix="kairos> ", status="")` shows `status` right-aligned (`rprompt`). `ask(label)` and `pick_session(sessions, initial_choice)` (the `/resume` picker; re-prompts on invalid input) take complete lines typed during the response-to-prompt handoff.

**Paste system** (module-level):
- `_paste_registry: Dict[str, dict]` — maps tokens to `{type: "text"|"image", ...}`; `_make_text_token()` / `_make_image_token()` create `(Pasted Text #N)` / `(Pasted Image #N)`; counters reset per prompt
- `_bracketed_paste_handler` — bracketed-paste events (line endings normalized)
- `_paste_handler` — Ctrl+V when the terminal passes it through; reads the clipboard only then
- `_alt_v_handler` — Alt+V: image paste (`[no image on clipboard]` if none)
- `_backspace_handler` — deletes a whole token when the cursor is in or right after it
- Paste detection is event-based; buffer changes and the clipboard sequence number are never used.

**Clipboard helpers**: `_read_system_clipboard()` (Windows: `CF_UNICODETEXT` through the Win32 API via `ctypes`; macOS `pbpaste`; Linux `xclip`), `_check_clipboard_has_image()` (Windows: PowerShell + `System.Windows.Forms.Clipboard`; macOS `pngpaste`; Linux `xclip`), `_detect_mime()`, `_image_data_to_url()`.

**Escape listener** (during a turn): `start_escape_listener(on_escape)` / `stop_escape_listener()` (joins the thread before the prompt owns stdin). Ordinary characters typed meanwhile are buffered (`_take_pending_input_for_prompt()` returns a complete line or pre-fills a partial one). Windows: `msvcrt.kbhit()`/`getwch()`; Unix: `tty.setcbreak()` + `select` + `os.read()`.

### `kairos/wake.py` — Wake Scheduling

`format_notifications(events)` renders events as one user-role message: each event's `text` under a `[Background notification]` line (`NOTIFICATION_PREFIX`).

`WakeScheduler(start_wake, is_idle, debounce=2.0, cap=10, on_paused=None)` is the host-side half of the wake system (gateway runtime, offline CLI):
- `push(event)` records an event (events with `in_run` are ignored: the active run handles them). The first pending event arms a `debounce` timer; later events join that batch.
- When the timer fires and `is_idle()` is true, all pending events go to `start_wake(events)` exactly once. When the host is busy, events wait (a user run takes them, or the timer re-arms).
- After `cap` consecutive wakes without a user message, the scheduler pauses and calls `on_paused(pending_count)`; events then wait for the user's next message.
- `user_message_sent()` resets the wake count and un-pauses; `take_pending()` returns and forgets undelivered events (the host attaches them to the next run); `close()` stops waking (pending events stay takeable).

### `kairos/workspaces.py`

`WORKSPACE_ROOT = ~/Documents/kairos_workspace`. `new_workspace()` creates and returns `WORKSPACE_ROOT/YYYY-MM-DD_HHMMSS` (local time; `_2`, `_3`, ... on collision). Used by the gateway for conversations created without a workspace and by the offline CLI when no path is given.

### `kairos/resume.py` — Saved-history Repair

`sanitize_history_for_resume(history)` returns `(history_or_none, last_agent_content, is_mid_execution)`. It anchors the decision at the latest real user request, ignoring agent-generated user messages (screenshot injections, `[Conversation compacted`, `[Background notification]`, and the older `Background terminal notifications arrived` prefix), so an older clean response cannot hide a newer interrupted request. A clean assistant reply after that request is the resume point; otherwise trailing screenshot injections are removed and `_repair_trailing_tool_chain()` keeps matching tool results in call order, adds synthetic failed results for missing calls and removes orphans. Used by the offline CLI and the Textual frontend; the gateway has its own variant in `kairos/gateway/history.py`.

### `kairos/agent.py` — Agent (Core)

**Class**: `Agent`

**Constructor**: `Agent(workspace: str, setup: Optional[str] = None)`
- Resolves `setup` (the default setup when None) to `self.spec`, creates `self.adapter`, sets `self.model` and `self.effort = spec.default_effort`
- `self.tokens = TokenCounter(model, spec.context_window, spec.pricing)` (`set_model` updates both); `self.cwd = Path(workspace).resolve()`
- Creates the tool instances, `SubAgentTool`, `BrowserManager` and the 19 `BrowserTool`s plus `BrowserScreenshotTool`, `DesktopManager` and the desktop tools, and `SkillManager(<workspace>/skills)`
- Sets itself as the terminal manager's completion callback and builds the system prompt

**Key attributes**:
- `spec`, `adapter`, `model`, `effort` — model access (replaced by `set_model()`)
- `cwd` — workspace path (`Path`); `tokens` — `TokenCounter`; `conversation_history` — `List[Dict]` (starts with the system prompt)
- `max_tool_result_chars` — `Config.MAX_TOOL_RESULT_CHARS()`
- `_interrupt_event` — hard-stop `threading.Event`; `_stop_requested` — retained for compatibility
- `_is_processing` — True while `run()` is active (guarded by `_processing_lock`)
- `_background_events` — background events held by the Agent (see below)
- `_is_subagent` — True for sub-agents (no browser or sub-agent tools)
- `terminal_manager`, `browser_manager`, `desktop_manager`, `skill_manager`
- `subagent_tool` — `SubAgentTool` (None in a sub-agent)

**Callbacks** (set by the host; all optional):
- `on_stream_start()`, `on_stream_token(token)`, `on_stream_end(content, has_tool_calls)`
- `on_tool_call(name, args)`, `on_tool_result(name, result_dict, result_json)`
- `on_token_update(counter)` — after each completed API turn
- `on_compact(message)` — compaction and history-repair notices, empty-response retries
- `on_background_event(event)` — fires whether or not a run is active; event: `{"id", "kind", "subagent_id"?, "terminal_id"?, "text", "created_at", "in_run"?}`; kinds `subagent_finished`, `subagent_failed`, `terminal_finished`, `terminal_matched`
- `on_subagent_status(status)` — sub-agent status dicts (see `SubAgentTool`)

**Model selection**: `set_model(setup=None, model=None, provider=None)` switches to a setup (the default setup when all are None) or an ad-hoc model and resets effort to the new default; history and token totals carry over, and the context window follows the spec. `set_effort(effort)` validates against the setup's efforts. `model_info()` returns `{setup, provider, model, format, effort, reasoning_efforts}`.

#### Background events

Sub-agent results (`SubAgentTool` `on_event`) and terminal events (`_on_terminal_event`, text from `TerminalManager.format_background_completion`) go through `_push_background_event()`:
- **During a run** (`_is_processing`) the event is appended to `_background_events` and passed to `on_background_event` with `in_run: True`. `step()` drains the queue before each API request and appends one `[Background notification]` user message (after the real user message when there is one).
- **While idle** with a host callback, only the host receives it (gateway/offline `WakeScheduler`: wake run, or the next user message). Without a host callback, it waits in `_background_events` for the next run.
- `_finish_run_unless_background()` checks the queue and clears `_is_processing` under one lock, so an event arriving during the final step either continues the same run (one more step) or goes to the host — never both, never lost.

Host API: `add_background_events(events)` (hand events to the next run), `drain_background_events()`, `has_background_work()` (running sub-agents or background commands), `stop_subagents()` (the user's `/stop`), `subagent_statuses()`, `is_processing`.

#### Immediate stop

`interrupt()` / `request_stop()` set the hard-stop event, close the active provider stream, kill blocking terminal commands (`TerminalManager.cancel_active_commands()`), and cancel the in-flight browser and desktop calls. The worker checks the event before/during streaming and between tools. Tool results are checkpointed one by one, so completed calls survive. It does not stop sub-agents. After an interrupt, `run()` returns `[Interrupted]` and the next request is wrapped as "The previous request was interrupted before it finished. Continue that work, taking this new instruction into account: ...", with the trailing tool chain repaired first.

#### System Prompt (`_setup_system_prompt`)

Sections: base instructions (role, absolute paths, tool failures, AGENTS.md handling), Terminal Tools, Background Work (notifications arrive as `[Background notification]`; never poll, sleep or wait), Tool Result Limits, Web Search, Browser Tools, Native Desktop Tools, and the workspace path. Then the workspace's `AGENTS.md` (if present) under `## AGENTS.md (Architecture Documentation)`, and the skill names from `skills/` (or a note that none exist).

#### Tool Schema (`_get_tool_schema()`)

52 function tools: `read`, `write`, `edit`, `search`, `web_search`, `git`; terminals `new_terminal`, `execute_command`, `read_logs`, `stop_command`, `close_terminal`, `get_terminal_info`; sub-agents `spawn_subagent`, `message_subagent`, `terminate_subagent`, `list_subagents`; 20 `browser_*` tools; 13 `desktop_*` tools; `list_skills`, `load_skill`, `write_skill`. For a sub-agent it drops `SUBAGENT_TOOL_NAMES` and every `browser_*` tool, leaving 28 (desktop tools stay).

#### Tool Execution (`_execute_tool(name, args)`)

A dispatch dict maps names to lambdas. Unknown tools return an error. Sub-agent tools called from a sub-agent are refused ("Sub-agents cannot spawn, message, terminate or list sub-agents."). A missing required argument (a `KeyError` from the lambda) returns `Missing required argument 'x' for tool y. Call it again with that argument.` Other exceptions return their text. Every result is capped by `_cap_tool_result()`: text `output`/`error` are limited to 20,000 characters by default (error to 4,000), oversized text keeps its beginning and end with a marker giving the original length, and `image_url` is not counted.

#### Tool Summaries (`_tool_summary(name, args)`)

Static method returning a one-line summary per tool call (CLI display, gateway `tool.started`, sub-agent `recent_actions`).

#### Streaming (`_stream_response()`)

Returns `(content, tool_calls, api_usage)`. Calls `adapter.stream(messages, tools, effort)` and consumes the normalized events: text deltas go to `on_stream_token`, tool-call deltas are accumulated by index, `usage` becomes `api_usage`, `message_extra` values are attached to the assistant message by `step()`. Arguments are parsed as JSON (`{}` on failure).

**Retries**: an initial attempt plus two retries (`API_MAX_RETRIES = 2`) with exponential backoff for retryable errors: `RateLimitError`, `APIConnectionError`, `httpx.TransportError`, status 429/500/502/503/504/529 (OpenAI SDK or `ProviderError`), and timeout/connection/network/`RemoteProtocolError`/incomplete-chunk messages. Retries cover opening and iterating the stream; a partial stream is closed in the UI and discarded. When attempts are exhausted it raises `APIRequestError` with `_format_api_error()` text: `API Error: <type>`, message, status/request ID when present, a hint (keys in `provider.json`, rate limit, connection), and the setup, provider, format, model and base URL. No traceback.

#### Step (`step(user_message?, image_url?)`)

Returns `(response_text | None, tool_calls_made)`.
1. Drains background events; appends the user message (vision content when `image_url` is given) and then any `[Background notification]` message
2. Normalizes legacy tool results and validates history (`on_compact` reports repairs)
3. `tokens.start_turn()` (history + tool schema), `_stream_response()`, then `set_turn_from_api()` with API usage or tiktoken estimates
4. Appends the assistant message (with `tool_calls` and provider extras) and calls `on_stream_end()`
5. Without tool calls: `finish_turn()`, `on_token_update`, return the content
6. With tool calls: for each, `on_tool_call`, `_execute_tool`, `on_tool_result`, then append the tool message immediately (checkpoint). Images are removed from tool messages and re-injected as one user vision message starting with `[Screenshot captured`. Tool results become input tokens of the next turn.

#### Run (`run(user_message, image_url?)`)

Marks the Agent processing, then loops: stop check; recount context with the pending user message and the tool schema; auto-compact above 80%; `step()`. A step with neither content nor tool calls is removed and retried up to twice (`on_compact` reports it). A step with content and no tool calls ends the run unless background events arrived meanwhile (then one more step shows them to the model). Returns the response, `[Stopped — waiting for your input]` if a stop was already pending, or `[Interrupted]`. The `finally` block clears processing and the stop event (the stable hand-off point).

#### Compaction

**Constants**: `COMPACT_RESERVE_TOKENS = 16384`, `COMPACT_KEEP_RECENT_PCT = 0.20`, `COMPACT_THRESHOLD_PCT = 80.0`.

**`compact()`**:
1. `_find_compact_boundary()` — walks back from the end with the same field-aware message counting as preflight and chooses a safe user boundary or complete assistant/tool-call chain while keeping 20% of the context window
2. `_serialize_messages_for_summary()` — readable text (tool results cut to 500 characters)
3. `_generate_summary()` — a streamed request through the same adapter and effort with a structured prompt; an existing summary is passed as `<previous-summary>` for an incremental update; the input is bounded by `_cap_compaction_prompt()` (context window minus the reserve and a 1,024-token margin, keeping head and tail)
4. Rebuilds history as `[system, "[Conversation compacted — summary of prior history]" user message, recent messages]` and recounts context (session totals are not inflated)

`auto_compact_if_needed()` runs before every API step in `run()`, including inside tool-call loops, because API usage from the previous step does not include the tool results just appended.

**Summary format**: `## Goal`, `## Constraints & Preferences`, `## Progress` (Done / In Progress), `## Key Decisions`, `## Next Steps`, `## Critical Context`, `## User Messages & Agent-to-User Messages` (`### User Messages`, `### Agent-to-User Messages`). The last two subsections keep a chronological record of every user message and substantive assistant reply (tool-call-only messages and tool results excluded); incremental compaction preserves and extends them.

#### History maintenance

- `_validate_history_before_api()` — before every request: no user message triggers `compact()`; trailing tool messages without a preceding assistant tool-call message are trimmed. A trailing user message (including a background notification) is valid.
- `_normalize_history_for_context()` — caps legacy saved tool results and replaces embedded `image_url` data with an omission marker.
- `_truncate_history_if_needed()` — message-count truncation at 10,000,000 (effectively disabled; keeps a user message in the tail).

#### Reset (`reset()`)

Clears stop state, rebuilds the system prompt and history, creates a new `TokenCounter`, closes the browser if open and resets the desktop manager.

### `kairos/tokens.py` — TokenCounter

**Constructor**: `TokenCounter(model="gpt-4o", context_window=None, pricing=None)` — tiktoken encoding for the model (falls back to `cl100k_base`); `context_window` defaults to `Config.CONTEXT_WINDOW()`; `pricing` is USD per 1M tokens (`input`, `cached_input`, `output`) or `None`.

**Attributes**: `session_input` / `session_output` (cumulative), `context_tokens`, `turn_input` / `turn_output` / `turn_cached` (one API call), `context_window`, `pricing`, `session_cost`.

**Methods**:
- `start_turn(messages, extra_tokens=0)` — counts history plus optional tool-schema tokens
- `count_request(messages, tools?)` — message fields, assistant tool-call metadata/arguments, tool IDs/names, function-tool definitions
- `count_tools(tools)`, `count_message(message)`
- `add_output_tokens(text)` — tiktoken estimate
- `set_turn_from_api(prompt_tokens, completion_tokens, cached_tokens=0)` — replaces estimates with provider-reported usage; every adapter reports cached prompt tokens (`("usage", prompt, completion, cached)`)
- `finish_turn()` also adds the call's cost: uncached input × `input` + cached × `cached_input` + output × `output`
- `finish_turn()` — adds turn totals to session totals
- `context_pct` — `(context_tokens / context_window) * 100`
- `status_data()` — the `token.updated` payload (`context_pct`, `context_tokens`, `context_window`, `turn_input`, `turn_output`, `session_input`, `session_output`, `session_cost` or `None`); `kairos.tokens.status_payload(counter)` also accepts minimal test stand-ins
- `format_status()` — `format_status_line(status_data())`

Images in vision content are estimated at 85 (low detail) or 765 (high detail) tokens, never from base64 length.

### `kairos/terminal_manager.py` — TerminalManager

**`create_terminal(background) -> int`**:
- Background: persistent shell (`cmd /Q /k` on Windows, `/bin/bash --login` elsewhere) with a reader thread; shell state survives between commands
- Blocking: no persistent process; each command gets its own subprocess

**`execute_command(terminal_id, command, timeout?, is_background?, until?)`**:
- Blocking: requires a finite positive timeout, capped at 20 s (`MAX_BLOCKING_TIMEOUT`), validated before spawning; non-zero exit is a failure; on timeout the process tree is killed and partial output returned. Running blocking processes are registered so `cancel_active_commands()` can kill them on hard stop.
- Background: writes the command plus a unique completion marker that carries the exit code (Windows: `echo <marker>:%errorlevel%` then `ver >nul`; Unix: `printf`) and returns at once; timeout is ignored. `until` (background only, a regex) produces one `terminal_matched` event for the first matching output line while the command keeps running; invalid regexes and `until` on blocking terminals are rejected.
- `is_background`, when given, must match the terminal type.

**Events**: the reader turns markers into `terminal_finished` events (`completion_id`, `terminal_id`, command, success, `stopped`, exit code, output, duration, optional error); `until` matches produce `terminal_matched` events (matched line and the last 20 output lines). With a consumer set by `set_completion_callback()` (the Agent sets one) every event is delivered to it exactly once, outside the lock; without one, events queue for `drain_completed_background_commands()` and are handed over when a consumer is set. A shell that exits or is closed before a marker produces failed completions for its pending commands. `format_background_completion(event)` renders an event as notification text, with output capped at 12,000 characters (`read_logs` keeps everything).

**`stop_command(terminal_id)`**: kills the process trees of the background shell's child processes (Windows: Toolhelp snapshot, skipping `conhost.exe`, then `taskkill /T /F`; Unix: `pgrep -P` and SIGTERM). The shell stays open and reports the command finished with `stopped: True`. A shell built-in has no child process; then it fails and suggests `close_terminal`.

**`read_logs(terminal_id, start_line?, end_line?, tail?)`**: background output with a `[lines A-B of N]` header. No range returns the last 100 lines (`DEFAULT_LOG_TAIL`); `tail` returns the last N; `start_line`/`end_line` select a 1-indexed inclusive range (end defaults to the last line). `tail` cannot be combined with a range.

**`close_terminal(terminal_id)`**: marks pending commands failed, terminates the process tree and removes the terminal. `running_background_commands()` counts unfinished background commands. `get_terminal_info()` returns id, type, closed, line count, pending commands and reader state. Shared state is protected by a `threading.Lock`.

### `kairos/browser_manager.py` — BrowserManager

**Class**: `BrowserManager` — one headed, humanized CloakBrowser session per Agent. External callers use `is_open`, `close()` and `cancel_active_operation()`; tools call the action methods below.

**Threading**: `_Worker` runs a dedicated thread with its own asyncio loop and an async Playwright instance, so the browser never touches the main thread's loop (rich/prompt_toolkit). Public methods are synchronous: `_run(coro_fn, timeout)` submits a coroutine with `asyncio.run_coroutine_threadsafe` under a lock and waits. `cancel_active_operation()` cancels the in-flight future (agent hard stop). Async is required so actions can be raced against dialogs.

**Launch**: lazy. The first browser call runs `_ensure()`, which launches CloakBrowser's binary (`ensure_binary()`, `build_args(stealth_args=True, headless=False)`, `IGNORE_DEFAULT_ARGS`) headed with a 1280x720 viewport and applies `cloakbrowser.human.patch_context_async`. There is no headless or non-humanized path. Closing the window by hand fires the context `close` event, which resets state so the next call relaunches.

**Profiles** (`profile(action, name?, proxy?)`): `list`, `use <name>` (persistent context in `~/.kairos/profiles/<name>`), `ephemeral`. Switching closes and relaunches. Each profile pins a fingerprint seed in `<profile>/.kairos-browser.json` (created randomly on first use) passed as `--fingerprint=<seed>`. A module-level `_open_profiles` set refuses opening a profile that another BrowserManager in the same process (another gateway conversation) has open.

**Snapshots**: `page.aria_snapshot(mode="ai")` (Playwright 1.63+). Refs look like `e12`, or `f1e12` inside iframes and after the page navigates. `_snapshot_text(page, mode)` keeps the trimmed lines of the previous snapshot; in `diff` mode on the same page and URL it returns only new/changed lines plus a count of removed lines, falling back to the full snapshot when the diff is large. Full snapshots over `SNAPSHOT_MAX_CHARS` (15,000) are truncated with a pointer to `browser_search_page`. `_state(mode)` prepends URL, title, tab count, a notice when the page opened a new tab (and switches to it), and an open dialog (in which case no snapshot is taken).

**Actions** go through `_act(fn, ref?, mode)`: refuses to act while a dialog is open, races the action against dialog events (`asyncio.wait`), lets a triggered navigation settle, and returns `summary + _state(mode)`. Errors raise `RuntimeError` carrying an explanation and a fresh full snapshot; stale refs say so explicitly.

**Ref resolution and humanize**: CloakBrowser's humanized `Locator` methods cannot resolve `aria-ref` selectors, so `_handle(page, ref)` resolves the ref to an element handle first (a ref with no match, or an unknown frame prefix, raises `StaleRefError` immediately). Handle methods are humanized by CloakBrowser. When its engine cannot map a handle ("cloakbrowser humanize: cannot identify ...", seen inside iframes), `_humanized()` falls back to the humanized `page.mouse` / `page.keyboard` at the element's box (`_center()` scrolls it into view and adds a little jitter).

| Method | Behaviour |
|--------|-----------|
| `navigate(url)` | `goto`, or `back` / `forward` / `reload`; clears the tab's network buffer; full snapshot |
| `snapshot()` | Full page state |
| `click(ref?, button, double, x?, y?)` | Handle click, or coordinates |
| `type(ref, text, submit)` | Humanized `fill` (keyboard fallback); dispatch timeout grows with text length |
| `fill_form(fields)` | textbox → type; combobox → `select_option`; checkbox/radio → `set_checked` |
| `select(ref, values)` / `hover(ref)` / `drag(...)` / `press_key(key)` / `scroll(direction, pages, ref?)` | As named; drag/scroll use the humanized mouse |
| `wait_for(text?, text_gone?, seconds?, timeout)` | Text visibility waits or a sleep (max 60 s) |
| `evaluate(expression, ref?)` | `page.evaluate` (expressions or functions) or element-handle evaluate; no snapshot |
| `handle_dialog(accept, prompt_text?)` | Answers the pending dialog, then returns the diff state |
| `tabs(action, index?, url?)` | list / new / select / close |
| `screenshot(full_page, ref?)` | Returns `(png, message, data_url)`; saves to `~/.kairos/screenshots/` |
| `search_page(pattern, regex, case_sensitive, max_results)` | Regex/text search over `document.body.innerText` with context |
| `console(index?, errors_only, max_results)` | See below |
| `network(index?, filter?, max_results)` | Requests captured from tab creation via Playwright `request` / `response` / `requestfinished` / `requestfailed` events: method, URL, type, status, duration, headers |

**Console**: CloakBrowser's binary suppresses Playwright's live console stream (enabling the CDP Runtime domain is a bot signal). `_console_replay()` opens a CDP session, sends `Runtime.enable` (Chrome replays the current document's `consoleAPICalled` and `exceptionThrown` events), waits 0.2 s, sends `Runtime.disable` and detaches. Messages therefore cover the current document since it loaded.

### `kairos/tools/base.py` — ToolResult

```python
class ToolResult:
    def __init__(self, success: bool, output: str, error: Optional[str] = None,
                 workspace_changed: Optional[str] = None, image_url: Optional[str] = None):
```

`to_dict()` returns `{"success", "output", "error"}` plus `image_url` when present (used by `ReadTool`, browser and desktop screenshots); image data is kept out of the textual tool message and re-injected as a user vision message.

### `kairos/tools/read.py` — ReadTool

```python
class ReadTool:
    def __call__(self, path: str) -> ToolResult:
```

The `read` schema tells the model that it supports UTF-8 text files and images, returned as vision data.

- **Text files**: UTF-8 decoded with `errors="replace"`, max 100KB
- **Image files** (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, `.tiff`, `.tif`, `.svg`): base64 data URL returned as `image_url`, max 20MB
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

- Compiles `pattern` as `re.IGNORECASE` regex; `os.walk` with `SKIP_DIRS` pruning; `include` glob via `fnmatch.translate()`
- Skips binary files (a `\x00` in the first 512 bytes)
- Enforces a finite, non-negative timeout (default 10 s) with a monotonic deadline; a timed-out search fails with the partial matches found so far
- Validates `max_results` as a positive integer; returns `"file:line: text"` lines

### `kairos/tools/web_search.py` — WebSearchTool

`WebSearchTool(query, max_results=5)` posts to `https://api.tavily.com/search` (`Authorization: Bearer <tavily_api_key>` from `provider.json`, `max_results` clamped to 1-10, `include_answer: "basic"`, 30 s timeout) and returns `Answer: ...` followed by numbered title, URL and a 300-character snippet per result.
- No key: fails with a pointer to the browser.
- 429 (rate limit): waits 2 s and retries once; a second 429 fails with the browser advice.
- 432/433 (plan or pay-as-you-go credits exhausted): sets the class attribute `out_of_credits`, so every agent in the process stops calling Tavily, and fails with "Tavily is out of credits ...".
- The browser advice reads "Use the browser to search instead (sub-agents have no browser: say in your result that web search was unavailable)."
- Other HTTP errors fail with the status and Tavily's error text. Sub-agents have the tool too.

### `kairos/tools/git.py` — GitTool

```python
class GitTool:
    def set_workspace(self, path: str): ...
    def __call__(self, command: str, **kwargs) -> ToolResult:
```

Sub-commands: `status` (`git status --porcelain`), `diff` (`git diff [-- path]`), `log` (`git log --oneline -n N`, default 10), `commit` (`git add -A` + `git commit -m message`), `branch` (`git branch --list`). All run with `cwd` set to the workspace.

### `kairos/tools/terminal.py` — Terminal Tools

Six wrappers around `TerminalManager`: `NewTerminalTool`, `ExecuteCommandTool` (`terminal_id, command, timeout, is_background, until`), `ReadLogsTool` (`terminal_id, start_line, end_line, tail`), `StopCommandTool`, `CloseTerminalTool`, `GetTerminalInfoTool`. Each returns a `ToolResult` and turns exceptions into failures.

### `kairos/tools/subagent.py` — SubAgentTool

```python
class SubAgentTool:
    MAX_RUNNING = 5
    MAX_RESULT_CHARS = 12_000

    def __init__(self, workspace, model_config, on_event, on_status): ...
    def spawn(self, prompt: str) -> ToolResult: ...
    def message(self, subagent_id: str, message: str) -> ToolResult: ...
    def terminate(self, subagent_id: str) -> ToolResult: ...
    def list(self) -> ToolResult: ...
```

Sub-agents always run in the background, each on its own thread; every tool returns at once.
- **`spawn(prompt)`**: refuses when 5 are running; creates a child `Agent(workspace)` switched to the parent's current spec and effort (`model_config()` is read at spawn time), with `_is_subagent = True` and no `subagent_tool`. IDs are 12 hex characters.
- **`message(id, message)`**: continues an idle, failed or terminated sub-agent with its previous context; refused while it runs or when 5 are running.
- **`terminate(id)`**: interrupts a running sub-agent; its context is kept for `message`.
- **`list()`**: one line per sub-agent: ID, state, elapsed time, current tool, task (first 100 characters) and `recent_actions` (the last 3 tool summaries, oldest first).
- States: `running`, `idle` (finished, context kept), `failed`, `terminated`, `lost`.
- When a run ends, `on_event` receives a background event: `subagent_finished` (result capped at 12,000 characters, with a hint that `message_subagent` continues it) or `subagent_failed`.
- `on_status` receives a status dict (`subagent_id`, `state`, `elapsed_s`, `prompt`, `recent_actions`, `current_tool`?, `tokens`) on spawn, each tool call, finish, failure, termination and loss.

Host API: `statuses()`, `running_count()`, `child_agents()`, `stop_all()` (the user's `/stop`), `mark_lost()` (the host is going away: running sub-agents are interrupted, marked `lost`, and a `subagent_failed` notification says the work may be incomplete).

**Sub-agent restrictions**: no sub-agent tools (removed from the schema and refused in `_execute_tool`) and no browser tools; they have the other 28 tools, including `web_search` and the desktop tools. A sub-agent's own background terminals notify its next step (it has no host callback).

### `kairos/tools/skills.py` — SkillManager

```python
class SkillManager:
    SKILL_FILENAME = "SKILL.md"

    def __init__(self, skills_dir: str): ...
    def list_skills(self) -> ToolResult: ...
    def load_skill(self, skill_name: str) -> ToolResult: ...
    def write_skill(self, skill_name: str, content: str, overwrite: bool = False) -> ToolResult: ...
```

Skills are stored under `<workspace>/skills/<skill_name>/SKILL.md`. Only skill names are loaded into the system prompt; full content is fetched on demand. `write_skill` validates the name (no `..`, `/`, `\`, special characters) and refuses to overwrite unless `overwrite=true`. The `skills/` directory is gitignored.

### `kairos/tools/browser.py` — Browser Tools

- `BrowserTool(method, failure_label)` wraps one `BrowserManager` method and returns `ToolResult(True, output)`, or `ToolResult(False, "", "<label> failed: <error>")`. `ImportError` returns an installation message.
- `BrowserScreenshotTool(bm)` returns the screenshot as `image_url` for vision injection.

`Agent.__init__` builds `self.browser_tools`, a dict from tool name to `BrowserTool`, plus `self.browser_screenshot_tool`. Schema parameter names equal the `BrowserManager` keyword arguments, so `_execute_tool` dispatches with `tool(**args)`; `tests/test_browser.py` checks this. The 20 tools: `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_fill_form`, `browser_select`, `browser_hover`, `browser_drag`, `browser_press_key`, `browser_scroll`, `browser_wait_for`, `browser_screenshot`, `browser_evaluate`, `browser_search_page`, `browser_tabs`, `browser_handle_dialog`, `browser_console`, `browser_network`, `browser_profile`, `browser_close`. Sub-agents receive none of them.

### `kairos/tools/session.py` — SessionManager

Chat store of the offline CLI and the Textual frontend (gateway conversations live in SQLite). Each workspace has an isolated store at `~/.kairos/chats/<directory>--<stable-path-id>/chats.json`: the directory basename stays readable and a deterministic SHA-256 path prefix prevents collisions between workspaces with the same basename. The JSON envelope stores the absolute workspace path in `workspace`. Direct callers that omit a workspace use the current working directory. The offline CLI passes `~/Documents/kairos_workspace` when no path was given, so all fresh folders share one list.

`<workspace>/chats/chats.json` remains a read-compatible legacy source. Legacy-only sessions appear in the picker and are saved into the canonical store on the next save; the legacy file is never modified.

```python
class SessionManager:
    def __init__(self, workspace: str | os.PathLike[str] | None = None): ...
    def save_chat(self, conversation_history: List[Dict]): ...
    def new_session(self): ...
    def set_current_session(self, session_id: str): ...
    def list_sessions(self) -> List[Dict[str, str]]: ...
    def load_session(self, session_id: str) -> Optional[List[Dict]]: ...
```

- `_load_all()` loads canonical sessions first (canonical IDs win), then merges legacy sessions; accepts the envelope and the old flat format and recovers damaged JSON by parsing valid JSON boundaries.
- `_save_all()` writes atomically (temp file, `fsync()`, `os.replace()` with 5 attempts), falling back to a direct write on persistent Windows locks.
- `save_chat()` updates `_current_session_id` when it exists, otherwise creates `chat_<timestamp>`. No fuzzy matching.

```json
{
  "workspace": "C:/Users/arja/Documents/App",
  "sessions": {
    "chat_2024-01-15 10:30:00": {"timestamp": "2024-01-15 10:30:00", "preview": "Fix the login bug", "messages": [...]}
  }
}
```

### `kairos/temp_cli.py` — Textual Frontend (legacy)

A Textual app (`python run_temp_cli.py [workspace]`; `textual` is not in `requirements.txt`) that runs `Agent(workspace)` in-process with `SessionManager(agent.cwd)` and the shared resume logic. No launcher uses it. It does not host a `WakeScheduler`, so background results are delivered with the next message instead of waking the agent.

---

## Tests

Run the unittest suites with `python -m unittest discover -s tests -v`; the pytest-style files (`test_gateway_*.py`, `test_terminal_manager.py`) need `pytest`; `python tests/run_gateway_tests.py` is a dependency-light gateway check. Tests that construct a real Agent point `config_path()` at a temporary `provider.json`; no test calls a live model.

| File | Covers |
|------|--------|
| `test_providers.py` | Setup resolution, ad-hoc models, effort validation, key rotation/cooldown, the three adapters (request translation, recorded streams, errors), Agent streaming and model selection |
| `test_background.py` | Background sub-agents (events, failures, recent actions, cap, terminate, stop_all, lost), sub-agent tool exclusion and refusal, in-run notification injection, parent hard stop leaving sub-agents running, terminal `until`/`stop_command`/`read_logs` tail, event hand-over, missing-argument errors |
| `test_wake.py` | WakeScheduler debounce, in-run events, busy host, cap/pause/reset, exactly-once delivery under concurrency, close, `format_notifications` |
| `test_cli_gateway.py` | Command and argument parsing, rendering (word wrap, latest lines, one-line banner), GatewayBackend against a real local gateway with a fake agent, `/model`/`/effort`, OfflineBackend, wake rendering, sub-agent toolbar, `/stop`, offline and gateway wake turns, output above an open prompt |
| `test_web_search.py` | Result formatting, missing key, out of credits, 429 retry, errors, schema for agents and sub-agents |
| `test_workspaces.py` | Fresh folder naming and collisions |
| `test_compaction.py` | Tool/schema accounting, vision estimates, pending-user preflight, compaction prompt bounds, safe boundaries, legacy result normalization |
| `test_sessions.py` | Workspace-isolated `~/.kairos` chat stores and legacy loading |
| `test_browser.py` | Real headed CloakBrowser against `tests/fixtures/` (refs, diffs, forms, dialogs, stale refs, iframes, console/network, tabs, hard stop, profiles) and schema/signature parity; `KAIROS_SKIP_BROWSER_TESTS=1` skips the browser part |
| `test_desktop_manager.py` | Cua Driver response parsing and lifecycle without a native desktop |
| `test_gateway_api.py`, `test_gateway_manager.py`, `test_gateway_repository.py`, `test_gateway_history.py`, `test_gateway_config.py`, `test_terminal_manager.py` | Gateway routes and fresh workspaces, manager runs/unload/concurrent loads, repository, history repair, port validation, blocking timeouts (pytest) |
| `run_gateway_tests.py` | Gateway end-to-end with fake agents: runs, replay, interrupts, model settings, wake runs, pending notifications, lost sub-agents |

## Design Principles

1. **Streaming First** — Tokens print as they arrive; no waiting for full response
2. **Absolute Paths** — No workspace containment
3. **One Tool Per File** — Easy to add new tools
4. **Interruptible** — Esc and Ctrl+C hard-stop the main agent and return to the prompt at once; `/stop` stops sub-agents
5. **Never Poll** — Background work reports back through notifications and wakes
6. **Token Aware** — Session, context, and turn token counts displayed
7. **Config in Two Places** — Models and keys in `provider.json`; everything else in environment variables, read on first access
8. **Minimal Dependencies** — `openai`, `rich`, `prompt_toolkit`, `tiktoken`, `fastapi`, `uvicorn`, `websockets`, `playwright`, `cloakbrowser` (`httpx` comes with `openai`)

## Adding New Tools

1. Create `kairos/tools/your_tool.py` with a callable class returning `ToolResult`
2. Import in `kairos/tools/__init__.py` and add to `__all__`
3. Import in `kairos/agent.py` (top-level imports block) and create the instance in `Agent.__init__`
4. Add the tool schema to `Agent._get_tool_schema()` (OpenAI function definition)
5. Add a dispatch entry to `Agent._execute_tool()`; read required arguments as `a["x"]` so a missing one is reported by name
6. Add a summary case to `Agent._tool_summary()`
7. If sub-agents must not have it, add its name to `SUBAGENT_TOOL_NAMES` (browser tools are excluded by their `browser_` prefix)
8. **Update this AGENTS.md and README.md**

## Gateway Architecture

The gateway under `kairos/gateway/` owns durable SQLite-backed conversations, Agent runtimes, runs, wake scheduling and event replay. Start it with `python -m kairos.gateway_main [workspace]`, `python gateway_main.py` or `python -m kairos.main_gateway`; `kairos.bat` starts it when needed. It validates `provider.json` at startup. The optional workspace argument (or `KAIROS_DEFAULT_WORKSPACE`) is used only as the target of the legacy-chat import (`KAIROS_LEGACY_CHAT_FILE`). Workspace paths are context, not a security boundary.

**Configuration**: `KAIROS_GATEWAY_HOST`, `KAIROS_GATEWAY_PORT`, `KAIROS_DATA_DIR` (database `kairos.sqlite3`, default `~/.kairos`), `KAIROS_MAX_CONCURRENT_RUNS`, `KAIROS_RUNTIME_IDLE_SECONDS`, `KAIROS_AUTH_TOKEN` (Bearer token for HTTP and WebSocket; `/healthz` and `/readyz` stay public; the WebSocket also accepts `?token=`), `KAIROS_LEGACY_CHAT_FILE`, `KAIROS_DEFAULT_WORKSPACE`.

**Repository** (`repository.py`, schema version 4, WAL mode): tables `workspaces`, `conversations`, `runs`, `messages`, `events`, `schema_meta`. Columns added by idempotent migrations: `conversations.active_run_id`, `conversations.model_settings` (JSON `{setup, model, provider, effort}`; NULL = default setup), `conversations.pending_notifications` (JSON list of background events saved at unload), `runs.request_content`, `runs.request_image_url`, `runs.origin` (`user` or `wake`). Events have monotonic per-conversation IDs. Runs left `queued`/`running` by a previous process are marked interrupted at startup.

**Manager** (`manager.py`, `GatewayManager`):
- Conversations created without a workspace get a fresh folder (`workspaces.new_workspace()`).
- A runtime (`AgentRuntime`) is created on demand: `agent_factory(workspace)` (injectable for tests), stored model settings applied (invalid stored settings fall back to the default setup), history restored through `history.sanitize_for_resume`, pending notifications taken from the database and handed to `agent.add_background_events()`.
- Runs are serialized per conversation (FIFO queue, asyncio lock) and run in a thread pool of `KAIROS_MAX_CONCURRENT_RUNS` workers across conversations. After each run the full history is persisted (`replace_messages`). Control operations (compaction, continuation) reserve the runtime first.
- Model settings: `list_models()` and `configure_conversation(id, setup?, model?, provider?, effort?)` validate against `provider.json`, persist the choice and apply it to a loaded runtime (effective at its next API step); with no changes it returns the current model info.
- Wake: each runtime has a `WakeScheduler` (`wake_debounce=2.0`, `wake_cap=10`). The Agent's `on_background_event` emits `background.notification` and pushes the event; an idle runtime gets a wake run (origin `wake`, source `wake`, input `format_notifications(events)`); a run that starts while notifications are pending takes them; a user message or continuation resets the wake count. `on_subagent_status` emits `subagent.status` (running updates live-only, final states persisted). `list_subagents()` and `stop_subagents()` back the WebSocket commands.
- Idle unloading (`cleanup_idle`, every 60 s) skips runtimes that are running or have background work (sub-agents or background commands). Unload closes the scheduler, marks running sub-agents lost (with notifications), persists history, closes browser/terminal resources (also of child agents), and saves undelivered notifications to `pending_notifications`. Shutdown cancels queued runs, interrupts running ones, waits for them, then force-unloads every runtime.

**Events** (envelope `{"type": "event", "event_id", "event", "conversation_id", "run_id", "created_at", "data"}`): `conversation.loaded`, `conversation.unloaded`, `run.queued`, `run.started`, `run.completed`, `run.interrupted`, `run.failed`, `run.cancelled` (run events carry `origin`), `assistant.stream_started`, `assistant.token` (live-only, not persisted; `assistant.stream_ended` carries the full text), `assistant.stream_ended`, `tool.started`, `tool.finished`, `token.updated`, `conversation.compacted`, `background.notification`, `subagent.status`, `wake.paused`.

**WebSocket** `/api/v1/ws` (commands `{"type": "command", "command", "request_id", "conversation_id"?, "payload"}`, answered with `ack` or `error`): `gateway.hello`, `conversation.list`, `conversation.create`, `conversation.load` (returns `conversation`, visible `history` and `needs_continue`), `conversation.unload`, `conversation.subscribe` (`after_event_id`; replay then live, deduplicated by event ID), `conversation.unsubscribe`, `conversation.messages`, `message.send`, `conversation.compact`, `conversation.continue`, `run.interrupt`, `run.cancel`, `model.list`, `conversation.configure`, `subagent.list`, `subagent.stop_all`, `ping`.

**REST**: `GET /healthz`, `GET /readyz`, `GET /api/v1/capabilities`, `GET|POST /api/v1/workspaces`, `GET /api/v1/workspaces/{id}`, `GET|POST /api/v1/conversations`, `GET|PATCH|DELETE /api/v1/conversations/{id}`, `POST .../runtime/load`, `POST .../runtime/unload`, `GET|POST .../messages`, `POST .../continue`, `POST .../compact`, `GET /api/v1/runs/{id}`, `POST /api/v1/runs/{id}/interrupt`, `POST /api/v1/runs/{id}/cancel`. Model settings and sub-agent commands are WebSocket-only.

Compile check: `python -m compileall -q kairos gateway_main.py tests`. Every code change must update this file and `README.md`.

### Native Desktop / Cua Driver Integration

Kairos optionally integrates the Cua Driver through `kairos/desktop_manager.py` and `kairos/tools/desktop.py`. `DesktopManager` accepts an executable or directory in `KAIROS_CUA_DRIVER_PATH`, then lazily discovers `cua-driver` using PATH or the official Windows per-user location under `%LOCALAPPDATA%\Programs\Cua\cua-driver\bin`, starts its persistent daemon, invokes daemon-backed CLI tools, extracts screenshots into `ToolResult.image_url`, supports cancellation, and closes the daemon on reset. Native desktop tools include doctor, window listing/state, screenshots (implemented through the current driver's `get_window_state` response), application launching, clicking, typing, keyboard shortcuts, scrolling, moving, dragging, and window actions. Accessibility indices are primary for supported actions; pixel coordinates are required for cursor movement and dragging. Wrappers map Kairos drag coordinates to Cua's `from_x/from_y/to_x/to_y` fields, add scroll defaults, and map `activate_window` to `bring_to_front`. The current driver does not expose minimize/maximize/close tools, so those actions return explicit unsupported results. Desktop wrappers normalize accidental simultaneous accessibility-index and pixel arguments by preferring the semantic address. Desktop tools are available to sub-agents. Regression coverage is in `tests/test_desktop_manager.py` and uses mocked/fake transport behavior without requiring a native desktop.
