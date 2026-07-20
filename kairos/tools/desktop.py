"""Kairos wrappers for Cua Driver native desktop automation."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import ToolResult


class DesktopTool:
    def __init__(self, desktop_manager):
        self.dm = desktop_manager

    def _call(self, name: str, args: Optional[Dict[str, Any]] = None) -> ToolResult:
        return self.dm.call(name, args or {})


class DesktopDoctorTool(DesktopTool):
    def __call__(self) -> ToolResult:
        return self.dm.doctor()


class DesktopListWindowsTool(DesktopTool):
    def __call__(self, pid: Optional[int] = None) -> ToolResult:
        return self._call("list_windows", {"pid": pid} if pid is not None else {})


class DesktopWindowStateTool(DesktopTool):
    def __call__(self, pid: int, window_id: int, include_screenshot: bool = True) -> ToolResult:
        return self._call("get_window_state", {
            "pid": pid, "window_id": window_id, "include_screenshot": include_screenshot,
        })


class DesktopScreenshotTool(DesktopTool):
    def __call__(self, pid: int, window_id: int) -> ToolResult:
        # Current Cua Driver releases fold the native screenshot into
        # get_window_state; the historical standalone `screenshot` tool is no
        # longer registered by the default driver registry.
        return self._call("get_window_state", {
            "pid": pid, "window_id": window_id, "include_screenshot": True,
        })


class DesktopLaunchAppTool(DesktopTool):
    def __call__(self, **kwargs: Any) -> ToolResult:
        return self._call("launch_app", kwargs)


def _normalize_address_args(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Keep Cua's accessibility and pixel addressing modes mutually exclusive."""
    normalized = dict(kwargs)
    has_index = normalized.get("element_index") is not None or normalized.get("element_token") is not None
    has_pixel = normalized.get("x") is not None or normalized.get("y") is not None
    if has_index and has_pixel:
        # Models/frontends sometimes populate optional x/y defaults even when
        # selecting an accessibility element. Prefer the explicitly semantic
        # element address, matching the desktop tool contract.
        normalized.pop("x", None)
        normalized.pop("y", None)
    return normalized


class DesktopClickTool(DesktopTool):
    def __call__(self, **kwargs: Any) -> ToolResult:
        return self._call("click", _normalize_address_args(kwargs))


class DesktopTypeTool(DesktopTool):
    def __call__(self, pid: int, text: str, **kwargs: Any) -> ToolResult:
        return self._call("type_text", _normalize_address_args({"pid": pid, "text": text, **kwargs}))


class DesktopPressKeyTool(DesktopTool):
    def __call__(self, pid: int, key: str, **kwargs: Any) -> ToolResult:
        return self._call("press_key", _normalize_address_args({"pid": pid, "key": key, **kwargs}))


class DesktopHotkeyTool(DesktopTool):
    def __call__(self, pid: int, keys: list[str], **kwargs: Any) -> ToolResult:
        return self._call("hotkey", _normalize_address_args({"pid": pid, "keys": keys, **kwargs}))


class DesktopScrollTool(DesktopTool):
    def __call__(self, pid: int, direction: str, **kwargs: Any) -> ToolResult:
        args = {"pid": pid, "direction": direction, **kwargs}
        args.setdefault("by", "line")
        args.setdefault("amount", 3)
        return self._call("scroll", args)


class DesktopMoveTool(DesktopTool):
    def __call__(self, pid: int, x: float, y: float, **kwargs: Any) -> ToolResult:
        # Cua's move_cursor is pixel-only; accessibility indices are not a
        # valid addressing mode for cursor movement.
        return self._call("move_cursor", {"pid": pid, "x": x, "y": y, **kwargs})


class DesktopDragTool(DesktopTool):
    def __call__(self, pid: int, **kwargs: Any) -> ToolResult:
        # Kairos keeps compact x1/y1/x2/y2 names in its public schema while
        # Cua uses from_x/from_y/to_x/to_y.
        args = {"pid": pid, **kwargs}
        for old, new in (("x1", "from_x"), ("y1", "from_y"), ("x2", "to_x"), ("y2", "to_y")):
            if old in args:
                args[new] = args.pop(old)
        return self._call("drag", args)


class DesktopWindowActionTool(DesktopTool):
    def __init__(self, desktop_manager, action: str):
        super().__init__(desktop_manager)
        self.action = action

    def __call__(self, pid: int, **kwargs: Any) -> ToolResult:
        if self.action == "activate_window":
            return self._call("bring_to_front", {"pid": pid, **kwargs})
        return ToolResult(
            False, "",
            f"Cua Driver 0.9.0 does not provide {self.action}; only activate_window "
            "is supported through bring_to_front.",
        )


__all__ = [
    "DesktopDoctorTool", "DesktopListWindowsTool", "DesktopWindowStateTool",
    "DesktopScreenshotTool", "DesktopLaunchAppTool", "DesktopClickTool",
    "DesktopTypeTool", "DesktopPressKeyTool", "DesktopHotkeyTool", "DesktopScrollTool",
    "DesktopMoveTool", "DesktopDragTool", "DesktopWindowActionTool",
]
