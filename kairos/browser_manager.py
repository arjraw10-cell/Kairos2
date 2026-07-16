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
  - CDP-based DOM extraction (cross-origin iframes, a11y tree, layout data)
  - Auto new-tab detection after clicks
  - In-page text search and CSS element querying
  - Index-based element interaction (click/type by snapshot index)
"""

import base64
import json
import os
import re as _re
import threading
import queue
import time as _time
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Callable
from urllib.parse import urlparse

from .cdp_manager import CDPManager

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
    pass


class _WorkerThread:
    """A dedicated thread that hosts a persistent Playwright instance."""

    def __init__(self):
        self._task_queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._pw = None
        self._interrupt_event: Optional[threading.Event] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._started.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="playwright-worker"
        )
        self._thread.start()
        self._started.wait(timeout=10)

    def _run(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._started.set()
        while True:
            task = self._task_queue.get()
            if task is None:
                break
            fn, result_holder = task
            try:
                result_holder["result"] = fn()
            except Exception as e:
                result_holder["error"] = e
            finally:
                result_holder["_done"].set()
        try:
            self._pw.stop()
        except Exception:
            pass
        self._pw = None

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def dispatch(self, fn, timeout=30):
        if not self._thread or not self._thread.is_alive():
            raise RuntimeError("Worker thread is not running")
        done = threading.Event()
        result_holder: Dict[str, Any] = {"_done": done}
        self._task_queue.put((fn, result_holder))
        # Poll with short intervals so we can react to interrupt signals
        deadline = _time.monotonic() + timeout + 5
        while not done.wait(timeout=0.05):
            if self._interrupt_event and self._interrupt_event.is_set():
                raise InterruptedError("Browser operation interrupted by user")
            if _time.monotonic() > deadline:
                raise TimeoutError(
                    f"Browser operation timed out after {timeout + 5}s. "
                    "The page may be unresponsive or the operation is taking too long."
                )
        if "error" in result_holder:
            raise result_holder["error"]
        if "result" in result_holder:
            return result_holder["result"]

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._task_queue.put(None)
            self._thread.join(timeout=10)
        self._thread = None
        self._pw = None


class BrowserManager:
    """Manages a single Playwright browser instance in a dedicated thread."""

    DEFAULT_PROFILES_DIR = Path("~/.kairos/profiles").expanduser()

    def __init__(self):
        self._browser = None
        self._context = None
        self._pages: List = []
        self._current_idx = 0
        self._profile_name: Optional[str] = None
        self._headless: bool = False
        self._lock = threading.Lock()
        self._worker = _WorkerThread()
        self._active_frame = None
        self._active_frame_type = None  # "playwright" or "cdp"
        # CDP support
        self._cdp = CDPManager()
        # Snapshot cache for index-based interactions
        self._last_snapshot_elements: List[Dict[str, Any]] = []

    def set_interrupt_event(self, event: threading.Event):
        """Wire the agent's interrupt event to the browser worker thread."""
        self._worker._interrupt_event = event

    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    #  Auto-snapshot fingerprinting
    # ------------------------------------------------------------------

    _FINGERPRINT_JS = """() => {
        return {
            url: location.href,
            title: document.title || '',
            elementCount: document.querySelectorAll('*').length,
            bodyTextLength: (document.body.innerText || '').length,
            hasOverlay: !!document.querySelector('.modal.show, [role="dialog"]:not([hidden]), .popup:not([hidden]), .overlay:not([hidden])'),
            overlayCount: document.querySelectorAll('.modal.show, [role="dialog"]:not([hidden]), .popup:not([hidden])').length,
            alertCount: document.querySelectorAll('[role="alert"], .toast.show').length,
            iframeCount: document.querySelectorAll('iframe').length
        };
    }"""

    def _capture_fingerprint(self) -> Dict[str, Any]:
        """Capture a lightweight DOM fingerprint to detect significant page changes."""
        target = self._target()
        if not target:
            return {}
        try:
            return self._worker.dispatch(
                lambda: target.evaluate(self._FINGERPRINT_JS), timeout=5
            )
        except Exception:
            return {}

    @staticmethod
    def _has_significant_change(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
        """Compare two fingerprints to decide if a snapshot+auto-screenshot is warranted."""
        if not before or not after:
            return True  # Missing data → be safe and snapshot
        # URL changed → navigation
        if before.get("url") != after.get("url"):
            return True
        # Title changed → usually a new page/view
        if before.get("title") != after.get("title"):
            return True
        # Modal/popup appeared
        if after.get("hasOverlay") and not before.get("hasOverlay"):
            return True
        if after.get("overlayCount", 0) > before.get("overlayCount", 0):
            return True
        # Alert appeared (error messages, success toasts)
        if after.get("alertCount", 0) > before.get("alertCount", 0):
            return True
        # New iframe loaded
        if after.get("iframeCount", 0) > before.get("iframeCount", 0):
            return True
        # Big DOM change (>20% element count shift)
        before_e = before.get("elementCount", 0)
        if before_e > 0:
            delta = abs(after.get("elementCount", 0) - before_e) / before_e
            if delta > 0.20:
                return True
        # Big text content change (>50% body text length shift)
        before_t = before.get("bodyTextLength", 0)
        if before_t > 0:
            delta = abs(after.get("bodyTextLength", 0) - before_t) / before_t
            if delta > 0.50:
                return True
        return False

    # ------------------------------------------------------------------
    #  Properties
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._browser is not None or self._context is not None

    def _target(self):
        if self._active_frame is not None:
            if self._active_frame_type == "cdp":
                # CDP cross-origin frame — can't use Playwright Frame API on it.
                # Return current_page; cross-origin content is handled separately
                # by _get_cross_origin_snapshot_section() in snapshot().
                return self.current_page
            return self._active_frame
        return self.current_page

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
    #  Post-action helpers
    # ------------------------------------------------------------------

    def _post_action(
        self,
        action_result: str,
        pre_fingerprint: Optional[Dict[str, Any]] = None,
        is_navigation: bool = False,
    ) -> str:
        """Smart auto-screenshot and auto-snapshot based on page state changes.

        - is_navigation=True: always auto-screenshot + auto-snapshot (page fundamentally changed)
        - pre_fingerprint provided: compare before/after, auto-append if significant change detected
        - Neither: just return the result (e.g. error states, non-visual actions)
        """
        parts = [action_result]
        should_snapshot = False

        if is_navigation:
            # Navigations always warrant a snapshot
            should_snapshot = True
        elif pre_fingerprint is not None:
            post_fingerprint = self._capture_fingerprint()
            should_snapshot = self._has_significant_change(
                pre_fingerprint, post_fingerprint
            )

        if should_snapshot:
            try:
                snap = self.snapshot()
                parts.append(f"\n\n[Page State]\n{snap}")
            except Exception:
                pass
            try:
                _png, msg, _data_url = self.screenshot(full_page=False)
                if _png is not None:
                    parts.append(f"\n\n{msg}")
            except Exception:
                pass

        return "".join(parts)

    def _detect_new_tab(self, tabs_before_count: int) -> str:
        """Check if a new tab was opened and switch to it. Returns a message or empty string."""
        if len(self._pages) > tabs_before_count:
            new_idx = len(self._pages) - 1
            self._current_idx = new_idx

            def _get_title():
                return self.current_page.title() or "(no title)"

            try:
                title = self._worker.dispatch(_get_title, timeout=5)
            except Exception:
                title = "(no title)"
            return f" [Auto-switched to new tab: {title}]"
        return ""

    # ------------------------------------------------------------------
    #  Launch
    # ------------------------------------------------------------------

    def launch(
        self,
        profile=None,
        headless=False,
        proxy=None,
        humanize=True,
        viewport_width=1280,
        viewport_height=720,
        extra_args=None,
        chrome_profile=None,
        connect_cdp=None,
    ) -> str:
        with self._lock:
            if self._browser or self._context:
                return "Browser is already running. Close it first with browser_close."
        # Browser automation is intentionally always humanized and headed.
        headless = False
        humanize = True
        self._headless = headless
        if connect_cdp:
            return self._launch_cdp(connect_cdp)
        if chrome_profile:
            return self._launch_chrome_profile(
                chrome_profile,
                headless,
                proxy,
                humanize,
                viewport_width,
                viewport_height,
                extra_args,
            )
        self._worker.start()
        self._profile_name = profile
        launch_args = list(extra_args or [])
        return self._worker.dispatch(
            lambda: self._do_launch(
                profile,
                headless,
                proxy,
                humanize,
                viewport_width,
                viewport_height,
                launch_args,
            ),
            timeout=60,
        )

    def _do_launch(self, profile, headless, proxy, humanize, vw, vh, launch_args):
        pw = self._worker._pw
        executable_path = None
        ignore_default_args = []
        chrome_args = list(launch_args)
        if _CLOAKBROWSER_AVAILABLE and _cloakbinary_path:
            executable_path = _cloakbinary_path
            chrome_args = build_args(
                stealth_args=True, extra_args=launch_args, headless=headless
            )
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
            self._context = pw.chromium.launch_persistent_context(
                profile_dir,
                headless=headless,
                viewport={"width": vw, "height": vh},
                args=chrome_args,
                **launch_kwargs,
            )
            self._browser = None
            self._pages = list(self._context.pages)
            if not self._pages:
                self._pages.append(self._context.new_page())
            self._current_idx = 0
            engine = "CloakBrowser" if _CLOAKBROWSER_AVAILABLE else "Playwright"
            return (
                f"Launched {engine} browser with profile '{profile}' at {profile_dir}"
            )
        else:
            self._browser = pw.chromium.launch(
                headless=headless, args=chrome_args, **launch_kwargs
            )
            ctx_kwargs = {"viewport": {"width": vw, "height": vh}}
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
        self._worker.start()
        self._profile_name = f"cdp:{cdp_url}"
        return self._worker.dispatch(lambda: self._do_cdp_connect(cdp_url), timeout=30)

    def _do_cdp_connect(self, cdp_url: str):
        pw = self._worker._pw
        try:
            self._browser = pw.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return (
                f"Failed to connect to Chrome at {cdp_url}: {e}\n\n"
                "Launch Chrome yourself with:  chrome.exe --remote-debugging-port=9222"
            )
        self._context = self._browser.contexts[0] if self._browser.contexts else None
        self._pages = list(self._context.pages) if self._context else []
        self._current_idx = 0
        return f"Connected via CDP at {cdp_url}\nFound {len(self._pages)} tab(s). Active: {self._pages[0].url if self._pages else 'none'}"

    def _launch_chrome_profile(
        self, chrome_profile, headless, proxy, humanize, vw, vh, extra_args
    ) -> str:
        from shutil import copytree, rmtree

        src = Path(chrome_profile).expanduser().resolve()
        if not src.exists():
            return f"Chrome profile not found at: {src}"
        if (src / "SingletonLock").exists():
            return f"Chrome profile is LOCKED. Close Chrome first, or use connect_cdp."
        copy_name = f"_chrome_copy_{src.name}"
        copy_dir = self.DEFAULT_PROFILES_DIR / copy_name
        if copy_dir.exists():
            rmtree(copy_dir, ignore_errors=True)
        try:
            copytree(src, copy_dir)
        except Exception as e:
            return f"Failed to copy Chrome profile: {e}"
        self._profile_name = f"chrome:{src.name}"
        self._worker.start()
        result = self._worker.dispatch(
            lambda: self._do_persistent_launch(
                str(copy_dir), headless, list(extra_args or []), proxy, vw, vh
            ),
            timeout=60,
        )
        return f"Copied Chrome profile '{src.name}' to {copy_dir}\n{result}"

    def _do_persistent_launch(self, profile_dir, headless, launch_args, proxy, vw, vh):
        pw = self._worker._pw
        executable_path = None
        ignore_default_args = []
        chrome_args = list(launch_args)
        if _CLOAKBROWSER_AVAILABLE and _cloakbinary_path:
            executable_path = _cloakbinary_path
            chrome_args = build_args(
                stealth_args=True, extra_args=launch_args, headless=headless
            )
            if _cloak_ignore_args:
                ignore_default_args = list(_cloak_ignore_args)
        ctx_kwargs = {
            "headless": headless,
            "viewport": {"width": vw, "height": vh},
            "args": chrome_args,
        }
        if executable_path:
            ctx_kwargs["executable_path"] = executable_path
        if ignore_default_args:
            ctx_kwargs["ignore_default_args"] = ignore_default_args
        if proxy:
            ctx_kwargs["proxy"] = {"server": proxy}
        self._context = pw.chromium.launch_persistent_context(profile_dir, **ctx_kwargs)
        self._browser = None
        self._pages = list(self._context.pages)
        if not self._pages:
            self._pages.append(self._context.new_page())
        self._current_idx = 0
        engine = "CloakBrowser" if _CLOAKBROWSER_AVAILABLE else "Playwright"
        return f"Launched {engine} browser with your Chrome data."

    # ------------------------------------------------------------------
    #  Close
    # ------------------------------------------------------------------

    def close(self) -> str:
        with self._lock:
            if not self._browser and not self._context:
                self._worker.stop()
                return "No browser is running."
            try:
                self._worker.dispatch(self._do_close_internal, timeout=15)
            except Exception:
                pass
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
            self._cdp.invalidate_all()
            self._last_snapshot_elements = []
            self._worker.stop()
            return "Browser closed."

    def _do_close_internal(self):
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
    #  Navigation
    # ------------------------------------------------------------------

    def navigate(self, url: str) -> str:
        page = self.current_page
        if not page:
            return "No active page. Launch the browser first."
        self._active_frame = None
        self._active_frame_type = None

        def _do():
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status = resp.status if resp else "unknown"
            title = page.title() or "(no title)"
            return status, title

        try:
            status, title = self._worker.dispatch(_do, timeout=35)
            result = f"Navigated to {url}\nStatus: {status}\nTitle: {title}"
        except TimeoutError:
            result = f"Navigation timed out: {url}"
        except Exception as e:
            err_str = str(e)
            if "ERR_NAME_NOT_RESOLVED" in err_str:
                result = f"Navigation failed: DNS error for: {url}"
            elif "ERR_CONNECTION" in err_str or "ERR_TIMED_OUT" in err_str:
                result = f"Navigation failed: connection error for {url}"
            else:
                result = f"Navigation failed: {e}"
        return self._post_action(result, is_navigation=True)

    def go_back(self) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        self._active_frame = None
        self._active_frame_type = None
        try:
            url = self._worker.dispatch(
                lambda: (page.go_back(wait_until="domcontentloaded"), page.url)[1],
                timeout=15,
            )
            result = f"Went back. Now on: {url}"
        except Exception as e:
            result = f"Go back failed: {e}"
        return self._post_action(result, is_navigation=True)

    def go_forward(self) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        self._active_frame = None
        self._active_frame_type = None
        try:
            url = self._worker.dispatch(
                lambda: (page.go_forward(wait_until="domcontentloaded"), page.url)[1],
                timeout=15,
            )
            result = f"Went forward. Now on: {url}"
        except Exception as e:
            result = f"Go forward failed: {e}"
        return self._post_action(result, is_navigation=True)

    def reload(self) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        self._active_frame = None
        self._active_frame_type = None
        try:
            url = self._worker.dispatch(
                lambda: (page.reload(wait_until="domcontentloaded"), page.url)[1],
                timeout=15,
            )
            result = f"Page reloaded: {url}"
        except Exception as e:
            result = f"Reload failed: {e}"
        return self._post_action(result, is_navigation=True)

    # ------------------------------------------------------------------
    #  Scroll (NEW)
    # ------------------------------------------------------------------

    def scroll(self, direction: str = "down", pages: float = 1.0) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        fp = self._capture_fingerprint()
        try:

            def _do():
                vp = page.viewport_size
                vh = vp["height"] if vp else 720
                vw = vp["width"] if vp else 1280
                scroll_px = int(vh * pages)
                if direction == "up":
                    scroll_px = -scroll_px
                page.mouse.move(vw // 2, vh // 2)
                page.mouse.wheel(0, scroll_px)
                return direction, abs(int(scroll_px)), vh

            d, px, vh = self._worker.dispatch(_do, timeout=10)
            # Small delay to let scroll complete
            _time.sleep(0.15)
            result = (
                f"Scrolled {d} {px}px (~{pages} viewport{'s' if pages != 1 else ''})"
            )
        except Exception as e:
            result = f"Scroll failed: {e}"
        return self._post_action(result, pre_fingerprint=fp)

    # ------------------------------------------------------------------
    #  Wait (NEW)
    # ------------------------------------------------------------------

    def wait(self, seconds: int = 3) -> str:
        fp = self._capture_fingerprint()
        actual = min(max(seconds, 0), 30)
        # Interruptible sleep — checks every 50ms
        deadline = _time.monotonic() + actual
        while _time.monotonic() < deadline:
            if self._worker._interrupt_event and self._worker._interrupt_event.is_set():
                return "Wait interrupted by user"
            _time.sleep(min(0.05, deadline - _time.monotonic()))
        return self._post_action(f"Waited {actual} seconds", pre_fingerprint=fp)

    # ------------------------------------------------------------------
    #  Wait For (element/text condition)
    # ------------------------------------------------------------------

    def wait_for(self, selector=None, text=None, timeout=10) -> str:
        """Wait for a specific element to become visible or text to appear.

        More efficient than blind sleeping for AJAX/dynamic content.
        Provide either selector (CSS) or text to wait for.
        """
        target = self._target()
        if not target:
            return "No active page."
        if not selector and not text:
            return "Must provide either selector or text to wait for."
        fp = self._capture_fingerprint()
        timeout_ms = min(max(timeout, 1), 30) * 1000
        try:
            if selector:

                def _do_wait():
                    target.wait_for_selector(
                        selector, state="visible", timeout=timeout_ms
                    )

                self._worker.dispatch(_do_wait, timeout=timeout // 1 + 10)
                result = f"Element '{selector}' appeared on page"
            else:
                # Poll in a loop until text appears or timeout
                deadline = _time.monotonic() + (timeout_ms / 1000)

                def _do_wait_text():
                    while True:
                        found = target.evaluate(
                            "text => document.body.innerText.includes(text)", text
                        )
                        if found:
                            return True
                        if _time.monotonic() >= deadline:
                            raise Exception(f"Text '{text}' not found")
                        _time.sleep(0.25)

                self._worker.dispatch(_do_wait_text, timeout=timeout // 1 + 10)
                result = f"Text '{text}' found on page"
        except Exception as e:
            err_str = str(e)
            if (
                "Timeout" in err_str
                or "timeout" in err_str
                or "not found" in err_str.lower()
            ):
                target_desc = selector or text
                result = f"Timed out waiting ({timeout}s) for: {target_desc}"
            else:
                result = f"wait_for failed: {e}"
        return self._post_action(result, pre_fingerprint=fp)

    # ------------------------------------------------------------------
    #  Send Keys (NEW)
    # ------------------------------------------------------------------

    def send_keys(self, keys: str) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        fp = self._capture_fingerprint()
        try:

            def _do():
                page.keyboard.press(keys)
                return True

            self._worker.dispatch(_do, timeout=5)
            result = f"Sent keys: {keys}"
        except Exception as e:
            result = f"Send keys failed: {e}"
        return self._post_action(result, pre_fingerprint=fp)

    # ------------------------------------------------------------------
    #  Click (selector-based, existing)
    # ------------------------------------------------------------------

    def click(self, selector: str) -> str:
        target = self._target()
        if not target:
            return "No active page."

        tabs_before = len(self._pages)
        fp = self._capture_fingerprint()

        # Pre-click state
        try:
            pre_state = self._worker.dispatch(
                lambda: {"url": target.url, "title": target.title() or ""}, timeout=5
            )
        except Exception:
            pre_state = {}

        # Fallback chain: CSS -> text -> label -> JS
        clicked_method = None
        try:
            self._worker.dispatch(
                lambda: target.locator(selector).click(timeout=10000), timeout=15
            )
            clicked_method = "CSS selector"
        except Exception:
            pass
        if not clicked_method:
            try:
                self._worker.dispatch(
                    lambda: target.get_by_text(selector, exact=False).first.click(
                        timeout=5000
                    ),
                    timeout=10,
                )
                clicked_method = "visible text"
            except Exception:
                pass
        if not clicked_method:
            clicked_method = self._worker.dispatch(
                lambda: self._click_label_fallback(target, selector), timeout=10
            )
        if not clicked_method:
            return self._post_action(
                f"Click failed for '{selector}': no matching element",
                pre_fingerprint=fp,
            )

        # Post-click verification
        try:
            post_state = self._worker.dispatch(
                lambda: {
                    "url": target.url,
                    "title": target.title() or "",
                    "modal_visible": bool(
                        target.locator(
                            ".modal.show, [role='dialog']:not([hidden])"
                        ).count()
                    ),
                    "dropdown_visible": bool(
                        target.locator(
                            ".dropdown-menu.show, [role='listbox']:not([hidden])"
                        ).count()
                    ),
                },
                timeout=5,
            )
        except Exception:
            post_state = {}

        changes = []
        if post_state.get("url") != pre_state.get("url"):
            changes.append(f"URL changed: {post_state['url']}")
        if post_state.get("title") != pre_state.get("title"):
            changes.append(f"Title changed: {post_state['title']}")
        if post_state.get("modal_visible"):
            changes.append("modal appeared")
        if post_state.get("dropdown_visible"):
            changes.append("dropdown appeared")

        try:

            def _check():
                loc = target.locator(selector)
                tag = loc.evaluate("el => el.tagName.toLowerCase()")
                if tag == "input":
                    return loc.evaluate("el => el.type"), loc.evaluate(
                        "el => el.checked"
                    )
                return None, None

            input_type, checked = self._worker.dispatch(_check, timeout=5)
            if input_type in ("radio", "checkbox"):
                changes.append(f"{input_type} checked={checked}")
        except Exception:
            pass

        result = f"Clicked: {selector} (via {clicked_method})"
        if changes:
            result += f" — {'; '.join(changes)}"
        else:
            result += " — no visible page state change detected"

        # Auto new-tab detection
        tab_msg = self._detect_new_tab(tabs_before)
        result += tab_msg

        return self._post_action(result, pre_fingerprint=fp)

    def _click_label_fallback(self, target, selector):
        """Label wrapping and JS force-click fallback."""
        el = target.locator(selector).first
        if el.count() == 0:
            return None
        el_id = el.get_attribute("id")
        if el_id:
            label_loc = target.locator(f'label[for="{el_id}"]')
            if label_loc.count() > 0:
                label_loc.first.click(timeout=3000)
                return "label[for]"
            aria_id = el.get_attribute("aria-labelledby")
            if aria_id:
                aria_loc = target.locator(f'[id="{aria_id}"]')
                if aria_loc.count() > 0:
                    aria_loc.first.click(timeout=3000)
                    return "aria-labelledby"
        wrapping = target.locator(f"label:has({selector})").first
        if wrapping.count() > 0:
            wrapping.click(timeout=3000)
            return "wrapping label"
        try:
            result = target.evaluate(
                """(sel) => {
                const idMatch = sel.match(/^\\[id="(.+)"\\]$/);
                let el = idMatch ? document.getElementById(idMatch[1]) : document.querySelector(sel);
                if (el) { el.click(); return true; }
                return false;
            }""",
                selector,
            )
            if result:
                return "JS force-click"
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    #  Click by Index (NEW)
    # ------------------------------------------------------------------

    def click_by_index(self, index: int) -> str:
        if not self._last_snapshot_elements:
            # Re-snapshot to populate the cache
            try:
                self.snapshot()
            except Exception:
                pass
        if index < 0 or index >= len(self._last_snapshot_elements):
            return self._post_action(
                f"Element index {index} not found. Available: 0-{len(self._last_snapshot_elements) - 1}",
            )
        el = self._last_snapshot_elements[index]
        selector = el.get("selector", "")
        text = el.get("text", el.get("name", ""))
        if not selector:
            return self._post_action(
                f"Element [{index}] has no clickable selector ({text})"
            )
        return self.click(selector)

    # ------------------------------------------------------------------
    #  Click XY
    # ------------------------------------------------------------------

    def click_xy(self, x: float, y: float) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        tabs_before = len(self._pages)
        fp = self._capture_fingerprint()
        try:
            self._worker.dispatch(lambda: page.mouse.click(x, y), timeout=10)
            result = f"Clicked at ({x}, {y})"
            result += self._detect_new_tab(tabs_before)
        except Exception as e:
            result = f"Click at ({x}, {y}) failed: {e}"
        return self._post_action(result, pre_fingerprint=fp)

    # ------------------------------------------------------------------
    #  Hover (selector-based)
    # ------------------------------------------------------------------

    def hover(self, selector: str) -> str:
        """Hover over an element to trigger hover states (dropdowns, tooltips, etc.)."""
        target = self._target()
        if not target:
            return "No active page."
        fp = self._capture_fingerprint()
        # Pre-hover state
        try:
            pre_state = self._worker.dispatch(
                lambda: {"url": target.url, "title": target.title() or ""}, timeout=5
            )
        except Exception:
            pre_state = {}
        try:
            self._worker.dispatch(
                lambda: target.locator(selector).hover(timeout=10000), timeout=15
            )
            result = f"Hovered over: {selector}"
        except Exception:
            # Fallback: try visible text
            try:
                self._worker.dispatch(
                    lambda: target.get_by_text(selector, exact=False).first.hover(
                        timeout=5000
                    ),
                    timeout=10,
                )
                result = f"Hovered over: {selector} (via text)"
            except Exception as e:
                return self._post_action(
                    f"Hover failed for '{selector}': {e}", pre_fingerprint=fp
                )
        # Post-hover verification
        try:
            post_state = self._worker.dispatch(
                lambda: {
                    "url": target.url,
                    "title": target.title() or "",
                    "dropdown_visible": bool(
                        target.locator(
                            ".dropdown-menu.show, [role='listbox']:not([hidden])"
                        ).count()
                    ),
                    "tooltip_visible": bool(
                        target.locator(
                            ".tooltip.show, .popover.show, [role='tooltip']:not([hidden])"
                        ).count()
                    ),
                },
                timeout=5,
            )
        except Exception:
            post_state = {}
        changes = []
        if post_state.get("dropdown_visible"):
            changes.append("dropdown appeared")
        if post_state.get("tooltip_visible"):
            changes.append("tooltip appeared")
        if changes:
            result += f" — {'; '.join(changes)}"
        else:
            result += " — no visible hover state change detected"
        return self._post_action(result, pre_fingerprint=fp)

    # ------------------------------------------------------------------
    #  Hover by Index
    # ------------------------------------------------------------------

    def hover_by_index(self, index: int) -> str:
        """Hover over an element by its snapshot index number."""
        if not self._last_snapshot_elements:
            try:
                self.snapshot()
            except Exception:
                pass
        if index < 0 or index >= len(self._last_snapshot_elements):
            return self._post_action(
                f"Element index {index} not found. Available: 0-{len(self._last_snapshot_elements) - 1}",
            )
        el = self._last_snapshot_elements[index]
        selector = el.get("selector", "")
        text = el.get("text", el.get("name", ""))
        if not selector:
            return self._post_action(
                f"Element [{index}] has no clickable selector ({text})"
            )
        return self.hover(selector)

    # ------------------------------------------------------------------
    #  Drag (selector-based)
    # ------------------------------------------------------------------

    def drag(self, selector_from: str, selector_to: str) -> str:
        """Drag an element to another element."""
        target = self._target()
        if not target:
            return "No active page."
        page = self.current_page
        if not page:
            return "No active page."
        fp = self._capture_fingerprint()
        try:

            def _do():
                source = target.locator(selector_from).first
                dest = target.locator(selector_to).first
                source_box = source.bounding_box(timeout=5000)
                dest_box = dest.bounding_box(timeout=5000)
                if not source_box:
                    return None, "Source element not visible"
                if not dest_box:
                    return None, "Destination element not visible"
                sx = source_box["x"] + source_box["width"] / 2
                sy = source_box["y"] + source_box["height"] / 2
                dx = dest_box["x"] + dest_box["width"] / 2
                dy = dest_box["y"] + dest_box["height"] / 2
                page.mouse.move(sx, sy)
                page.mouse.down()
                # Smooth drag with intermediate steps
                steps = 10
                for i in range(1, steps + 1):
                    cx = sx + (dx - sx) * i / steps
                    cy = sy + (dy - sy) * i / steps
                    page.mouse.move(cx, cy)
                page.mouse.up()
                return True, None

            success, err = self._worker.dispatch(_do, timeout=15)
            if not success:
                result = f"Drag failed: {err}"
            else:
                result = f"Dragged from '{selector_from}' to '{selector_to}'"
        except Exception as e:
            result = f"Drag failed: {e}"
        return self._post_action(result, pre_fingerprint=fp)

    # ------------------------------------------------------------------
    #  Drag XY
    # ------------------------------------------------------------------

    def drag_xy(self, x1: float, y1: float, x2: float, y2: float) -> str:
        """Drag from one coordinate to another."""
        page = self.current_page
        if not page:
            return "No active page."
        fp = self._capture_fingerprint()
        try:

            def _do():
                page.mouse.move(x1, y1)
                page.mouse.down()
                steps = 10
                for i in range(1, steps + 1):
                    cx = x1 + (x2 - x1) * i / steps
                    cy = y1 + (y2 - y1) * i / steps
                    page.mouse.move(cx, cy)
                page.mouse.up()
                return True

            self._worker.dispatch(_do, timeout=10)
            result = f"Dragged from ({x1}, {y1}) to ({x2}, {y2})"
        except Exception as e:
            result = f"Drag from ({x1}, {y1}) to ({x2}, {y2}) failed: {e}"
        return self._post_action(result, pre_fingerprint=fp)

    # ------------------------------------------------------------------
    #  Type (selector-based, existing)
    # ------------------------------------------------------------------

    def type_text(self, selector: str, text: str, press_enter=False) -> str:
        target = self._target()
        if not target:
            return "No active page."

        fp = self._capture_fingerprint()

        def _do_fill():
            loc = target.locator(selector)
            loc.fill(text, timeout=5000)
            return loc.input_value(timeout=3000)

        def _do_keys():
            loc = target.locator(selector)
            loc.click(timeout=5000)
            loc.fill("", timeout=3000)
            loc.type(text, delay=30)
            return loc.input_value(timeout=3000)

        actual = None
        try:
            actual = self._worker.dispatch(_do_fill, timeout=15)
        except Exception:
            pass
        if actual != text:
            try:
                actual = self._worker.dispatch(_do_keys, timeout=20)
            except Exception:
                pass
        if actual != text:
            try:

                def _do_ph():
                    loc = target.get_by_placeholder(selector, exact=False).first
                    loc.fill(text, timeout=5000)
                    return loc.input_value(timeout=3000)

                actual = self._worker.dispatch(_do_ph, timeout=15)
            except Exception:
                pass

        if actual == text:
            result = f"Typed into {selector}: '{text}'"
        elif actual is not None and actual.strip() == text.strip():
            result = f"Typed into {selector}: '{text}' (stripped match — value is '{actual}')"
        elif actual is not None:
            result = f"WARNING: Typed into {selector} but value mismatch.\n  Expected: '{text}'\n  Actual:   '{actual}'"
        else:
            result = f"Typed into {selector}: '{text}' (could not verify)"

        if press_enter:
            try:
                self._worker.dispatch(
                    lambda: target.locator(selector).press("Enter"), timeout=5
                )
                result += " + Enter"
            except Exception:
                result += " + Enter (failed)"

        return self._post_action(result, pre_fingerprint=fp)

    # ------------------------------------------------------------------
    #  Type by Index (NEW)
    # ------------------------------------------------------------------

    def type_by_index(self, index: int, text: str, press_enter=False) -> str:
        if not self._last_snapshot_elements:
            try:
                self.snapshot()
            except Exception:
                pass
        if index < 0 or index >= len(self._last_snapshot_elements):
            return self._post_action(
                f"Element index {index} not found. Available: 0-{len(self._last_snapshot_elements) - 1}",
            )
        el = self._last_snapshot_elements[index]
        selector = el.get("selector", "")
        if not selector:
            return self._post_action(f"Element [{index}] has no input selector")
        return self.type_text(selector, text, press_enter)

    # ------------------------------------------------------------------
    #  Select Option
    # ------------------------------------------------------------------

    def select_option(self, selector: str, value: str) -> str:
        target = self._target()
        if not target:
            return "No active page."

        fp = self._capture_fingerprint()

        def _do_select():
            loc = target.locator(selector)
            for attempt in [("value", {"value": value}), ("label", {"label": value})]:
                try:
                    loc.select_option(**attempt[1], timeout=3000)
                    return attempt[0], loc.input_value(timeout=3000)
                except Exception:
                    pass
            try:
                loc.select_option(index=int(value), timeout=3000)
                return "index", loc.input_value(timeout=3000)
            except Exception:
                return None, None

        method, actual = self._worker.dispatch(_do_select, timeout=10)
        if not method:
            return self._post_action(
                f"Select failed for '{selector}': '{value}' not found",
                pre_fingerprint=fp,
            )

        if actual and actual.strip() == value.strip():
            result = f"Selected '{value}' in {selector} (matched by {method}, verified)"
        elif actual:
            result = f"Selected '{value}' in {selector} via {method}, but verification shows: '{actual}'"
        else:
            result = f"Selected '{value}' in {selector} via {method}"
        return self._post_action(result, pre_fingerprint=fp)

    # ------------------------------------------------------------------
    #  Select Option by Index
    # ------------------------------------------------------------------

    def select_option_by_index(self, index: int, value: str) -> str:
        """Select an option from a <select> by snapshot index (PREFERRED)."""
        if not self._last_snapshot_elements:
            try:
                self.snapshot()
            except Exception:
                pass
        if index < 0 or index >= len(self._last_snapshot_elements):
            return self._post_action(
                f"Element index {index} not found. Available: 0-{len(self._last_snapshot_elements) - 1}",
            )
        el = self._last_snapshot_elements[index]
        if el.get("tag") != "select":
            text = el.get("text", el.get("name", ""))
            return self._post_action(
                f"Element [{index}] is not a <select> (it's a {el.get('tag', '?')}): {text}"
            )
        selector = el.get("selector", "")
        if not selector:
            return self._post_action(f"Element [{index}] has no selector")
        return self.select_option(selector, value)

    # ------------------------------------------------------------------
    #  Search Page (NEW)
    # ------------------------------------------------------------------

    def search_page(
        self, pattern: str, regex=False, case_sensitive=False, max_results=20
    ) -> str:
        page = self.current_page
        if not page:
            return "No active page."

        def _do():
            js = f"""(() => {{
                var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                var fullText = '';
                var nodeOffsets = [];
                while (walker.nextNode()) {{
                    var node = walker.currentNode;
                    var text = node.textContent;
                    if (text && text.trim()) {{
                        nodeOffsets.push({{offset: fullText.length, length: text.length, node: node}});
                        fullText += text;
                    }}
                }}
                var flags = {"g" if case_sensitive else "gi"};
                var pattern = {json.dumps(pattern)};
                var re;
                try {{
                    re = {json.dumps(pattern) if regex else "null"};
                    if ({"true" if regex else "false"}) {{
                        re = new RegExp(pattern, flags);
                    }} else {{
                        re = new RegExp(pattern.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&'), flags);
                    }}
                }} catch(e) {{ return {{error: 'Invalid regex: ' + e.message, matches: [], total: 0}}; }}
                var matches = [];
                var totalFound = 0;
                var match;
                while ((match = re.exec(fullText)) !== null) {{
                    totalFound++;
                    if (matches.length < {max_results}) {{
                        var start = Math.max(0, match.index - 40);
                        var end = Math.min(fullText.length, match.index + match[0].length + 40);
                        var context = fullText.slice(start, end);
                        matches.push({{
                            match_text: match[0],
                            context: (start > 0 ? '...' : '') + context + (end < fullText.length ? '...' : '')
                        }});
                    }}
                    if (match[0].length === 0) re.lastIndex++;
                }}
                return {{matches: matches, total: totalFound, has_more: totalFound > {max_results}}};
            }})()"""
            return page.evaluate(js)

        try:
            data = self._worker.dispatch(_do, timeout=10)
        except Exception as e:
            return f"search_page failed: {e}"

        if isinstance(data, dict) and data.get("error"):
            return f"search_page: {data['error']}"
        if not isinstance(data, dict):
            return f"search_page returned unexpected result: {data}"

        matches = data.get("matches", [])
        total = data.get("total", 0)
        has_more = data.get("has_more", False)

        if total == 0:
            return f'No matches found for "{pattern}" on page.'

        lines = [
            f'Found {total} match{"es" if total != 1 else ""} for "{pattern}" on page:'
        ]
        for i, m in enumerate(matches):
            lines.append(f"[{i + 1}] {m.get('context', '')}")
        if has_more:
            lines.append(f"\n... showing {len(matches)} of {total} total matches.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  Find Elements (NEW)
    # ------------------------------------------------------------------

    def find_elements(self, selector: str, max_results=50) -> str:
        page = self.current_page
        if not page:
            return "No active page."

        def _do():
            try:
                elements = page.query_selector_all(selector)
            except Exception as e:
                return {
                    "error": f"Invalid CSS selector: {e}",
                    "elements": [],
                    "total": 0,
                }
            total = len(elements)
            limit = min(total, max_results)
            results = []
            for i in range(limit):
                el = elements[i]
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                text = (el.inner_text() or "").strip()
                if len(text) > 120:
                    text = text[:120] + "..."
                item = {"index": i, "tag": tag, "text": text}
                href = el.get_attribute("href")
                if href:
                    item["href"] = href
                name_attr = el.get_attribute("name")
                if name_attr:
                    item["name"] = name_attr
                results.append(item)
            return {"elements": results, "total": total, "showing": limit}

        try:
            data = self._worker.dispatch(_do, timeout=10)
        except Exception as e:
            return f"find_elements failed: {e}"

        if isinstance(data, dict) and data.get("error"):
            return f"find_elements: {data['error']}"

        elements = data.get("elements", [])
        total = data.get("total", 0)
        showing = data.get("showing", 0)

        if total == 0:
            return f'No elements found matching "{selector}".'

        lines = [
            f'Found {total} element{"s" if total != 1 else ""} matching "{selector}":'
        ]
        for el in elements:
            idx = el.get("index", 0)
            tag = el.get("tag", "?")
            text = el.get("text", "")
            parts = [f"[{idx}] <{tag}>"]
            if text:
                parts.append(f'"{text}"')
            attrs = []
            for k in ["href", "name"]:
                if el.get(k):
                    attrs.append(f'{k}="{el[k]}"')
            if attrs:
                parts.append("{" + ", ".join(attrs) + "}")
            lines.append(" ".join(parts))
        if showing < total:
            lines.append(f"\nShowing {showing} of {total} total elements.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  Evaluate JS
    # ------------------------------------------------------------------

    def evaluate(self, expression: str) -> str:
        target = self._target()
        if not target:
            return "No active page."
        try:
            try:
                result = self._worker.dispatch(
                    lambda: target.evaluate(expression), timeout=15
                )
            except Exception as e:
                # Playwright raises its own Error for JS syntax issues, not SyntaxError
                err_str = str(e).lower()
                if "syntaxerror" in err_str or "unexpected token" in err_str:
                    result = self._worker.dispatch(
                        lambda: target.evaluate(f"() => {{ {expression} }}"), timeout=15
                    )
                else:
                    raise
            if result is None:
                return "JavaScript executed (returned null/undefined)"
            if isinstance(result, str):
                return (
                    result if len(result) < 10000 else result[:10000] + "...[truncated]"
                )
            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            return f"JavaScript error: {type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    #  Frame management (enhanced for cross-origin)
    # ------------------------------------------------------------------

    def switch_frame(self, frame_selector=None) -> str:
        page = self.current_page
        if not page:
            return "No active page."
        if not frame_selector:
            self._active_frame = None
            return "Switched back to top-level page."

        def _do():
            frames = page.frames
            sel_lower = frame_selector.lower()
            # 1. Try matching by URL or name in all Playwright frames
            for f in frames:
                if f == page.main_frame:
                    continue
                if (
                    sel_lower in (f.url or "").lower()
                    or sel_lower in (f.name or "").lower()
                ):
                    return f, "playwright"
            # 2. Try CDP frame tree for cross-origin frames
            try:
                cdp_frames = self._cdp.get_all_frame_ids(page)
                for cf in cdp_frames:
                    if (
                        sel_lower in cf.get("url", "").lower()
                        or sel_lower in cf.get("name", "").lower()
                    ):
                        # Found a cross-origin frame — mark it for a11y-based access
                        return cf, "cdp_cross_origin"
            except Exception:
                pass
            return None, None

        try:
            frame, frame_type = self._worker.dispatch(_do, timeout=10)
        except Exception as e:
            return f"Frame switch failed: {e}"

        if frame is None:
            avail = []
            for f in page.frames:
                if f != page.main_frame:
                    avail.append(f"  - name={f.name!r} url={f.url}")
            return (
                f"No frame matching '{frame_selector}'.\nAvailable frames:\n"
                + "\n".join(avail)[:2000]
            )

        if frame_type == "cdp_cross_origin":
            # Store the CDP frame info for snapshot to use
            self._active_frame = frame  # Store the dict, not a Frame object
            self._active_frame_type = "cdp"
            return f"Switched to cross-origin frame: {frame.get('url', '')}"

        self._active_frame = frame
        self._active_frame_type = "playwright"
        try:
            url = frame.url or ""
        except Exception:
            url = ""
        return f"Switched to frame: {url}"

    # ------------------------------------------------------------------
    #  Screenshot
    # ------------------------------------------------------------------

    def screenshot(self, full_page=False) -> Tuple[Optional[bytes], str, Optional[str]]:
        page = self.current_page
        if not page:
            return None, "No active page.", None
        screenshots_dir = Path("~/.kairos/screenshots").expanduser()
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = _time.strftime("%Y%m%d_%H%M%S")
        filepath = screenshots_dir / f"screenshot_{ts}.png"

        def _do():
            png_bytes = page.screenshot(full_page=full_page, type="png")
            filepath.write_bytes(png_bytes)
            data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode(
                "ascii"
            )
            return png_bytes, page.url, len(png_bytes), data_url

        try:
            png_bytes, url, size_bytes, data_url = self._worker.dispatch(
                _do, timeout=30
            )
            msg = f"Screenshot captured ({size_bytes / 1024:.0f} KB) — {url}\nSaved to: {filepath}"
            return png_bytes, msg, data_url
        except Exception as e:
            return None, f"Screenshot failed: {e}", None

    # ------------------------------------------------------------------
    #  Snapshot (enhanced with CDP cross-origin a11y)
    # ------------------------------------------------------------------

    def snapshot(self) -> str:
        target = self._target()
        if not target:
            return "No active page."
        try:
            data = self._worker.dispatch(
                lambda: target.evaluate(_SNAPSHOT_JS), timeout=15
            )
            # Cache elements for index-based interactions
            self._last_snapshot_elements = data.get("elements", [])
            result = self._format_snapshot(data)
            # Append cross-origin iframe a11y data if available
            result += self._get_cross_origin_snapshot_section()
            return result
        except Exception as e:
            try:
                title = self._worker.dispatch(
                    lambda: target.title() if hasattr(target, "title") else "",
                    timeout=5,
                )
                url = self._worker.dispatch(
                    lambda: target.url if hasattr(target, "url") else "", timeout=5
                )
                text = self._worker.dispatch(
                    lambda: (
                        target.inner_text("body")
                        if hasattr(target, "inner_text")
                        else ""
                    ),
                    timeout=10,
                )
                if len(text) > 3000:
                    text = text[:3000] + "\n...[truncated]"
                return f"Page: {title}\nURL: {url}\n\n{text}"
            except Exception as e2:
                return f"Snapshot failed: {e}\nFallback also failed: {e2}"

    def _get_cross_origin_snapshot_section(self) -> str:
        """Get a11y content for cross-origin iframes via CDP."""
        page = self.current_page
        if not page:
            return ""
        try:
            main_url = self._worker.dispatch(lambda: page.url, timeout=5)
            cross_origin_data = self._worker.dispatch(
                lambda: self._cdp.get_cross_origin_iframe_content(page, main_url),
                timeout=15,
            )
        except Exception:
            return ""
        if not cross_origin_data:
            return ""
        lines = ["", "[Cross-Origin Iframes]"]
        for iframe in cross_origin_data:
            url = iframe.get("url", "")
            name = iframe.get("name", "")
            elements = iframe.get("elements", [])
            if not elements:
                continue
            label = name or url[:60]
            lines.append(f"  Frame: {label}")
            for el in elements[:30]:
                role = el.get("role", "")
                name_text = el.get("name", "")
                desc = el.get("description", "")
                is_interactive = el.get("is_interactive", False)
                prefix = "  →" if is_interactive else "    "
                desc_str = f" ({desc})" if desc else ""
                lines.append(f"{prefix} [{role}] {name_text}{desc_str}")
        return "\n".join(lines)

    def _format_snapshot(self, data: Dict[str, Any]) -> str:
        lines = []
        title = data.get("title", "(no title)")
        url = data.get("url", "")
        lines.append(f"[Page] {title}")
        if url:
            lines.append(f"[URL] {url}")
        lines.append("")

        if len(self._pages) > 1:

            def _get_tab_titles():
                result = []
                for i, p in enumerate(self._pages):
                    try:
                        t = p.title() or p.url[:30]
                    except Exception:
                        t = "(error)"
                    result.append(
                        f"{'*' if i == self._current_idx else ' '} Tab {i}: {t}"
                    )
                return result

            try:
                tab_titles = self._worker.dispatch(_get_tab_titles, timeout=10)
            except Exception:
                tab_titles = [
                    f"{'*' if i == self._current_idx else ' '} Tab {i}: (error)"
                    for i in range(len(self._pages))
                ]
            lines.append(f"[Tabs] {' | '.join(tab_titles)}")
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
                    itype = el.get("input_type", "text")
                    placeholder = el.get("placeholder", "")
                    value = el.get("value", "")
                    if itype in ("checkbox", "radio"):
                        label_text = el.get("label", "")
                        display = label_text or text or placeholder or value
                        checked = "✓" if el.get("checked") else "✗"
                        label_sel = el.get("label_selector", "")
                        if label_sel:
                            parts.append(
                                f'{itype}: "{display}" {checked} (label: {label_sel})'
                            )
                        else:
                            parts.append(f'{itype}: "{display}" {checked}')
                    else:
                        display = placeholder or text or itype
                        val_str = f' = "{value}"' if value else ""
                        parts.append(f'Input({itype}): "{display}"{val_str}')
                elif tag == "textarea":
                    parts.append(f'Textarea: "{el.get("placeholder", "")}"')
                elif tag == "select":
                    selected = el.get("selected", "")
                    opts = el.get("options", [])
                    if opts:
                        opt_strs = [
                            f'"{o.get("text", "")}" (val="{o.get("value", "")}"){" *" if o.get("selected") else ""}'
                            for o in opts
                        ]
                        parts.append(
                            f'Select: "{text}" selected="{selected}" options=[{", ".join(opt_strs)}]'
                        )
                    else:
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
                ctx = el.get("context", "")
                if ctx:
                    lines.append(f"       ↳ Q: {ctx}")
            lines.append("")

        text_blocks = data.get("text_blocks", [])
        if text_blocks:
            lines.append("[Text]")
            for t in text_blocks:
                lines.append(f"  {t}")
            lines.append("")

        inputs = [
            e for e in elements if e.get("tag") in ("input", "textarea", "select")
        ]
        if inputs:
            lines.append("[Form State]")
            for inp in inputs:
                tag = inp.get("tag", "")
                sel = inp.get("selector", "?")
                if tag == "input":
                    val = inp.get("value", "")
                    if val:
                        lines.append(f'  {sel} = "{val}"')
                elif tag == "textarea":
                    val = inp.get("value", "")
                    if val:
                        preview = val[:60] + "..." if len(val) > 60 else val
                        lines.append(f'  {sel} = "{preview}"')
                elif tag == "select":
                    lines.append(f'  {sel} = "{inp.get("selected", "")}"')

        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  Tab management
    # ------------------------------------------------------------------

    def open_new_tab(self, url=None) -> str:
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
                try:
                    title = self._worker.dispatch(
                        lambda: page.title() or "(no title)", timeout=5
                    )
                except Exception:
                    title = "(no title)"
                return f"Opened new tab ({len(self._pages)} tabs) — {title} — {url}"
            return f"Opened new tab ({len(self._pages)} tabs) — about:blank"
        except Exception as e:
            return f"Failed to open tab: {e}"

    def switch_tab(self, index=None, url_pattern=None) -> str:
        if not self._pages:
            return "No tabs open."

        def _get_tab_title(page):
            try:
                return self._worker.dispatch(
                    lambda: page.title() or "(no title)", timeout=5
                )
            except Exception:
                return "(error)"

        if url_pattern:
            for i, page in enumerate(self._pages):
                try:
                    page_url = self._worker.dispatch(lambda p=page: p.url, timeout=5)
                except Exception:
                    page_url = ""
                if url_pattern.lower() in page_url.lower():
                    self._current_idx = i
                    return f"Switched to tab {i}: {_get_tab_title(page)} — {page_url}"
            return f"No tab matches URL pattern: '{url_pattern}'"
        if index is None:
            return "Specify tab index or url_pattern."
        if index < 0 or index >= len(self._pages):
            return f"Invalid tab index {index}. Valid: 0-{len(self._pages) - 1}"
        self._current_idx = index
        return f"Switched to tab {index}: {_get_tab_title(self._pages[index])} — {self._pages[index].url}"

    def list_tabs(self) -> str:
        if not self._pages:
            return "No tabs open."

        def _get_all_tab_info():
            result = []
            for i, p in enumerate(self._pages):
                try:
                    t = p.title() or "(no title)"
                except Exception:
                    t = "(error)"
                try:
                    u = p.url
                except Exception:
                    u = "(error)"
                result.append((i, t, u))
            return result

        try:
            tab_data = self._worker.dispatch(_get_all_tab_info, timeout=10)
        except Exception:
            return "Failed to get tab info."
        lines = [
            f"  Tab {i}{'*' if i == self._current_idx else ' '}  {t}  —  {u}"
            for i, t, u in tab_data
        ]
        return f"Open tabs ({len(self._pages)}):\n" + "\n".join(lines)

    def close_tab(self, index=None) -> str:
        if not self._pages:
            return "No tabs open."
        if index is None:
            index = self._current_idx
        if index < 0 or index >= len(self._pages):
            return f"Invalid tab index {index}."
        if len(self._pages) == 1:
            return "Can't close the last tab. Use browser_close instead."
        try:
            p = self._pages[index]
            self._worker.dispatch(lambda: p.close(), timeout=10)
            self._cdp.invalidate_session(p)
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
        try:
            title, url = self._worker.dispatch(
                lambda: (page.title() or "(no title)", page.url), timeout=5
            )
            return f"Title: {title}\nURL: {url}\nTab: {self._current_idx} of {len(self._pages)}\nProfile: {self._profile_name or '(ephemeral)'}"
        except Exception as e:
            return f"Error: {e}"


# ------------------------------------------------------------------
#  Snapshot JS — enhanced with ancestor visibility checking
# ------------------------------------------------------------------

_SNAPSHOT_JS = """() => {
    const result = { title: document.title || '', url: window.location.href, elements: [], headings: [], text_blocks: [] };

    function queryShadow(root, selector, maxDepth) {
        maxDepth = maxDepth || 10;
        const found = [];
        function walk(node, depth) {
            if (depth > maxDepth) return;
            const els = node.querySelectorAll(selector);
            for (let i = 0; i < els.length; i++) found.push(els[i]);
            const allEls = node.querySelectorAll('*');
            for (let i = 0; i < allEls.length; i++) {
                if (allEls[i].shadowRoot) walk(allEls[i].shadowRoot, depth + 1);
            }
        }
        walk(root, 0);
        return found;
    }

    function getSelector(el) {
        if (el.id) return '[id="' + el.id + '"]';
        if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
        if (el.dataset && el.dataset.testid) return '[data-testid="' + el.dataset.testid + '"]';
        const path = [];
        let current = el;
        while (current && current !== document.body && path.length < 4) {
            let selector = current.tagName.toLowerCase();
            if (current.id) {
                path.unshift('[id="' + current.id + '"]');
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
                    selector += ':nth-of-type(' + (siblings.indexOf(current) + 1) + ')';
                }
            }
            path.unshift(selector);
            current = current.parentElement;
        }
        return path.join(' > ');
    }

    function getText(el, maxLen) {
        maxLen = maxLen || 80;
        let text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
        if (text.length > maxLen) text = text.substring(0, maxLen) + '...';
        return text;
    }

    function isVisible(el) {
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        // Walk up ancestors and check for hidden parents
        let current = el.parentElement;
        while (current && current !== document.body) {
            const cs = window.getComputedStyle(current);
            if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
            current = current.parentElement;
        }
        return true;
    }

    function isInViewport(el) {
        // Check if element intersects the viewport (accounting for scrolling)
        const rect = el.getBoundingClientRect();
        const vh = window.innerHeight || document.documentElement.clientHeight;
        const vw = window.innerWidth || document.documentElement.clientWidth;
        // Allow 1000px below viewport for elements that are "scroll-reachable"
        return rect.bottom >= -1000 && rect.top <= vh + 1000 &&
               rect.right >= -1000 && rect.left <= vw + 1000;
    }

    function findQuestionContext(el) {
        const questionSelectors = [
            '.qtext', '.formulation', '.question-text', '.quiz-problem',
            'fieldset', 'legend', '[role="group"]', '[role="radiogroup"]'
        ];
        let current = el;
        let questionText = '';
        for (let i = 0; i < 10 && current && current !== document.body; i++) {
            current = current.parentElement;
            if (!current) break;
            for (const sel of questionSelectors) {
                if (current.matches && current.matches(sel)) {
                    const qtextEl = current.querySelector('.qtext') || current;
                    let text = getText(qtextEl, 300);
                    if (text && text.length > 3) { questionText = text; break; }
                }
            }
            if (questionText) break;
            if (/^H[1-4]$/.test(current.tagName)) {
                const text = getText(current, 150);
                if (text && text.length > 3) { questionText = text; break; }
            }
        }
        // For matching questions: find the left-side label text (e.g. "el fósforo")
        // Walk up from the element to find a row/container with a text-only sibling
        let row = el;
        for (let i = 0; i < 8 && row && row !== document.body; i++) {
            row = row.parentElement;
            if (!row) break;
            const siblings = Array.from(row.children);
            for (const sib of siblings) {
                if (sib === el || sib.contains(el) || el.contains(sib)) continue;
                if (sib.querySelector('select, input, button, textarea')) continue;
                const t = getText(sib, 100);
                if (t && t.length > 1 && !/^\s*Answer \d/.test(t)) {
                    return t + (questionText ? ' → ' + questionText : '');
                }
            }
        }
        return questionText;
    }

    const interactiveSelectors = 'a, button, input, textarea, select, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="radio"], [role="checkbox"], [role="option"], [role="listbox"], [role="combobox"], [role="menuitemcheckbox"], [role="menuitemradio"], [onclick]';
    queryShadow(document, interactiveSelectors).forEach(el => {
        const tag = el.tagName.toLowerCase();
        if (tag === 'input') {
            const inputType = el.type || 'text';
            if (inputType !== 'radio' && inputType !== 'checkbox' && !isVisible(el)) return;
        } else {
            if (!isVisible(el)) return;
        }
        // Soft visibility: note if off-viewport but still in DOM
        const inViewport = isInViewport(el);
        const entry = { tag: tag, selector: getSelector(el), text: getText(el, 60) };
        if (!inViewport) entry._offscreen = true;

        if (tag === 'input' || tag === 'select') {
            const inputType = el.type || (tag === 'select' ? 'select' : 'text');
            if (inputType === 'radio' || inputType === 'checkbox' || tag === 'select') {
                const ctx = findQuestionContext(el);
                if (ctx) entry.context = ctx;
            }
        }
        if (tag === 'input') {
            entry.input_type = el.type || 'text';
            entry.placeholder = el.placeholder || '';
            entry.value = el.value || '';
            entry.checked = el.checked || false;
            if (entry.input_type === 'radio' || entry.input_type === 'checkbox') {
                const inputId = el.id;
                if (inputId) {
                    const label = document.querySelector('label[for="' + inputId + '"]');
                    if (label) entry.label = getText(label, 80);
                }
                if (!entry.label) {
                    const parentLabel = el.closest('label');
                    if (parentLabel) {
                        const clone = parentLabel.cloneNode(true);
                        clone.querySelectorAll('input').forEach(inp => inp.remove());
                        const lt = getText(clone, 80);
                        if (lt) entry.label = lt;
                    }
                }
                const ariaLB = el.getAttribute('aria-labelledby');
                if (ariaLB && !entry.label) {
                    const ariaEl = document.getElementById(ariaLB);
                    if (ariaEl) { const at = getText(ariaEl, 80); if (at) entry.label = at; }
                }
                if (ariaLB) {
                    const ariaEl = document.getElementById(ariaLB);
                    if (ariaEl) entry.label_selector = getSelector(ariaEl);
                }
            }
        } else if (tag === 'textarea') {
            entry.placeholder = el.placeholder || '';
            entry.value = (el.value || '').substring(0, 100);
        } else if (tag === 'select') {
            entry.selected = el.value || '';
            entry.options = Array.from(el.options).slice(0, 20).map(o => ({
                value: o.value, text: o.text.substring(0, 50), selected: o.selected
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

    const labels = queryShadow(document, 'label');
    let labelCount = 0;
    labels.forEach(el => {
        if (labelCount >= 30) return;
        const text = getText(el, 100);
        if (!text || text.length < 2) return;
        const forAttr = el.getAttribute('for');
        const input = forAttr ? document.getElementById(forAttr) : el.querySelector('input');
        if (input) {
            const t = input.type || 'text';
            if (t === 'radio' || t === 'checkbox') return;
        }
        result.text_blocks.push(text);
        labelCount++;
    });

    queryShadow(document, 'h1, h2, h3, h4').forEach(el => {
        if (!isVisible(el)) return;
        const text = getText(el, 100);
        if (text) result.headings.push({ level: el.tagName.toLowerCase(), text: text });
    });

    const textEls = queryShadow(document, 'p, label, li, dt, dd, [role="text"], [role="heading"]');
    let textCount = 0;
    textEls.forEach(el => {
        if (!isVisible(el) || textCount >= 40) return;
        const text = getText(el, 120);
        if (text && text.length > 5) { result.text_blocks.push(text); textCount++; }
    });

    return result;
}
"""
