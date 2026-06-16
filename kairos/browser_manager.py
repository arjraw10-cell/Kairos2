"""
BrowserManager — manages Playwright/CloakBrowser lifecycle, profiles, and pages.

Runs ALL Playwright operations in a dedicated worker thread with NO asyncio
event loop, avoiding conflicts with the main thread's asyncio (from
rich/prompt_toolkit).

Uses CloakBrowser's stealth Chromium binary and fingerprint patches by default.
Falls back to standard Playwright Chromium if CloakBrowser is not installed.

Supports:
  - CloakBrowser stealth Chromium (source-level fingerprint patches)
  - Named persistent profiles (cookies, localStorage, cache survive across sessions)
  - Ephemeral sessions (incognito-like)
  - Human-like mouse/keyboard/scroll behavior (via CloakBrowser humanize)
  - Multi-tab management
  - Screenshot capture (returns file path)
  - Compact page snapshots (accessibility tree / DOM summary)
  - CDP connection to running Chrome
  - Chrome profile copying
"""

import base64
import json
import os
import tempfile
import threading
import queue
import time as _time
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Callable

# --- CloakBrowser stealth integration ---
_CLOAKBROWSER_AVAILABLE = False
_cloakbinary_path: Optional[str] = None
_cloak_ignore_args: Optional[List[str]] = None

try:
    import cloakbrowser
    from cloakbrowser import ensure_binary, build_args
    from cloakbrowser.browser import IGNORE_DEFAULT_ARGS as _CLOAK_IGNORE_DEFAULT_ARGS

    _cloakbinary_path = ensure_binary()
    _cloak_ignore_args = _CLOAK_IGNORE_DEFAULT_ARGS
    _CLOAKBROWSER_AVAILABLE = True
except ImportError:
    pass
except Exception:
    pass  # binary download failure etc — fall back gracefully


class _WorkerThread:
    """A dedicated thread that hosts a persistent Playwright instance.

    The thread keeps sync_playwright() alive for its entire lifetime, so all
    dispatched callables share the same greenlet context. This avoids the
    "Cannot switch to a different thread" greenlet errors that occur when
    sync_playwright() is started and stopped across separate dispatch calls.

    Lifecycle:
      1. start() — spawns the thread, creates sync_playwright, enters the loop
      2. dispatch(fn, timeout) — queues fn, blocks until result or error
      3. stop() — sends sentinel, waits for thread to exit
    """

    def __init__(self):
        self._task_queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._pw = None  # Playwright instance (only valid inside the worker thread)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._started.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="playwright-worker")
        self._thread.start()
        self._started.wait(timeout=10)

    def _run(self):
        """Worker loop: init Playwright, then process tasks."""
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()

        while True:
            task = self._task_queue.get()
            if task is None:
                break
            fn, result_holder = task
            try:
                result_holder["result"] = fn()
            except Exception as e:
                result_holder["error"] = e

        # Cleanup
        try:
            self._pw.stop()
        except Exception:
            pass
        self._pw = None

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def dispatch(self, fn, timeout=30):
        """Submit a callable and block until it completes. Returns the result."""
        if not self._thread or not self._thread.is_alive():
            raise RuntimeError("Worker thread is not running")

        result_holder: Dict[str, Any] = {}
        self._task_queue.put((fn, result_holder))

        deadline = _time.time() + timeout + 5
        while _time.time() < deadline:
            if "result" in result_holder or "error" in result_holder:
                break
            _time.sleep(0.1)

        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder.get("result")

    def stop(self):
        """Shut down the worker thread."""
        if self._thread and self._thread.is_alive():
            self._task_queue.put(None)  # Sentinel
            self._thread.join(timeout=10)
        self._thread = None
        self._pw = None


