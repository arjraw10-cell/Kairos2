"""
Browser tools for Kairos — all interaction tools use smart auto-snapshot
(page fingerprinting detects significant DOM changes automatically).
"""

from typing import Optional
from .base import ToolResult


class BrowserLaunchTool:
    """Launch a browser with optional persistent profile."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(
        self, profile=None, headless=True, proxy=None, humanize=False,
        chrome_profile=None, connect_cdp=None,
    ) -> ToolResult:
        try:
            if self.bm.is_open:
                return ToolResult(True, f"Browser already open (profile: {self.bm.profile_name or 'ephemeral'}). Close first.")
            result = self.bm.launch(
                profile=profile, headless=headless, proxy=proxy,
                humanize=humanize, chrome_profile=chrome_profile, connect_cdp=connect_cdp,
            )
            return ToolResult(True, result)
        except ImportError as e:
            return ToolResult(False, "", f"Browser dependencies not installed: {e}\nInstall: pip install playwright && playwright install chromium\nOr: pip install cloakbrowser")
        except Exception as e:
            return ToolResult(False, "", f"Launch failed: {e}")


class BrowserNavigateTool:
    """Navigate to a URL."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, url: str) -> ToolResult:
        try:
            return ToolResult(True, self.bm.navigate(url))
        except Exception as e:
            return ToolResult(False, "", f"Navigation failed: {e}")


class BrowserClickTool:
    """Click an element on the page."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, selector: str) -> ToolResult:
        try:
            return ToolResult(True, self.bm.click(selector))
        except Exception as e:
            return ToolResult(False, "", f"Click failed: {e}")


class BrowserClickIndexTool:
    """Click an element by its snapshot index number."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, index: int) -> ToolResult:
        try:
            return ToolResult(True, self.bm.click_by_index(index))
        except Exception as e:
            return ToolResult(False, "", f"Click index failed: {e}")


class BrowserTypeTool:
    """Type text into an input field."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, selector: str, text: str, press_enter=False) -> ToolResult:
        try:
            return ToolResult(True, self.bm.type_text(selector, text, press_enter=press_enter))
        except Exception as e:
            return ToolResult(False, "", f"Type failed: {e}")


class BrowserTypeIndexTool:
    """Type text into an element by its snapshot index number."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, index: int, text: str, press_enter=False) -> ToolResult:
        try:
            return ToolResult(True, self.bm.type_by_index(index, text, press_enter=press_enter))
        except Exception as e:
            return ToolResult(False, "", f"Type index failed: {e}")


class BrowserSelectTool:
    """Select an option from a dropdown."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, selector: str, value: str) -> ToolResult:
        try:
            return ToolResult(True, self.bm.select_option(selector, value))
        except Exception as e:
            return ToolResult(False, "", f"Select failed: {e}")


class BrowserSelectIndexTool:
    """Select an option from a dropdown by its snapshot index number."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, index: int, value: str) -> ToolResult:
        try:
            return ToolResult(True, self.bm.select_option_by_index(index, value))
        except Exception as e:
            return ToolResult(False, "", f"Select index failed: {e}")


class BrowserSnapshotTool:
    """Get a compact text representation of the page."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self) -> ToolResult:
        try:
            return ToolResult(True, self.bm.snapshot())
        except Exception as e:
            return ToolResult(False, "", f"Snapshot failed: {e}")


class BrowserScreenshotTool:
    """Capture a screenshot of the current page."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, full_page=False) -> ToolResult:
        try:
            png_bytes, message, data_url = self.bm.screenshot(full_page=full_page)
            if png_bytes is None:
                return ToolResult(False, "", message)
            return ToolResult(True, message, image_url=data_url)
        except Exception as e:
            return ToolResult(False, "", f"Screenshot failed: {e}")


class BrowserScrollTool:
    """Scroll the page up or down."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, direction="down", pages=1.0) -> ToolResult:
        try:
            return ToolResult(True, self.bm.scroll(direction=direction, pages=pages))
        except Exception as e:
            return ToolResult(False, "", f"Scroll failed: {e}")


class BrowserWaitTool:
    """Wait for a specified number of seconds."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, seconds=3) -> ToolResult:
        try:
            return ToolResult(True, self.bm.wait(seconds=seconds))
        except Exception as e:
            return ToolResult(False, "", f"Wait failed: {e}")


class BrowserSendKeysTool:
    """Send keyboard keys/shortcuts."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, keys: str) -> ToolResult:
        try:
            return ToolResult(True, self.bm.send_keys(keys))
        except Exception as e:
            return ToolResult(False, "", f"Send keys failed: {e}")


class BrowserSearchPageTool:
    """Search for text on the current page (like grep)."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, pattern: str, regex=False, case_sensitive=False, max_results=20) -> ToolResult:
        try:
            return ToolResult(True, self.bm.search_page(pattern, regex=regex, case_sensitive=case_sensitive, max_results=max_results))
        except Exception as e:
            return ToolResult(False, "", f"Search page failed: {e}")


