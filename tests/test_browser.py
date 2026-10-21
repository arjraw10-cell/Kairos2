"""End-to-end BrowserManager tests against local HTML fixtures.

These launch a real headed CloakBrowser, so they need the CloakBrowser binary and
a display. Set KAIROS_SKIP_BROWSER_TESTS=1 to skip them.
"""

import os
import re
import time
import unittest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
PAGE = (FIXTURES / "browser_page.html").as_uri()
IFRAME = (FIXTURES / "browser_iframe.html").as_uri()

try:
    import cloakbrowser  # noqa: F401
    HAVE_CLOAK = True
except ImportError:
    HAVE_CLOAK = False


def ref(text: str, pattern: str) -> str:
    """Ref of the first snapshot line matching pattern, e.g. ref(snap, r'button "Sign in"')."""
    m = re.search(pattern + r"[^\n]*\[ref=((?:f\d+)?e\d+)\]", text)
    if not m:
        raise AssertionError(f"no ref for {pattern!r} in:\n{text[:2000]}")
    return m.group(1)


@unittest.skipIf(os.environ.get("KAIROS_SKIP_BROWSER_TESTS") or not HAVE_CLOAK,
                 "real-browser tests disabled or CloakBrowser missing")
class BrowserManagerTests(unittest.TestCase):
    """Tests run in name order and share one browser to keep the suite fast."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from unittest import mock

        from kairos.browser_manager import BrowserManager
        cls.root = tempfile.TemporaryDirectory()  # never the real (shared with Claude Code) profiles
        cls.env = mock.patch.dict(os.environ, {"CLOAKBROWSER_PROFILES_DIR": cls.root.name})
        cls.env.start()
        cls.bm = BrowserManager()

    @classmethod
    def tearDownClass(cls):
        cls.bm.close()
        cls.env.stop()
        cls.root.cleanup()

    def fresh_page(self) -> str:
        return self.bm.navigate(PAGE)

    def test_01_first_call_launches_browser(self):
        self.assertIn("Page URL", self.bm.snapshot())
        self.assertTrue(self.bm.is_open)

    def test_02_navigate_returns_refs_with_real_names(self):
        out = self.fresh_page()
        self.assertIn("Page title: Browser test", out)
        for pattern in (r"listitem", r'textbox "Search"', r'combobox "Color"', r'checkbox "Agree"', r'button "Sign in"'):
            ref(out, pattern)

    def test_03_click_listitem_ref(self):
        out = self.bm.click(ref=ref(self.fresh_page(), r"listitem"))
        self.assertIn("Page title: li-clicked", out)

    def test_04_same_page_action_returns_diff_not_full_snapshot(self):
        full = self.fresh_page()
        out = self.bm.click(ref=ref(full, r"listitem"))
        self.assertTrue("Snapshot changes" in out or "unchanged" in out, out)
        self.assertLess(len(out), len(full))

    def test_05_fill_form_textbox_select_checkbox(self):
        snap = self.fresh_page()
        self.bm.fill_form([
            {"ref": ref(snap, r'textbox "Search"'), "type": "textbox", "value": "hello world"},
            {"ref": ref(snap, r'combobox "Color"'), "type": "combobox", "value": "Blue"},
            {"ref": ref(snap, r'checkbox "Agree"'), "type": "checkbox", "value": "true"},
        ])
        out = self.bm.evaluate("() => [document.querySelector('#q').value, document.querySelector('#c').value, document.querySelector('#agree').checked]")
        self.assertIn('"hello world"', out)
        self.assertIn('"Blue"', out)
        self.assertIn("true", out)

    def test_06_type_and_submit(self):
        snap = self.fresh_page()
        out = self.bm.type(ref(snap, r'textbox "Search"'), "abc", submit=True)
        self.assertIn("pressed Enter", out)

    def test_07_alert_returns_promptly_and_blocks_until_handled(self):
        snap = self.fresh_page()
        start = time.time()
        out = self.bm.click(ref=ref(snap, r'button "Alert me"'))
        self.assertLess(time.time() - start, 20)
        self.assertIn('Dialog: alert "hello"', out)
        self.assertIn("Dialog: alert", self.bm.snapshot())
        with self.assertRaises(RuntimeError):
            self.bm.click(ref=ref(snap, r'button "Sign in"'))
        out = self.bm.handle_dialog(accept=True)
        self.assertIn("Accepted alert dialog", out)
        self.assertNotIn("Dialog:", self.bm.snapshot())

    def test_08_confirm_answer_reaches_the_page(self):
        # Before this rewrite Playwright silently dismissed confirm() because nothing listened.
        snap = self.fresh_page()
        self.bm.click(ref=ref(snap, r'button "Confirm me"'))
        self.assertIn("Page title: confirmed", self.bm.handle_dialog(accept=True))
        self.bm.click(ref=ref(self.bm.snapshot(), r'button "Confirm me"'))
        self.assertIn("Page title: cancelled", self.bm.handle_dialog(accept=False))

    def test_09_stale_ref_fails_fast_with_fresh_snapshot(self):
        old = ref(self.fresh_page(), r"listitem")
        self.bm.navigate("reload")
        for stale in (old, "f9e999"):
            start = time.time()
            with self.assertRaises(RuntimeError) as cm:
                self.bm.click(ref=stale)
            self.assertLess(time.time() - start, 3)
            self.assertIn("no longer exists", str(cm.exception))
            self.assertIn("Snapshot:", str(cm.exception))

    def test_10_iframe_refs_type_and_click(self):
        snap = self.bm.navigate(IFRAME)
        user, button = ref(snap, r'textbox "Username"'), ref(snap, r'button "Sign in"')
        self.assertTrue(user.startswith("f"), user)
        self.bm.type(user, "alice")
        self.assertIn("Page title: iframe-clicked:alice", self.bm.click(ref=button))

    def test_11_refs_after_link_navigation(self):
        snap = self.bm.navigate(IFRAME)
        out = self.bm.click(ref=ref(snap, r'link "Go to page 2"'))
        self.assertIn("Page title: Browser test", out)
        out = self.bm.click(ref=ref(out, r"listitem"))
        self.assertIn("Page title: li-clicked", out)

    def test_12_console_and_network_are_captured_from_page_load(self):
        self.fresh_page()
        listing = self.bm.console()
        self.assertIn("fixture-loaded", listing)
        self.assertIn("fixture-error", self.bm.console(errors_only=True))
        self.assertNotIn("fixture-loaded", self.bm.console(errors_only=True))
        self.assertIn("browser_page.html", self.bm.network())
        self.assertIn("Method:", self.bm.network(index=0))

    def test_13_search_page_and_screenshot(self):
        self.fresh_page()
        self.assertIn("Found 1 match", self.bm.search_page("intro paragraph"))
        png, message, data_url = self.bm.screenshot()
        self.assertTrue(png and data_url.startswith("data:image/png;base64,"), message)

    def test_14_tabs_new_select_close(self):
        self.fresh_page()
        out = self.bm.tabs("new", url=IFRAME)
        self.assertIn("[1]", out)
        self.assertIn("Iframe login test", out)
        self.assertIn("Browser test", self.bm.tabs("select", index=0))
        self.bm.tabs("close", index=1)
        self.assertNotIn("[1]", self.bm.tabs("list"))

    def test_15_profile_list(self):
        self.assertIn("Active: (ephemeral)", self.bm.profile("list"))

    def test_16_relaunch_after_close(self):
        self.assertEqual(self.bm.close(), "Browser closed.")
        self.assertFalse(self.bm.is_open)
        self.assertIn("Page title: Browser test", self.fresh_page())

    def test_17_hard_stop_cancels_in_flight_call(self):
        import threading

        threading.Timer(1.0, self.bm.cancel_active_operation).start()
        start = time.time()
        with self.assertRaises(Exception):
            self.bm.wait_for(seconds=30)
        self.assertLess(time.time() - start, 10)
        self.assertIn("Page URL", self.bm.snapshot())  # still usable afterwards

    def test_18_profiles_pin_seed_and_refuse_double_open(self):
        import json
        import tempfile
        from unittest import mock

        from kairos.browser_manager import BrowserManager

        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"CLOAKBROWSER_PROFILES_DIR": root}):
            other = BrowserManager()
            try:
                self.assertIn("Relaunched in profile 'demo'", self.bm.profile("use", "demo"))
                seed = json.loads((Path(root) / "demo" / ".cloak-mcp.json").read_text())["seed"]
                self.assertIsInstance(seed, int)
                with self.assertRaises(RuntimeError) as cm:
                    other.profile("use", "demo")
                self.assertIn("already open", str(cm.exception))
                self.assertIn("ephemeral", self.bm.profile("ephemeral"))
            finally:
                other.close()


class _FakeContext:
    def __init__(self):
        self.pages, self.closed = [], False

    def on(self, *args):
        pass

    async def new_page(self):
        import types

        return types.SimpleNamespace(is_closed=lambda: False)

    async def close(self):
        self.closed = True


class _FakeChromium:
    """launch_persistent_context that takes `delay` seconds, like a real launch."""

    def __init__(self, delay):
        self.delay, self.launched = delay, []

    async def launch_persistent_context(self, path, **kwargs):
        import asyncio

        await asyncio.sleep(self.delay)
        ctx = _FakeContext()
        self.launched.append(ctx)
        return ctx


class BrowserManagerUnitTests(unittest.TestCase):
    """BrowserManager logic that needs no real browser."""

    def setUp(self):
        import tempfile
        from unittest import mock

        # Never touch the real (shared with Claude Code) profiles.
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        env = mock.patch.dict(os.environ, {"CLOAKBROWSER_PROFILES_DIR": root.name})
        env.start()
        self.addCleanup(env.stop)

    def manager(self, delay=0.0):
        import asyncio
        import threading
        import types

        from kairos.browser_manager import BrowserManager

        bm = BrowserManager()
        fake_pw = types.SimpleNamespace(chromium=_FakeChromium(delay))
        # build_args, ensure_binary, IGNORE_DEFAULT_ARGS, patch_context_async, resolve_config
        bm._cloak = lambda: (lambda **k: [], lambda: "chrome.exe", [], lambda *a: None, lambda *a: None)
        worker, ready = bm._worker, threading.Event()

        def run():
            worker._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(worker._loop)
            worker.pw = fake_pw
            ready.set()
            worker._loop.run_forever()
            worker._loop.close()

        worker._thread = threading.Thread(target=run, daemon=True)
        worker._thread.start()
        ready.wait()
        self.addCleanup(lambda: worker._loop.call_soon_threadsafe(worker._loop.stop))
        bm._state = lambda mode="full": asyncio.sleep(0, result="state")
        return bm, fake_pw.chromium

    def test_cancelled_profile_launch_releases_the_profile_and_closes_late_context(self):
        import threading

        from kairos import browser_manager as bmod

        bm, chromium = self.manager(delay=0.5)
        threading.Timer(0.1, bm.cancel_active_operation).start()
        with self.assertRaises(Exception):
            bm.profile("use", "work")  # Esc while the browser is still launching
        # The cancel reaches the worker's loop a moment after the call returned.
        deadline = time.time() + 5
        while (bmod._open_profiles or not (chromium.launched and chromium.launched[0].closed)) and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(bmod._open_profiles, set())
        self.assertTrue(chromium.launched[0].closed, "a browser that finished launching after Esc is closed")

        other, _ = self.manager()  # another conversation in the same process
        self.assertIn("Relaunched in profile 'work'", other.profile("use", "work"))
        other._reset()

    def test_failed_setup_after_launch_releases_the_profile_and_closes_it(self):
        from kairos import browser_manager as bmod

        bm, chromium = self.manager()

        def patch_fails(*args):
            raise RuntimeError("humanize patch failed")

        bm._cloak = lambda: (lambda **k: [], lambda: "chrome.exe", [], patch_fails, lambda *a: None)
        with self.assertRaises(RuntimeError):
            bm.profile("use", "work")
        self.assertEqual(bmod._open_profiles, set())
        self.assertFalse(bm.is_open)
        deadline = time.time() + 5
        while not chromium.launched[0].closed and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(chromium.launched[0].closed)

    def test_profiles_are_shared_with_cloak_mcp(self):
        import json
        from unittest import mock

        from kairos.browser_manager import BrowserManager, profiles_root

        self.assertEqual(profiles_root(), self.root)
        with mock.patch.dict(os.environ, {"CLOAKBROWSER_PROFILES_DIR": ""}):
            self.assertEqual(profiles_root(), Path.home() / ".cloak-browser-mcp" / "profiles")
        bm = BrowserManager()
        existing = self.root / "work"
        existing.mkdir()
        (existing / ".cloak-mcp.json").write_text(json.dumps({"seed": 424242}, indent=2))
        self.assertEqual(bm._profile_seed(existing), 424242)  # a profile cloak-mcp made keeps its device
        seed = bm._profile_seed(self.root / "new")
        self.assertEqual(json.loads((self.root / "new" / ".cloak-mcp.json").read_text()), {"seed": seed})

    def test_profile_in_use_by_another_browser_fails_clearly_without_launching(self):
        from unittest import mock

        from kairos import browser_manager as bmod

        self.assertFalse(bmod._profile_in_use(self.root))  # no lockfile: free, and none is created
        self.assertFalse((self.root / "lockfile").exists())
        bm, chromium = self.manager()
        with mock.patch.object(bmod, "_profile_in_use", return_value=True):
            with self.assertRaises(RuntimeError) as cm:
                bm.profile("use", "work")
        self.assertIn("in use by another browser", str(cm.exception))
        self.assertEqual(chromium.launched, [])
        self.assertEqual(bmod._open_profiles, set())
        self.assertFalse(bm.is_open)

    def test_screenshots_in_the_same_second_get_different_files(self):
        import tempfile
        from unittest import mock

        from kairos.browser_manager import BrowserManager

        bm = BrowserManager()
        bm._run = lambda fn, timeout=120: (b"png", "about:blank")
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}):
            first, second = bm.screenshot()[1], bm.screenshot()[1]
            self.assertNotEqual(first.split("Saved to: ")[1], second.split("Saved to: ")[1])
            self.assertEqual(len(list((Path(home) / ".kairos" / "screenshots").iterdir())), 2)

    def test_network_listing_keeps_the_end_of_long_urls(self):
        from kairos.browser_manager import BrowserManager

        url = "https://example.com/" + "a/" * 80 + "checkout.js?v=2"
        entry = {"method": "GET", "url": url, "type": "script", "status": 200, "duration_ms": 5, "failure": None}
        bm = BrowserManager()
        bm._run = lambda fn, timeout=120: [entry]
        line = bm.network()
        self.assertIn("https://example.com/", line)
        self.assertIn("checkout.js?v=2", line)
        self.assertLess(len(line), 140)


class ToolSchemaTests(unittest.TestCase):
    def test_browser_schema_matches_manager_signatures(self):
        """Dispatch passes schema arguments straight through as keyword arguments."""
        import inspect
        import types

        from kairos.agent import Agent
        from kairos.browser_manager import BrowserManager

        tools = Agent._get_tool_schema(types.SimpleNamespace(_is_subagent=False), all_groups=True)
        browser = {t["function"]["name"]: t["function"]["parameters"] for t in tools
                   if t["function"]["name"].startswith("browser_")}
        self.assertEqual(len(browser), 20)
        for name, params in browser.items():
            method = getattr(BrowserManager, name.removeprefix("browser_"))
            accepted = set(inspect.signature(method).parameters) - {"self"}
            self.assertLessEqual(set(params["properties"]), accepted, name)

    def test_subagents_get_no_browser_tools(self):
        import types

        from kairos.agent import Agent

        tools = Agent._get_tool_schema(types.SimpleNamespace(_is_subagent=True), all_groups=True)
        self.assertFalse([t for t in tools if t["function"]["name"].startswith("browser_")])


if __name__ == "__main__":
    unittest.main()
