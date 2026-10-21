# Kairos

A minimal personal coding agent in Python.

## Features

- **53 Tools**: File operations, terminals, search, web search, git, background sub-agents, skills, a browser automation suite (including console/network inspection), and optional native desktop automation through Cua Driver. The 20 browser and 13 desktop tools are loaded on demand (`enable_tools`), so a request carries 20 tools until the agent needs them
- **Direct Model Access**: Talks to OpenAI (Responses API), OpenRouter or any Chat Completions server, and Anthropic (Messages API) directly; providers, named model setups and API keys live in `provider.json`. Several keys per provider rotate on 401/402/429 with a 60-second cooldown. Model and reasoning effort are chosen per conversation with `/model` and `/effort`. A model's own reasoning (Anthropic thinking blocks, OpenRouter `reasoning_details`) is sent back to it on the next request, so it keeps its train of thought across tool calls.
- **Gateway**: One gateway process owns conversations (SQLite), Agents, terminals and the browser. The CLI is a client of it; `--offline` runs the agent inside the CLI instead. It refuses cross-site browser requests, and only one gateway can use a data folder at a time.
- **Background Work, No Polling**: Sub-agents always run in the background, and background terminal commands notify when they finish or when their output matches a pattern. Results arrive as notifications; if the agent is idle, it is woken to handle them, and the wake turn prints above your prompt without disturbing what you are typing.
- **Web Search**: Tavily search with a short answer and compact results; when it is unavailable the agent is told to use the browser
- **Workspaces**: `kairos <path>` or `kairos .` works in a folder; `kairos` alone starts in a fresh folder under `Documents/kairos_workspace`
- **Workspace-relative Paths**: Relative paths in file tools resolve against the conversation's workspace, and terminals start there; absolute paths reach anywhere — no workspace restrictions
- **Streaming First**: Tokens print as they arrive; no waiting for full responses
- **Token Aware**: after every model call: context used (% and tokens / window), that call's output tokens, session input/output tokens and session cost, using provider-reported usage when available and tiktoken estimates otherwise
- **Auto-Compaction**: History is summarized when the context passes 80% of the budget; your messages are kept word for word next to a handoff summary. Estimates include tool schemas and tool-call metadata, and the compaction request itself is bounded
- **Browser Automation**: CloakBrowser stealth Chromium, headed (visible) in the agent and always humanized, launched lazily on first use. `BrowserManager(headless=True)` exists for batch callers outside the agent. Accessibility snapshots with element refs, diff snapshots after actions, dialog handling, iframes, tabs, persistent profiles with pinned fingerprints, and console/network inspection.
- **Native Desktop Automation**: Optional Cua Driver integration exposes native window discovery, accessibility-tree snapshots, screenshots, app launching, clicking, typing, keyboard, scrolling, dragging, and window actions. Accessibility element indices are the primary addressing mode; pixel coordinates are available as a fallback. Desktop tools are available to sub-agents and are loaded lazily when Cua Driver is installed.
- **Skills**: Self-extensible skill system — the agent can create/load skills stored as `SKILL.md` files in the workspace's `skills/` directory
- **Paste System**: Bracketed paste and Ctrl+V insert text as a `(Pasted Text #1)` token; Alt+V pastes a clipboard image as `(Pasted Image #1)`. Backspace removes a whole token. Copying never counts as pasting.
- **`/resume`**: Continue a saved conversation from a numbered picker; an interrupted turn is repaired and continued automatically
- **Immediate Stop**: Esc and Ctrl+C stop the main agent and return to the prompt at once (stream closed, blocking commands killed, browser call cancelled), also while a message is still being sent or auto-compaction is summarizing (your message is kept); sub-agents keep running until `/stop`. Interrupted turns stay repairable so the next message continues them. Text typed while the agent works is pre-filled into the next prompt, never sent on its own.
- **Bounded Tool Results**: Tool text is capped at 20,000 characters per result (beginning and end kept, the full text saved to a file the agent can read); images go to the model as vision input, not text
- **Loop Guard**: when the same tool call (same name and arguments) fails 3 times in a row, its result carries a warning telling the model to change approach (and the CLI prints a notice); a 5th identical failure stops the run with `[Stopped: <tool> failed 5 times in a row ...]`
- **Honest Tool-Call Errors**: tool-call arguments that are not valid JSON or not an object, and every call of a reply cut off at the output-token limit, are not run; the model is told exactly what went wrong. Small slips (a tool name in the wrong case, `"20"` for a number, a JSON string for an array, `null` for an optional argument) are repaired against the tool's schema
- **Environment Aware**: the system prompt names the OS and the terminal shell (cmd.exe on Windows), and the current date is added to your message whenever it changes
- **Retries, Caching and Clean Errors**: Transient API failures (rate limits, overload, connection errors, 408/409/429/5xx) are retried up to 5 times with backoff, honouring the server's `retry-after`; a final failure shows a short `API Error` summary without a traceback. Anthropic requests mark the system prompt, tools and newest turn for prompt caching; OpenRouter requests carry a per-conversation `session_id` so the conversation stays on one host and its prompt cache
- **Clean CLI**: `rich` and `prompt_toolkit`; one-line banner, typed input and replies wrap at word boundaries, model and effort at the right of the prompt, sub-agents in the bottom toolbar, and Markdown tables with rounded boxes and alternating rows

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

