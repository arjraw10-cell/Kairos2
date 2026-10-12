"""Kairos package.

Exports are imported on first access so light entry points (the CLI in
gateway mode) do not pay ~1 s for importing the Agent and its tools.
"""
import importlib

_EXPORTS = {
    "Config": "kairos.config",
    "Agent": "kairos.agent",
    "ToolResult": "kairos.tools",
    "SessionManager": "kairos.tools",
    "SkillManager": "kairos.tools",
    "TerminalManager": "kairos.terminal_manager",
    "BrowserManager": "kairos.browser_manager",
    "DesktopManager": "kairos.desktop_manager",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        return getattr(importlib.import_module(_EXPORTS[name]), name)
    raise AttributeError(f"module 'kairos' has no attribute {name!r}")
