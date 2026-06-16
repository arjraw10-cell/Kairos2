from typing import Optional


class ToolResult:
    def __init__(
        self,
        success: bool,
        output: str,
        error: Optional[str] = None,
        workspace_changed: Optional[str] = None,
        image_url: Optional[str] = None,
    ):
        self.success = success
        self.output = output
        self.error = error
        self.workspace_changed = workspace_changed
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
