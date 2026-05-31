import os
from pathlib import Path
from typing import Tuple, Optional, List
from .terminal_manager import TerminalManager

class ToolResult:
    def __init__(self, success: bool, output: str, error: Optional[str] = None):
        self.success = success
        self.output = output
        self.error = error

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error
        }

class Tools:
    def __init__(self, workspace: str, terminal_manager: TerminalManager):
        self.workspace = Path(workspace).resolve()
        self.terminal_manager = terminal_manager

    def _resolve_path(self, path: str) -> Path:
        """Resolve path relative to workspace"""
        p = Path(path)
        if p.is_absolute():
            # Still enforce workspace constraint
            try:
                p.relative_to(self.workspace)
            except ValueError:
                raise ValueError(f"Path {path} is outside workspace {self.workspace}")
            return p
        return (self.workspace / p).resolve()

    def read(self, path: str) -> ToolResult:
        """Read file contents"""
        try:
            resolved = self._resolve_path(path)
            if not resolved.exists():
                return ToolResult(False, "", f"File not found: {path}")
            if not resolved.is_file():
                return ToolResult(False, "", f"Not a file: {path}")
            
            # Limit file size
            max_size = 100 * 1024  # 100KB
            if resolved.stat().st_size > max_size:
                return ToolResult(False, "", f"File too large (>100KB): {path}")
            
            content = resolved.read_text(encoding='utf-8', errors='replace')
            return ToolResult(True, content)
        except Exception as e:
            return ToolResult(False, "", f"Read error: {str(e)}")

    def write(self, path: str, content: str) -> ToolResult:
        """Write file contents (creates or overwrites)"""
        try:
            resolved = self._resolve_path(path)
            
            # Ensure path is within workspace
            try:
                resolved.relative_to(self.workspace)
            except ValueError:
                return ToolResult(False, "", f"Path outside workspace: {path}")
            
            # Create parent directories
            resolved.parent.mkdir(parents=True, exist_ok=True)
            
            resolved.write_text(content, encoding='utf-8')
            return ToolResult(True, f"Successfully wrote to {path}")
        except Exception as e:
            return ToolResult(False, "", f"Write error: {str(e)}")

    def edit(self, path: str, oldText: str, newText: str) -> ToolResult:
        """
        Strict find-and-replace edit.
        Requires exactly one occurrence of oldText.
        Fails loudly with line number info if 0 or >1 matches.
        """
        try:
            resolved = self._resolve_path(path)
            
            if not resolved.exists():
                return ToolResult(False, "", f"File not found: {path}")
            
            content = resolved.read_text(encoding='utf-8')
            
            # Find all occurrences
            matches = []
            start = 0
            while True:
                idx = content.find(oldText, start)
                if idx == -1:
                    break
                matches.append(idx)
                start = idx + 1
            
            if len(matches) == 0:
                # Provide line number context for debugging
                lines = content.split('\n')
                # Try to find similar text
                similar_lines = []
                for i, line in enumerate(lines, 1):
                    if oldText[:20] in line if len(oldText) > 20 else oldText in line:
                        similar_lines.append(i)
                
                error_msg = f"String not found in file. File has {len(lines)} lines."
                if similar_lines:
                    error_msg += f" Similar text found on line(s): {similar_lines[:5]}"
                return ToolResult(False, "", error_msg)
            
            if len(matches) > 1:
                # Calculate line numbers for each match
                lines = content[:matches[0]].count('\n') + 1
                line_positions = []
                for match_idx in matches:
                    line_num = content[:match_idx].count('\n') + 1
                    line_positions.append(line_num)
                
                return ToolResult(False, "", 
                    f"Found {len(matches)} occurrences of the string (must be exactly 1). "
                    f"Found at line(s): {line_positions}")
            
            # Exactly one match - perform replacement
            new_content = content[:matches[0]] + newText + content[matches[0] + len(oldText):]
            resolved.write_text(new_content, encoding='utf-8')
            
            # Calculate line number of the edit
            line_num = content[:matches[0]].count('\n') + 1
            return ToolResult(True, f"Successfully edited {path} at line {line_num}")
        
        except Exception as e:
            return ToolResult(False, "", f"Edit error: {str(e)}")

    def change_workspace(self, new_path: str) -> ToolResult:
        """Change the agent's workspace directory"""
        try:
            # Resolve the new path
            if os.path.isabs(new_path):
                new_workspace = Path(new_path).resolve()
            else:
                new_workspace = (Path(self.workspace) / new_path).resolve()
            
            if not new_workspace.exists():
                return ToolResult(False, "", f"Directory not found: {new_path}")
            
            if not new_workspace.is_dir():
                return ToolResult(False, "", f"Not a directory: {new_path}")
            
            self.workspace = new_workspace
            return ToolResult(True, f"Workspace changed to {new_workspace}")
        except Exception as e:
            return ToolResult(False, "", f"Change workspace error: {str(e)}")

    # Terminal tools
    def new_terminal(self, background: bool) -> ToolResult:
        """Create a new terminal"""
        try:
            terminal_id = self.terminal_manager.create_terminal(background)
            return ToolResult(True, f"Created {'background' if background else 'blocking'} terminal with ID {terminal_id}")
        except Exception as e:
            return ToolResult(False, "", f"Failed to create terminal: {str(e)}")

    def execute_command(self, terminal_id: int, command: str, timeout: Optional[int] = None, is_background: Optional[bool] = None) -> ToolResult:
        """Execute command in a terminal"""
        try:
            success, output = self.terminal_manager.execute_command(
                terminal_id, command, timeout, is_background
            )
            return ToolResult(success, output)
        except Exception as e:
            return ToolResult(False, "", f"Command execution error: {str(e)}")

    def read_logs(self, terminal_id: int, start_line: int, end_line: Optional[int] = None) -> ToolResult:
        """Read logs from a background terminal by line numbers"""
        try:
            success, output = self.terminal_manager.read_logs(terminal_id, start_line, end_line)
            return ToolResult(success, output)
        except Exception as e:
            return ToolResult(False, "", f"Read logs error: {str(e)}")

    def close_terminal(self, terminal_id: int) -> ToolResult:
        """Close a terminal"""
        try:
            success, output = self.terminal_manager.close_terminal(terminal_id)
            return ToolResult(success, output)
        except Exception as e:
            return ToolResult(False, "", f"Close terminal error: {str(e)}")

    def get_terminal_info(self, terminal_id: int) -> ToolResult:
        """Get info about a terminal"""
        try:
            info = self.terminal_manager.get_terminal_info(terminal_id)
            if info:
                return ToolResult(True, str(info))
            return ToolResult(False, "", f"Terminal {terminal_id} not found")
        except Exception as e:
            return ToolResult(False, "", f"Get terminal info error: {str(e)}")