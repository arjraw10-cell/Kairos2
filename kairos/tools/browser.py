"""
Browser tools for Kairos. Each tool forwards to one BrowserManager method; every
action returns the resulting page state (diff snapshot on the same page, full
snapshot after navigation).
"""

from .base import ToolResult


class BrowserTool:
    """Wrap a BrowserManager method as a tool returning ToolResult."""

    def __init__(self, method, failure_label: str):
        self.method = method
        self.failure_label = failure_label

    def __call__(self, **kwargs) -> ToolResult:
        try:
            return ToolResult(True, self.method(**kwargs))
        except ImportError as e:
            return ToolResult(False, "", f"Browser dependencies not installed: {e}")
        except Exception as e:
            return ToolResult(False, "", f"{self.failure_label} failed: {e}")


class BrowserScreenshotTool:
    """Capture a screenshot; the image is returned for vision."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, full_page=False, ref=None) -> ToolResult:
        png_bytes, message, data_url = self.bm.screenshot(full_page=full_page, ref=ref)
        if png_bytes is None:
            return ToolResult(False, "", message)
        return ToolResult(True, message, image_url=data_url)
