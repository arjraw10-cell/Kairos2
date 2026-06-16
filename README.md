# Kairos

A minimal personal coding agent in Python.

## Features

- **9 Core Tools**: `read`, `write`, `edit` (strict find-and-replace), `change_workspace`, plus 5 terminal tools
- **Absolute Paths**: All file operations use absolute paths — no workspace restrictions
- **Terminal Management**: Background and blocking terminals with full lifecycle control
- **Chat Persistence**: All sessions saved to `chats/chats.json` with timestamps
- **`/resume`**: Load previous chats via arrow-key picker
- **Animated Thinking**: "Thinking..." indicator with cycling dots
- **Thinking Trace**: Model's reasoning displayed in italics with grey shading
- **Tool Summaries**: One-line display when tools are called (e.g., `read file: /path`)
- **Interrupt**: Press Ctrl+C during a response to stop and give feedback
- **OpenAI Compatible**: Uses OpenAI chat completions API
- **Clean CLI**: Beautiful terminal UI with `rich` and `prompt_toolkit`

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate    # Windows
# source .venv/bin/activate  # Linux/Mac

pip install -r requirements.txt
```

## Configuration

1. Copy `.env.example` to `.env`
2. Edit `.env` with your credentials:
```
OPENAI_API_KEY=your_api_key_here
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o
```

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
| `/resume` | Load a saved chat (arrow keys + enter) |
| `clear` | Clear the screen |
| `reset` | Save and reset conversation history |
| `exit` / `quit` / `q` | Save and exit |
| `Ctrl+C` | Interrupt a running response |

## Tools

### File Tools

| Tool | Description |
|------|-------------|
| `read(path)` | Read file contents (absolute path) |
| `write(path, content)` | Write/create a file (absolute path) |
| `edit(path, oldText, newText)` | Strict find-and-replace (must match exactly once) |
| `change_workspace(path)` | Change working directory |

### Terminal Tools

| Tool | Description |
|------|-------------|
| `new_terminal(background)` | Create a terminal (persistent or one-shot) |
| `execute_command(terminal_id, command, timeout?, is_background?)` | Run command |
| `read_logs(terminal_id, start_line, end_line?)` | Read terminal output |
| `close_terminal(terminal_id)` | Close a terminal |
| `get_terminal_info(terminal_id)` | Get terminal status |

## Architecture

```
kairos/
├── main.py             # Entry point, REPL loop
├── config.py           # Lazy config loading
├── agent.py            # Core agent loop with OpenAI
├── cli.py              # Terminal UI (animated thinking, trace, picker)
├── terminal_manager.py # Terminal lifecycle
└── tools/
    ├── base.py         # ToolResult
    ├── read.py         # Read file
    ├── write.py        # Write file
    ├── edit.py         # Strict edit
    ├── workspace.py    # Change workspace
    ├── terminal.py     # Terminal tools
    └── session.py      # Chat persistence
```

## Design Principles

1. **One Tool Per File** — easy to add new tools
2. **Absolute Paths** — no workspace containment, just use full paths
3. **Interruptible** — Ctrl+C stops the agent cleanly
4. **Persistent Chats** — sessions saved automatically, even on window close
5. **Minimal Dependencies** — only `openai`, `python-dotenv`, `rich`, `prompt_toolkit`

## License

MIT
