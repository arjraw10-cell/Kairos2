"""Test: What happens if we call sync_playwright().start() in the worker __init__ 
and keep it alive, then call page operations in subsequent dispatches?"""
import threading, queue, sys, time

class _PlaywrightWorker:
    """Worker that keeps sync_playwright alive across all dispatches."""
    def __init__(self):
        self._task_queue = queue.Queue()
        self._thread = None
        self._started = threading.Event()
        self._pw = None  # Keep sync_playwright alive

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._started.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="playwright-worker")
        self._thread.start()
        self._started.wait(timeout=10)

    def _run(self):
        self._started.set()
        # Create sync_playwright HERE, in the worker thread, and keep it alive
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
        
        # Cleanup on exit
        try: self._pw.stop()
        except: pass

    def dispatch(self, fn, timeout=30):
        if not self._thread or not self._thread.is_alive():
            raise RuntimeError("Worker thread is not running")
        result_holder = {}
        self._task_queue.put((fn, result_holder))
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

print("=== Test: Persistent sync_playwright worker ===", flush=True)
worker = _PlaywrightWorker()
worker.start()

# State stored by dispatch closures
state = {}

def do_launch():
    pw = worker._pw  # Use the persistent pw
    profile_dir = str(__import__('pathlib').Path("~/.kairos/profiles/Kairos").expanduser())
    ctx = pw.chromium.launch_persistent_context(profile_dir, headless=True, viewport={"width": 1280, "height": 720})
    pages = list(ctx.pages)
    if not pages:
        pages.append(ctx.new_page())
    state["pw"] = pw
    state["ctx"] = ctx
    state["page"] = pages[0]
    return f"Launched, pages={len(pages)}"

result = worker.dispatch(do_launch, timeout=30)
print(f"1. {result}", flush=True)

def do_nav():
    page = state["page"]
    resp = page.goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
    return page.title()

title = worker.dispatch(do_nav, timeout=30)
print(f"2. Navigate: {title}", flush=True)

def do_snapshot():
    return state["page"].title()

title2 = worker.dispatch(do_snapshot, timeout=10)
print(f"3. Snapshot: {title2}", flush=True)

def do_screenshot():
    return state["page"].screenshot(type="png")

png = worker.dispatch(do_screenshot, timeout=15)
print(f"4. Screenshot: {len(png)} bytes", flush=True)

def do_close():
    try: state["ctx"].close()
    except: pass
    # Don't stop pw here - let the worker stop handle it

worker.dispatch(do_close, timeout=15)
worker.stop()
print("5. Closed", flush=True)
print("\n=== ALL PASSED ===", flush=True)
