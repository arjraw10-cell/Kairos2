"""Test: Does regular Playwright (non-CloakBrowser) have the same greenlet issue?
The answer determines if we need to keep ALL operations in one dispatch() call."""
import threading, queue, sys

class _WorkerThread:
    def __init__(self):
        self._task_queue = queue.Queue()
        self._thread = None
        self._started = threading.Event()
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._started.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="playwright-worker")
        self._thread.start()
        self._started.wait(timeout=10)
    def _run(self):
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
    def dispatch(self, fn, timeout=30):
        if not self._thread or not self._thread.is_alive():
            raise RuntimeError("Worker thread is not running")
        result_holder = {}
        self._task_queue.put((fn, result_holder))
        import time
        deadline = time.time() + timeout + 5
        while time.time() < deadline:
            if "result" in result_holder or "error" in result_holder:
                break
            time.sleep(0.1)
        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder.get("result")
    def stop(self):
        if self._thread and self._thread.is_alive():
            self._task_queue.put(None)
            self._thread.join(timeout=5)
        self._thread = None

print("=== Testing regular Playwright with separate dispatch calls ===", flush=True)
worker = _WorkerThread()
worker.start()

from playwright.sync_api import sync_playwright

# Launch (creates the playwright + browser)
def do_launch():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context()
    page = ctx.new_page()
    return pw, browser, ctx, page

result = worker.dispatch(do_launch, timeout=30)
pw, browser, ctx, page = result
print(f"1. Launched OK, page.url={page.url}", flush=True)

# Navigate in a SEPARATE dispatch (like the real BrowserManager does)
def do_navigate():
    page.goto("https://example.com")
    return page.title()

try:
    title = worker.dispatch(do_navigate, timeout=15)
    print(f"2. Navigate OK: {title}", flush=True)
except Exception as e:
    print(f"2. Navigate FAILED: {e}", flush=True)

# Snapshot in a SEPARATE dispatch
def do_snapshot():
    return page.evaluate("() => document.title")

try:
    result = worker.dispatch(do_snapshot, timeout=15)
    print(f"3. Snapshot OK: {result}", flush=True)
except Exception as e:
    print(f"3. Snapshot FAILED: {e}", flush=True)

# Cleanup
def do_close():
    ctx.close()
    browser.close()
    pw.stop()
worker.dispatch(do_close, timeout=15)
worker.stop()
print("Done!", flush=True)
