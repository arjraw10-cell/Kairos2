"""Test the fixed BrowserManager from a fresh process."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

print("=== Testing Fixed BrowserManager ===", flush=True)

from kairos.browser_manager import BrowserManager

bm = BrowserManager()
print(f"1. BrowserManager created, is_open={bm.is_open}", flush=True)

# Test 1: Ephemeral launch
try:
    result = bm.launch(headless=True)
    print(f"2. Ephemeral launch: {result}", flush=True)
    print(f"   is_open={bm.is_open}, pages={len(bm.pages)}", flush=True)
    
    result = bm.navigate("https://example.com")
    print(f"3. Navigate: {result}", flush=True)
    
    result = bm.snapshot()
    print(f"4. Snapshot:\n{result[:300]}", flush=True)
    
    png, msg = bm.screenshot()
    print(f"5. Screenshot: {msg} (bytes: {len(png) if png else 0})", flush=True)
    
    # Test tab management
    result = bm.get_page_info()
    print(f"6. Page info: {result}", flush=True)
    
    result = bm.close()
    print(f"7. Close: {result}", flush=True)
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}", flush=True)
    import traceback
    traceback.print_exc()
    try: bm.close()
    except: pass

print()

# Test 2: Launch with profile
bm2 = BrowserManager()
try:
    result = bm2.launch(profile="Kairos", headless=True)
    print(f"8. Profile launch: {result}", flush=True)
    print(f"   is_open={bm2.is_open}, pages={len(bm2.pages)}", flush=True)
    
    result = bm2.navigate("https://example.com")
    print(f"9. Navigate: {result}", flush=True)
    
    result = bm2.snapshot()
    print(f"10. Snapshot:\n{result[:200]}", flush=True)
    
    png, msg = bm2.screenshot()
    print(f"11. Screenshot: {msg} (bytes: {len(png) if png else 0})", flush=True)
    
    result = bm2.close()
    print(f"12. Close: {result}", flush=True)
except Exception as e:
    print(f"ERROR (profile): {type(e).__name__}: {e}", flush=True)
    import traceback
    traceback.print_exc()
    try: bm2.close()
    except: pass

# Test 3: Launch after previous close (verify worker thread re-creation)
bm3 = BrowserManager()
try:
    result = bm3.launch(headless=True)
    print(f"13. Relaunch: {result}", flush=True)
    
    result = bm3.navigate("https://httpbin.org/user-agent")
    print(f"14. Navigate: {result}", flush=True)
    
    result = bm3.close()
    print(f"15. Close: {result}", flush=True)
except Exception as e:
    print(f"ERROR (relaunch): {type(e).__name__}: {e}", flush=True)
    import traceback
    traceback.print_exc()
    try: bm3.close()
    except: pass

print("\n=== All Tests Complete ===", flush=True)
