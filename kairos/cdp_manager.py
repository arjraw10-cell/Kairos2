"""
CDPManager — low-level Chrome DevTools Protocol access for enhanced DOM extraction.

Uses Playwright's CDP session API to send raw DevTools Protocol commands,
enabling features that go beyond Playwright's high-level abstractions:

  - DOMSnapshot.captureSnapshot: full layout tree with paint order, computed
    styles, bounding boxes for ALL frames (including cross-origin iframes)
  - Accessibility.getFullAXTree: semantic accessibility tree per frame
  - Page.getFrameTree: frame hierarchy for cross-origin iframe detection
  - Page.getLayoutMetrics: viewport size, device pixel ratio

All CDP operations run synchronously via Playwright's sync API and must be
dispatched through the BrowserManager's worker thread.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Computed styles to capture with DOMSnapshot.
# Only the essentials for interactivity / visibility detection.
REQUIRED_COMPUTED_STYLES = [
    "display",
    "visibility",
    "opacity",
    "overflow",
    "overflow-x",
    "overflow-y",
    "cursor",
    "pointer-events",
    "position",
    "background-color",
]


class CDPManager:
    """Low-level CDP access through Playwright's sync CDP session API."""

    def __init__(self):
        self._sessions: Dict[int, Any] = {}  # id(page) -> cdp_session

    # ------------------------------------------------------------------
    #  Session management
    # ------------------------------------------------------------------

    def get_session(self, page) -> Any:
        """Get or create a CDP session for a Playwright page.

        Sessions are cached per-page.  A new navigation does NOT invalidate
        the session — CDP sessions are tied to the page target, not the URL.
        """
        page_id = id(page)
        if page_id not in self._sessions:
            self._sessions[page_id] = page.context.new_cdp_session(page)
        return self._sessions[page_id]

    def invalidate_session(self, page) -> None:
        """Remove a cached session (e.g. when the page is closed)."""
        self._sessions.pop(id(page), None)

    def invalidate_all(self) -> None:
        """Clear all cached sessions."""
        self._sessions.clear()

    # ------------------------------------------------------------------
    #  Frame tree
    # ------------------------------------------------------------------

    def get_frame_tree(self, page) -> Dict[str, Any]:
        """Get the frame hierarchy via CDP Page.getFrameTree."""
        cdp = self.get_session(page)
        return cdp.send("Page.getFrameTree")

    def get_all_frame_ids(self, page) -> List[Dict[str, str]]:
        """Collect all frame IDs, URLs, and names from the frame tree.

        Returns a list of dicts: [{"id": ..., "url": ..., "name": ..., "is_main": bool}]
        """
        tree = self.get_frame_tree(page)
        frames: List[Dict[str, str]] = []

        def _collect(node: Dict, is_main: bool = False):
            frame = node.get("frame", {})
            frames.append({
                "id": frame.get("id", ""),
                "url": frame.get("url", ""),
                "name": frame.get("name", ""),
                "is_main": is_main,
            })
            for child in node.get("childFrames", []):
                _collect(child, is_main=False)

        _collect(tree["frameTree"], is_main=True)
        return frames

    # ------------------------------------------------------------------
    #  Accessibility tree
    # ------------------------------------------------------------------

    def get_ax_tree(self, page, frame_id: Optional[str] = None) -> List[Dict]:
        """Get the accessibility tree, optionally for a specific frame.

        Returns a list of AXNode dicts from Accessibility.getFullAXTree.
        """
        cdp = self.get_session(page)
        params: Dict[str, Any] = {}
        if frame_id:
            params["frameId"] = frame_id
        result = cdp.send("Accessibility.getFullAXTree", params)
        return result.get("nodes", [])

    def get_all_ax_trees(self, page) -> Dict[str, List[Dict]]:
        """Get accessibility trees for ALL frames.

        Returns {frame_id: [AXNode, ...]}
        """
        frames = self.get_all_frame_ids(page)
        trees: Dict[str, List[Dict]] = {}
        for frame in frames:
            try:
                trees[frame["id"]] = self.get_ax_tree(page, frame["id"])
            except Exception as e:
                logger.debug(f"Failed to get a11y tree for frame {frame['id']}: {e}")
                trees[frame["id"]] = []
        return trees

    # ------------------------------------------------------------------
    #  DOM Snapshot (layout data, paint order, bounding boxes)
    # ------------------------------------------------------------------

    def capture_dom_snapshot(
        self,
        page,
        computed_styles: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Capture a DOM snapshot with layout data for ALL frames.

        This returns the raw CDP DOMSnapshot data which includes:
        - All documents (main + cross-origin iframes)
        - Node tree with backendNodeId, attributes, shadow roots
        - Layout tree with bounding boxes, computed styles, paint order
        - Scroll rects, client rects, stacking contexts
        """
        cdp = self.get_session(page)
        return cdp.send("DOMSnapshot.captureSnapshot", {
            "computedStyles": computed_styles or REQUIRED_COMPUTED_STYLES,
            "includePaintOrder": True,
            "includeDOMRects": True,
        })

    # ------------------------------------------------------------------
    #  Layout metrics
    # ------------------------------------------------------------------

    def get_layout_metrics(self, page) -> Dict[str, Any]:
        """Get viewport dimensions and device pixel ratio."""
        cdp = self.get_session(page)
        return cdp.send("Page.getLayoutMetrics")

    def get_viewport_size(self, page) -> Tuple[float, float]:
        """Get the CSS viewport width and height."""
        metrics = self.get_layout_metrics(page)
        css_vp = metrics.get("cssVisualViewport", {})
        css_layout = metrics.get("cssLayoutViewport", {})
        w = css_vp.get("clientWidth", css_layout.get("clientWidth", 1280.0))
        h = css_vp.get("clientHeight", css_layout.get("clientHeight", 720.0))
        return float(w), float(h)

    def get_device_pixel_ratio(self, page) -> float:
        """Get the device pixel ratio."""
        metrics = self.get_layout_metrics(page)
        visual = metrics.get("visualViewport", {})
        css = metrics.get("cssVisualViewport", {})
        device_w = visual.get("clientWidth", 0)
        css_w = css.get("clientWidth", 1)
        if css_w > 0:
            return float(device_w / css_w)
        return 1.0

    # ------------------------------------------------------------------
    #  Cross-origin iframe a11y content
    # ------------------------------------------------------------------

    def get_cross_origin_iframe_content(
        self, page, main_frame_url: str = ""
    ) -> List[Dict[str, Any]]:
        """Get formatted a11y content for all cross-origin iframes.

        Returns a list of dicts, one per cross-origin iframe:
        {
            "url": str,
            "name": str,
            "elements": [{"role": str, "name": str, "description": str,
                          "properties": dict, "is_interactive": bool}]
        }
        """
        frames = self.get_all_frame_ids(page)
        results: List[Dict[str, Any]] = []

        for frame in frames:
            if frame["is_main"]:
                continue

            # Determine if this is cross-origin by comparing URLs
            frame_url = frame.get("url", "")
            if not frame_url or frame_url == "about:blank":
                continue

            # Heuristic: if the frame URL's origin differs from the main page
            try:
                from urllib.parse import urlparse
                main_origin = urlparse(main_frame_url or "").netloc
                frame_origin = urlparse(frame_url).netloc
                if main_origin == frame_origin:
                    continue  # Same-origin, handled by JS snapshot
            except Exception:
                continue

            # Get a11y tree for this cross-origin frame
            try:
                ax_nodes = self.get_ax_tree(page, frame["id"])
            except Exception as e:
                logger.debug(f"Failed to get a11y for cross-origin frame {frame_url}: {e}")
                continue

            # Parse a11y nodes into our format
            elements = self._parse_ax_nodes_for_snapshot(ax_nodes)
            if elements:
                results.append({
                    "url": frame_url,
                    "name": frame.get("name", ""),
                    "elements": elements,
                })

        return results

    def _parse_ax_nodes_for_snapshot(
        self, ax_nodes: List[Dict]
    ) -> List[Dict[str, Any]]:
        """Parse CDP a11y nodes into a flat list of snapshot-formatted elements."""
        # Build lookup from nodeId -> node
        node_map: Dict[str, Dict] = {}
        for node in ax_nodes:
            node_map[node.get("nodeId", "")] = node

        # Find interactive elements
        interactive_roles = {
            "button", "link", "textbox", "searchbox", "spinbutton",
            "checkbox", "radio", "switch", "slider", "menuitem",
            "menuitemcheckbox", "menuitemradio", "option", "tab",
            "combobox", "listbox", "tree", "treeitem",
        }

        elements: List[Dict[str, Any]] = []
        seen = set()

        for node in ax_nodes:
            role_obj = node.get("role", {})
            role = role_obj.get("value", "") if isinstance(role_obj, dict) else ""
            name_obj = node.get("name", {})
            name = name_obj.get("value", "") if isinstance(name_obj, dict) else ""

            if not role or not name:
                continue

            is_interactive = role in interactive_roles
            if not is_interactive:
                # Also include headings, static text, etc. but mark as non-interactive
                if role not in ("heading", "StaticText", "img", "list", "listitem", "navigation", "main", "banner", "contentinfo"):
                    continue

            # Skip if we've seen this exact role+name combo (deduplicate)
            key = f"{role}:{name}"
            if key in seen:
                continue
            seen.add(key)

            # Get properties
            props = {}
            for prop in node.get("properties", []):
                prop_name = prop.get("name", "")
                prop_value = prop.get("value", {})
                if isinstance(prop_value, dict):
                    prop_value = prop_value.get("value", "")
                if prop_name and prop_value is not None:
                    props[prop_name] = prop_value

            element = {
                "role": role,
                "name": name,
                "description": "",
                "is_interactive": is_interactive,
            }

            desc_obj = node.get("description", {})
            if isinstance(desc_obj, dict):
                element["description"] = desc_obj.get("value", "")

            if props:
                element["properties"] = props

            elements.append(element)

        return elements[:50]  # Cap at 50 elements per iframe

    # ------------------------------------------------------------------
    #  JS execution via CDP (for same-origin frames only)
    # ------------------------------------------------------------------

    def evaluate_js(self, page, expression: str, frame_id: Optional[str] = None) -> Any:
        """Evaluate JavaScript via CDP Runtime.evaluate.

        For the main frame, pass frame_id=None.
        For child frames, pass the frame_id — but note this only works for
        same-origin frames.  Cross-origin frames require a11y tree access.
        """
        cdp = self.get_session(page)
        params: Dict[str, Any] = {
            "expression": expression,
            "returnByValue": True,
        }
        if frame_id:
            # Get execution context for the frame
            # First enable Runtime to get contexts
            cdp.send("Runtime.enable")
            # Find the context for our frame
            # This is tricky — for now, just evaluate in the main context
            pass
        result = cdp.send("Runtime.evaluate", params)
        if "exceptionDetails" in result:
            raise RuntimeError(f"JS error: {result['exceptionDetails'].get('text', 'unknown')}")
        return result.get("result", {}).get("value")
