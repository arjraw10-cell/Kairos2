from __future__ import annotations

from kairos.terminal_manager import TerminalManager


def test_blocking_terminal_requires_timeout_and_caps_duration():
    manager = TerminalManager()
    terminal_id = manager.create_terminal(background=False)
    success, message = manager.execute_command(terminal_id, "echo ok")
    assert success is False
    assert "timeout is required" in message.lower()

    success, message = manager.execute_command(terminal_id, "echo ok", timeout=21)
    assert success is True
    assert "ok" in message

    success, output = manager.execute_command(terminal_id, "echo ok", timeout=1)
    assert success is True
    assert "ok" in output
    manager.close_terminal(terminal_id)
