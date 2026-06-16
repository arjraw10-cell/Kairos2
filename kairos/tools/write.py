from pathlib import Path
from typing import Optional
from .base import ToolResult


class WriteTool:
    """Write or create a file using absolute paths."""

    def __call__(self, path: str, content: str) -> ToolResult:
        try:
            resolved = Path(path).resolve()

            resolved.parent.mkdir(parents=True, exist_ok=True)

            resolved.write_text(content, encoding="utf-8")
            return ToolResult(True, f"Successfully wrote to {path}")
        except Exception as e:
            return ToolResult(False, "", f"Write error: {str(e)}")
