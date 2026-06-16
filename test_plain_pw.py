"""Test: Skip CloakBrowser, use plain Playwright for everything."""
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

print("=== Test: Plain Playwright with profile ===", flush=True)
worker = _WorkerThread()
worker.start()

from playwright.sync_api import sync_playwright

# Step 1: Launch with persistent context
def do_launch():
    pw = sync_playwright().start()
    profile_dir = str(__import__('pathlib').Path("~/.kairos/profiles/Kairos").expanduser())
    ctx = pw.chromium.launch_persistent_context(
        profile_dir,
        headless=True,
        viewport={"width": 1280, "height": 720},
    )
    pages = list(ctx.pages)
    if not pages:
        pages.append(ctx.new_page())
    return pw, ctx, pages

pw, ctx, pages = worker.dispatch(do_launch, timeout=30)
print(f"1. Launched OK, pages={len(pages)}", flush=True)
page = pages[0]

# Step 2: Navigate
def do_nav():
    page.goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
    return page.title()

title = worker.dispatch(do_nav, timeout=30)
print(f"2. Navigate OK: {title}", flush=True)

# Step 3: Snapshot
def do_snapshot():
    return page.title()

title2 = worker.dispatch(do_snapshot, timeout=10)
print(f"3. Snapshot OK: {title2}", flush=True)

# Step 4: Screenshot
def do_screenshot():
    return page.screenshot(type="png")

png = worker.dispatch(do_screenshot, timeout=15)
print(f"4. Screenshot OK: {len(png)} bytes", flush=True)

# Step 5: Close
def do_close():
    try: ctx.close()
    except: pass
    try: pw.stop()
    except: pass

worker.dispatch(do_close, timeout=15)
worker.stop()
print("5. Closed OK", flush=True)
print("\n=== ALL PASSED ===", flush=True)
