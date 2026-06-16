# Kairos Architecture Documentation

## Overview

Kairos is a minimal coding agent written in Python, designed to assist with file operations and terminal commands using absolute paths. It uses the OpenAI chat completions API with streaming and function calling to autonomously execute tasks through a set of well-defined tools.

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
├── chats/                  # Saved chat sessions (gitignored)
│   └── chats.json          # All chat history in one file
└── kairos/
    ├── __init__.py         # Package exports
    ├── main.py             # CLI entry point and main loop
    ├── config.py           # Environment variable loading (lazy)
    ├── agent.py            # Core agent logic, streaming, OpenAI integration
    ├── cli.py              # Terminal UI with rich/prompt_toolkit
    ├── tokens.py           # Token counting with tiktoken
    ├── terminal_manager.py # Terminal lifecycle management
    └── tools/
        ├── __init__.py     # Tool exports
        ├── base.py         # ToolResult class
        ├── read.py         # Read file tool
        ├── write.py        # Write file tool
        ├── edit.py         # Strict find-and-replace tool
        ├── workspace.py    # Change workspace tool
        ├── terminal.py     # All terminal tools
        ├── search.py       # Regex file search (ripgrep-like)
        ├── git.py          # Git command tools
        └── session.py      # Chat save/load manager
```

## Component Architecture

### 1. Entry Point (`main.py` at root)

Simple wrapper that imports and calls `kairos.main.main()`.

### 2. CLI Main Loop (`kairos/main.py`)

**Responsibility**: Orchestrates the application lifecycle.

**Flow**:
1. Validates configuration via `Config.validate()`
2. Sets workspace from command line or defaults to cwd
3. Initializes `CLI`, `Agent`, and `SessionManager`
4. Wires agent callbacks to CLI display methods (streaming, tools, tokens)
5. Enters REPL loop:
   - Gets user input via `cli.get_user_input()`
   - Handles commands: `exit`, `clear`, `reset`, `/resume`
   - Sends input to `agent.run()` in a **background thread** (allows Ctrl+C)
   - Streaming tokens print as they arrive
   - Token status displayed after each turn
   - **Auto-saves** after each exchange (plus every 60 seconds and on window close)
6. Saves chat history on exit

**Streaming Flow**:
```
agent.run() → agent._stream_response() → OpenAI stream
       ↓
on_stream callback → cli.on_stream_token() → sys.stdout.write(token)
       ↓
Stream ends → cli.finish_stream()
       ↓
Tool calls executed → more streaming...
       ↓
Final response returned → cli.print_response()
       ↓
Token status printed
```

### 3. Configuration (`kairos/config.py`)

**Responsibility**: Lazy-load environment variables on first access.

Uses classmethods with `lru_cache`. Supports `Config.reload()` for testing.

| Variable | Required | Default |
|----------|----------|---------|
| `OPENAI_API_KEY` | Yes | — |
| `OPENAI_BASE_URL` | No | `https://api.openai.com/v1` |
| `OPENAI_MODEL` | No | `gpt-4o` |

### 4. CLI Module (`kairos/cli.py`)

**Responsibility**: Terminal UI with animated feedback and streaming display.

**Key Features**:
- **Animated Thinking**: `start_thinking()` / `stop_thinking()` cycle dots in background
- **Streaming Display**: `start_stream()`, `on_stream_token()`, `finish_stream()` print tokens live
- **Tool Summaries**: `print_tool_summary()` shows one-liners like `read file: /path`
- **Thinking Trace**: `print_thinking_trace()` renders reasoning in italic grey panel
- **Token Status**: `print_token_status()` shows session/context/turn token counts
- **Session Picker**: `pick_session()` arrow-key list of saved chats
- **Paste Support**: Ctrl+V pastes clipboard text directly into the input

### 5. Agent (`kairos/agent.py`)

**Responsibility**: Core AI reasoning loop with streaming OpenAI integration.

#### Streaming Architecture

The agent uses OpenAI's streaming API (`stream=True`). The `_stream_response()` method:
1. Opens a streaming connection to OpenAI
2. Yields content tokens via `on_stream` callback as they arrive
3. Assembles tool call deltas into complete tool calls
4. Returns `(full_content, assembled_tool_calls)`

Tool calls come as deltas that must be accumulated:
- First chunk: `tool_call.id` and `tool_call.function.name`
- Subsequent chunks: `tool_call.function.arguments` (partial JSON strings)
- Final: parse the assembled arguments

#### Token Counting

The `TokenCounter` (from `kairos/tokens.py`) tracks:
- **Session totals**: Cumulative input/output tokens across all turns
- **Context window**: Current conversation_history token count as % of max
- **Turn totals**: Input/output for the current API call

Updated after each `step()` via `on_token_update` callback.

#### 11 Tools

| # | Tool | Description |
|---|------|-------------|
| 1 | `read(path)` | Read file (absolute path) |
| 2 | `write(path, content)` | Write/create file |
| 3 | `edit(path, oldText, newText)` | Strict find-and-replace |
| 4 | `change_workspace(path)` | Change working directory |
| 5 | `search(pattern, path?, include?, max_results?)` | Regex file search |
| 6 | `git(command, **kwargs)` | Git: status, diff, log, commit, branch |
| 7 | `new_terminal(background)` | Create terminal |
| 8 | `execute_command(terminal_id, command, timeout?, is_background?)` | Run command |
| 9 | `read_logs(terminal_id, start_line, end_line?)` | Read terminal output |
| 10 | `close_terminal(terminal_id)` | Close terminal |
| 11 | `get_terminal_info(terminal_id)` | Get terminal status |

