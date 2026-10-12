from pathlib import Path
from typing import Optional


def resolve_path(path: str, workspace: Optional[str] = None) -> Path:
    """Resolve ``path``; a relative path is taken relative to ``workspace``.

    Without a workspace, relative paths resolve against the process CWD.
    """
    candidate = Path(path)
    if workspace is not None and not candidate.is_absolute():
        candidate = Path(workspace) / candidate
    return candidate.resolve()


class ToolResult:
    def __init__(
        self,
        success: bool,
        output: str,
        error: Optional[str] = None,
        image_url: Optional[str] = None,
    ):
        self.success = success
        self.output = output
        self.error = error
        self.image_url = image_url

    def to_dict(self) -> dict:
        d = {
            "success": self.success,
            "output": self.output,
            "error": self.error,
        }
        if self.image_url:
            d["image_url"] = self.image_url
        return d
