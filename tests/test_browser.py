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
MODAL = (FIXTURES / "browser_modal.html").as_uri()
SPA = (FIXTURES / "browser_spa.html").as_uri()

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

    def test_19_input_behind_an_inert_modal_is_refused_with_the_dialog_named(self):
        snap = self.bm.navigate(MODAL)
        self.assertNotIn("Dialog open", snap)
        home, add = ref(snap, r'textbox "Application home page"'), ref(snap, r'button "Add domain"')
        out = self.bm.click(ref=ref(snap, r'button "Information and summary"'))
        close = ref(out, r'button "Close"')
        self.assertIn(f'\n\nDialog open: "Verification status" [ref=', out)
        self.assertIn(f"(Close: [ref={close}]). The page behind it is inert.", out)
        self.assertIn("Dialog open:", self.bm.snapshot().splitlines()[0])
        for act in (lambda: self.bm.click(ref=add), lambda: self.bm.type(home, "https://example.com")):
            start = time.time()
            with self.assertRaises(RuntimeError) as cm:
                act()
            self.assertLess(time.time() - start, 6)
            msg = str(cm.exception)
            self.assertIn(f'is covered by an open dialog "Verification status"; close it first (Close button: {close}).', msg)
            self.assertIn("Snapshot:", msg)
        # Nothing went through.
        self.assertIn("Result:\nModal test", self.bm.evaluate("() => document.title"))
        self.assertIn("Result:\n\n", self.bm.evaluate("() => document.getElementById('home').value"))
        self.assertNotIn("Dialog open", self.bm.click(ref=close))
        self.assertIn("Page title: added", self.bm.click(ref=add))

    def test_20_native_modal_dialog_covers_the_page(self):
        snap = self.bm.navigate(MODAL)
        add = ref(snap, r'button "Add domain"')
        out = self.bm.click(ref=ref(snap, r'button "Open native modal"'))
        dismiss = ref(out, r'button "Dismiss"')
        self.assertIn(f'Dialog open: "Native modal"', out)
        with self.assertRaises(RuntimeError) as cm:
            self.bm.click(ref=add)
        self.assertIn(f'covered by an open dialog "Native modal"; close it first (Close button: {dismiss}).', str(cm.exception))
        self.assertNotIn("Dialog open", self.bm.click(ref=dismiss))

    def test_21_custom_checkbox_under_its_label_is_clickable(self):
        # The styled span covers the real <input>; the hit lands on the input's own label.
        snap = self.bm.navigate(MODAL)
        self.bm.click(ref=ref(snap, r'checkbox "Subscribe"'))
        self.assertIn("true", self.bm.evaluate("() => document.getElementById('sub').checked"))

    def test_22_navigate_waits_for_the_app_to_render(self):
        start = time.time()
        out = self.bm.navigate(SPA)  # a loading bar for 2 s, then the content
        self.assertGreater(time.time() - start, 1.5)
        self.assertIn('button "Create project"', out)
        self.assertNotIn("progressbar", out)

    def test_23_type_into_a_field_that_applies_its_value_late(self):
        snap = self.bm.navigate(SPA)
        out = self.bm.type(ref(snap, r'textbox "Project name"'), "demo")
        self.assertNotIn("Snapshot: unchanged", out)
        self.assertRegex(out, r'textbox "Project name"[^\n]*: demo')

    def test_24_a_crashed_tab_is_replaced_on_the_next_call(self):
        import asyncio

        self.fresh_page()

        async def crash():
            page = self.bm._current
            session = await page.context.new_cdp_session(page)
            try:
                await asyncio.wait_for(session.send("Page.crash"), 3)  # never answers: the tab is gone
            except Exception:
                pass
            await asyncio.sleep(0.5)

        self.bm._run(crash)
        self.assertIn("Page title: Browser test", self.fresh_page())

    def test_25_long_text_types_fast_and_restores_the_typing_speed(self):
        text = "One. Two. Three. Four. Five. Six. Seven."
        snap = self.fresh_page()
        human = self.bm._current._impl_obj._cloak_human
        before = human.cfg
        out = self.bm.type(ref(snap, r'textbox "Search"'), text)
        self.assertIn(text, out)
        self.assertIs(human.cfg, before)

    @unittest.skipUnless(os.name == "nt", "the sandbox is kept on only on Windows")
    def test_26_renderers_run_sandboxed(self):
        async def status():
            page = await self.bm._page()
            await page.goto("chrome://sandbox")
            await page.wait_for_function("document.body.innerText.includes('Renderer')", timeout=5_000)
            return await page.evaluate("document.body.innerText")

        text = self.bm._run(status)
        renderers = [line for line in text.splitlines() if "	Renderer	" in line]
        self.assertTrue(renderers, text[:500])
        for line in renderers:
            self.assertNotIn("Not Sandboxed", line)
            self.assertIn("Lockdown", line)

    def test_27_large_page_card_search_and_section(self):
        rows = "".join(f"<tr><td>SKU-{10000 + i}</td><td>Widget {i}</td><td>{'backordered' if i % 4 else 'in stock'}"
                       "</td><td><button>Restock</button></td></tr>" for i in range(600))
        big = Path(self.root.name) / "large.html"
        big.write_text(f"<h1>Inventory</h1><select><option>A</option><option>B</option></select>"
                       f"<table><tr><th>SKU</th><th>Name</th><th>Status</th><th></th></tr>{rows}</table>",
                       encoding="utf-8")
        card = self.bm.navigate(big.as_uri())
        self.assertIn("large page", card)
        self.assertLess(len(card), 7_000)
        self.assertRegex(card, r"- table .*\(601 rows\)")
        self.assertRegex(card, r"- combobox .*\(2 options\)")
        saved = re.search(r"saved to (\S+\.yml)", card).group(1)
        self.assertTrue(Path(saved).is_file())

        found = self.bm.search_page("SKU-10433")
        self.assertIn("Found 1 match", found)
        self.assertIn("Widget 433", found)
        self.assertIn("- table", found)  # ancestor path
        # A row near the top: humanized scrolling to row 433 (~10,000 px) takes ~30 s, past the click timeout.
        near = self.bm.search_page("SKU-10003")
        self.assertIn("Clicked", self.bm.click(ref(near, r'button "Restock"')))
        many = self.bm.search_page(r"/back\w+/i", regex=True, max_results=3)
        self.assertIn("showing the first 3", many)
        self.assertIn("No matches", self.bm.search_page("zzz-not-there"))
        with self.assertRaises(ValueError):
            self.bm.search_page("(", regex=True)

        row = re.search(r'- row \[ref=(\w+)\]:\n\s*- cell "SKU-10433"', found).group(1)
        section = self.bm.snapshot(target=row)
        self.assertIn(f"element {row}", section)
        self.assertIn("Widget 433", section)
        self.assertLess(len(section), 600)
        table = ref(card, r"- table")
        shallow = self.bm.snapshot(target=table, depth=1)
        self.assertIn(f"element {table}, depth 1", shallow)
        self.assertNotIn("Widget 433", shallow)
        with self.assertRaisesRegex(RuntimeError, "no longer exists"):
            self.bm.snapshot(target="e99999")
        self.assertTrue(self.bm.snapshot().startswith("Page URL") and "large page" not in self.bm.snapshot())
        self.assertNotIn("large page", self.fresh_page())  # small pages still come back whole


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

    def test_open_dialog_is_found_in_the_snapshot(self):
        from kairos.browser_manager import open_dialog

        snap = "\n".join([
            "- main [ref=e2]:",
            '  - button "Add domain" [ref=e5]',
            "- dialog [ref=e11]:",
            '  - heading "Verification status" [level=2] [ref=e12]',
            '  - button "Save" [ref=e13]',
            '  - button "Close panel" [ref=e14]',
            '- button "Close" [ref=e20]',  # outside the dialog
        ])
        self.assertEqual(open_dialog(snap), ("Verification status", "e11", "e14"))
        self.assertEqual(open_dialog('- alertdialog "Push to production?" [ref=f1e3]:\n  - button "Confirm" [ref=f1e4]'),
                         ("Push to production?", "f1e3", None))
        self.assertIsNone(open_dialog('- main [ref=e2]:\n  - button "Close" [ref=e3]'))

    def test_covered_target_error_names_the_dialog_or_the_blocker(self):
        from kairos.browser_manager import BrowserManager, CoveredError

        modal = ("Verification status", "e572", "e588", True)
        covered = CoveredError("e353", {"inDialog": False, "blocker": '<div#panel role=dialog> "Verification status"'})
        self.assertEqual(BrowserManager._explain(covered, "e353", modal),
                         'Target e353 is covered by an open dialog "Verification status"; close it first (Close button: e588).')
        self.assertEqual(BrowserManager._explain(covered, "e353", ("", "e572", None, False)),
                         "Target e353 is covered by an open dialog [ref=e572]; close it first.")
        self.assertIn('covered by <div#panel role=dialog> "Verification status"', BrowserManager._explain(covered, "e353"))
        inert = CoveredError("e9", {"inert": True, "inDialog": False, "blocker": "<main#bg>"})
        self.assertIn("Target e9 is inert (inside <main#bg>", BrowserManager._explain(inert, "e9"))
        # A target inside the dialog is not covered by that dialog: name the real blocker.
        toast = CoveredError("e14", {"inDialog": True, "blocker": '<div role=status> "Saved"'})
        self.assertIn('covered by <div role=status> "Saved"', BrowserManager._explain(toast, "e14", modal))

    def test_guard_waits_out_a_transient_cover_and_refuses_a_lasting_one(self):
        import asyncio

        from kairos.browser_manager import BrowserManager, CoveredError

        class Handle:
            def __init__(self, results):
                self.results, self.scrolls = list(results), 0

            async def evaluate(self, js, *args):
                return self.results.pop(0) if len(self.results) > 1 else self.results[0]

            async def scroll_into_view_if_needed(self, timeout=None):
                self.scrolls += 1

        bm, cover = BrowserManager(), {"inDialog": False, "blocker": "<div>"}
        transient = Handle([cover, cover, cover, None])  # an overlay fading out
        asyncio.run(bm._guard(transient, "e1", poll=2))
        self.assertEqual(transient.scrolls, 1)  # brought into view (humanized) before testing again
        start = time.time()
        with self.assertRaises(CoveredError):
            asyncio.run(bm._guard(Handle([cover]), "e1", poll=0.3))
        self.assertLess(time.time() - start, 2)
        inert = Handle([{"inert": True, "inDialog": False, "blocker": "<main>"}])
        with self.assertRaises(CoveredError):
            asyncio.run(bm._guard(inert, "e1", poll=0.3))
        self.assertEqual(inert.scrolls, 0)
        asyncio.run(bm._guard(Handle([{"offscreen": True}]), "e1", poll=0.3))  # untestable: let the action run

    def test_await_render_never_fails(self):
        import asyncio
        import types

        from kairos.browser_manager import BrowserManager

        bm = BrowserManager()
        answers = [Exception("Execution context was destroyed"), False, True]

        async def evaluate(js):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        asyncio.run(bm._await_render(types.SimpleNamespace(evaluate=evaluate), timeout=5))
        self.assertEqual(answers, [])
        start = time.time()
        asyncio.run(bm._await_render(types.SimpleNamespace(evaluate=lambda js: asyncio.sleep(0, result=False)), timeout=0.5))
        self.assertLess(time.time() - start, 2)

    def test_long_text_is_typed_faster_and_the_speed_restored(self):
        from types import SimpleNamespace

        from cloakbrowser.human.config import HumanConfig

        from kairos.browser_manager import _FAST_TYPING_DIVISOR as D, _fast_typing, _is_long_text

        self.assertTrue(_is_long_text("A. B. C. D. E. F."))
        self.assertTrue(_is_long_text("x" * 501))
        self.assertFalse(_is_long_text("One. Two. Three. Four. Five."))
        self.assertFalse(_is_long_text("see https://example.com/a.b.c.d.e.f.g"))

        cfg = HumanConfig()
        human = SimpleNamespace(cfg=cfg)
        page = SimpleNamespace(_impl_obj=SimpleNamespace(_cloak_human=human))
        with _fast_typing(page, "short"):
            self.assertIs(human.cfg, cfg)
        with self.assertRaises(RuntimeError), _fast_typing(page, "x" * 600):
            self.assertEqual(human.cfg.typing_delay, cfg.typing_delay / D)
            self.assertEqual(human.cfg.key_hold, tuple(n / D for n in cfg.key_hold))
            self.assertEqual(human.cfg.mistype_chance, cfg.mistype_chance)
            raise RuntimeError
        self.assertIs(human.cfg, cfg)

    def test_a_crashed_page_is_dropped(self):
        import asyncio

        from kairos.browser_manager import BrowserManager

        class Page:
            def __init__(self):
                self.handlers, self.closed = {}, False

            def on(self, event, fn):
                self.handlers[event] = fn

            async def close(self):
                self.closed = True

        async def run():
            bm, first, second = BrowserManager(), Page(), Page()
            bm._attach(first)
            bm._attach(second)
            bm._current = second
            second.handlers["crash"](second)
            await asyncio.sleep(0.05)
            return bm, first, second

        bm, first, second = asyncio.run(run())
        self.assertIs(bm._current, first)
        self.assertNotIn(second, bm._pages)
        self.assertTrue(second.closed)


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
