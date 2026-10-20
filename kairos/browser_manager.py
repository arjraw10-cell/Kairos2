"""
BrowserManager — CloakBrowser stealth Chromium driven by Playwright's async API.

Playwright runs on a dedicated worker thread with its own asyncio loop, so it
never conflicts with the main thread's event loop (rich/prompt_toolkit) and
actions can be raced against JavaScript dialogs.

Design:
  - Snapshots use Playwright's ``aria_snapshot(mode="ai")``: real ARIA roles and
    names, ``[ref=eN]`` handles (``f1eN`` inside iframes and after the page
    navigates), and iframe contents inline.
  - Every action returns the resulting page state. On the same URL only the
    changed snapshot lines are returned; after navigation, the full snapshot.
  - Actions race against ``dialog`` events, so alert/confirm/prompt are reported
    to the agent instead of blocking or being silently dismissed.
  - Always headed and always humanized (CloakBrowser human layer). Refs are
    resolved to element handles first, because CloakBrowser's humanized Locator
    methods cannot resolve ``aria-ref`` selectors; when its humanize engine
    cannot map a handle (iframes), the humanized page mouse/keyboard is used
    at the element's box instead.
  - The browser launches lazily on the first browser tool call and relaunches
    if a human closes the window.
  - Named profiles persist logins and pin a fingerprint seed per profile. They
    are shared with Claude Code's cloakbrowser MCP (cloak-mcp): same root, same
    seed file.
"""

import asyncio
import base64
import json
import os
import random
import re
import threading
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SNAPSHOT_MAX_CHARS = 15_000
ACTION_TIMEOUT_MS = 10_000
_REF = re.compile(r"^(f\d+)?e\d+$")
_HUMANIZE_MISS = "cloakbrowser humanize: cannot identify"
_BUFFER_LIMIT = 500

# Profile directories open in this process. Chromium cannot open one profile
# directory twice, so a second gateway conversation gets a clear error.
_open_profiles: set = set()
_open_profiles_lock = threading.Lock()


def profiles_root() -> Path:
    """cloak-mcp's profiles root, so Kairos and Claude Code share logins."""
    return Path(os.environ.get("CLOAKBROWSER_PROFILES_DIR") or "~/.cloak-browser-mcp/profiles").expanduser()


def _profile_in_use(profile_dir: Path) -> bool:
    """A running Chromium holds <profile>/lockfile open exclusively (Windows). Launching
    anyway hands off to that browser, which opens a stray window, then fails."""
    try:
        os.close(os.open(profile_dir / "lockfile", os.O_WRONLY))
    except PermissionError:
        return True
    except OSError:
        pass
    return False


class StaleRefError(Exception):
    pass


class _Worker:
    """Thread hosting an asyncio loop and a persistent async Playwright instance."""

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self.pw = None
        self._current = None  # concurrent.futures.Future of the running call

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            from playwright.async_api import async_playwright

            self.pw = self._loop.run_until_complete(async_playwright().start())
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True, name="playwright-worker")
        self._thread.start()
        if not ready.wait(timeout=30):
            raise RuntimeError("Playwright worker failed to start")

    def run(self, coro_fn: Callable, timeout: float = 120):
        self.start()
        fut = asyncio.run_coroutine_threadsafe(coro_fn(), self._loop)
        self._current = fut
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            fut.cancel()
            raise TimeoutError(f"Browser operation timed out after {timeout:.0f}s.")
        finally:
            self._current = None

    def cancel_current(self):
        fut = self._current
        if fut is not None:
            fut.cancel()

    def stop(self):
        if not (self._thread and self._thread.is_alive()):
            return

        async def shutdown():
            if self.pw:
                await self.pw.stop()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._thread = None
        self.pw = None


