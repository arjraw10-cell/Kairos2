"""
Browser tools for Kairos — provides browser_launch, browser_navigate, browser_click,
browser_type, browser_select, browser_snapshot, browser_screenshot, browser_tab_list,
browser_tab_switch, browser_tab_open, browser_evaluate, browser_close.

Each tool is a callable class that returns a ToolResult.
"""

from typing import Optional
from .base import ToolResult


class BrowserLaunchTool:
    """Launch a browser with optional persistent profile."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(
        self,
        profile: Optional[str] = None,
        headless: bool = True,
        proxy: Optional[str] = None,
        humanize: bool = False,
        chrome_profile: Optional[str] = None,
        connect_cdp: Optional[str] = None,
    ) -> ToolResult:
        try:
            if self.bm.is_open:
                return ToolResult(
                    True,
                    f"Browser already open (profile: {self.bm.profile_name or 'ephemeral'}). "
                    f"Close it first with browser_close, or use the open browser directly.",
                )
            result = self.bm.launch(
                profile=profile,
                headless=headless,
                proxy=proxy,
                humanize=humanize,
                chrome_profile=chrome_profile,
                connect_cdp=connect_cdp,
            )
            return ToolResult(True, result)
        except ImportError as e:
            return ToolResult(
                False,
                "",
                f"Browser dependencies not installed: {e}\n"
                "Install with: pip install playwright && playwright install chromium\n"
                "Or for stealth: pip install cloakbrowser",
            )
        except Exception as e:
            return ToolResult(False, "", f"Launch failed: {e}")


class BrowserNavigateTool:
    """Navigate to a URL."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, url: str) -> ToolResult:
        try:
            result = self.bm.navigate(url)
            return ToolResult(True, result)
        except Exception as e:
            return ToolResult(False, "", f"Navigation failed: {e}")


class BrowserClickTool:
    """Click an element on the page."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, selector: str) -> ToolResult:
        try:
            result = self.bm.click(selector)
            return ToolResult(True, result)
        except Exception as e:
            return ToolResult(False, "", f"Click failed: {e}")


class BrowserTypeTool:
    """Type text into an input field."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(
        self, selector: str, text: str, press_enter: bool = False
    ) -> ToolResult:
        try:
            result = self.bm.type_text(selector, text, press_enter=press_enter)
            return ToolResult(True, result)
        except Exception as e:
            return ToolResult(False, "", f"Type failed: {e}")


class BrowserSelectTool:
    """Select an option from a dropdown."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, selector: str, value: str) -> ToolResult:
        try:
            result = self.bm.select_option(selector, value)
            return ToolResult(True, result)
        except Exception as e:
            return ToolResult(False, "", f"Select failed: {e}")


class BrowserSnapshotTool:
    """Get a compact text representation of the page (accessibility tree / DOM summary)."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self) -> ToolResult:
        try:
            result = self.bm.snapshot()
            return ToolResult(True, result)
        except Exception as e:
            return ToolResult(False, "", f"Snapshot failed: {e}")


class BrowserScreenshotTool:
    """Capture a screenshot of the current page. Saved to disk, not sent via API."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, full_page: bool = False) -> ToolResult:
        try:
            png_bytes, message = self.bm.screenshot(full_page=full_page)
            if png_bytes is None:
                return ToolResult(False, "", message)

            # Return as text-only — do NOT send base64 image data through the
            # API.  OpenRouter and many providers return 400 for inline base64
            # data URLs in tool messages.  The file path is in `message`.
            return ToolResult(True, message)
        except Exception as e:
            return ToolResult(False, "", f"Screenshot failed: {e}")


class BrowserTabListTool:
    """List all open browser tabs."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self) -> ToolResult:
        try:
            result = self.bm.list_tabs()
            return ToolResult(True, result)
        except Exception as e:
            return ToolResult(False, "", f"Tab list failed: {e}")


class BrowserTabSwitchTool:
    """Switch to a different tab by index or URL pattern."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(
        self,
        index: Optional[int] = None,
        url_pattern: Optional[str] = None,
    ) -> ToolResult:
        try:
            result = self.bm.switch_tab(index=index, url_pattern=url_pattern)
            return ToolResult(True, result)
        except Exception as e:
            return ToolResult(False, "", f"Tab switch failed: {e}")


class BrowserTabOpenTool:
    """Open a new browser tab, optionally with a URL."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, url: Optional[str] = None) -> ToolResult:
        try:
            result = self.bm.open_new_tab(url=url)
            return ToolResult(True, result)
        except Exception as e:
            return ToolResult(False, "", f"Open tab failed: {e}")


class BrowserEvaluateTool:
    """Execute JavaScript in the page and return the result."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, expression: str) -> ToolResult:
        try:
            result = self.bm.evaluate(expression)
            return ToolResult(True, result)
        except Exception as e:
            return ToolResult(False, "", f"JavaScript error: {e}")


class BrowserCloseTool:
    """Close the browser and clean up."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self) -> ToolResult:
        try:
            result = self.bm.close()
            return ToolResult(True, result)
        except Exception as e:
            return ToolResult(False, "", f"Close failed: {e}")
