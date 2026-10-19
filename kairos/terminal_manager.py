import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class _BackgroundCommand:
    """A command submitted to a persistent background shell."""

    marker: str
    command: str
    started_at: float
    output: str = ""
    until: Optional["re.Pattern[str]"] = None  # notify once when a line matches
    matched: bool = False
    stopped: bool = False  # set by stop_command


@dataclass
class Terminal:
    id: int
    background: bool
    process: Optional[subprocess.Popen] = None
    closed: bool = False
    output_buffer: str = ""  # Accumulated output for background terminals
    pending_commands: Deque[_BackgroundCommand] = field(default_factory=deque)
    reader_finished: bool = False


class TerminalManager:
    """Own blocking and persistent background terminal sessions.

    Blocking commands are deliberately short-lived and have a hard maximum
    timeout. Background commands are submitted to a persistent shell and are
    completed asynchronously by the shell-output reader.
    """

    MAX_BLOCKING_TIMEOUT = 20.0
    # Notifications are deliberately capped before they enter the model
    # context or the visible CLI. The complete terminal output remains in the
    # terminal's read_logs buffer.
    MAX_NOTIFICATION_OUTPUT = 12_000
    MATCH_TAIL_LINES = 20
    DEFAULT_LOG_TAIL = 100

    # cmd prints "More? " for each continuation line of the stdin-isolating
    # block (see execute_command), after the prompt and before the output.
    _CMD_MORE_PROMPTS = re.compile(r"^([^>\r\n]*>)?(?:More\? )+")

    def __init__(self, cwd: Optional[str] = None):
        # Working directory of every terminal (the Agent's workspace); None
        # means the process CWD.
        self.cwd = cwd
        self.terminals: Dict[int, Terminal] = {}
        self.next_id = 1
        self._lock = threading.Lock()
        self._completed_background_commands: Deque[dict] = deque()
        self._completion_callback = None
        # Blocking commands are registered while communicate() is waiting so
        # an external hard interrupt can kill them immediately.
        self._active_blocking: Dict[int, subprocess.Popen] = {}

    def set_completion_callback(self, callback) -> None:
        """Set the consumer of background-command events.

        With a callback, every event (completion or ``until`` match) is
        delivered to it exactly once; events queued before it was set are
        handed over immediately. Without one, events wait in the queue.
        """
        with self._lock:
            self._completion_callback = callback
            queued = list(self._completed_background_commands) if callback else []
            if callback:
                self._completed_background_commands.clear()

        for event in queued:
            self._emit_completion((event, callback))

    def create_terminal(self, background: bool) -> int:
        with self._lock:
            terminal_id = self.next_id
            self.next_id += 1

        term = Terminal(id=terminal_id, background=background)

        if background:
            # Start a shell process for interactive commands. Commands are
            # submitted one line at a time so shell state (cd, env vars, venv
            # activation, etc.) survives between execute_command calls.
            if sys.platform == "win32":
                shell_cmd = ["cmd", "/Q", "/k"]
            else:
                shell_cmd = ["/bin/bash", "--login"]

            popen_kwargs = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
                "cwd": self.cwd,
            }
            if sys.platform == "win32":
                # Helps close/timeout cleanup target the shell process. The
                # process-tree kill below is still needed for its descendants.
                popen_kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            else:
                popen_kwargs["start_new_session"] = True

            term.process = subprocess.Popen(shell_cmd, **popen_kwargs)
            # Start reader thread before returning so output and completion
            # markers are consumed even while the agent is doing other work.
            threading.Thread(
                target=self._read_output, args=(term,), daemon=True
            ).start()

        with self._lock:
            self.terminals[terminal_id] = term

        return terminal_id

    @staticmethod
    def _coerce_output(value) -> str:
        """Normalize subprocess output, including partial timeout output."""
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _parse_completion_marker(line: str, marker: str) -> Tuple[bool, Optional[int]]:
        """Return ``(is_marker, exit_code)`` for a shell completion line."""
        # Windows cmd emits its prompt before command output, so the marker
        # may appear after a prompt prefix rather than at column zero.
        marker_pos = line.find(marker)
        if marker_pos < 0:
            return False, None
        raw_code = line[marker_pos + len(marker):].lstrip()
        if not raw_code.startswith(":"):
            return False, None
        raw_code = raw_code[1:].strip()
        try:
            return True, int(raw_code)
        except ValueError:
            # The marker still means the shell reached the completion point,
            # even if a particular shell did not expose a numeric status.
            return True, None

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen) -> None:
        """Best-effort termination of a process and all of its descendants."""
        if process.poll() is not None:
            return

        if sys.platform == "win32":
            try:
                # taskkill /T is important: killing only cmd.exe can leave a
                # child process holding stdout/stderr open indefinitely.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                    check=False,
                )
            except Exception:
                pass
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except Exception:
                try:
                    process.terminate()
                except Exception:
                    pass

        # If the tree kill did not reach the direct process, force-kill it.
        if process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass

    @staticmethod
    def _close_process_streams(process: subprocess.Popen) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    @staticmethod
    def _reap_process(process: subprocess.Popen) -> None:
        """Reap a process asynchronously if timeout cleanup could not wait."""
        try:
            process.wait()
        except Exception:
            pass

    def _queue_background_completion_locked(
        self,
        term: Terminal,
        command: _BackgroundCommand,
        exit_code: Optional[int],
        error: Optional[str] = None,
    ):
        """Build a completion event and deliver it (see ``_deliver_locked``)."""
        event = {
            "completion_id": uuid.uuid4().hex,
            "kind": "terminal_finished",
            "terminal_id": term.id,
            "command": command.command,
            "success": error is None and not command.stopped and (exit_code is None or exit_code == 0),
            "stopped": command.stopped,
            "exit_code": exit_code,
            "output": command.output,
            "duration_seconds": max(0.0, time.monotonic() - command.started_at),
        }
        if error:
            event["error"] = error
        return self._deliver_locked(event)

    def _queue_match_locked(self, term: Terminal, command: _BackgroundCommand, line: str):
        """Deliver the one-time ``until`` match of a still-running command."""
        command.matched = True
        return self._deliver_locked({
            "completion_id": uuid.uuid4().hex,
            "kind": "terminal_matched",
            "terminal_id": term.id,
            "command": command.command,
            "line": line.rstrip("\r\n"),
            "tail": "\n".join(command.output.splitlines()[-self.MATCH_TAIL_LINES:]),
            "duration_seconds": max(0.0, time.monotonic() - command.started_at),
        })

    def _deliver_locked(self, event: dict):
        """Queue an event, or return a callback delivery when a consumer is set.

        Called while ``self._lock`` is held; the caller invokes the returned
        callback after releasing the lock.
        """
        callback = self._completion_callback
        if callback is None:
            self._completed_background_commands.append(event)
            return None
        return event, callback

    @staticmethod
    def _emit_completion(delivery) -> None:
        event, callback = delivery
        try:
            callback(event)
        except Exception:
            # A notification consumer must never kill the shell reader.
            pass

    def _read_output(self, term: Terminal):
        """Read output and turn shell completion markers into notifications."""
        try:
            if term.process and term.process.stdout:
                for line in iter(term.process.stdout.readline, ""):
                    if sys.platform == "win32":
                        line = self._CMD_MORE_PROMPTS.sub(r"\1", line, count=1)
                    deliveries = []
                    is_internal_marker = False
                    with self._lock:
                        if term.pending_commands:
                            command = term.pending_commands[0]
                            # Match any pending command's marker, so one lost
                            # marker cannot stall every later command.
                            index, exit_code = None, None
                            for i, candidate in enumerate(term.pending_commands):
                                is_marker, code = self._parse_completion_marker(
                                    line, candidate.marker
                                )
                                if is_marker:
                                    index, exit_code = i, code
                                    break
                            if index is not None:
                                # Commands queued before it have finished too,
                                # but their markers never appeared.
                                for _ in range(index):
                                    deliveries.append(self._queue_background_completion_locked(
                                        term,
                                        term.pending_commands.popleft(),
                                        None,
                                        error="The command's completion marker was lost.",
                                    ))
                                command = term.pending_commands.popleft()
                                marker_pos = line.find(command.marker)
                                # Preserve output written without a trailing
                                # newline before the marker. The marker itself
                                # remains internal and is excluded from logs.
                                if marker_pos > 0:
                                    prefix = line[:marker_pos]
                                    command.output += prefix
                                    term.output_buffer += prefix
                                is_internal_marker = True
                                deliveries.append(self._queue_background_completion_locked(
                                    term, command, exit_code
                                ))
                            else:
                                command.output += line
                                if (
                                    command.until is not None
                                    and not command.matched
                                    and command.until.search(line)
                                ):
                                    deliveries.append(self._queue_match_locked(term, command, line))

                        if not is_internal_marker:
                            # Keep complete terminal output while excluding
                            # the internal completion marker.
                            term.output_buffer += line

                    for delivery in deliveries:
                        if delivery is not None:
                            self._emit_completion(delivery)
        finally:
            # If the shell exits before emitting a marker, do not leave the
            # agent waiting forever for a completion that can never arrive.
            deliveries: List[tuple] = []
            with self._lock:
                term.reader_finished = True
                while term.pending_commands:
                    command = term.pending_commands.popleft()
                    error = (
                        "Background terminal was closed before the command finished."
                        if term.closed
                        else "Background terminal exited before the command finished."
                    )
                    delivery = self._queue_background_completion_locked(
                        term, command, None, error=error
                    )
                    if delivery is not None:
                        deliveries.append(delivery)
            for delivery in deliveries:
                self._emit_completion(delivery)

    @staticmethod
    def _normalize_blocking_timeout(
        timeout: Optional[float],
    ) -> Tuple[Optional[float], Optional[str]]:
        if timeout is None:
            # A missing timeout gets the cap rather than an error: models that
            # omit it otherwise retry the same failing call.
            return TerminalManager.MAX_BLOCKING_TIMEOUT, None
        if isinstance(timeout, bool):
            return None, "Timeout must be a positive number of seconds."
        try:
            seconds = float(timeout)
        except (TypeError, ValueError):
            return None, "Timeout must be a positive number of seconds."
        if not math.isfinite(seconds) or seconds <= 0:
            return None, "Timeout must be a finite number greater than zero."
        return min(seconds, TerminalManager.MAX_BLOCKING_TIMEOUT), None

    def execute_command(
        self,
        terminal_id: int,
        command: str,
        timeout: Optional[float] = None,
        is_background: Optional[bool] = None,
        until: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Execute a command in a terminal.

        Blocking terminals require a timeout and clamp it to 20 seconds.
        Background terminals submit commands immediately; their timeout is
        intentionally ignored because the persistent shell may run commands
        for an arbitrary length of time. Completion is returned later through
        the configured completion callback.
        ``until`` (background only) is a regex: the first output line that
        matches produces a one-time ``terminal_matched`` event.
        """
        with self._lock:
            term = self.terminals.get(terminal_id)

        if not term:
            return False, f"Terminal {terminal_id} not found"

        if term.closed:
            return False, f"Terminal {terminal_id} is closed"

        # Reject invalid blocking timeouts before touching a process. For
        # background commands, timeout is intentionally not interpreted.
        if not term.background:
            _, timeout_error = self._normalize_blocking_timeout(timeout)
            if timeout_error:
                return False, timeout_error

        # Validate background flag matches terminal type
        if is_background is not None and is_background != term.background:
            return False, (
                f"Terminal {terminal_id} is "
                f"{'background' if term.background else 'blocking'}, but request specified "
                f"{'background' if is_background else 'blocking'}"
            )

        until_pattern = None
        if until:
            if not term.background:
                return False, "until is only supported for background terminals."
            try:
                until_pattern = re.compile(until)
            except re.error as e:
                return False, f"Invalid until regex: {e}"

        if term.background:
            # Send the command to the persistent shell and append a unique
            # marker. The reader thread uses the marker to know when this
            # particular command has finished, while the shell itself stays
            # alive for subsequent commands.
            if not term.process or not term.process.stdin:
                return False, "Background terminal process not available"
            if term.process.poll() is not None or term.reader_finished:
                return False, "Background terminal process has exited"

            marker = f"__KAIROS_DONE_{uuid.uuid4().hex}__"
            pending = _BackgroundCommand(
                marker=marker,
                command=command,
                started_at=time.monotonic(),
                until=until_pattern,
            )
            # The shell reads its commands from the stdin pipe, so a command
            # that reads stdin would swallow the marker line and never
            # complete. The command therefore runs in a block whose stdin is
            # NUL (/dev/null); the block keeps shell state (cd, set, export).
            if sys.platform == "win32":
                # `cmd /Q` suppresses command echo. The marker is emitted on
                # the following line while ERRORLEVEL still belongs to that
                # command. `ver` resets ERRORLEVEL for the next queued
                # command (built-ins such as `echo` do not reliably do so).
                wrapped_command = (
                    f"(\n{command}\n) <nul\n"
                    f"echo {marker}:%errorlevel%\n"
                    f"ver >nul 2>nul\n"
                )
            else:
                # Print a leading newline for the same no-trailing-newline
                # case, then report the command's status from the same shell.
                wrapped_command = (
                    f"{{\n{command}\n}} </dev/null\n"
                    f"printf '\\n{marker}:%s\\n' \"$?\"\n"
                )

            with self._lock:
                # Re-check state after acquiring the lock because close or
                # shell-exit can race with a new command submission.
                if term.closed:
                    return False, f"Terminal {terminal_id} is closed"
                if term.process.poll() is not None or term.reader_finished:
                    return False, "Background terminal process has exited"
                term.pending_commands.append(pending)
                try:
                    term.process.stdin.write(wrapped_command)
                    term.process.stdin.flush()
                except Exception as e:
                    # Remove this command if it was never accepted by the
                    # shell, so it cannot generate a phantom completion.
                    if term.pending_commands and term.pending_commands[-1] is pending:
                        term.pending_commands.pop()
                    return False, f"Failed to send command: {str(e)}"

            return True, (
                "Command sent to background terminal. You will be notified when it finishes"
                + (" or its output matches `until`" if until else "")
                + "; end your turn or keep working instead of polling."
            )

        # Blocking execution: timeout defaults to, and is capped at, 20 seconds.
        effective_timeout, timeout_error = self._normalize_blocking_timeout(timeout)
        if timeout_error:
            # This is defensive; invalid blocking values are rejected above
            # before the process path is entered.
            return False, timeout_error

        process_kwargs = {
            "shell": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            # Never inherit the host's stdin: under pythonw, a detached
            # gateway or pytest capture it is invalid ([WinError 6]).
            "stdin": subprocess.DEVNULL,
            "cwd": self.cwd,
        }
        if sys.platform == "win32":
            process_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            process_kwargs["start_new_session"] = True

        process = None
        try:
            process = subprocess.Popen(command, **process_kwargs)
            with self._lock:
                # Retain the process by terminal ID for hard cancellation.
                self._active_blocking[terminal_id] = process
            try:
                stdout, stderr = process.communicate(timeout=effective_timeout)
            except subprocess.TimeoutExpired as exc:
                # Kill descendants as well as the shell wrapper, otherwise a
                # child retaining the output pipe can make communicate() wait
                # long after the advertised timeout.
                self._terminate_process_tree(process)
                partial_output = self._coerce_output(exc.output)
                partial_error = self._coerce_output(exc.stderr)
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    threading.Thread(
                        target=self._reap_process, args=(process,), daemon=True
                    ).start()
                partial = partial_output + partial_error
                self._close_process_streams(process)
                message = f"Command timed out after {effective_timeout:g} seconds"
                if partial:
                    message += f"\nPartial output:\n{partial}"
                return False, message

            output = self._coerce_output(stdout) + self._coerce_output(stderr)
            if process.returncode == 0:
                return True, output if output else "Command executed successfully (no output)"
            return False, output if output else (
                f"Command exited with code {process.returncode} (no output)"
            )
        except Exception as e:
            return False, f"Command execution failed: {str(e)}"
        finally:
            with self._lock:
                if self._active_blocking.get(terminal_id) is process:
                    self._active_blocking.pop(terminal_id, None)

    def cancel_active_commands(self) -> int:
        """Kill all currently running blocking commands and return a count."""
        with self._lock:
            processes = list(self._active_blocking.values())
        for process in processes:
            self._terminate_process_tree(process)
        return len(processes)

    def running_background_commands(self) -> int:
        """Number of submitted background commands that have not finished."""
        with self._lock:
            return sum(len(t.pending_commands) for t in self.terminals.values() if t.background)

    def stop_command(self, terminal_id: int) -> Tuple[bool, str]:
        """Kill the running command's process tree but keep the shell.

        The shell then reports the command finished (marked stopped) through
        the usual completion event.
        """
        with self._lock:
            term = self.terminals.get(terminal_id)
            if not term:
                return False, f"Terminal {terminal_id} not found"
            if not term.background:
                return False, f"Terminal {terminal_id} is not a background terminal"
            if term.closed or not term.process:
                return False, f"Terminal {terminal_id} is closed"
            if not term.pending_commands:
                return False, f"No command is running in terminal {terminal_id}."
            command = term.pending_commands[0]
            command.stopped = True
            shell_pid = term.process.pid

        children = self._child_pids(shell_pid)
        if not children:
            with self._lock:
                command.stopped = False
            return False, (
                f"Found no process for the running command in terminal {terminal_id} "
                "(it may be a shell built-in). Use close_terminal to stop it."
            )
        for pid in children:
            self._kill_pid_tree(pid)
        return True, (
            f"Stopped the running command in terminal {terminal_id}; the terminal stays "
            "open. A notification follows when the shell reports the command finished."
        )

    @staticmethod
    def _child_pids(pid: int) -> List[int]:
        """Direct child processes of ``pid`` (the commands a shell is running)."""
        if sys.platform != "win32":
            try:
                out = subprocess.run(
                    ["pgrep", "-P", str(pid)], stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=2,
                ).stdout
            except Exception:
                return []
            return [int(p) for p in out.split()]

        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == wintypes.HANDLE(-1).value:
            return []
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        children = []
        try:
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                # conhost.exe hosts the shell's console; killing it kills the shell.
                if entry.th32ParentProcessID == pid and entry.szExeFile.lower() != "conhost.exe":
                    children.append(entry.th32ProcessID)
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return children

    @classmethod
    def _kill_pid_tree(cls, pid: int) -> None:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            return
        for child in cls._child_pids(pid):
            cls._kill_pid_tree(child)
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    @classmethod
    def format_background_completion(cls, event: dict) -> str:
        """Format one completion or ``until`` match for the agent and the UI.

        The event keeps the captured output, but this rendered notification is
        capped so a noisy background process cannot consume the conversation
        context. The terminal ID and command let the agent call ``read_logs``
        when it needs the uncapped output.
        """
        terminal_id = event.get("terminal_id", "?")
        command = event.get("command", "")
        duration = event.get("duration_seconds")
        if event.get("kind") == "terminal_matched":
            return "\n".join([
                f"Terminal {terminal_id}: output matched `until` after "
                f"{float(duration or 0):.1f}s; the command is still running.",
                f"Command: {command}",
                f"Matched line: {event.get('line', '')}",
                "Recent output:",
                str(event.get("tail") or "")[-cls.MAX_NOTIFICATION_OUTPUT:],
            ])
        success = bool(event.get("success"))
        exit_code = event.get("exit_code")
        status = "finished successfully" if success else "finished with errors"
        if event.get("stopped"):
            status = "was stopped by stop_command"
        if exit_code is not None:
            status += f" (exit code {exit_code})"
        if duration is not None:
            status += f" in {float(duration):.1f}s"

        lines = [
            f"Terminal {terminal_id}: command {status}",
            f"Command: {command}",
        ]
        if event.get("error"):
            lines.append(f"Error: {event['error']}")

        output = str(event.get("output") or "")
        if output:
            if len(output) > cls.MAX_NOTIFICATION_OUTPUT:
                output = (
                    output[: cls.MAX_NOTIFICATION_OUTPUT]
                    + "\n... [output truncated in notification; use read_logs for the full output]"
                )
            lines.extend(["Output:", output])
        else:
            lines.append("Output: (none)")
        return "\n".join(lines)

    def read_logs(
        self,
        terminal_id: int,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        tail: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Read a background terminal's output.

        ``tail`` returns the last N lines; ``start_line``/``end_line`` select a
        1-indexed inclusive range (end defaults to the last line). With
        neither, the last ``DEFAULT_LOG_TAIL`` lines are returned. Output
        starts with a ``[lines A-B of N]`` header.
        """
        with self._lock:
            term = self.terminals.get(terminal_id)

            if not term:
                return False, f"Terminal {terminal_id} not found"

            if not term.background:
                return False, f"Terminal {terminal_id} is not a background terminal"

            if term.closed:
                return False, f"Terminal {terminal_id} is closed"

            lines = term.output_buffer.splitlines()

        total = len(lines)
        if tail is not None and (start_line is not None or end_line is not None):
            return False, "Pass either tail or start_line/end_line, not both."
        if start_line is None and end_line is None:
            tail = self.DEFAULT_LOG_TAIL if tail is None else tail
            if tail < 1:
                return False, "tail must be >= 1"
            start_line, end_line = max(1, total - tail + 1), total
        else:
            start_line = 1 if start_line is None else start_line
            end_line = total if end_line is None else min(end_line, total)
            if start_line < 1:
                return False, "start_line must be >= 1"
            if total and start_line > total:
                return False, f"start_line ({start_line}) exceeds available lines ({total})"
            if total and end_line < start_line:
                return False, f"end_line ({end_line}) is before start_line ({start_line})"
        if total == 0:
            return True, "[no output yet]"

        selected_lines = lines[start_line - 1:end_line]
        return True, f"[lines {start_line}-{end_line} of {total}]\n" + "\n".join(selected_lines)

    def close_terminal(self, terminal_id: int) -> Tuple[bool, str]:
        deliveries: List[tuple] = []
        with self._lock:
            term = self.terminals.get(terminal_id)

            if not term:
                return False, f"Terminal {terminal_id} not found"

            if term.closed:
                return False, f"Terminal {terminal_id} is already closed"

            term.closed = True
            pending_commands = list(term.pending_commands)
            term.pending_commands.clear()
            for command in pending_commands:
                delivery = self._queue_background_completion_locked(
                    term,
                    command,
                    None,
                    error="Background terminal was closed before the command finished.",
                )
                if delivery is not None:
                    deliveries.append(delivery)
            del self.terminals[terminal_id]

        success = True
        error_msg = ""
        if term.background and term.process:
            try:
                self._terminate_process_tree(term.process)
                try:
                    term.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    threading.Thread(
                        target=self._reap_process,
                        args=(term.process,),
                        daemon=True,
                    ).start()
            except Exception as e:
                success = False
                error_msg = f" (warning: {str(e)})"

        for delivery in deliveries:
            self._emit_completion(delivery)

        if success:
            return True, f"Terminal {terminal_id} closed"
        return False, f"Terminal {terminal_id} closed with errors{error_msg}"

    def get_terminal_info(self, terminal_id: int) -> Optional[dict]:
        with self._lock:
            term = self.terminals.get(terminal_id)

            if not term:
                return None

            return {
                "id": term.id,
                "background": term.background,
                "closed": term.closed,
                "line_count": len(term.output_buffer.splitlines()) if term.background else 0,
                "pending_commands": len(term.pending_commands),
                "reader_finished": term.reader_finished,
            }