class BrowserManager:
    """Manages a single Playwright browser instance in a dedicated thread."""

    DEFAULT_PROFILES_DIR = Path("~/.kairos/profiles").expanduser()

    def __init__(self):
        self._browser = None      # Browser or BrowserContext (persistent)
        self._context = None      # BrowserContext (for non-persistent)
        self._pages: List = []    # All open pages (tabs)
        self._current_idx = 0     # Index into _pages for current active tab
        self._profile_name: Optional[str] = None
        self._headless: bool = True
        self._lock = threading.Lock()
        self._worker = _WorkerThread()

    # ------------------------------------------------------------------
    #  Properties
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._browser is not None or self._context is not None

    @property
    def current_page(self):
        if not self._pages:
            return None
        if self._current_idx >= len(self._pages):
            self._current_idx = len(self._pages) - 1
        return self._pages[self._current_idx]

    @property
    def current_page_index(self) -> int:
        return self._current_idx

    @property
    def pages(self) -> List:
        return list(self._pages)

    @property
    def context(self):
        return self._context

    @property
    def profile_name(self) -> Optional[str]:
        return self._profile_name

    # ------------------------------------------------------------------
    #  Launch
    # ------------------------------------------------------------------

    def launch(
        self,
        profile: Optional[str] = None,
        headless: bool = True,
        proxy: Optional[str] = None,
        humanize: bool = False,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        extra_args: Optional[List[str]] = None,
        chrome_profile: Optional[str] = None,
        connect_cdp: Optional[str] = None,
    ) -> str:
        """Launch a browser. Returns a status message."""
        with self._lock:
            if self._browser or self._context:
                return "Browser is already running. Close it first with browser_close."

        self._headless = headless

        # ---- CDP: Connect to running Chrome ----
        if connect_cdp:
            return self._launch_cdp(connect_cdp)

        # ---- Chrome profile copy ----
        if chrome_profile:
            return self._launch_chrome_profile(
                chrome_profile, headless, proxy, humanize,
                viewport_width, viewport_height, extra_args,
            )

        # ---- Standard launch via worker thread ----
        self._worker.start()
        self._profile_name = profile
        launch_args = list(extra_args or [])

        result = self._worker.dispatch(
            lambda: self._do_launch(
                profile, headless, proxy, humanize,
                viewport_width, viewport_height, launch_args,
            ),
            timeout=60,
        )
        return result

    def _do_launch(
        self, profile, headless, proxy, humanize,
        viewport_width, viewport_height, launch_args,
    ):
        """Launch browser via Playwright. Runs in worker thread.

        Uses CloakBrowser's stealth Chromium binary and fingerprint patches
        when available, falling back to standard Playwright Chromium.
        Uses the persistent sync_playwright instance from self._worker._pw.
        """
        pw = self._worker._pw

        # --- CloakBrowser stealth args ---
        executable_path = None
        ignore_default_args = []
        chrome_args = list(launch_args)

        if _CLOAKBROWSER_AVAILABLE and _cloakbinary_path:
            executable_path = _cloakbinary_path
            # Get CloakBrowser's stealth args (--fingerprint-*, etc.)
            stealth_args = build_args(
                stealth_args=True,
                extra_args=launch_args,
                headless=headless,
            )
            chrome_args = stealth_args
            if _cloak_ignore_args:
                ignore_default_args = list(_cloak_ignore_args)

        launch_kwargs = {}
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        if ignore_default_args:
            launch_kwargs["ignore_default_args"] = ignore_default_args
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}

        if profile:
            profile_dir = str(self.DEFAULT_PROFILES_DIR / profile)
            context_kwargs = {
                "headless": headless,
                "viewport": {"width": viewport_width, "height": viewport_height},
                "args": chrome_args,
                **launch_kwargs,
            }
            self._context = pw.chromium.launch_persistent_context(
                profile_dir,
                **context_kwargs,
            )
            self._browser = None
            self._pages = list(self._context.pages)
            if not self._pages:
                self._pages.append(self._context.new_page())
            self._current_idx = 0
            engine = "CloakBrowser" if _CLOAKBROWSER_AVAILABLE else "Playwright"
            return f"Launched {engine} browser with profile '{profile}' at {profile_dir}"

        else:
            # Ephemeral session
            self._browser = pw.chromium.launch(
                headless=headless,
                args=chrome_args,
                **launch_kwargs,
            )
            ctx_kwargs = {
                "viewport": {"width": viewport_width, "height": viewport_height},
            }
            if proxy:
                ctx_kwargs["proxy"] = {"server": proxy}
            self._context = self._browser.new_context(**ctx_kwargs)
            self._pages = []
            page = self._context.new_page()
            self._pages.append(page)
            self._current_idx = 0
            engine = "CloakBrowser" if _CLOAKBROWSER_AVAILABLE else "Playwright"
            return f"Launched ephemeral {engine} browser (headed)"

    def _launch_cdp(self, cdp_url: str) -> str:
        """Connect to an already-running Chrome via CDP."""
        self._worker.start()
        self._profile_name = f"cdp:{cdp_url}"

        result = self._worker.dispatch(
            lambda: self._do_cdp_connect(cdp_url),
            timeout=30,
        )
        return result

    def _do_cdp_connect(self, cdp_url: str):
        """Run CDP connection in worker thread."""
        pw = self._worker._pw
        try:
            self._browser = pw.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return (
                f"Failed to connect to Chrome at {cdp_url}: {e}\n\n"
                "To use this mode, launch Chrome yourself with:\n"
                '  chrome.exe --remote-debugging-port=9222\n\n'
                "Or on a different port, then pass that URL to connect_cdp."
            )

        self._context = self._browser.contexts[0] if self._browser.contexts else None
        if self._context:
            self._pages = list(self._context.pages)
        else:
            self._pages = []
        self._current_idx = 0

        tab_count = len(self._pages)
        first_url = self._pages[0].url if self._pages else "none"
        return (
            f"Connected to Chrome via CDP at {cdp_url}\n"
            f"Found {tab_count} tab(s). Active: {first_url}"
        )

    def _launch_chrome_profile(
        self, chrome_profile, headless, proxy, humanize,
        viewport_width, viewport_height, extra_args,
    ) -> str:
        """Launch using a real Chrome user data directory (copied)."""
        from shutil import copytree, rmtree

        src = Path(chrome_profile).expanduser().resolve()

        if not src.exists():
            return (
                f"Chrome profile not found at: {src}\n\n"
                "On Windows, Chrome profiles are typically at:\n"
                '  C:\\Users\\<you>\\AppData\\Local\\Google\\Chrome\\User Data\\\n\n'
                "The profile directory is usually 'Default', 'Profile 1', etc."
            )

        lock_file = src / "SingletonLock"
        if lock_file.exists():
            return (
                f"Chrome profile is LOCKED (Chrome is running with this profile).\n"
                f"Path: {src}\n\n"
                "Either close Chrome first, or use connect_cdp mode instead."
            )

        copy_name = f"_chrome_copy_{src.name}"
        copy_dir = self.DEFAULT_PROFILES_DIR / copy_name
        if copy_dir.exists():
            rmtree(copy_dir, ignore_errors=True)

        try:
            copytree(src, copy_dir)
        except Exception as e:
            return f"Failed to copy Chrome profile: {e}"

        self._profile_name = f"chrome:{src.name}"

        # Now launch with the copied profile in worker thread
        self._worker.start()
        launch_args = list(extra_args or [])
        profile_dir = str(copy_dir)

        result = self._worker.dispatch(
            lambda: self._do_persistent_launch(
                profile_dir, headless, launch_args, proxy, viewport_width, viewport_height,
            ),
            timeout=60,
        )
        return (
            f"Copied Chrome profile '{src.name}' to {copy_dir}\n"
            f"{result}"
        )

    def _do_persistent_launch(self, profile_dir, headless, launch_args, proxy, vw, vh):
        """Launch persistent context in worker thread.

        Uses CloakBrowser stealth Chromium when available.
        """
        pw = self._worker._pw

        # --- CloakBrowser stealth args ---
        executable_path = None
        ignore_default_args = []
        chrome_args = list(launch_args)

        if _CLOAKBROWSER_AVAILABLE and _cloakbinary_path:
            executable_path = _cloakbinary_path
            chrome_args = build_args(
                stealth_args=True,
                extra_args=launch_args,
                headless=headless,
            )
            if _cloak_ignore_args:
                ignore_default_args = list(_cloak_ignore_args)

        context_kwargs = {
            "headless": headless,
            "viewport": {"width": vw, "height": vh},
            "args": chrome_args,
        }
        if executable_path:
            context_kwargs["executable_path"] = executable_path
        if ignore_default_args:
            context_kwargs["ignore_default_args"] = ignore_default_args
        if proxy:
            context_kwargs["proxy"] = {"server": proxy}

        self._context = pw.chromium.launch_persistent_context(
            profile_dir,
            **context_kwargs,
        )
        self._browser = None
        self._pages = list(self._context.pages)
        if not self._pages:
            self._pages.append(self._context.new_page())
        self._current_idx = 0
        engine = "CloakBrowser" if _CLOAKBROWSER_AVAILABLE else "Playwright"
        return f"Launched {engine} browser with your Chrome data (cookies, logins, history)."

    # ------------------------------------------------------------------
    #  Close
    # ------------------------------------------------------------------

    def close(self) -> str:
        """Close browser and clean up. Returns status message."""
        with self._lock:
            if not self._browser and not self._context:
                self._worker.stop()
                return "No browser is running."

            # Do the close in the worker thread
            try:
                self._worker.dispatch(self._do_close_internal, timeout=15)
            except Exception:
                pass

            # Clean up Chrome profile copies (before clearing _profile_name)
            if self._profile_name and self._profile_name.startswith("chrome:"):
                copy_name = f"_chrome_copy_{self._profile_name.split(':', 1)[1]}"
                copy_dir = self.DEFAULT_PROFILES_DIR / copy_name
                if copy_dir.exists():
                    try:
                        from shutil import rmtree
                        rmtree(copy_dir, ignore_errors=True)
                    except Exception:
                        pass

            self._browser = None
            self._context = None
            self._pages = []
            self._current_idx = 0
            self._profile_name = None

            self._worker.stop()
            return "Browser closed."

    def _do_close_internal(self):
        """Close Playwright resources in the worker thread."""
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  Page operations (dispatched to worker thread)
    # ------------------------------------------------------------------

    def navigate(self, url: str) -> str:
        page = self.current_page
        if not page:
            return "No active page. Launch the browser first."
        def _do():
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status = resp.status if resp else "unknown"
            title = page.title() or "(no title)"
            return status, title
        try:
            status, title = self._worker.dispatch(_do, timeout=35)
            return f"Navigated to {url}\nStatus: {status}\nTitle: {title}"
        except Exception as e:
            return f"Navigation failed: {e}"

    def go_back(self) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        def _do():
            page.go_back(wait_until="domcontentloaded")
            return page.url
        try:
            url = self._worker.dispatch(_do, timeout=15)
            return f"Went back. Now on: {url}"
        except Exception as e:
            return f"Go back failed: {e}"

    def go_forward(self) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        def _do():
            page.go_forward(wait_until="domcontentloaded")
            return page.url
        try:
            url = self._worker.dispatch(_do, timeout=15)
            return f"Went forward. Now on: {url}"
        except Exception as e:
            return f"Go forward failed: {e}"

    def reload(self) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        def _do():
            page.reload(wait_until="domcontentloaded")
            return page.url
        try:
            url = self._worker.dispatch(_do, timeout=15)
            return f"Page reloaded: {url}"
        except Exception as e:
            return f"Reload failed: {e}"

    def click(self, selector: str) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        try:
            self._worker.dispatch(lambda: page.locator(selector).click(timeout=10000), timeout=15)
            return f"Clicked: {selector}"
        except Exception as e:
            try:
                self._worker.dispatch(lambda: page.get_by_text(selector, exact=False).first.click(timeout=5000), timeout=10)
                return f"Clicked element with text: {selector}"
            except Exception:
                return f"Click failed for '{selector}': {e}"

    def type_text(self, selector: str, text: str, press_enter: bool = False) -> str:
        page = self.current_page
        if not page:
            return "No active page."

        def _do_type():
            loc = page.locator(selector)
            loc.click(timeout=5000)
            loc.fill("")
            loc.type(text, delay=30)
            if press_enter:
                loc.press("Enter")

        try:
            self._worker.dispatch(_do_type, timeout=15)
            result = f"Typed into {selector}: '{text}'"
            if press_enter:
                result += " + Enter"
            return result
        except Exception as e:
            try:
                self._worker.dispatch(
                    lambda: page.get_by_placeholder(selector, exact=False).first.fill(text), timeout=10
                )
                return f"Typed into placeholder '{selector}': '{text}'"
            except Exception:
                return f"Type failed for '{selector}': {e}"

    def select_option(self, selector: str, value: str) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        try:
            self._worker.dispatch(lambda: page.locator(selector).select_option(value=value, timeout=5000), timeout=10)
            return f"Selected '{value}' in {selector}"
        except Exception as e:
            return f"Select failed for '{selector}': {e}"

    def evaluate(self, expression: str) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        try:
            result = self._worker.dispatch(lambda: page.evaluate(expression), timeout=15)
            if result is None:
                return "JavaScript executed (returned null/undefined)"
            if isinstance(result, str):
                return result if len(result) < 10000 else result[:10000] + "...[truncated]"
            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            return f"JavaScript error: {e}"

    # ------------------------------------------------------------------
    #  Observation
    # ------------------------------------------------------------------

    def screenshot(self, full_page: bool = False) -> Tuple[Optional[bytes], str]:
        """Capture a screenshot. Returns (png_bytes, message_with_file_path).

        Screenshots are saved to ~/.kairos/screenshots/ with a timestamped name.
        This avoids sending large base64 data through the API (which causes 400
        errors with OpenRouter and similar providers).
        """
        page = self.current_page
        if not page:
            return None, "No active page."

        screenshots_dir = Path("~/.kairos/screenshots").expanduser()
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        import time as _t
        ts = _t.strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{ts}.png"
        filepath = screenshots_dir / filename

        def _do():
            png_bytes = page.screenshot(full_page=full_page, type="png")
            filepath.write_bytes(png_bytes)
            return png_bytes, page.url, len(png_bytes)

        try:
            png_bytes, url, size_bytes = self._worker.dispatch(_do, timeout=30)
            size_kb = size_bytes / 1024
            return png_bytes, (
                f"Screenshot captured ({size_kb:.0f} KB) — {url}\n"
                f"Saved to: {filepath}"
            )
        except Exception as e:
            return None, f"Screenshot failed: {e}"

    def snapshot(self) -> str:
        page = self.current_page
        if not page:
            return "No active page."

        try:
            data = self._worker.dispatch(lambda: page.evaluate(_SNAPSHOT_JS), timeout=15)
            return self._format_snapshot(data)
        except Exception as e:
            try:
                title = self._worker.dispatch(lambda: page.title(), timeout=5)
                url = self._worker.dispatch(lambda: page.url, timeout=5)
                text = self._worker.dispatch(lambda: page.inner_text("body"), timeout=10)
                if len(text) > 3000:
                    text = text[:3000] + "\n...[truncated]"
                return f"Page: {title}\nURL: {url}\n\n{text}"
            except Exception as e2:
                return f"Snapshot failed: {e}\nFallback also failed: {e2}"

    def _format_snapshot(self, data: Dict[str, Any]) -> str:
        """Format snapshot data into a compact, token-efficient text representation."""
        lines = []

        title = data.get("title", "(no title)")
        url = data.get("url", "")
        lines.append(f"[Page] {title}")
        if url:
            lines.append(f"[URL] {url}")
        lines.append("")

        if len(self._pages) > 1:
            # Use dispatch to get tab titles safely
            def _get_tab_titles():
                result = []
                for i, p in enumerate(self._pages):
                    try:
                        t = p.title() or p.url[:30]
                    except Exception:
                        t = "(error)"
                    result.append(f"{'*' if i == self._current_idx else ' '} Tab {i}: {t}")
                return result
            try:
                tab_titles = self._worker.dispatch(_get_tab_titles, timeout=10)
            except Exception:
                tab_titles = [f"{'*' if i == self._current_idx else ' '} Tab {i}: (error)" for i in range(len(self._pages))]
            tab_info = " | ".join(tab_titles)
            lines.append(f"[Tabs] {tab_info}")
            lines.append("")

        headings = data.get("headings", [])
        if headings:
            lines.append("[Content]")
            for h in headings:
                level = h.get("level", "h2")
                indent = "  " * (int(level[1]) - 1) if len(level) > 1 else ""
                lines.append(f"  {indent}{h['text']}")
            lines.append("")

        elements = data.get("elements", [])
        if elements:
            lines.append("[Interactive Elements]")
            for i, el in enumerate(elements):
                tag = el.get("tag", "?")
                selector = el.get("selector", "")
                text = el.get("text", "")
                parts = [f"  [{i}]"]

                if tag == "a":
                    parts.append(f'Link: "{text}"')
                elif tag == "button":
                    parts.append(f'Button: "{text}"')
                elif tag == "input":
                    input_type = el.get("input_type", "text")
                    placeholder = el.get("placeholder", "")
                    value = el.get("value", "")
                    if input_type in ("checkbox", "radio"):
                        checked = "✓" if el.get("checked") else "✗"
                        parts.append(f'{input_type}: "{text or placeholder}" {checked}')
                    else:
                        display = placeholder or text or input_type
                        val_str = f' = "{value}"' if value else ""
                        parts.append(f'Input({input_type}): "{display}"{val_str}')
                elif tag == "textarea":
                    parts.append(f'Textarea: "{el.get("placeholder", "")}"')
                elif tag == "select":
                    selected = el.get("selected", "")
                    parts.append(f'Select: "{text}" selected="{selected}"')
                else:
                    role = el.get("role", "")
                    parts.append(f'{tag}({role}): "{text}"')

                role = el.get("role", "")
                aria = el.get("aria_label", "")
                if role and role not in ("", "button", "link"):
                    parts.append(f"[role={role}]")
                if aria:
                    parts.append(f'[aria="{aria}"]')

                parts.append(f" -> {selector}")
                lines.append(" ".join(parts))
            lines.append("")

        text_blocks = data.get("text_blocks", [])
        if text_blocks:
            lines.append("[Text]")
            for t in text_blocks:
                lines.append(f"  {t}")
            lines.append("")

        inputs = [e for e in elements if e.get("tag") in ("input", "textarea", "select")]
        if inputs:
            lines.append("[Form State]")
            for inp in inputs:
                tag = inp.get("tag", "")
                if tag == "input":
                    val = inp.get("value", "")
                    if val:
                        lines.append(f'  {inp.get("selector", "?")} = "{val}"')
                elif tag == "textarea":
                    val = inp.get("value", "")
                    if val:
                        preview = val[:60] + "..." if len(val) > 60 else val
                        lines.append(f'  {inp.get("selector", "?")} = "{preview}"')
                elif tag == "select":
                    lines.append(f'  {inp.get("selector", "?")} = "{inp.get("selected", "")}"')

        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  Tab management
    # ------------------------------------------------------------------

    def open_new_tab(self, url: Optional[str] = None) -> str:
        if not self._context:
            return "No browser context. Launch the browser first."

        def _do():
            page = self._context.new_page()
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return page

        try:
            page = self._worker.dispatch(_do, timeout=35)
            self._pages.append(page)
            self._current_idx = len(self._pages) - 1
            if url:
                def _get_title():
                    return page.title() or "(no title)"
                try:
                    title = self._worker.dispatch(_get_title, timeout=5)
                except Exception:
                    title = "(no title)"
                return f"Opened new tab ({len(self._pages)} tabs) — {title} — {url}"
            return f"Opened new tab ({len(self._pages)} tabs) — about:blank"
        except Exception as e:
            return f"Failed to open tab: {e}"

    def switch_tab(self, index: Optional[int] = None, url_pattern: Optional[str] = None) -> str:
        if not self._pages:
            return "No tabs open."

        def _get_tab_title(page):
            """Get title from a page via worker thread."""
            def _do():
                return page.title() or "(no title)"
            try:
                return self._worker.dispatch(_do, timeout=5)
            except Exception:
                return "(error)"

        if url_pattern:
            for i, page in enumerate(self._pages):
                if url_pattern.lower() in page.url.lower():
                    self._current_idx = i
                    title = _get_tab_title(page)
                    return f"Switched to tab {i}: {title} — {page.url}"
            return f"No tab matches URL pattern: '{url_pattern}'"

        if index is None:
            return "Specify tab index or url_pattern."
        if index < 0 or index >= len(self._pages):
            return f"Invalid tab index {index}. Valid: 0-{len(self._pages)-1}"

        self._current_idx = index
        page = self._pages[index]
        title = _get_tab_title(page)
        return f"Switched to tab {index}: {title} — {page.url}"

    def list_tabs(self) -> str:
        if not self._pages:
            return "No tabs open."

        def _get_all_tab_info():
            results = []
            for i, page in enumerate(self._pages):
                try:
                    title = page.title() or "(no title)"
                except Exception:
                    title = "(error)"
                try:
                    url = page.url
                except Exception:
                    url = "(error)"
                results.append((i, title, url))
            return results

        try:
            tab_data = self._worker.dispatch(_get_all_tab_info, timeout=10)
        except Exception:
            return "Failed to get tab info."

        lines = []
        for i, title, url in tab_data:
            marker = " *" if i == self._current_idx else "  "
            lines.append(f"  Tab {i}{marker}  {title}  —  {url}")
        return f"Open tabs ({len(self._pages)}):\n" + "\n".join(lines)

    def close_tab(self, index: Optional[int] = None) -> str:
        if not self._pages:
            return "No tabs open."
        if index is None:
            index = self._current_idx
        if index < 0 or index >= len(self._pages):
            return f"Invalid tab index {index}."
        if len(self._pages) == 1:
            return "Can't close the last tab. Use browser_close instead."
        try:
            self._worker.dispatch(lambda i=index: self._pages[i].close(), timeout=10)
        except Exception:
            pass
        self._pages.pop(index)
        if self._current_idx >= len(self._pages):
            self._current_idx = len(self._pages) - 1
        return f"Closed tab {index}. Now on tab {self._current_idx}."

    def get_page_info(self) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        def _do():
            title = page.title() or "(no title)"
            url = page.url
            return title, url
        try:
            title, url = self._worker.dispatch(_do, timeout=5)
            return (
                f"Title: {title}\n"
                f"URL: {url}\n"
                f"Tab: {self._current_idx} of {len(self._pages)}\n"
                f"Profile: {self._profile_name or '(ephemeral)'}"
            )
        except Exception as e:
            return f"Error getting page info: {e}"