class BrowserManager:
    """One headed, humanized CloakBrowser session per agent."""

    def __init__(self, headless: bool = False):
        # Headed by default (the agent's interactive browser). headless=True is
        # for batch callers that run many instances and never look at them.
        self._headless = headless
        self._worker = _Worker()
        self._lock = threading.Lock()
        self._browser = None
        self._context = None
        self._pages: List[Any] = []
        self._current = None
        self._new_tabs: List[Any] = []
        self._dialog = None
        self._modal_waiters: List[asyncio.Future] = []
        self._network: Dict[Any, List[Dict]] = {}
        self._last_snapshot: Optional[Tuple[Any, str, set]] = None
        self._profile_dir: Optional[Path] = None
        self._proxy: Optional[str] = None
        self._closing: set = set()  # closes of launches that finished after a cancel

    # ------------------------------------------------------------------
    #  Public state
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._context is not None

    @property
    def profile_name(self) -> Optional[str]:
        return self._profile_dir.name if self._profile_dir else None

    def cancel_active_operation(self) -> None:
        """Agent hard stop: cancel the in-flight browser call."""
        self._worker.cancel_current()

    def _run(self, coro_fn: Callable, timeout: float = 120):
        with self._lock:
            return self._worker.run(coro_fn, timeout)

    # ------------------------------------------------------------------
    #  Launch / close
    # ------------------------------------------------------------------

    @staticmethod
    def _cloak():
        try:
            from cloakbrowser import build_args, ensure_binary
            from cloakbrowser.browser import IGNORE_DEFAULT_ARGS
            from cloakbrowser.human import patch_context_async
            from cloakbrowser.human.config import resolve_config
        except ImportError as exc:
            raise ImportError(
                "CloakBrowser with its humanize layer is required: pip install --upgrade cloakbrowser"
            ) from exc
        return build_args, ensure_binary, IGNORE_DEFAULT_ARGS, patch_context_async, resolve_config

    def _profile_seed(self, profile_dir: Path) -> int:
        """Each profile keeps one fingerprint seed so a logged-in identity is always the same device."""
        meta = profile_dir / ".cloak-mcp.json"  # cloak-mcp's file, so both tools present the same device
        try:
            seed = json.loads(meta.read_text(encoding="utf-8"))["seed"]
            if isinstance(seed, int):
                return seed
        except Exception:
            pass
        seed = random.randint(1, 2**31 - 1)
        profile_dir.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"seed": seed}, indent=2), encoding="utf-8")
        return seed

    def _close_later(self, opened) -> None:
        """Close a browser or context in the background (also from a cancelled call)."""
        async def close():
            try:
                await opened.close()
            except Exception:
                pass

        closing = asyncio.ensure_future(close())
        self._closing.add(closing)  # asyncio keeps only weak references to tasks
        closing.add_done_callback(self._closing.discard)

    async def _launch(self, start):
        """Await a browser launch. If this call is cancelled meanwhile, the
        launch still finishes in Playwright; close what it opens then."""
        task = asyncio.ensure_future(start)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            def discard(t):
                if not t.cancelled() and t.exception() is None:
                    self._close_later(t.result())

            task.add_done_callback(discard)
            raise

    async def _ensure(self):
        if self._context is not None:
            return self._context
        build_args, ensure_binary, ignore_args, patch_context_async, resolve_config = self._cloak()
        pw = self._worker.pw
        extra = []
        if self._profile_dir:
            extra.append(f"--fingerprint={self._profile_seed(self._profile_dir)}")
        kwargs: Dict[str, Any] = {
            "headless": self._headless,
            "executable_path": ensure_binary(),
            "ignore_default_args": list(ignore_args),
            "args": build_args(stealth_args=True, extra_args=extra, headless=self._headless),
            "viewport": {"width": 1280, "height": 720},
        }
        if self._proxy:
            kwargs["proxy"] = {"server": self._proxy}

        key = str(self._profile_dir.resolve()) if self._profile_dir else None
        if key:
            with _open_profiles_lock:
                if key in _open_profiles:
                    raise RuntimeError(
                        f"Profile '{self._profile_dir.name}' is already open in another Kairos browser "
                        "(another conversation). Close it there, or use a different profile."
                    )
                _open_profiles.add(key)
        browser = ctx = None
        try:
            if key:
                in_use = (f"Profile '{self._profile_dir.name}' is in use by another browser (such as Claude Code's "
                          "cloakbrowser MCP). Close it there, or use a different profile.")
                if _profile_in_use(self._profile_dir):
                    raise RuntimeError(in_use)
                try:
                    ctx = await self._launch(pw.chromium.launch_persistent_context(str(self._profile_dir), **kwargs))
                except Exception as e:
                    if "existing browser session" in str(e):
                        raise RuntimeError(in_use)
                    raise RuntimeError(
                        f"Could not open profile '{self._profile_dir.name}' (it may be open in another "
                        f"browser window): {str(e).splitlines()[0]}"
                    )
            else:
                viewport = kwargs.pop("viewport")
                proxy = kwargs.pop("proxy", None)
                browser = await self._launch(pw.chromium.launch(**kwargs))
                ctx = await browser.new_context(viewport=viewport, **({"proxy": proxy} if proxy else {}))
            patch_context_async(ctx, resolve_config("default", None))
            ctx.on("page", self._on_new_page)
            ctx.on("close", lambda _ctx: self._reset() if self._context is ctx else None)
        except BaseException:
            # Also on Esc or the call timeout (CancelledError): otherwise the
            # profile stays locked for the rest of the process.
            if key:
                with _open_profiles_lock:
                    _open_profiles.discard(key)
            for opened in (ctx, browser):
                if opened is not None:
                    self._close_later(opened)
            raise

        self._browser = browser
        self._context = ctx
        for p in ctx.pages:
            self._attach(p)
        self._current = self._pages[0] if self._pages else None
        return ctx

    def _on_new_page(self, page):
        self._attach(page)
        self._new_tabs.append(page)

    def _attach(self, page):
        if page in self._pages:
            return
        self._pages.append(page)
        network: List[Dict] = []
        self._network[page] = network

        def push(buf, item):
            buf.append(item)
            if len(buf) > _BUFFER_LIMIT:
                del buf[0]

        by_request: Dict[Any, Dict] = {}

        def on_request(req):
            entry = {"method": req.method, "url": req.url, "type": req.resource_type, "status": None,
                     "request_headers": req.headers, "response_headers": None,
                     "duration_ms": None, "failure": None, "time": _time.strftime("%H:%M:%S")}
            by_request[req] = entry
            push(network, entry)

        def on_response(resp):
            entry = by_request.get(resp.request)
            if entry:
                entry["status"] = resp.status
                entry["response_headers"] = resp.headers

        def on_finished(req):
            entry = by_request.pop(req, None)
            end = (req.timing or {}).get("responseEnd", -1)
            if entry and end and end > 0:
                entry["duration_ms"] = round(end)

        def on_failed(req):
            entry = by_request.pop(req, None)
            if entry:
                entry["failure"] = req.failure or "failed"

        def on_dialog(dialog):
            self._dialog = dialog
            self._notify_modal()

        def on_close(_page):
            if page in self._pages:
                self._pages.remove(page)
            if page in self._new_tabs:
                self._new_tabs.remove(page)
            if self._current is page:
                self._current = self._pages[-1] if self._pages else None

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfinished", on_finished)
        page.on("requestfailed", on_failed)
        page.on("dialog", on_dialog)
        page.on("close", on_close)

    def _notify_modal(self):
        for fut in self._modal_waiters:
            if not fut.done():
                fut.set_result(True)
        self._modal_waiters.clear()

    def _reset(self):
        if self._profile_dir:
            with _open_profiles_lock:
                _open_profiles.discard(str(self._profile_dir.resolve()))
        self._browser = None
        self._context = None
        self._pages = []
        self._current = None
        self._new_tabs = []
        self._dialog = None
        self._network = {}
        self._last_snapshot = None

    async def _page(self):
        ctx = await self._ensure()
        if self._current is None or self._current.is_closed():
            self._current = next((p for p in self._pages if not p.is_closed()), None) or await ctx.new_page()
            if self._current in self._new_tabs:
                self._new_tabs.remove(self._current)
        return self._current

    async def _close(self):
        ctx, browser = self._context, self._browser
        self._reset()
        for closer in (ctx, browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass

    def close(self) -> str:
        if not self.is_open:
            self._worker.stop()
            return "No browser is running."
        try:
            self._run(self._close, timeout=30)
        finally:
            self._worker.stop()
        return "Browser closed."

    def profile(self, action: str, name: Optional[str] = None, proxy: Optional[str] = None) -> str:
        """list | use <name> | ephemeral. use/ephemeral relaunch the browser."""
        if action == "list":
            root = profiles_root()
            names = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []
            active = self.profile_name or "(ephemeral)"
            listing = "\n".join(f"  {n}" for n in names) or "  (none)"
            return f"Active: {active}\nProfiles in {root}:\n{listing}"
        if action not in ("use", "ephemeral"):
            raise ValueError("action must be list, use or ephemeral")
        if action == "use":
            if not name or not re.fullmatch(r"[\w-]+", name):
                raise ValueError("use needs a profile name of letters, digits, _ or -")
            target = profiles_root() / name
        else:
            target = None

        async def relaunch():
            await self._close()
            self._profile_dir = target
            self._proxy = proxy
            await self._page()
            label = f"profile '{name}'" if target else "an ephemeral profile"
            return f"Relaunched in {label}.\n\n{await self._state('full')}"

        return self._run(relaunch)

    # ------------------------------------------------------------------
    #  Snapshot and page state
    # ------------------------------------------------------------------

    async def _snapshot_text(self, page, mode: str) -> str:
        snap = await page.aria_snapshot(mode="ai", timeout=10_000)
        lines = snap.split("\n")
        keys = {line.strip() for line in lines}  # ignore re-indentation from inserted wrappers
        prev = self._last_snapshot
        self._last_snapshot = (page, page.url, keys)
        if mode == "diff" and prev and prev[0] is page and prev[1] == page.url:
            added = [line for line in lines if line.strip() not in prev[2]]
            removed = len(prev[2] - keys)
            if not added and not removed:
                return "Snapshot: unchanged since the last one."
            diff = "\n".join(added)
            if len(diff) < SNAPSHOT_MAX_CHARS // 2:
                return (f"Snapshot changes (new or updated lines; {removed} lines gone; unchanged parts "
                        f"omitted, browser_snapshot shows the full page):\n{diff}")
        if len(snap) > SNAPSHOT_MAX_CHARS:
            snap = (snap[:SNAPSHOT_MAX_CHARS] + f"\n... [snapshot truncated: {len(snap) - SNAPSHOT_MAX_CHARS} "
                    "more chars. Scroll, or use browser_search_page / browser_evaluate for specific content]")
        return f"Snapshot:\n{snap}"

    async def _state(self, mode: str = "full") -> str:
        page = self._current
        if page is None or page.is_closed():
            return "No open page."
        lines = []
        opened = [p for p in self._new_tabs if not p.is_closed()]
        if opened:
            # Match the old behavior: follow a tab the action opened.
            self._current = page = opened[-1]
            lines.append(f"New tab opened by the page; switched to it: {page.url}")
            self._new_tabs = []
        if len(self._pages) > 1:
            lines.append(f"Tabs: {len(self._pages)} open, current is [{self._pages.index(page)}]")
        if self._dialog is not None:
            d = self._dialog
            lines.append(f'Dialog: {d.type} "{d.message[:300]}" is blocking the page. '
                         "Call browser_handle_dialog before anything else.")
            return "\n".join([f"Page URL: {page.url}"] + lines)
        try:
            title = await page.title()
        except Exception:
            title = ""
        lines = [f"Page URL: {page.url}", f"Page title: {title}"] + lines
        if mode != "none":
            try:
                lines.append(await self._snapshot_text(page, mode))
            except Exception as e:
                lines.append(f"(snapshot unavailable: {str(e).splitlines()[0]})")
        return "\n".join(lines)

    def snapshot(self) -> str:
        async def do():
            await self._page()
            return await self._state("full")

        return self._run(do)

    # ------------------------------------------------------------------
    #  Action plumbing
    # ------------------------------------------------------------------

    @staticmethod
    def _explain(e: Exception, ref: Optional[str]) -> str:
        if isinstance(e, StaleRefError):
            return (f"Ref {ref} no longer exists: the page changed since the last snapshot. "
                    "Use a ref from the fresh snapshot below.")
        first = (str(e).splitlines() or [type(e).__name__])[0]
        if "Timeout" in first:
            return f"{first} The element may be hidden, covered, or not rendered yet; check the snapshot below."
        return first

    async def _act(self, fn: Callable, ref: Optional[str] = None, mode: str = "diff") -> str:
        """Run an action raced against dialogs, then report the resulting page state."""
        page = await self._page()
        if self._dialog is not None:
            raise RuntimeError(f'A {self._dialog.type} dialog is open ("{self._dialog.message[:200]}"). '
                               "Call browser_handle_dialog first.")
        modal = asyncio.get_running_loop().create_future()
        self._modal_waiters.append(modal)
        work = asyncio.ensure_future(fn(page))
        try:
            done, _ = await asyncio.wait({work, modal}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                summary = work.result()
                await self._settle(self._current or page)
            else:
                # The action keeps running behind the dialog; it finishes once the dialog is handled.
                work.add_done_callback(lambda t: t.cancelled() or t.exception())
                summary = "The action opened a dialog."
            return f"{summary}\n\n{await self._state(mode)}"
        except asyncio.CancelledError:
            work.cancel()
            raise
        except Exception as e:
            raise RuntimeError(f"{self._explain(e, ref)}\n\n{await self._state('full')}") from None
        finally:
            if modal in self._modal_waiters:
                self._modal_waiters.remove(modal)

    @staticmethod
    async def _settle(page):
        """Let a navigation triggered by the action commit and reach DOMContentLoaded."""
        await asyncio.sleep(0.4)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass

    @staticmethod
    async def _handle(page, ref: str):
        loc = page.locator(f"aria-ref={ref}") if _REF.match(ref) else page.locator(ref)
        if _REF.match(ref):
            try:
                count = await loc.count()
            except Exception:  # unknown frame prefix
                count = 0
            if count == 0:
                raise StaleRefError(ref)
        handle = await loc.first.element_handle(timeout=ACTION_TIMEOUT_MS)
        if handle is None:
            raise StaleRefError(ref)
        return handle

    @staticmethod
    async def _center(handle) -> Tuple[float, float]:
        await handle.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})")
        box = await handle.bounding_box()
        if not box:
            raise RuntimeError("Element has no visible box (hidden or detached).")
        jitter = lambda size: (random.random() - 0.5) * min(size * 0.3, 10)
        return box["x"] + box["width"] / 2 + jitter(box["width"]), box["y"] + box["height"] / 2 + jitter(box["height"])

    @staticmethod
    async def _humanized(action: Callable, fallback: Callable):
        """Prefer CloakBrowser's humanized handle action; when its engine cannot map the
        element (e.g. inside iframes), use the humanized page mouse/keyboard instead."""
        try:
            return await action()
        except Exception as e:
            if _HUMANIZE_MISS not in str(e):
                raise
            return await fallback()

    async def _click(self, page, ref: str, button: str = "left", double: bool = False):
        h = await self._handle(page, ref)

        async def native():
            if double:
                await h.dblclick(button=button, timeout=ACTION_TIMEOUT_MS)
            else:
                await h.click(button=button, timeout=ACTION_TIMEOUT_MS)

        async def by_mouse():
            x, y = await self._center(h)
            await page.mouse.click(x, y, button=button, click_count=2 if double else 1)

        await self._humanized(native, by_mouse)

    async def _type(self, page, ref: str, text: str):
        h = await self._handle(page, ref)

        async def by_keyboard():
            x, y = await self._center(h)
            await page.mouse.click(x, y)
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(text)

        await self._humanized(lambda: h.fill(text, timeout=ACTION_TIMEOUT_MS), by_keyboard)

    # ------------------------------------------------------------------
    #  Actions
    # ------------------------------------------------------------------

    def navigate(self, url: str) -> str:
        async def go(page):
            self._network.get(page, []).clear()
            if url == "back":
                await page.go_back(wait_until="domcontentloaded", timeout=60_000)
            elif url == "forward":
                await page.go_forward(wait_until="domcontentloaded", timeout=60_000)
            elif url == "reload":
                await page.reload(wait_until="domcontentloaded", timeout=60_000)
            else:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            return f"Navigated ({url})."

        return self._run(lambda: self._act(go, mode="full"))

    def click(self, ref: Optional[str] = None, button: str = "left", double: bool = False,
              x: Optional[float] = None, y: Optional[float] = None) -> str:
        async def do(page):
            if ref:
                await self._click(page, ref, button, double)
                return f"Clicked {ref}."
            if x is None or y is None:
                raise ValueError("Pass a ref, or x and y.")
            await page.mouse.click(x, y, button=button, click_count=2 if double else 1)
            return f"Clicked at ({x}, {y})."

        return self._run(lambda: self._act(do, ref))

    def type(self, ref: str, text: str, submit: bool = False) -> str:
        async def do(page):
            await self._type(page, ref, text)
            if submit:
                await page.keyboard.press("Enter")
            return f"Typed into {ref}{' and pressed Enter' if submit else ''}."

        # Humanized typing runs at human speed, so long text needs more time.
        return self._run(lambda: self._act(do, ref), timeout=120 + len(text) * 0.5)

    def fill_form(self, fields: List[Dict[str, str]]) -> str:
        async def do(page):
            for f in fields:
                ref, kind, value = f["ref"], f.get("type", "textbox"), str(f["value"])
                if kind == "textbox":
                    await self._type(page, ref, value)
                elif kind == "combobox":
                    await (await self._handle(page, ref)).select_option(value, timeout=ACTION_TIMEOUT_MS)
                elif kind in ("checkbox", "radio"):
                    await (await self._handle(page, ref)).set_checked(value.lower() == "true", timeout=ACTION_TIMEOUT_MS)
                else:
                    raise ValueError(f"Unknown field type {kind!r}")
            return f"Filled {len(fields)} field(s)."

        chars = sum(len(str(f.get("value", ""))) for f in fields)
        return self._run(lambda: self._act(do), timeout=120 + chars * 0.5)

    def select(self, ref: str, values: List[str]) -> str:
        async def do(page):
            picked = await (await self._handle(page, ref)).select_option(values, timeout=ACTION_TIMEOUT_MS)
            return f"Selected {', '.join(picked)}."

        return self._run(lambda: self._act(do, ref))

    def hover(self, ref: str) -> str:
        async def do(page):
            h = await self._handle(page, ref)

            async def by_mouse():
                x, y = await self._center(h)
                await page.mouse.move(x, y)

            await self._humanized(lambda: h.hover(timeout=ACTION_TIMEOUT_MS), by_mouse)
            return f"Hovered {ref}."

        return self._run(lambda: self._act(do, ref))

    def drag(self, from_ref: Optional[str] = None, to_ref: Optional[str] = None,
             x1: Optional[float] = None, y1: Optional[float] = None,
             x2: Optional[float] = None, y2: Optional[float] = None) -> str:
        async def do(page):
            if from_ref and to_ref:
                start = await self._center(await self._handle(page, from_ref))
                end = await self._center(await self._handle(page, to_ref))
            elif None not in (x1, y1, x2, y2):
                start, end = (x1, y1), (x2, y2)
            else:
                raise ValueError("Pass from_ref and to_ref, or x1, y1, x2, y2.")
            await page.mouse.move(*start)
            await page.mouse.down()
            await page.mouse.move(*end, steps=12)
            await page.mouse.up()
            return "Dragged."

        return self._run(lambda: self._act(do, from_ref))

    def press_key(self, key: str) -> str:
        async def do(page):
            await page.keyboard.press(key)
            return f"Pressed {key}."

        return self._run(lambda: self._act(do))

    def scroll(self, direction: str = "down", pages: float = 1.0, ref: Optional[str] = None) -> str:
        async def do(page):
            if ref:
                await page.mouse.move(*(await self._center(await self._handle(page, ref))))
            height = (page.viewport_size or {"height": 720})["height"]
            dy = height * pages * (-1 if direction == "up" else 1)
            await page.mouse.wheel(0, dy)
            return f"Scrolled {direction} {pages} page(s)."

        return self._run(lambda: self._act(do, ref))

    def wait_for(self, text: Optional[str] = None, text_gone: Optional[str] = None,
                 seconds: Optional[float] = None, timeout: float = 30) -> str:
        async def do(page):
            if seconds:
                await asyncio.sleep(min(float(seconds), 60))
            if text:
                await page.get_by_text(text).first.wait_for(state="visible", timeout=timeout * 1000)
            if text_gone:
                await page.get_by_text(text_gone).first.wait_for(state="hidden", timeout=timeout * 1000)
            if not (seconds or text or text_gone):
                raise ValueError("Pass text, text_gone, or seconds.")
            return "Wait finished."

        return self._run(lambda: self._act(do), timeout=timeout + 70)

    def evaluate(self, expression: str, ref: Optional[str] = None) -> str:
        async def do(page):
            if ref:
                result = await (await self._handle(page, ref)).evaluate(expression)
            else:
                result = await page.evaluate(expression)
            text = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
            return f"Result:\n{text}"

        return self._run(lambda: self._act(do, ref, mode="none"))

    def handle_dialog(self, accept: bool, prompt_text: Optional[str] = None) -> str:
        async def do():
            d = self._dialog
            if d is None:
                return "No dialog is open."
            self._dialog = None
            try:
                if accept:
                    await (d.accept(prompt_text) if prompt_text is not None else d.accept())
                else:
                    await d.dismiss()
            except Exception:
                pass
            await self._settle(self._current)
            return f"{'Accepted' if accept else 'Dismissed'} {d.type} dialog.\n\n{await self._state('diff')}"

        return self._run(do)

    def tabs(self, action: str = "list", index: Optional[int] = None, url: Optional[str] = None) -> str:
        async def do():
            ctx = await self._ensure()
            await self._page()
            if action == "new":
                page = await ctx.new_page()
                if page in self._new_tabs:
                    self._new_tabs.remove(page)
                self._current = page
                if url:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            elif action == "select":
                if index is None or not 0 <= index < len(self._pages):
                    raise ValueError(f"No tab at index {index}; {len(self._pages)} open.")
                self._current = self._pages[index]
                await self._current.bring_to_front()
            elif action == "close":
                target = self._current if index is None else (self._pages[index] if 0 <= index < len(self._pages) else None)
                if target is None:
                    raise ValueError(f"No tab at index {index}.")
                await target.close()
                await self._page()
            elif action != "list":
                raise ValueError("action must be list, new, select or close")
            listing = "\n".join(f"{'*' if p is self._current else ' '} [{i}] {p.url}" for i, p in enumerate(self._pages))
            return f"{listing}\n\n{await self._state('none' if action == 'list' else 'full')}"

        return self._run(do)

    def screenshot(self, full_page: bool = False, ref: Optional[str] = None) -> Tuple[Optional[bytes], str, Optional[str]]:
        async def do():
            page = await self._page()
            if ref:
                return await (await self._handle(page, ref)).screenshot(type="png"), page.url
            return await page.screenshot(full_page=full_page, type="png"), page.url

        try:
            png, url = self._run(do)
        except Exception as e:
            return None, f"Screenshot failed: {str(e).splitlines()[0]}", None
        folder = Path("~/.kairos/screenshots").expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        # Microseconds: screenshots taken within one second must not overwrite each other.
        path = folder / f"screenshot_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
        path.write_bytes(png)
        data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        return png, f"Screenshot captured ({len(png) / 1024:.0f} KB) — {url}\nSaved to: {path}", data_url

    def search_page(self, pattern: str, regex: bool = False, case_sensitive: bool = False, max_results: int = 20) -> str:
        js = """([pattern, isRegex, caseSensitive, max]) => {
            const text = document.body ? document.body.innerText : '';
            let re;
            try {
                re = new RegExp(isRegex ? pattern : pattern.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'),
                                caseSensitive ? 'g' : 'gi');
            } catch (e) { return {error: 'Invalid regex: ' + e.message}; }
            const matches = []; let total = 0, m;
            while ((m = re.exec(text)) !== null) {
                total++;
                if (matches.length < max) {
                    const s = Math.max(0, m.index - 40), e = Math.min(text.length, m.index + m[0].length + 40);
                    matches.push((s > 0 ? '...' : '') + text.slice(s, e).replace(/\\s+/g, ' ') + (e < text.length ? '...' : ''));
                }
                if (m[0].length === 0) re.lastIndex++;
            }
            return {matches, total};
        }"""

        async def do():
            page = await self._page()
            return await page.evaluate(js, [pattern, regex, case_sensitive, max_results])

        data = self._run(do)
        if data.get("error"):
            return f"search_page: {data['error']}"
        total = data["total"]
        if total == 0:
            return f'No matches for "{pattern}" on the page.'
        lines = [f'Found {total} match{"es" if total != 1 else ""} for "{pattern}":']
        lines += [f"[{i + 1}] {m}" for i, m in enumerate(data["matches"])]
        if total > len(data["matches"]):
            lines.append(f"... showing {len(data['matches'])} of {total}.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  Console and network
    # ------------------------------------------------------------------

    async def _console_replay(self, page) -> List[Dict]:
        """CloakBrowser suppresses the live console stream (Runtime.enable is a bot signal),
        but enabling Runtime briefly makes Chrome replay the current document's console
        messages and uncaught exceptions. Enable, collect, disable."""
        session = await page.context.new_cdp_session(page)
        msgs: List[Dict] = []

        def where(stack):
            frame = ((stack or {}).get("callFrames") or [{}])[0]
            return frame.get("url", ""), frame.get("lineNumber", 0) + 1, frame.get("columnNumber", 0) + 1

        def on_console(p):
            text = " ".join(str(a.get("value", a.get("description", a.get("type", "")))) for a in p.get("args", []))
            url, line, col = where(p.get("stackTrace"))
            level = {"warning": "warn"}.get(p.get("type", "log"), p.get("type", "log"))
            msgs.append({"level": level, "text": text, "url": url, "line": line, "column": col})

        def on_exception(p):
            d = p.get("exceptionDetails", {})
            text = (d.get("exception") or {}).get("description") or d.get("text", "Uncaught exception")
            url, line, col = where(d.get("stackTrace"))
            msgs.append({"level": "pageerror", "text": text, "url": url or d.get("url", ""), "line": line, "column": col})

        session.on("Runtime.consoleAPICalled", on_console)
        session.on("Runtime.exceptionThrown", on_exception)
        try:
            await session.send("Runtime.enable")
            await asyncio.sleep(0.2)  # replayed events arrive right after enable
            await session.send("Runtime.disable")
        finally:
            await session.detach()
        return msgs[-_BUFFER_LIMIT:]

    def console(self, index: Optional[int] = None, errors_only: bool = False, max_results: int = 50) -> str:
        async def do():
            return await self._console_replay(await self._page())

        msgs = self._run(do)
        if index is not None:
            if not 0 <= index < len(msgs):
                return f"No console message at index {index} ({len(msgs)} captured)."
            m = msgs[index]
            return (f"Level:  {m['level'].upper()}\nText:   {m['text']}\nURL:    {m['url']}\n"
                    f"Line:   {m['line']}:{m['column']}")
        rows = [(i, m) for i, m in enumerate(msgs) if not errors_only or m["level"] in ("error", "pageerror")]
        if not rows:
            return "No console messages captured."
        out = []
        for i, m in rows[-max_results:]:
            text = m["text"][:160] + ("..." if len(m["text"]) > 160 else "")
            where = f" {m['url'].rsplit('/', 1)[-1]}:{m['line']}" if m["url"] else ""
            out.append(f"[{i}] {m['level'].upper()}: {text}{where}")
        return "\n".join(out)

    def network(self, index: Optional[int] = None, filter: Optional[str] = None, max_results: int = 50) -> str:
        async def do():
            return list(self._network.get(await self._page(), []))

        reqs = self._run(do)
        if index is not None:
            if not 0 <= index < len(reqs):
                return f"No request at index {index} ({len(reqs)} captured)."
            r = reqs[index]
            lines = [f"Method:   {r['method']}", f"URL:      {r['url']}", f"Type:     {r['type']}",
                     f"Status:   {r['status'] if r['status'] is not None else r['failure'] or 'pending'}"]
            if r["duration_ms"] is not None:
                lines.append(f"Duration: {r['duration_ms']}ms")
            for title, headers in (("Request headers", r["request_headers"]), ("Response headers", r["response_headers"])):
                if headers:
                    lines.append(f"{title}:")
                    lines += [f"  {k}: {v}" for k, v in headers.items()]
            return "\n".join(lines)
        rows = [(i, r) for i, r in enumerate(reqs) if not filter or filter in r["url"]]
        if not rows:
            return "No network requests captured."
        out = []
        for i, r in rows[-max_results:]:
            status = r["status"] if r["status"] is not None else (f"FAILED {r['failure']}" if r["failure"] else "pending")
            dur = f" {r['duration_ms']}ms" if r["duration_ms"] is not None else ""
            # Elide the middle: the tail (file, endpoint, query) is the informative part.
            url = r["url"] if len(r["url"]) <= 100 else f"{r['url'][:50]}...{r['url'][-47:]}"
            out.append(f"[{i}] {r['method']} {status} {r['type']} {url}{dur}")
        return "\n".join(out)