#### Callbacks
- `on_tool_call(name, args)` — Before each tool execution
- `on_thinking(text)` — Model's reasoning content
- `on_stream(token)` — Each token during streaming
- `on_token_update(counter)` — After each turn completes

#### Interrupt
- **Hard interrupt (Ctrl+C)**: `interrupt()` sets `_interrupt_event` → raises `InterruptedError` mid-step. Agent runs in background thread; main thread catches `KeyboardInterrupt`.
- **Graceful stop (Escape)**: `request_stop()` sets `_stop_requested` → checked in `run()` *between* steps (after tool calls finish, before next API call). Returns `"[Stopped — waiting for your input]"` so the user can continue the conversation.

### 6. Token Counter (`kairos/tokens.py`)

Uses `tiktoken` for accurate token counting.

```
Session: 12,345 in / 2,345 out  |  Context: 45.2%  |  Turn: 1,234 in / 567 out
```

**Key Methods**:
- `start_turn(messages)` — Count input tokens for conversation_history
- `add_output_tokens(text)` — Accumulate output tokens during streaming
- `finish_turn()` — Update session totals
- `format_status()` — Render the status line

### 7. Tools (`kairos/tools/`)

Each tool is a callable class in its own file.

#### `search.py` — SearchTool
Regex-based file content search (like ripgrep). Uses `os.walk` with:
- Skip list: `.git`, `__pycache__`, `node_modules`, `.venv`, etc.
- Binary file detection (null bytes)
- Glob-to-regex conversion for `include` parameter
- Returns file paths, line numbers, matching lines

#### `git.py` — GitTool
Dispatches to git subcommands via subprocess:
- `status` → `git status --porcelain`
- `diff` → `git diff [-- path]`
- `log` → `git log --oneline -n N`
- `commit` → `git add -A && git commit -m "message"`
- `branch` → `git branch --list`

#### `session.py` — SessionManager
Saves/loads chats to `chats/chats.json`. Each session keyed by timestamp, stores preview (first 20 chars of first user message) and full conversation history.

### 8. Terminal Manager (`kairos/terminal_manager.py`)

- **Background**: Persistent shell, lock-protected output buffer, reader thread
- **Blocking**: One-shot `subprocess.run()` with timeout
- Close tracks success/error separately

### 9. Compaction (in `kairos/agent.py`)

**Purpose**: Prevent context window overflow by summarizing old messages.

**How it works** (inspired by Pi's compaction):
1. `_find_compact_boundary()` walks backward through history, accumulating tokens, and finds a cut point that keeps ~20k tokens of recent context
2. `_generate_summary()` sends the old messages to the model with a structured summarization prompt (Goal, Constraints, Progress, Key Decisions, Next Steps, Critical Context)
3. History is rebuilt: `[system_prompt, compaction_summary, recent_messages]`
4. If an existing compaction summary exists, the new summary is an *update* that preserves prior information

**Triggers**:
- **Manual**: `/compact` command in the REPL
- **Auto**: Before each turn in `agent.run()`, if context usage exceeds 80% of the context window

**Key constants**:
- `COMPACT_RESERVE_TOKENS = 16384` — tokens reserved for summary generation
- `COMPACT_KEEP_RECENT = 20000` — tokens of recent context preserved
- `COMPACT_THRESHOLD_PCT = 80.0` — auto-compact threshold

**Callbacks**:
- `on_compact(status_msg)` — fired when compaction completes

## Data Flow

```
User Input
       ↓
main.py → agent.run()  [background thread]
       ↓
agent.step() → tokens.start_turn() → _stream_response()
       ↓                                        ↓
       ↓                              OpenAI stream → on_stream callback
       ↓                                        ↓
       ↓                              cli.on_stream_token() → stdout
       ↓
Tool calls? → Yes → on_tool_call → tool summary → execute → results
       ↓                                                        ↓
       ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ←
       ↓ No
Final response → cli.print_response()
       ↓
tokens.finish_turn() → on_token_update → cli.print_token_status()
```

## Design Principles

1. **Streaming First** — Tokens print as they arrive; no waiting for full response
2. **Absolute Paths** — No workspace containment
3. **One Tool Per File** — Easy to add new tools
4. **Interruptible** — Ctrl+C stops the agent cleanly via background thread
5. **Token Aware** — Session, context, and turn token counts displayed
6. **Search + Git** — Agent can discover code and inspect version control
7. **Lazy Config** — Environment loaded on first access, not import
8. **Minimal Dependencies** — `openai`, `python-dotenv`, `rich`, `prompt_toolkit`, `tiktoken`

## Adding New Tools

1. Create `kairos/tools/your_tool.py` with a callable class
2. Import in `kairos/tools/__init__.py`
3. Add schema to `Agent._get_tool_schema()`
4. Add dispatch to `Agent._execute_tool()`
5. Add summary case to `Agent._tool_summary()`
