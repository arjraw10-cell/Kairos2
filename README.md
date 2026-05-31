# Kairos

A minimal personal coding agent in Python, inspired by Pi.

## Features

- **4 Core Tools**: `read`, `write`, `edit` (strict find-and-replace), `change_workspace`
- **Terminal Management**: Background and blocking terminals with full lifecycle control
- **Strict Editing**: Edit tool fails loudly with line numbers if text not found or appears multiple times
- **OpenAI Compatible**: Uses OpenAI chat completions API (easily extensible to other providers)
- **Clean CLI**: Beautiful terminal UI with syntax highlighting using `rich` and `prompt_toolkit`

## Installation

```bash
# Create virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt
```

## Configuration

1. Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

2. Edit `.env` with your OpenAI credentials:
```
OPENAI_API_KEY=your_api_key_here
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o
```

## Usage

```bash
# Run with current directory as workspace
python main.py

# Run with specific workspace
python main.py /path/to/project
```

### Commands

- Type your coding request naturally
- `exit` or `quit` - Exit the agent
- `clear` - Clear the screen
- `reset` - Reset conversation history

## Tools

### File Tools

| Tool | Description |
|------|-------------|
| `read(path)` | Read file contents (relative to workspace) |
| `write(path, content)` | Write/create a file |
| `edit(path, oldText, newText)` | Strict find-and-replace (must match exactly once) |
| `change_workspace(path)` | Change current workspace |

### Terminal Tools

| Tool | Description |
|------|-------------|
| `new_terminal(background)` | Create a terminal (background or blocking) |
| `execute_command(terminal_id, command, timeout?, is_background?)` | Run command |
| `read_logs(terminal_id, start_line, end_line?)` | Read background terminal output by line numbers |
| `close_terminal(terminal_id)` | Close a terminal |
| `get_terminal_info(terminal_id)` | Get terminal status |

## Architecture

```
kairos/
├── config.py         # Environment configuration
├── terminal_manager.py  # Background/blocking terminal handling
├── tools.py          # Tool implementations
├── agent.py          # Core agent loop with OpenAI integration
├── cli.py            # Terminal UI with rich/prompt_toolkit
└── main.py           # Entry point
```

## Design Principles

1. **Strict Tool Calling**: Tools fail loudly with detailed error messages (including line numbers) so the agent can self-correct
2. **Workspace Isolation**: All file operations are relative to a configurable workspace
3. **Terminal Flexibility**: Background terminals for long-running processes, blocking for quick commands
4. **Minimal Dependencies**: Only essential libraries
5. **Provider Agnostic**: Currently OpenAI, but designed for easy extension

## License

MIT