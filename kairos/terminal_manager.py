import math
import os
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

    def __init__(self):
        self.terminals: Dict[int, Terminal] = {}
        self.next_id = 1
        self._lock = threading.Lock()
        self._completed_background_commands: Deque[dict] = deque()
        self._completion_callback = None
        # Blocking commands are registered while communicate() is waiting so
        # an external hard interrupt can kill them immediately.
        self._active_blocking: Dict[int, subprocess.Popen] = {}

    def set_completion_callback(self, callback) -> None:
        """Set a callback for asynchronous background-command completions.

        When a callback is configured, new completions are delivered to it
        immediately in addition to remaining in the manager's polling queue.
        Completions that arrived before the callback was configured are also
        delivered immediately.
        """
        with self._lock:
            self._completion_callback = callback
            queued = list(self._completed_background_commands) if callback else []

        if callback:
            # These events remain queued for the next API turn; this callback
            # is only an availability/CLI notification.
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
        """Queue a completion or return a callback delivery.

        This method is called while ``self._lock`` is held. The callback is
        returned to the caller and invoked after releasing the lock.
        """
        output = command.output
        event = {
            "completion_id": uuid.uuid4().hex,
            "terminal_id": term.id,
            "command": command.command,
            "success": error is None and (exit_code is None or exit_code == 0),
            "exit_code": exit_code,
            "output": output,
            "duration_seconds": max(0.0, time.monotonic() - command.started_at),
        }
        if error:
            event["error"] = error

        # Always retain the event for the agent's next API turn. The callback
        # is only an immediate visibility notification and must never consume
        # the context-delivery queue.
        self._completed_background_commands.append(event)
        callback = self._completion_callback
        if callback is None:
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
                    delivery = None
                    is_internal_marker = False
                    with self._lock:
                        if term.pending_commands:
                            command = term.pending_commands[0]
                            is_marker, exit_code = self._parse_completion_marker(
                                line, command.marker
                            )
                            if is_marker:
                                marker_pos = line.find(command.marker)
                                # Preserve output written without a trailing
                                # newline before the marker. The marker itself
                                # remains internal and is excluded from logs.
                                if marker_pos > 0:
                                    prefix = line[:marker_pos]
                                    command.output += prefix
                                    term.output_buffer += prefix
                                term.pending_commands.popleft()
                                is_internal_marker = True
                                delivery = self._queue_background_completion_locked(
                                    term, command, exit_code
                                )
                            else:
                                command.output += line

                        if not is_internal_marker:
                            # Keep complete terminal output while excluding
                            # the internal completion marker.
                            term.output_buffer += line

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
            return None, "A timeout is required for blocking terminals."
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
    ) -> Tuple[bool, str]:
        """
        Execute a command in a terminal.

        Blocking terminals require a timeout and clamp it to 20 seconds.
        Background terminals submit commands immediately; their timeout is
        intentionally ignored because the persistent shell may run commands
        for an arbitrary length of time. Completion is returned later through
        the configured callback or drain_completed_background_commands().
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
            )
            if sys.platform == "win32":
                # `cmd /Q` suppresses command echo. Keep the user's command
                # in the persistent shell so its quoting and shell state are
                # unchanged; the marker is emitted on the following line
                # while ERRORLEVEL still belongs to that command. `ver`
                # resets ERRORLEVEL for the next queued command (built-ins
                # such as `echo` do not reliably do so).
                wrapped_command = (
                    f"{command}\n"
                    f"echo {marker}:%errorlevel%\n"
                    f"ver >nul 2>nul\n"
                )
            else:
                # Print a leading newline for the same no-trailing-newline
                # case, then report the command's status from the same shell.
                wrapped_command = (
                    f"{command}\n"
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
                "Command sent to background terminal; it will remain running "
                "as needed and report its completion asynchronously."
            )

        # Blocking execution: timeout is required and capped at 20 seconds.
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

    def drain_completed_background_commands(self) -> List[dict]:
        """Return and remove background completions in finish order."""
        with self._lock:
            completions = list(self._completed_background_commands)
            self._completed_background_commands.clear()
        return completions

    @classmethod
    def format_background_completion(cls, event: dict) -> str:
        """Format one completion for the agent and the user-facing CLI.

        The event keeps the captured output, but this rendered notification is
        capped so a noisy background process cannot consume the conversation
        context. The terminal ID and command let the agent call ``read_logs``
        when it needs the uncapped output.
        """
        terminal_id = event.get("terminal_id", "?")
        command = event.get("command", "")
        success = bool(event.get("success"))
        exit_code = event.get("exit_code")
        duration = event.get("duration_seconds")
        status = "finished successfully" if success else "finished with errors"
        if exit_code is not None:
            status += f" (exit code {exit_code})"
        if duration is not None:
            status += f" in {float(duration):.1f}s"

        lines = [
            "[Background terminal notification]",
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
        start_line: int,
        end_line: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Read logs from a background terminal by line numbers.
        start_line: 1-indexed line number to start reading from
        end_line: 1-indexed line number to end at (optional, defaults to end of buffer)
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

        if start_line < 1:
            return False, "start_line must be >= 1"

        if start_line > len(lines):
            return False, f"start_line ({start_line}) exceeds available lines ({len(lines)})"

        if end_line is None:
            end_line = len(lines)
        else:
            end_line = min(end_line, len(lines))

        selected_lines = lines[start_line - 1:end_line]
        return True, "\n".join(selected_lines)

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