# ------------------------------------------------------------------
#  Snapshot JS (defined at module level so lambdas can reference it)
# ------------------------------------------------------------------

_SNAPSHOT_JS = """
() => {
    const result = {
        title: document.title || '',
        url: window.location.href,
        elements: [],
        headings: [],
        text_blocks: []
    };

    function getSelector(el) {
        if (el.id) return '#' + CSS.escape(el.id);
        if (el.name) {
            const tag = el.tagName.toLowerCase();
            return tag + '[name="' + CSS.escape(el.name) + '"]';
        }
        if (el.dataset && el.dataset.testid) {
            return '[data-testid="' + el.dataset.testid + '"]';
        }
        const path = [];
        let current = el;
        while (current && current !== document.body && path.length < 4) {
            let selector = current.tagName.toLowerCase();
            if (current.id) {
                selector = '#' + CSS.escape(current.id);
                path.unshift(selector);
                break;
            }
            if (current.className && typeof current.className === 'string') {
                const classes = current.className.trim().split(/\\s+/).filter(c => c && !c.startsWith('css-'));
                if (classes.length > 0 && classes.length <= 3) {
                    selector += '.' + classes.map(c => CSS.escape(c)).join('.');
                }
            }
            const parent = current.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children).filter(s => s.tagName === current.tagName);
                if (siblings.length > 1) {
                    const idx = siblings.indexOf(current) + 1;
                    selector += ':nth-of-type(' + idx + ')';
                }
            }
            path.unshift(selector);
            current = current.parentElement;
        }
        return path.join(' > ');
    }

    function getText(el, maxLen) {
        maxLen = maxLen || 80;
        let text = (el.innerText || el.textContent || '').trim();
        text = text.replace(/\\s+/g, ' ');
        if (text.length > maxLen) text = text.substring(0, maxLen) + '...';
        return text;
    }

    function isVisible(el) {
        const style = window.getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
    }

    const interactiveSelectors = 'a, button, input, textarea, select, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [onclick]';
    document.querySelectorAll(interactiveSelectors).forEach(el => {
        if (!isVisible(el)) return;
        const tag = el.tagName.toLowerCase();
        const entry = {
            tag: tag,
            selector: getSelector(el),
            text: getText(el, 60)
        };

        if (tag === 'input') {
            entry.input_type = el.type || 'text';
            entry.placeholder = el.placeholder || '';
            entry.value = el.value || '';
            entry.checked = el.checked || false;
        } else if (tag === 'textarea') {
            entry.placeholder = el.placeholder || '';
            entry.value = (el.value || '').substring(0, 100);
        } else if (tag === 'select') {
            entry.selected = el.value || '';
            entry.options = Array.from(el.options).slice(0, 10).map(o => ({
                value: o.value,
                text: o.text.substring(0, 40),
                selected: o.selected
            }));
        } else if (tag === 'a') {
            entry.href = el.href || '';
        }

        const role = el.getAttribute('role');
        if (role) entry.role = role;
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) entry.aria_label = ariaLabel;

        result.elements.push(entry);
    });

    document.querySelectorAll('h1, h2, h3, h4').forEach(el => {
        if (!isVisible(el)) return;
        const text = getText(el, 100);
        if (text) {
            result.headings.push({
                level: el.tagName.toLowerCase(),
                text: text
            });
        }
    });

    const textEls = document.querySelectorAll('p, label, li, dt, dd, [role="text"], [role="heading"]');
    let textCount = 0;
    textEls.forEach(el => {
        if (!isVisible(el) || textCount >= 20) return;
        const text = getText(el, 120);
        if (text && text.length > 5) {
            result.text_blocks.push(text);
            textCount++;
        }
    });

    return result;
}
"""
