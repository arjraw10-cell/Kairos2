import json
from typing import List, Dict, Any, Optional
from openai import OpenAI
from .config import Config
from .tools import Tools
from .terminal_manager import TerminalManager

class Agent:
    def __init__(self, workspace: str):
        self.client = OpenAI(
            api_key=Config.OPENAI_API_KEY,
            base_url=Config.OPENAI_BASE_URL
        )
        self.model = Config.OPENAI_MODEL
        self.terminal_manager = TerminalManager()
        self.tools = Tools(workspace, self.terminal_manager)
        self.conversation_history: List[Dict[str, Any]] = []
        
        self._setup_system_prompt()

    def _setup_system_prompt(self):
        """Initialize system prompt with tool definitions and workspace info"""
        self.system_prompt = f"""You are Kairos, a minimal but powerful coding agent. 
You operate in the workspace: {self.tools.workspace}

All file paths in your tool calls must be RELATIVE to this workspace.

AVAILABLE TOOLS:
1. read(path: str) - Read a file's contents
2. write(path: str, content: str) - Write/create a file
3. edit(path: str, oldText: str, newText: str) - Strict find-and-replace. MUST match exactly ONE occurrence. Will fail loudly if 0 or >1 matches.
4. change_workspace(path: str) - Change current workspace directory

TERMINAL TOOLS:
5. new_terminal(background: bool) - Create a terminal. Background terminals run persistently, blocking terminals execute once.
6. execute_command(terminal_id: int, command: str, timeout: int|null, is_background: bool|null) - Run command. 
   - For background terminals: timeout not needed, is_background should be True
   - For blocking terminals: timeout REQUIRED, is_background should be False
7. read_logs(terminal_id: int, start_line: int, end_line: int|null) - Read output from background terminal by line numbers (1-indexed)
8. close_terminal(terminal_id: int) - Close a terminal
9. get_terminal_info(terminal_id: int) - Get terminal status

RULES:
- Be precise with edit operations - ensure oldText is unique in the file
- For blocking commands, ALWAYS specify a timeout to prevent hanging
- Background terminals accumulate output; use read_logs with specific line ranges
- If a tool fails, read the error carefully and adjust your approach
- Think step-by-step before making changes

Respond with tool calls when needed, or provide your final answer."""

        self.conversation_history = [{
            "role": "system",
            "content": self.system_prompt
        }]

    def _get_tool_schema(self) -> List[Dict[str, Any]]:
        """Define tool schemas for OpenAI function calling"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file's contents",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Path relative to workspace"}
                        },
                        "required": ["path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "write",
                    "description": "Write or create a file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Path relative to workspace"},
                            "content": {"type": "string", "description": "File content"}
                        },
                        "required": ["path", "content"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "edit",
                    "description": "Strict find-and-replace edit. Must match exactly one occurrence.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Path relative to workspace"},
                            "oldText": {"type": "string", "description": "Exact text to find (must appear exactly once)"},
                            "newText": {"type": "string", "description": "Replacement text"}
                        },
                        "required": ["path", "oldText", "newText"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "change_workspace",
                    "description": "Change the current workspace directory",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "New workspace path (absolute or relative to current)"}
                        },
                        "required": ["path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "new_terminal",
                    "description": "Create a new terminal (background or blocking)",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "background": {"type": "boolean", "description": "True for background terminal, False for blocking"}
                        },
                        "required": ["background"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "execute_command",
                    "description": "Execute a command in a terminal",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {"type": "integer", "description": "Terminal ID to use"},
                            "command": {"type": "string", "description": "Command to execute"},
                            "timeout": {"type": "integer", "description": "Timeout in seconds (required for blocking terminals)"},
                            "is_background": {"type": "boolean", "description": "Must match terminal type (True for background, False for blocking)"}
                        },
                        "required": ["terminal_id", "command", "is_background"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_logs",
                    "description": "Read output from a background terminal by line numbers",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {"type": "integer", "description": "Background terminal ID"},
                            "start_line": {"type": "integer", "description": "Starting line number (1-indexed)"},
                            "end_line": {"type": "integer", "description": "Ending line number (optional, defaults to end)"}
                        },
                        "required": ["terminal_id", "start_line"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "close_terminal",
                    "description": "Close a terminal",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {"type": "integer", "description": "Terminal ID to close"}
                        },
                        "required": ["terminal_id"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_terminal_info",
                    "description": "Get information about a terminal",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "terminal_id": {"type": "integer", "description": "Terminal ID"}
                        },
                        "required": ["terminal_id"]
                    }
                }
            }
        ]

    def _execute_tool(self, name: str, args: Dict[str, Any]) -> str:
        """Execute a tool and return the result as a string"""
        tool_map = {
            "read": lambda a: self.tools.read(a["path"]).to_dict(),
            "write": lambda a: self.tools.write(a["path"], a["content"]).to_dict(),
            "edit": lambda a: self.tools.edit(a["path"], a["oldText"], a["newText"]).to_dict(),
            "change_workspace": lambda a: self.tools.change_workspace(a["path"]).to_dict(),
            "new_terminal": lambda a: self.tools.new_terminal(a["background"]).to_dict(),
            "execute_command": lambda a: self.tools.execute_command(
                a["terminal_id"], 
                a["command"], 
                a.get("timeout"),
                a.get("is_background")
            ).to_dict(),
            "read_logs": lambda a: self.tools.read_logs(
                a["terminal_id"], 
                a["start_line"], 
                a.get("end_line")
            ).to_dict(),
            "close_terminal": lambda a: self.tools.close_terminal(a["terminal_id"]).to_dict(),
            "get_terminal_info": lambda a: self.tools.get_terminal_info(a["terminal_id"]).to_dict()
        }
        
        if name not in tool_map:
            return json.dumps({"success": False, "output": "", "error": f"Unknown tool: {name}"})
        
        try:
            result = tool_map[name](args)
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"success": False, "output": "", "error": str(e)})

    def step(self, user_message: Optional[str] = None) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """
        Run one step of the agent loop.
        Returns: (response_text, tool_calls_made)
        - response_text: Final response if no tool calls, None if continuing
        - tool_calls_made: List of tool calls made in this step
        """
        if user_message:
            self.conversation_history.append({
                "role": "user",
                "content": user_message
            })

        response = self.client.chat.completions.create(
            model=self.model,
            messages=self.conversation_history,
            tools=self._get_tool_schema(),
            tool_choice="auto"
        )

        assistant_message = response.choices[0].message
        self.conversation_history.append(assistant_message)

        tool_calls = assistant_message.tool_calls or []
        
        if not tool_calls:
            # No tool calls - return final response
            return assistant_message.content, []
        
        # Execute tool calls
        tool_results = []
        for tc in tool_calls:
            func_name = tc.function.name
            func_args = json.loads(tc.function.arguments)
            
            result_json = self._execute_tool(func_name, func_args)
            
            tool_results.append({
                "tool_call_id": tc.id,
                "role": "tool",
                "name": func_name,
                "content": result_json
            })
        
        # Add tool results to conversation
        self.conversation_history.extend(tool_results)
        
        return None, tool_calls

    def run(self, user_message: str, max_iterations: int = 50) -> str:
        """
        Run the agent loop until completion or max iterations.
        Returns the final response.
        """
        current_message = user_message
        
        for i in range(max_iterations):
            response, tool_calls = self.step(current_message)
            current_message = None  # Only send user message on first iteration
            
            if response:
                # Agent provided a final response
                return response
            
            if not tool_calls:
                # Shouldn't happen, but safety check
                return "Agent stopped without response."
        
        return f"Reached maximum iterations ({max_iterations}). Conversation may be incomplete."

    def reset(self):
        """Reset conversation history while keeping workspace and terminals"""
        self._setup_system_prompt()