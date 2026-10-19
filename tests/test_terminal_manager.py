from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

from kairos.terminal_manager import Terminal, TerminalManager, _BackgroundCommand

PWD = "cd" if os.name == "nt" else "pwd"


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def finished(events):
    return [e for e in events if e["kind"] == "terminal_finished"]


def test_blocking_terminal_defaults_timeout_and_caps_duration():
    manager = TerminalManager()
    terminal_id = manager.create_terminal(background=False)
    # A missing timeout defaults to the 20 s cap instead of failing the call.
    success, message = manager.execute_command(terminal_id, "echo ok")
    assert success is True
    assert "ok" in message

    success, message = manager.execute_command(terminal_id, "echo ok", timeout=21)
    assert success is True
    assert "ok" in message

    success, output = manager.execute_command(terminal_id, "echo ok", timeout=1)
    assert success is True
    assert "ok" in output
    manager.close_terminal(terminal_id)


def test_terminals_start_in_the_given_cwd(tmp_path):
    manager = TerminalManager(cwd=str(tmp_path))
    blocking = manager.create_terminal(background=False)
    ok, out = manager.execute_command(blocking, PWD, timeout=10)
    assert ok, out
    assert Path(out.strip()).resolve() == tmp_path.resolve()

    events = []
    manager.set_completion_callback(events.append)
    background = manager.create_terminal(background=True)
    try:
        manager.execute_command(background, PWD)
        assert wait_for(lambda: finished(events))
        assert str(tmp_path.resolve()) in finished(events)[0]["output"]
    finally:
        manager.close_terminal(background)


def test_background_command_reading_stdin_does_not_swallow_its_marker():
    manager = TerminalManager()
    events = []
    manager.set_completion_callback(events.append)
    tid = manager.create_terminal(background=True)
    try:
        manager.execute_command(
            tid, f'"{sys.executable}" -c "import sys; print(repr(sys.stdin.read()))"'
        )
        manager.execute_command(tid, "echo second_command")
        assert wait_for(lambda: len(finished(events)) == 2), manager.terminals[tid].output_buffer
        first, second = finished(events)
        assert first["success"] and "''" in first["output"]
        assert second["success"] and "second_command" in second["output"]
        assert manager.running_background_commands() == 0
        assert "More?" not in manager.terminals[tid].output_buffer  # cmd block noise
    finally:
        manager.close_terminal(tid)


class _FakeProcess:
    def __init__(self, text):
        self.stdout = io.StringIO(text)


def test_reader_matches_a_later_marker_when_an_earlier_one_was_lost():
    manager = TerminalManager()
    events = []
    manager.set_completion_callback(events.append)
    lost = _BackgroundCommand(marker="__M1__", command="reads stdin", started_at=time.monotonic())
    later = _BackgroundCommand(marker="__M2__", command="echo hi", started_at=time.monotonic())
    term = Terminal(id=1, background=True, process=_FakeProcess("hi\n__M2__:0\n"))
    term.pending_commands.extend([lost, later])

    manager._read_output(term)

    assert [e["command"] for e in events] == ["reads stdin", "echo hi"]
    assert events[0]["success"] is False and "marker" in events[0]["error"]
    assert events[1]["success"] is True and events[1]["exit_code"] == 0
    assert not term.pending_commands


def test_stop_command_kills_the_running_command():
    # taskkill/pgrep must not inherit pytest's captured stdin ([WinError 50]).
    manager = TerminalManager()
    events = []
    manager.set_completion_callback(events.append)
    tid = manager.create_terminal(background=True)
    try:
        manager.execute_command(tid, f'"{sys.executable}" -c "import time; time.sleep(30)"')
        assert wait_for(lambda: manager._child_pids(manager.terminals[tid].process.pid))
        ok, message = manager.stop_command(tid)
        assert ok, message
        assert wait_for(lambda: finished(events))
        assert finished(events)[0]["stopped"] is True
    finally:
        manager.close_terminal(tid)