### Models: `provider.json`

Copy `provider.example.json` to `provider.json` in the repository root and fill in your keys. `provider.json` is gitignored; never commit it.

```json
{
  "default_setup": "gpt-6-luna",
  "providers": {
    "openai": {"format": "responses", "base_url": "https://api.openai.com/v1", "api_keys": ["sk-proj-..."]},
    "openrouter": {"format": "completions", "base_url": "https://openrouter.ai/api/v1", "api_keys": ["sk-or-v1-...", "sk-or-v1-..."]},
    "anthropic": {"format": "anthropic", "base_url": "https://api.anthropic.com", "api_keys": []}
  },
  "setups": {
    "gpt-6-luna": {"provider": "openai", "model": "gpt-6-luna", "reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"], "default_effort": "high"}
  },
  "tavily_api_key": "tvly-..."
}
```

- **`providers`**: `format` is `responses` (OpenAI Responses API), `completions` (Chat Completions: OpenAI, OpenRouter, compatible servers) or `anthropic` (Messages API). `api_keys` is a list; extra keys are used when one is rate-limited or out of quota.
- **`setups`**: named model choices with `provider`, `model`, `reasoning_efforts` and `default_effort`, plus `max_context` (the model's real context limit) and `pricing` (`input`, `cached_input`, `output`, USD per 1M tokens) for the status line's cost. A setup may override `format`, `base_url` or `api_keys`.
- **`default_setup`**: the setup new conversations start with.
- **`tavily_api_key`**: for `web_search` (optional).

Setups in the example:

| Setup | Provider | Efforts |
|-------|----------|---------|
| `gpt-6-luna`, `gpt-6-sol` | OpenAI | none, low, medium, high, xhigh, max |
| `gpt-6.1-sol`, `gpt-6-astra` | OpenAI | low, medium, high, xhigh, max |
| `glm-5.3-flash` | OpenRouter | minimal, low, medium, high, xhigh, max (reasoning always on) |
| `mimo-v2.5` | OpenRouter | on, off |

The Anthropic format is implemented and unit-tested but has not yet been used against the live API.

### Environment variables (optional)

| Variable | Default | Purpose |
|----------|---------|---------|
| `KAIROS_CONTEXT_WINDOW` | 300000 | Context cap; each model uses min(cap, its `max_context`) and auto-compacts at 80% of that |
| `KAIROS_MAX_TOOL_RESULT_CHARS` | 20000 | Text cap per tool result |
| `KAIROS_GATEWAY_HOST` / `KAIROS_GATEWAY_PORT` | 127.0.0.1 / 8765 | Gateway address (CLI and gateway) |
| `KAIROS_AUTH_TOKEN` | none | Bearer token required by the gateway and sent by the CLI; needed to listen on a non-loopback address |
| `KAIROS_DATA_DIR` | `~/.kairos` | Gateway database folder |
| `KAIROS_MAX_CONCURRENT_RUNS` | 8 | Runs executing at once across conversations |
| `KAIROS_RUNTIME_IDLE_SECONDS` | 1800 | Unload idle conversations after this many seconds (0 = never) |
| `KAIROS_LEGACY_CHAT_FILE` / `KAIROS_DEFAULT_WORKSPACE` | none | One-time import of an old `chats.json` into that workspace |
| `KAIROS_CUA_DRIVER_PATH` | PATH lookup | `cua-driver` executable or folder |

There is no `.env` file; set these in the environment.

### Gateway security

The gateway listens on `127.0.0.1` by default. Browser requests from other sites (a non-loopback `Origin`) are refused, so a web page cannot drive the agent; the CLI sends no `Origin` and is allowed. Without `KAIROS_AUTH_TOKEN`, requests whose `Host` is not loopback are refused too (DNS rebinding), and the gateway will not start on a non-loopback address. With a token, every request except `/healthz` and `/readyz` must carry it (`Authorization: Bearer`, or `?token=` on the WebSocket, which is kept out of the logs), and any `Host` is accepted. Only one gateway can use a data folder (`<data dir>/gateway.lock`); a second one exits with an error.

## Usage

```bash
kairos                  # gateway mode, new conversation in a fresh folder
kairos .                # current folder as the workspace
kairos C:\path\to\app   # that folder as the workspace
kairos_offline .        # same, with the agent running inside the CLI (no gateway)
```

`kairos.bat` keeps your current folder, checks the gateway's `/healthz`, starts `python -m kairos.gateway_main` in a minimized "Kairos Gateway" window if it is not running (the window stays open with the error if the gateway fails), waits up to about 30 seconds for it, and then opens the CLI. `kairos_offline.bat` runs `main.py --offline`. Without the launchers: `python main.py [path] [--offline]`, and start the gateway yourself with `python -m kairos.gateway_main`.

Without a path, each conversation gets a new folder such as `Documents/kairos_workspace/2026-10-11_143005`. With a path, `/reset` starts a new conversation in the same folder; without one, in a new folder.

After each model call the CLI prints a status line: share of the context window used, tokens in context / the window, that call's output tokens, the session's input and output tokens, and the session cost (from the setup's `pricing`):

```
Context 12.0% · 36,120 / 300,000 tokens · this call: 812 out · session: 120,433 in / 8,210 out · $0.4213
```

### Commands and keys

| Command / key | Description |
|---------------|-------------|
| `Esc` / `Ctrl+C` (during a turn) | Stop the main agent and return to the prompt at once; sub-agents keep running |
| `Esc` (at the prompt, during a wake turn) | Stop the wake turn |
| `Ctrl+V` | Paste text (terminals that send bracketed paste are handled automatically) |
| `Alt+V` | Paste an image from the clipboard |
| `/resume` / `/resume 1` | Pick a saved conversation (this workspace's, or all fresh-folder ones when no path was given) |
| `/compact` | Compact the conversation history now |
| `/model` | List the setups and pick one by number or name; `/model <name>` picks directly |
| `/model <model-id> <openai\|anthropic\|openrouter>` | Use any model id on that provider |
| `/effort` / `/effort <level>` | Show or set the reasoning effort |
| `/stop` | Stop all running sub-agents |
| `/reset` | Start a new conversation |
| `/clear` | Clear the screen |
| `/quit` | Exit (also Ctrl+C or Ctrl+D at the prompt) |

Unknown commands are reported instead of being sent to the agent; a path such as `/etc/hosts` at the start of a message is not a command. A message that is only a pasted image is sent as "Describe this image."

### Background work

- `spawn_subagent` returns immediately; the sub-agent works in the background (at most 5 at once) and its result arrives as a notification. Running sub-agents are listed in the bottom toolbar with their current tool and elapsed time.
- A background terminal command notifies when it finishes, or once when a line of its output matches `until` (for example `Listening on` for a dev server) while it keeps running.
- If a notification arrives while the agent is working, it is added before its next step. If the agent is idle, it is woken (notifications within 2 seconds are batched) and the wake turn prints above your prompt with a header like `» woke: sub-agent a3f2 finished`; what you are typing stays in the input.
- After 10 wakes in a row without a message from you, auto-wake pauses (`paused auto-wake: ...`); the waiting results are sent with your next message.
- The gateway keeps conversations with running background work loaded. If it shuts down, running sub-agents are marked lost and the agent is told on the next turn.

### Resume behavior

`/resume` lists saved conversations and loads the one you pick, showing its last reply; when Kairos was started without a path, the conversation continues in its own folder. If it ended in the middle of a turn, the unfinished tool calls are repaired (missing results become failed results) and the agent continues with "Continue where you left off". In gateway mode conversations live in the gateway database; in offline mode chats are saved per workspace in `~/.kairos/chats/<folder>--<id>/chats.json` (older `<workspace>/chats/chats.json` files are still read), after every turn, every 60 seconds (a failed save is logged and retried at the next interval), and when the window closes (on Windows also at logoff or shutdown).

## Tools

### File Tools

| Tool | Description |
|------|-------------|
| `read(path, offset?, limit?)` | Read UTF-8 text (SVG is read as text) one page at a time: up to 2000 numbered lines and no more than the tool-result cap, ending with `[Showing lines A-B of N. Use offset=B+1 to continue.]` or `[End of file — N lines]`; lines over 2000 characters are cut. Or a PNG, JPEG, GIF or WebP image (detected from its content, up to 3,932,160 bytes so the encoded image stays under 5 MB), returned as vision data; other image formats are refused |
| `write(path, content)` | Write or create a file as UTF-8, byte for byte (parent directories created automatically) |
| `edit(path, oldText, newText, replace_all?)` | Find-and-replace that must match exactly once (or every match with `replace_all`). An exact match wins; only when there is none does it retry with typographic quotes, dashes and spaces normalized, then ignoring trailing whitespace, then with `read`'s line-number prefixes removed. A fuzzy match replaces only the original span. Keeps the file's line endings and BOM and refuses files that are not UTF-8 |

Relative paths resolve against the conversation's workspace.

### Search, Web & Git

| Tool | Description |
|------|-------------|
| `search(pattern, path?, include?, max_results?, timeout?)` | Regex file content search (like ripgrep) in `path` or the workspace — skips binary files and non-source directories; defaults to a 10-second timeout, which also bounds a single slow regex match, and returns partial matches if the deadline is reached |
| `web_search(query, max_results?)` | Tavily web search: a short answer plus title, URL and snippet per result (1-10 results, default 5). A rate limit is retried once; when Tavily is out of credits, search is disabled until Kairos restarts and the agent is told to use the browser |
| `git(command, **kwargs)` | Git operations: `status`, `diff`, `log`, `commit`, `branch` |

Tool results are capped before they are stored or sent: 20,000 characters per result by default, keeping the beginning and end with the original length noted. The full text is saved to `~/.kairos/tool-output/` (files older than 7 days are deleted) and the marker names the file, so the agent can `read` or `search` it. Inline image data is sent as a separate vision message. Older saved chats are normalized the same way before their next request.

### Terminal Tools

| Tool | Description |
|------|-------------|
| `new_terminal(background)` | Create a terminal — `true` for a persistent shell, `false` for one-shot commands |
| `execute_command(command, terminal_id?, timeout?, until?)` | Run a command; without `terminal_id` it runs in a default blocking terminal. Blocking commands wait up to `timeout` seconds (default and maximum 20) and return plain text: `Exit code`, `Wall time` and the output. Background terminals return at once and notify when the command finishes; `until` (a regex) also notifies once when an output line matches |
| `read_logs(terminal_id, tail?, start_line?, end_line?)` | Background output with a `[lines A-B of N]` header; the last 100 lines by default |
| `stop_command(terminal_id)` | Kill the command running in a background terminal; the shell stays open |
| `close_terminal(terminal_id)` | Close a terminal and release resources |
| `get_terminal_info(terminal_id)` | Terminal status (type, closed, line count, pending commands) |

Terminals start in the workspace, with `NO_COLOR=1`, `TERM=dumb` and `PAGER`/`GIT_PAGER`/`GH_PAGER=cat`. A blocking command still running at its timeout is not killed: it moves to a new background terminal (`read_logs`, `stop_command` and `close_terminal` work on it) and notifies when it finishes; a missing timeout means 20 s, an invalid one is rejected before the command starts. Background commands get an empty stdin, so a command that reads input gets end-of-file instead of swallowing the shell's next lines. Background shells keep their state (folder, environment) between commands and keep the full output for `read_logs`; notifications include up to 12,000 characters of output.

### Sub-Agent Tools

| Tool | Description |
|------|-------------|
| `spawn_subagent(prompt)` | Start a sub-agent in the background; returns its ID at once |
| `message_subagent(subagent_id, message)` | Continue a finished sub-agent with its previous context (after `terminate_subagent`, waits up to 2 s for the stopped run to finish) |
| `terminate_subagent(subagent_id)` | Stop a running sub-agent (its context is kept) |
| `list_subagents()` | ID, state, current tool, elapsed time, task and last 3 tool calls of each sub-agent |

Sub-agents use the parent's current model and effort and share its workspace. They have every tool except the browser and the sub-agent tools (desktop tools through `enable_tools`), and their own system prompt that describes only those tools.

### Skill Tools

| Tool | Description |
|------|-------------|
| `list_skills()` | List all available skill names (from `skills/` directory) |
| `load_skill(skill_name)` | Load and return a skill's full `SKILL.md` content |
| `write_skill(skill_name, content, overwrite?)` | Create or update a skill's `SKILL.md` (default: refuses if exists; `overwrite=true` to replace) |

Skills are stored in `skills/<skill-name>/SKILL.md`; the `skills/` folder is created when the first skill is written. Only skill names are injected into the system prompt — full content is loaded on demand via `load_skill`.

### Optional tool groups

Browser and desktop tools are off until needed: `enable_tools(group)` (`browser` or `desktop`) turns a group on for the rest of the conversation and returns its usage guide. A direct call to a tool of a group that is off turns the group on and runs. Enabled groups survive compaction and `/resume`.

### Browser Tools

The browser is a headed, humanized CloakBrowser that launches automatically on the first browser tool call. It passes bot detection (Cloudflare, reCAPTCHA, fingerprinting checks), and the system prompt tells the agent so. Snapshots are Playwright's AI accessibility snapshots: elements carry refs like `[ref=e12]` (`f1e12` inside iframes and after the page navigates), and tools take those refs. Every action returns the resulting page state: only the changed snapshot lines while the URL stays the same, the full snapshot after navigation, and a fresh full snapshot with any error.

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
| `browser_screenshot(full_page?, ref?)` | Screenshot returned as vision data and saved to `~/.kairos/screenshots/screenshot_YYYYmmdd_HHMMSS_ffffff.png` |
| `browser_evaluate(expression, ref?)` | Run JavaScript; an expression or a function (with `ref`, the function receives the element) |
| `browser_search_page(pattern, regex?, case_sensitive?, max_results?)` | Find text in the page's visible text with context |
| `browser_tabs(action, index?, url?)` | `list` / `new` / `select` / `close` tabs |
| `browser_handle_dialog(accept, prompt_text?)` | Answer an open alert/confirm/prompt |
| `browser_console(index?, errors_only?, max_results?)` | Console messages and uncaught errors of the current page |
| `browser_network(index?, filter?, max_results?)` | Network requests of the current tab (long URLs shortened in the middle); `index` shows headers and timing |
| `browser_profile(action, name?, proxy?)` | `list` / `use <name>` / `ephemeral`; switching relaunches the browser |
| `browser_close()` | Close the browser; the next browser tool relaunches it |

Browser features:
- **Stealth and humanized input**: CloakBrowser's patched Chromium, with every click, keystroke and mouse path humanized. Refs are resolved to element handles before acting, because CloakBrowser's humanized Locator methods cannot resolve `aria-ref` selectors; inside iframes, where its engine cannot map a handle, the humanized page mouse and keyboard act at the element's box.
- **Real accessibility snapshots**: `page.aria_snapshot(mode="ai")` gives real ARIA roles and names, iframe content inline, and refs for every element; snapshots over 15,000 characters are truncated with a pointer to `browser_search_page`.
- **Diff snapshots**: actions on the same page return only the snapshot lines that changed (refs are stable across snapshots), so a click on a large page returns a few hundred characters instead of the whole page.
- **Dialogs**: actions race against `dialog` events. An alert/confirm/prompt is reported to the agent and answered with `browser_handle_dialog`, instead of hanging the call or being silently dismissed.
- **Stale refs**: a ref that no longer exists fails immediately with a fresh snapshot.
- **Lazy launch and recovery**: the browser starts on the first call, a failed or cancelled launch (Esc, timeout) releases its profile and can be retried, and closing the window by hand makes the next call relaunch.
- **Persistent profiles**: `browser_profile use <name>` keeps cookies and logins under `~/.cloak-browser-mcp/profiles/<name>` (or `CLOAKBROWSER_PROFILES_DIR`), the profiles of Claude Code's cloakbrowser MCP, and pins a fingerprint seed in that MCP's `.cloak-mcp.json`, so a logged-in identity always presents the same device in both tools. A profile can be open in only one browser at a time; a second conversation, or a profile already open in the MCP, gets a clear error.
- **New tabs**: when an action opens a tab, Kairos switches to it and says so.
- **Console and network**: network requests are captured from tab creation with status, timing and headers. CloakBrowser suppresses the live console stream (enabling the CDP Runtime domain is a bot signal), so `browser_console` briefly enables it on demand, which makes Chrome replay the current page's console messages and uncaught exceptions.

### Native desktop tools

When `cua-driver` is installed, Kairos also exposes native desktop automation through Cua Driver. Use `desktop_window_state(pid, window_id)` before interacting with a window and again afterward to verify the result. Accessibility `element_index` values from the latest state are the primary interaction method for supported actions; pixel coordinates are required for cursor movement and dragging. If both addressing forms are accidentally supplied, Kairos prefers the accessibility address to match Cua's mutually exclusive contract. Kairos maps drag coordinates to Cua's `from_x/from_y/to_x/to_y` fields, supplies scroll defaults, maps `activate_window` to `bring_to_front`, and reports minimize/maximize/close as unsupported because the current driver does not expose those tools. Desktop tools are available to sub-agents. Set `KAIROS_CUA_DRIVER_PATH` to override PATH discovery; on Windows the official per-user install under `%LOCALAPPDATA%\Programs\Cua\cua-driver\bin` is also checked. The Cua daemon starts when a desktop tool is first used; its error output goes to `~/.kairos/cua-driver.log`.

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

## Architecture

```
main.py                     # Entry point (CLI)
gateway_main.py             # Gateway entry point
kairos.bat                  # CLI launcher; starts the gateway when needed
kairos_offline.bat          # CLI launcher without the gateway
provider.example.json       # Template for provider.json (models, keys)
kairos/
├── main.py                 # CLI: arguments, REPL, commands, gateway and offline backends
├── cli.py                  # Terminal UI (streaming panel, wake output above the prompt, toolbar, paste)
├── gateway_client.py       # WebSocket client of the gateway
├── gateway/                # Gateway: SQLite repository, runtime manager, REST/WebSocket server
├── providers/              # provider.json, OpenAI Responses / Chat Completions / Anthropic adapters
├── agent.py                # Core agent loop, streaming, tool dispatch, background events, compaction
├── wake.py                 # Wake scheduling for background results
├── workspaces.py           # Fresh workspace folders
├── resume.py               # Saved-history repair
├── config.py               # Environment settings
├── tokens.py               # Token counting
├── terminal_manager.py     # Terminals (blocking, background, until, stop)
├── browser_manager.py      # CloakBrowser via async Playwright on a worker thread
├── desktop_manager.py      # Cua Driver integration
└── tools/                  # read, write, edit, search, web_search, git, terminal, subagent,
                            # browser, desktop, skills, session
tests/                      # unittest/pytest suites and tests/run_gateway_tests.py
```

## Key Concepts

### Gateway and clients

The gateway owns every conversation: its history, model settings, Agent, terminals, sub-agents and browser. Clients send messages and receive streamed events over a WebSocket (`/api/v1/ws`); a REST API covers conversations, messages and runs. The CLI is the first client; a desktop and a phone app are planned. Offline mode is the same CLI with the agent in-process, for when the gateway is not wanted.

### Streaming

Replies stream into a grey panel that shows the latest lines; the final reply becomes a green panel rendered as Markdown. Tool-call preambles stay as grey traces, and each tool call prints a one-line summary such as `→ read file: /path`.

### Compaction

When the conversation context exceeds 80% of the context window (min(300k, the model's `max_context`)), older messages are replaced by your messages from that part, word for word (newest first, up to 20,000 tokens or a fifth of the window; older ones are counted as omitted), and a handoff summary (Progress, Key Decisions, Constraints & Preferences, Current State, Next Steps, Key Files & Context) that tells the model to build on the work rather than restart. The most recent 20% of the budget is kept as is. The check runs before every API step, including inside long tool-call loops. `/compact` triggers it manually.

### Skills

Skills are self-contained knowledge modules stored in `skills/<skill-name>/SKILL.md`. Only skill names are injected into the system prompt; the agent loads one with `load_skill` and can write new ones with `write_skill`.

### AGENTS.md

Kairos auto-loads an `AGENTS.md` file from the workspace root into the system prompt. Use this to give the agent project-specific conventions, instructions, or context.

## Design Principles

1. **Streaming First** — Tokens print as they arrive; no waiting for full response
2. **Workspace-relative, not contained** — Relative paths resolve against the workspace; absolute paths reach anywhere
3. **One Tool Per File** — Easy to add new tools
4. **Interruptible** — Esc and Ctrl+C stop the main agent and hand control back at once; `/stop` stops sub-agents
5. **Never Poll** — Background work reports back through notifications and wakes
6. **Token Aware** — Session, context, and turn token counts displayed
7. **Minimal Dependencies** — `openai`, `rich`, `prompt_toolkit`, `tiktoken`, `regex`, `fastapi`, `uvicorn`, `websockets`, `playwright`, `cloakbrowser`
8. **Loud by Default** — Tools always report what they did. Success includes specifics (e.g. "Wrote 42 lines to `main.py`"). Failure clearly states what went wrong. Nothing happens silently — overwrites, creations, and deletions are always announced.

## Tests

```bash
python -m unittest discover -s tests -v   # unittest suites
python -m pytest tests                    # also runs the pytest-style gateway tests
python tests/run_gateway_tests.py         # dependency-light gateway checks
```

The suites cover the provider adapters and key rotation, the agent loop (tool-chain repair, stops, compaction), the file tools, background sub-agents, terminals and the wake scheduler, the CLI in both modes (against a local gateway with a fake agent), web search, workspace folders, compaction and token accounting, chat storage, the gateway (including its cross-site and DNS-rebinding protection), desktop integration, and a real headed browser against local fixtures (`KAIROS_SKIP_BROWSER_TESTS=1` skips it; 240 unittest tests, 18 skipped). No test calls a live model.

## License

MIT