class BrowserFindElementsTool:
    """Find elements on the page by CSS selector."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, selector: str, max_results=50) -> ToolResult:
        try:
            return ToolResult(True, self.bm.find_elements(selector, max_results=max_results))
        except Exception as e:
            return ToolResult(False, "", f"Find elements failed: {e}")


class BrowserTabListTool:
    """List all open browser tabs."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self) -> ToolResult:
        try:
            return ToolResult(True, self.bm.list_tabs())
        except Exception as e:
            return ToolResult(False, "", f"Tab list failed: {e}")


class BrowserTabSwitchTool:
    """Switch to a different tab."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, index=None, url_pattern=None) -> ToolResult:
        try:
            return ToolResult(True, self.bm.switch_tab(index=index, url_pattern=url_pattern))
        except Exception as e:
            return ToolResult(False, "", f"Tab switch failed: {e}")


class BrowserTabOpenTool:
    """Open a new browser tab."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, url=None) -> ToolResult:
        try:
            return ToolResult(True, self.bm.open_new_tab(url=url))
        except Exception as e:
            return ToolResult(False, "", f"Open tab failed: {e}")


class BrowserEvaluateTool:
    """Execute JavaScript in the page."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, expression: str) -> ToolResult:
        try:
            return ToolResult(True, self.bm.evaluate(expression))
        except Exception as e:
            return ToolResult(False, "", f"JavaScript error: {e}")


class BrowserGoBackTool:
    """Navigate back in browser history."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self) -> ToolResult:
        try:
            return ToolResult(True, self.bm.go_back())
        except Exception as e:
            return ToolResult(False, "", f"Go back failed: {e}")


class BrowserGoForwardTool:
    """Navigate forward in browser history."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self) -> ToolResult:
        try:
            return ToolResult(True, self.bm.go_forward())
        except Exception as e:
            return ToolResult(False, "", f"Go forward failed: {e}")


class BrowserReloadTool:
    """Reload the current page."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self) -> ToolResult:
        try:
            return ToolResult(True, self.bm.reload())
        except Exception as e:
            return ToolResult(False, "", f"Reload failed: {e}")


class BrowserCloseTool:
    """Close the browser."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self) -> ToolResult:
        try:
            return ToolResult(True, self.bm.close())
        except Exception as e:
            return ToolResult(False, "", f"Close failed: {e}")


class BrowserHoverTool:
    """Hover over an element to trigger hover states."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, selector: str) -> ToolResult:
        try:
            return ToolResult(True, self.bm.hover(selector))
        except Exception as e:
            return ToolResult(False, "", f"Hover failed: {e}")


class BrowserHoverIndexTool:
    """Hover over an element by its snapshot index number."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, index: int) -> ToolResult:
        try:
            return ToolResult(True, self.bm.hover_by_index(index))
        except Exception as e:
            return ToolResult(False, "", f"Hover index failed: {e}")


class BrowserDragTool:
    """Drag an element to another element."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, selector_from: str, selector_to: str) -> ToolResult:
        try:
            return ToolResult(True, self.bm.drag(selector_from, selector_to))
        except Exception as e:
            return ToolResult(False, "", f"Drag failed: {e}")


class BrowserDragXYTool:
    """Drag from one coordinate to another."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, x1: float, y1: float, x2: float, y2: float) -> ToolResult:
        try:
            return ToolResult(True, self.bm.drag_xy(x1, y1, x2, y2))
        except Exception as e:
            return ToolResult(False, "", f"Drag at ({x1}, {y1}) to ({x2}, {y2}) failed: {e}")


class BrowserWaitForTool:
    """Wait for an element or text to appear on the page."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, selector=None, text=None, timeout=10) -> ToolResult:
        try:
            return ToolResult(True, self.bm.wait_for(selector=selector, text=text, timeout=timeout))
        except Exception as e:
            return ToolResult(False, "", f"Wait for failed: {e}")


class BrowserClickXYTool:
    """Click at absolute viewport coordinates."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, x: float, y: float) -> ToolResult:
        try:
            return ToolResult(True, self.bm.click_xy(x, y))
        except Exception as e:
            return ToolResult(False, "", f"Click at ({x}, {y}) failed: {e}")


class BrowserSwitchFrameTool:
    """Switch into/out of iframes."""

    def __init__(self, browser_manager):
        self.bm = browser_manager

    def __call__(self, frame_selector=None) -> ToolResult:
        try:
            return ToolResult(True, self.bm.switch_frame(frame_selector))
        except Exception as e:
            return ToolResult(False, "", f"Frame switch failed: {e}")
