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
    assert Path(out.strip().splitlines()[-1]).resolve() == tmp_path.resolve()

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


PY = f'"{sys.executable}"'


def test_blocking_result_states_exit_code_wall_time_and_output():
    manager = TerminalManager()
    tid = manager.create_terminal(background=False)
    ok, out = manager.execute_command(tid, f'{PY} -c "print(1); print(2)"', timeout=10)
    assert ok, out
    lines = out.splitlines()
    assert lines[0] == "Exit code: 0"
    assert lines[1].startswith("Wall time: ") and lines[1].endswith("s")
    assert lines[2:] == ["Output:", "1", "2"]

    # A failure with output still states its exit code; stderr is included.
    ok, out = manager.execute_command(
        tid, f'{PY} -c "import sys; print(\'oops\', file=sys.stderr); sys.exit(3)"', timeout=10
    )
    assert not ok
    assert out.startswith("Exit code: 3\n") and out.endswith("Output:\noops\n")


def test_commands_get_plain_output_environment():
    manager = TerminalManager()
    probe = f'{PY} -c "import os; print([os.environ.get(k) for k in (\'NO_COLOR\', \'TERM\', \'PAGER\', \'GIT_PAGER\')])"'
    expected = "['1', 'dumb', 'cat', 'cat']"
    ok, out = manager.execute_command(None, probe, timeout=10)
    assert ok and expected in out, out

    events = []
    manager.set_completion_callback(events.append)
    background = manager.create_terminal(background=True)
    try:
        manager.execute_command(background, probe)
        assert wait_for(lambda: finished(events))
        assert expected in finished(events)[0]["output"]
    finally:
        manager.close_terminal(background)


def test_default_terminal_is_created_lazily_and_recreated_after_close():
    manager = TerminalManager()
    ok, out = manager.execute_command(None, "echo hi")
    assert ok and "hi" in out
    first = manager._default_terminal_id
    assert manager.get_terminal_info(first)["background"] is False
    manager.execute_command(None, "echo again")
    assert manager._default_terminal_id == first
    manager.close_terminal(first)
    ok, _ = manager.execute_command(None, "echo after close")
    assert ok and manager._default_terminal_id != first
    # Arguments from older histories still apply to the default terminal.
    assert "only supported for background" in manager.execute_command(None, "echo x", until="x")[1]


def test_slow_blocking_command_moves_to_background_and_reports_its_finish():
    manager = TerminalManager()
    events = []
    manager.set_completion_callback(events.append)
    blocking = manager.create_terminal(background=False)
    ok, out = manager.execute_command(
        blocking, f'{PY} -u -c "import time; print(\'started\'); time.sleep(2); print(\'done\')"', timeout=1
    )
    assert ok, out
    assert out.startswith("Still running after 1s; moved to background as terminal ")
    assert "Output so far:\nstarted" in out
    moved = int(out.split("terminal ")[1].split(".")[0])
    assert moved != blocking
    assert manager.get_terminal_info(moved)["background"] is True
    assert manager.running_background_commands() == 1
    # The moved terminal has no shell for new commands; the blocking one is unchanged.
    assert "accepts no new commands" in manager.execute_command(moved, "echo x")[1]
    assert manager.execute_command(blocking, "echo still-blocking")[0]

    assert wait_for(lambda: finished(events))
    event = finished(events)[0]
    assert (event["terminal_id"], event["success"], event["exit_code"]) == (moved, True, 0)
    assert event["output"].split() == ["started", "done"]
    assert event["duration_seconds"] >= 2
    assert manager.read_logs(moved)[1].splitlines()[1:] == ["started", "done"]
    assert manager.running_background_commands() == 0
    manager.close_terminal(moved)
    assert len(events) == 1


def test_moved_command_can_be_stopped_or_closed():
    manager = TerminalManager()
    events = []
    manager.set_completion_callback(events.append)
    sleep = f'{PY} -c "import time; time.sleep(30)"'

    out = manager.execute_command(None, sleep, timeout=0.5)[1]
    moved = int(out.split("terminal ")[1].split(".")[0])
    ok, message = manager.stop_command(moved)
    assert ok, message
    assert wait_for(lambda: finished(events))
    assert finished(events)[0]["stopped"] is True

    out = manager.execute_command(None, sleep, timeout=0.5)[1]
    moved = int(out.split("terminal ")[1].split(".")[0])
    process = manager.terminals[moved].process
    manager.close_terminal(moved)
    assert wait_for(lambda: len(finished(events)) == 2)
    assert "closed" in finished(events)[1]["error"]
    assert wait_for(lambda: process.poll() is not None)
    time.sleep(0.3)  # the reader must not report the closed command a second time
    assert len(finished(events)) == 2
