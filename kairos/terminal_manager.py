import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import queue

@dataclass
class Terminal:
    id: int
    background: bool
    process: Optional[subprocess.Popen] = None
    closed: bool = False
    output_buffer: str = ""  # Accumulated output for background terminals

class TerminalManager:
    def __init__(self):
        self.terminals: Dict[int, Terminal] = {}
        self.next_id = 1
        self._lock = threading.Lock()
        self._interrupt_event: Optional[threading.Event] = None

    def create_terminal(self, background: bool) -> int:
        with self._lock:
            terminal_id = self.next_id
            self.next_id += 1
        
        term = Terminal(id=terminal_id, background=background)
        
        if background:
            # Start a shell process for interactive commands
            if sys.platform == "win32":
                shell_cmd = ["cmd", "/k"]
            else:
                shell_cmd = ["/bin/bash", "--login"]
            term.process = subprocess.Popen(
                shell_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1
            )
            # Start reader thread
            threading.Thread(target=self._read_output, args=(term,), daemon=True).start()
        
        with self._lock:
            self.terminals[terminal_id] = term
        
        return terminal_id

    def _read_output(self, term: Terminal):
        """Background thread to read output from background terminal."""
        if term.process and term.process.stdout:
            for line in iter(term.process.stdout.readline, ''):
                if line:
                    # Must acquire lock before modifying output_buffer
                    with self._lock:
                        term.output_buffer += line

    def execute_command(self, terminal_id: int, command: str, timeout: Optional[int] = None, is_background: Optional[bool] = None) -> Tuple[bool, str]:
        """
        Execute command in specified terminal.
        Returns (success, output_or_error)
        """
        with self._lock:
            term = self.terminals.get(terminal_id)
        
        if not term:
            return False, f"Terminal {terminal_id} not found"
        
        if term.closed:
            return False, f"Terminal {terminal_id} is closed"

        # Validate background flag matches terminal type
        if is_background is not None and is_background != term.background:
            return False, f"Terminal {terminal_id} is {'background' if term.background else 'blocking'}, but request specified {'background' if is_background else 'blocking'}"

        if term.background:
            # Send command to background shell
            if term.process and term.process.stdin:
                try:
                    term.process.stdin.write(command + "\n")
                    term.process.stdin.flush()
                    return True, "Command sent to background terminal"
                except Exception as e:
                    return False, f"Failed to send command: {str(e)}"
            return False, "Background terminal process not available"
        else:
            # Interruptible blocking execution using Popen + poll loop
            try:
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                deadline = time.time() + timeout if timeout else None
                while True:
                    ret = proc.poll()
                    if ret is not None:
                        break
                    if self._interrupt_event and self._interrupt_event.is_set():
                        proc.kill()
                        proc.wait(timeout=2)
                        return False, "[Interrupted]"
                    if deadline and time.time() > deadline:
                        proc.kill()
                        proc.wait(timeout=2)
                        return False, f"Command timed out after {timeout} seconds"
                    time.sleep(0.05)
                output = proc.stdout.read() if proc.stdout else ""
                return True, output if output else "Command executed successfully (no output)"
            except Exception as e:
                return False, f"Command execution failed: {str(e)}"

    def read_logs(self, terminal_id: int, start_line: int, end_line: Optional[int] = None) -> Tuple[bool, str]:
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
        with self._lock:
            term = self.terminals.get(terminal_id)
        
        if not term:
            return False, f"Terminal {terminal_id} not found"
        
        if term.closed:
            return False, f"Terminal {terminal_id} is already closed"
        
        success = True
        error_msg = ""
        
        if term.background and term.process:
            try:
                term.process.terminate()
                try:
                    term.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    term.process.kill()
                    term.process.wait(timeout=2)
            except Exception as e:
                success = False
                error_msg = f" (warning: {str(e)})"
        
        term.closed = True
        with self._lock:
            del self.terminals[terminal_id]
        
        if success:
            return True, f"Terminal {terminal_id} closed"
        else:
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
            "line_count": len(term.output_buffer.splitlines()) if term.background else 0
        }