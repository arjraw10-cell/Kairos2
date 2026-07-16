from typing import Optional
from .base import ToolResult


class NewTerminalTool:
    """Create a new terminal."""

    def __init__(self, terminal_manager):
        self.terminal_manager = terminal_manager

    def __call__(self, background: bool) -> ToolResult:
        try:
            terminal_id = self.terminal_manager.create_terminal(background)
            kind = "background" if background else "blocking"
            return ToolResult(True, f"Created {kind} terminal with ID {terminal_id}")
        except Exception as e:
            return ToolResult(False, "", f"Failed to create terminal: {str(e)}")


class ExecuteCommandTool:
    """Execute a command in a terminal."""

    def __init__(self, terminal_manager):
        self.terminal_manager = terminal_manager

    def __call__(
        self,
        terminal_id: int,
        command: str,
        timeout: Optional[float] = None,
        is_background: Optional[bool] = None,
    ) -> ToolResult:
        try:
            success, output = self.terminal_manager.execute_command(
                terminal_id, command, timeout, is_background
            )
            return ToolResult(success, output)
        except Exception as e:
            return ToolResult(False, "", f"Command execution error: {str(e)}")


class ReadLogsTool:
    """Read output from a background terminal by line numbers."""

    def __init__(self, terminal_manager):
        self.terminal_manager = terminal_manager

    def __call__(
        self,
        terminal_id: int,
        start_line: int,
        end_line: Optional[int] = None,
    ) -> ToolResult:
        try:
            success, output = self.terminal_manager.read_logs(
                terminal_id, start_line, end_line
            )
            return ToolResult(success, output)
        except Exception as e:
            return ToolResult(False, "", f"Read logs error: {str(e)}")


class CloseTerminalTool:
    """Close a terminal."""

    def __init__(self, terminal_manager):
        self.terminal_manager = terminal_manager

    def __call__(self, terminal_id: int) -> ToolResult:
        try:
            success, output = self.terminal_manager.close_terminal(terminal_id)
            return ToolResult(success, output)
        except Exception as e:
            return ToolResult(False, "", f"Close terminal error: {str(e)}")


class GetTerminalInfoTool:
    """Get info about a terminal."""

    def __init__(self, terminal_manager):
        self.terminal_manager = terminal_manager

    def __call__(self, terminal_id: int) -> ToolResult:
        try:
            info = self.terminal_manager.get_terminal_info(terminal_id)
            if info:
                return ToolResult(True, str(info))
            return ToolResult(False, "", f"Terminal {terminal_id} not found")
        except Exception as e:
            return ToolResult(False, "", f"Get terminal info error: {str(e)}")
