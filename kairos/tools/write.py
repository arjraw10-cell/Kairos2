from typing import Optional
from .base import ToolResult, resolve_path


class WriteTool:
    """Write or create a file. Relative paths resolve against ``workspace``."""

    def __init__(self, workspace: Optional[str] = None):
        self.workspace = workspace

    def __call__(self, path: str, content: str) -> ToolResult:
        try:
            resolved = resolve_path(path, self.workspace)

            resolved.parent.mkdir(parents=True, exist_ok=True)

            # Bytes, so the content is written exactly as given (no newline
            # translation on Windows).
            resolved.write_bytes(content.encode("utf-8"))
            return ToolResult(True, f"Successfully wrote to {path}")
        except Exception as e:
            return ToolResult(False, "", f"Write error: {str(e)}")
