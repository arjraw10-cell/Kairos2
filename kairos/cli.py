import threading
import time
import sys
import os
import base64
from typing import Optional, Callable, Dict

import subprocess
import tempfile
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from rich.live import Live
from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings

console = Console()

PROMPT_STYLE = Style.from_dict({
    "prompt": "ansicyan bold",
    "": "ansidefault",
})

# ---- Paste counters (incrementing numbers for display, reset per prompt) ----
_image_counter = 0
_text_counter = 0


def _reset_paste_counters():
    """Reset paste token counters at the start of each prompt."""
    global _image_counter, _text_counter
    _image_counter = 0
    _text_counter = 0


def _make_image_token() -> str:
    """Create a numbered image paste token like ``(Pasted Image #1)``."""
    global _image_counter
    _image_counter += 1
    return f"(Pasted Image #{_image_counter})"


def _make_text_token() -> str:
    """Create a numbered text paste token like ``(Pasted Text #1)``."""
    global _text_counter
    _text_counter += 1
    return f"(Pasted Text #{_text_counter})"


# Token registry: maps token string -> content dict
# content = {"type": "text", "text": original_text, "text_stripped": stripped}
#         or {"type": "image", "data_url": base64_data_url}
_paste_registry: Dict[str, dict] = {}

_kb = KeyBindings()


# ------------------------------------------------------------------ #
#  Clipboard sequence number (cheap change detection on Windows)      #
# ------------------------------------------------------------------ #

def _get_clipboard_sequence_number() -> int:
    """Return the current clipboard sequence number (Windows only).

    This is a single ctypes call -- virtually free. The number increments
    whenever clipboard content changes, letting us detect pastes without
    spawning subprocesses on every keystroke.

    Returns 0 on non-Windows or on failure.
    """
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        return ctypes.windll.user32.GetClipboardSequenceNumber()
    except Exception:
        return 0


def _ctypes_read_clipboard() -> str:
    """Read clipboard text via Win32 API. Sub-millisecond on Windows.

    Uses direct ctypes calls (OpenClipboard / GetClipboardData / GlobalLock)
    instead of spawning PowerShell.  Returns empty string on failure or
    non-Windows.
    """
    if sys.platform != "win32":
        return _read_system_clipboard()
    try:
        import ctypes
        CF_UNICODETEXT = 13
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.OpenClipboard(0):
            return ""
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            p = kernel32.GlobalLock(h)
            if not p:
                return ""
            text = ctypes.c_wchar_p(p).value or ""
            kernel32.GlobalUnlock(h)
            return text
        finally:
            user32.CloseClipboard()
    except Exception:
        return ""


# ------------------------------------------------------------------ #
#  Clipboard helpers (cross-platform)                                 #
# ------------------------------------------------------------------ #

def _check_clipboard_has_image() -> Optional[bytes]:
    """Quick check if the clipboard contains an image. Returns raw image bytes or None."""
    try:
        if sys.platform == "win32":
            # Fast ctypes check
            try:
                import ctypes
                user32 = ctypes.windll.user32
                CF_DIB = 8
                if not user32.IsClipboardFormatAvailable(CF_DIB):
                    return None
            except Exception:
                pass

            # Save clipboard image to a temp file via PowerShell
            # NOTE: no text=True — we don't read stdout, and text mode
            #       causes UnicodeDecodeError on cp1252 systems.
            tmp_path = Path(tempfile.gettempdir()) / f"kairos_paste_{int(time.time() * 1000)}.png"
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -Assembly System.Windows.Forms; "
                 "$img = [System.Windows.Forms.Clipboard]::GetImage(); "
                 f"if ($img) {{ $img.Save('{tmp_path}') }}"],
                capture_output=True, timeout=5,
            )
            if tmp_path.exists() and tmp_path.stat().st_size > 0:
                image_data = tmp_path.read_bytes()
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
                return image_data
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass

        elif sys.platform == "darwin":
            tmp_path = Path(tempfile.gettempdir()) / f"kairos_paste_{int(time.time() * 1000)}.png"
            result = subprocess.run(
                ["pngpaste", str(tmp_path)],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
                image_data = tmp_path.read_bytes()
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
                return image_data
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass

        else:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout

    except Exception:
        pass
    return None


def _detect_mime(data: bytes) -> str:
    """Detect image MIME type from raw bytes."""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:4] == b'GIF8':
        return 'image/gif'
    if data[:4] == b'RIFF' and len(data) > 12 and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[:2] == b'BM':
        return 'image/bmp'
    if data[:4] in (b'II\x2a\x00', b'MM\x00\x2a'):
        return 'image/tiff'
    return 'image/png'


def _read_system_clipboard() -> str:
    """Read text from the OS clipboard (cross-platform)."""
    try:
        if sys.platform == "win32":
            check = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -Assembly System.Windows.Forms; "
                 "if ([System.Windows.Forms.Clipboard]::ContainsText()) { 'yes' }"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=3,
            )
            if "yes" not in check.stdout.lower():
                return ""
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -Assembly System.Windows.Forms; "
                 "[System.Windows.Forms.Clipboard]::GetText()"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=3,
            )
            return r.stdout
        elif sys.platform == "darwin":
            r = subprocess.run(["pbpaste"], capture_output=True,
                               text=True, encoding="utf-8",
                               errors="replace", timeout=3)
            if r.returncode != 0:
                return ""
            return r.stdout
        else:
            r = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=3)
            if r.returncode != 0:
                return ""
            return r.stdout
    except Exception:
        return ""


def _image_data_to_url(data: bytes) -> str:
    """Convert raw image bytes to a base64 data URL for the vision API."""
    mime = _detect_mime(data)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


# ------------------------------------------------------------------ #
#  Clipboard monitoring helpers                                       #
# ------------------------------------------------------------------ #

def _get_clipboard_text_clean() -> str:
    """Read clipboard text, stripped. Returns empty string on failure."""
    try:
        t = _read_system_clipboard()
        return t.strip() if t else ""
    except Exception:
        return ""


# ------------------------------------------------------------------ #
#  Key bindings (work on terminals that pass Ctrl+V through)          #
# ------------------------------------------------------------------ #

@_kb.add("c-v")
def _paste_handler(event):
    """Handle Ctrl+V on terminals that pass the key event through.

    Text paste only. Images are pasted via Alt+V (see _alt_v_handler).

    On Windows Terminal, Ctrl+V is intercepted by the terminal itself
    and this handler is NEVER called.  For that case, ``get_user_input()``
    uses ``Buffer.on_text_changed`` to detect text pastes.
    """
    try:
        buf = event.app.current_buffer
        text = _read_system_clipboard()
        if text:
            token = _make_text_token()
            _paste_registry[token] = {
                "type": "text",
                "text": text,
                "text_stripped": text.strip(),
            }
            buf.insert_text(token)
    except Exception:
        pass


@_kb.add("escape", "v")
def _alt_v_handler(event):
    """Handle Alt+V: paste an image from the clipboard.

    Reads the clipboard for image data and inserts a ``(Pasted Image #N)``
    token if found.  If the clipboard has no image, shows a brief message.
    """
    try:
        buf = event.app.current_buffer
        image_data = _check_clipboard_has_image()
        if image_data and len(image_data) > 0:
            data_url = _image_data_to_url(image_data)
            token = _make_image_token()
            _paste_registry[token] = {"type": "image", "data_url": data_url}
            buf.insert_text(token)
        else:
            # Brief inline hint — doesn't survive long but user sees it
            buf.insert_text("[no image on clipboard]")
    except Exception:
        pass


@_kb.add("backspace")
def _backspace_handler(event):
    """Custom backspace: if the cursor is inside or immediately after a paste
    token, delete the entire token.  Otherwise, fall back to normal backspace."""
    buf = event.app.current_buffer
    token = _find_token_at_or_before_cursor(buf.text, buf.cursor_position)
    if token:
        idx = buf.text.find(token, buf.cursor_position - len(token))
        if idx == -1:
            buf.delete_before_cursor()
            return
        buf.text = buf.text[:idx] + buf.text[idx + len(token):]
        buf.cursor_position = idx
        _paste_registry.pop(token, None)
    else:
        buf.delete_before_cursor()


def _find_token_at_or_before_cursor(text: str, cursor_pos: int) -> Optional[str]:
    """Return the paste token whose span contains cursor_pos, or that
    ends right at cursor_pos, or None."""
    for token in _paste_registry:
        idx = text.find(token, max(0, cursor_pos - len(token)))
        if idx != -1 and idx <= cursor_pos <= idx + len(token):
            return token
    for token in _paste_registry:
        start = cursor_pos - len(token)
        if start >= 0 and text[start:cursor_pos] == token:
            return token
    return None




# ------------------------------------------------------------------ #
#  CLI class                                                          #
# ------------------------------------------------------------------ #

class CLI:
    def __init__(self):
        self.console = console
        self._stop_thinking = threading.Event()
        self._thinking_thread: Optional[threading.Thread] = None
        self._live: Optional[Live] = None
        self._stream_text = ""
        self._skip_print_response = False

        # PromptSession with paste support (Ctrl+V / Alt+V)
        self._prompt_session = PromptSession(
            key_bindings=_kb,
            enable_open_in_editor=False,
        )

        # Escape key listener (active during agent execution)
        self._escape_listening = False
        self._escape_listener_thread: Optional[threading.Thread] = None
        self._on_escape: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------ #
    #  Banner / info                                                       #
    # ------------------------------------------------------------------ #

    def print_banner(self):
        # Build banner using chr(92) to avoid all backslash escaping issues
        BS = chr(92)  # backslash character
        line1 = " __  __     ______   __  __        ____  ____  ___"
        line2 = "/  " + BS + "/  | __|__  / | /  " + BS + "/  | __ _|  _ " + BS + "/ ___|/ __|"
        line3 = BS + "      / |_ / / /| ||      |/ _" + chr(96) + " | |_) " + BS + "___ " + BS + " (__ "
        line4 = " " + BS + "__" + BS + "__/ /__/____|_|" + BS + "____/" + BS + "__,_|____/|____/" + BS + "___|"
        banner_art = line1 + chr(10) + line2 + chr(10) + line3 + chr(10) + line4 + chr(10)
        banner = Text()
        banner.append(banner_art, style="bold cyan")
        banner.append("Minimal Coding Agent", style="italic white")
        self.console.print(Panel(banner, border_style="cyan", padding=(1, 2)))
        self.console.print()

    def print_workspace(self, path: str):
        self.console.print(f"[dim]Workspace:[/dim] [bold cyan]{path}[/bold cyan]")

    def print_info(self, message: str):
        self.console.print(f"[cyan]Info:[/cyan] {message}")

    def print_error(self, message: str):
        if "\n" in message:
            self.console.print(
                Panel(f"[red]{message}[/red]", border_style="red",
                      title="Error", padding=(1, 2))
            )
        else:
            self.console.print(f"[red]Error:[/red] {message}")

    def print_exit(self):
        self.console.print("\n[dim]Goodbye![/dim]\n")

    def clear_screen(self):
        self.console.clear()

    # ------------------------------------------------------------------ #
    #  Animated thinking dots                                              #
    # ------------------------------------------------------------------ #

    def start_thinking(self):
        self._stop_thinking.clear()
        self._thinking_thread = threading.Thread(target=self._thinking_animation, daemon=True)
        self._thinking_thread.start()

    def stop_thinking(self):
        self._stop_thinking.set()
        if self._thinking_thread and self._thinking_thread.is_alive():
            self._thinking_thread.join(timeout=0.5)
        sys.stdout.write("\r" + " " * 30 + "\r")
        sys.stdout.flush()
        if self._live:
            self._live.stop()
            self._live = None
            self._stream_text = ""

    def _thinking_animation(self):
        dots = 0
        while not self._stop_thinking.is_set():
            dots = (dots % 3) + 1
            msg = "Thinking" + "." * dots + " " * (3 - dots)
            sys.stdout.write(f"\r\033[2m\033[3m{msg}\033[0m")
            sys.stdout.flush()
            self._stop_thinking.wait(0.4)

    # ------------------------------------------------------------------ #
    #  Streaming display (grey panel)                                      #
    # ------------------------------------------------------------------ #

    def start_stream(self):
        self.stop_thinking()
        self._stream_text = ""
        self._live = Live(
            Panel("[italic dim]...[/italic dim]", border_style="dim", padding=(0, 1)),
            console=self.console,
            refresh_per_second=8,
            transient=False,
        )
        self._live.start()

    def on_stream_token(self, token: str):
        self._stream_text += token
        if self._live:
            self._live.update(
                Panel(f"[italic dim]{self._stream_text}[/italic dim]",
                      border_style="dim", padding=(0, 1))
            )

    def finish_stream(self) -> str:
        text = self._stream_text
        if self._live:
            if not text.strip() or text.strip() == "...":
                self._live.update(Text(""))
            self._live.stop()
            self._live = None
        self._stream_text = ""
        return text

    def finalize_stream_as_response(self):
        text = self._stream_text
        if self._live:
            try:
                display = Markdown(text) if text else Text("")
            except Exception:
                display = text or ""
            self._live.update(Panel(display, border_style="green", padding=(1, 2)))
            self._live.stop()
            self._live = None
        self._stream_text = ""

    # ------------------------------------------------------------------ #
    #  Tool call summary                                                   #
    # ------------------------------------------------------------------ #

    def print_tool_summary(self, summary: str):
        self.console.print(f"  [dim]\u2192[/dim] {summary}")

    # ------------------------------------------------------------------ #
    #  Thinking trace                                                      #
    # ------------------------------------------------------------------ #

    def print_thinking_trace(self, text: str):
        self.console.print(
            Panel(f"[italic dim]{text}[/italic dim]", border_style="dim",
                  padding=(0, 1))
        )

    # ------------------------------------------------------------------ #
    #  Token status bar                                                    #
    # ------------------------------------------------------------------ #

    def print_token_status(self, token_counter):
        status = token_counter.format_status()
        self.console.print(f"[dim]{status}[/dim]")

    # ------------------------------------------------------------------ #
    #  Final response (green panel)                                        #
    # ------------------------------------------------------------------ #

    def print_response(self, content: str):
        try:
            md = Markdown(content)
            self.console.print(Panel(md, border_style="green", padding=(1, 2)))
        except Exception:
            self.console.print(Panel(content, border_style="green", padding=(1, 2)))

    def print_code(self, code: str, language: str = "python"):
        syntax = Syntax(code, language, theme="monokai", line_numbers=True)
        self.console.print(syntax)

    def print_tool_call(self, tool_name: str, args: dict):
        args_str = ", ".join(f"{k}=[yellow]{v}[/yellow]" for k, v in args.items())
        self.console.print(f"[blue]\u2192[/blue] [bold]{tool_name}[/bold]({args_str})")

    def print_tool_result(self, success: bool, output: str, error: Optional[str] = None):
        if success:
            if output and len(output) < 200:
                self.console.print(f"[green]\u2713[/green] {output}")
            else:
                self.console.print(f"[green]\u2713[/green] Command executed")
        else:
            error_msg = error or output
            self.console.print(Panel(f"[red]{error_msg}[/red]", border_style="red",
                                     title="Tool Error"))

    # ------------------------------------------------------------------ #
    #  Chat list picker                                                    #
    # ------------------------------------------------------------------ #

    def pick_session(self, sessions: list) -> Optional[str]:
        if not sessions:
            self.console.print("[yellow]No saved chats found.[/yellow]")
            return None
        self.console.print()
        self.console.print("[bold]Saved chats:[/bold]")
        for i, s in enumerate(sessions):
            ts = s.get("timestamp", "unknown")
            preview = s.get("preview", "")
            self.console.print(f"  [cyan]{i + 1}.[/cyan] {ts}  {preview}...")
        self.console.print()
        try:
            choice = self._prompt_session.prompt(
                HTML('<style class="prompt">Pick a chat (number) or press Enter to cancel: </style>'),
                style=PROMPT_STYLE,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not choice:
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(sessions):
                return sessions[idx]["id"]
        except ValueError:
            pass
        self.console.print("[red]Invalid choice.[/red]")
        return None

    # ------------------------------------------------------------------ #
    #  Escape key listener                                                 #
    # ------------------------------------------------------------------ #

    def start_escape_listener(self, on_escape: Callable[[], None]):
        self._on_escape = on_escape
        self._escape_listening = True
        self._escape_listener_thread = threading.Thread(
            target=self._escape_listener_loop, daemon=True
        )
        self._escape_listener_thread.start()

    def stop_escape_listener(self):
        self._escape_listening = False
        if self._escape_listener_thread and self._escape_listener_thread.is_alive():
            self._escape_listener_thread.join(timeout=0.5)
        self._escape_listener_thread = None
        self._on_escape = None

    def _escape_listener_loop(self):
        try:
            if sys.platform == "win32":
                self._escape_listener_windows()
            else:
                self._escape_listener_unix()
        except Exception:
            pass

    def _escape_listener_windows(self):
        import msvcrt
        while self._escape_listening:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch == "\x1b":
                    if self._on_escape:
                        self._on_escape()
                    return
            time.sleep(0.05)

    def _escape_listener_unix(self):
        import tty, select, termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while self._escape_listening:
                rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
                if rlist:
                    ch = os.read(fd, 1)
                    if ch == b"\x1b":
                        if self._on_escape:
                            self._on_escape()
                        return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            try:
                import termios as _t
                _t.tcflush(fd, _t.TCIFLUSH)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  User input  (the main entry point — handles all paste detection)   #
    # ------------------------------------------------------------------ #

    def get_user_input(self, prefix: str = "kairos> ") -> Optional[str]:
        try:
            # Reset paste token counters for this prompt
            _reset_paste_counters()
            _paste_registry.clear()

            buf = self._prompt_session.default_buffer

            # ---- Text paste detection via on_text_changed ----
            # Windows Terminal intercepts Ctrl+V and writes raw characters to
            # the buffer one at a time (growth=1 per keystroke).  We detect
            # pastes by reading the clipboard via a fast ctypes Win32 call
            # (~0.001ms) on every text increase and checking if the clipboard
            # text now appears in the buffer.  This works regardless of when
            # the text was copied (before or after prompt start).
            #
            # For long text that arrives character-by-character, we hold a
            # pending state and check each subsequent event until the full
            # clipboard text appears in the buffer.
            _handling = [False]           # prevent recursion flag
            _prev_text = [buf.text]       # last known buffer state
            _pre_clip_seq = [_get_clipboard_sequence_number()]
            _pending_paste = [None]       # clipboard text waiting to be matched
            _last_clip_seq = [0]          # seq number when pending state started
            _pending_count = [0]          # events since pending started (timeout)

            def _on_text_changed(b):
                """Replace terminal-intercepted text paste with a token."""
                if _handling[0]:
                    return

                current = b.text
                if current == _prev_text[0]:
                    return

                # Buffer shrank (user deleted) — not a paste
                if len(current) <= len(_prev_text[0]):
                    _prev_text[0] = current
                    _pending_paste[0] = None
                    return

                # ---- Try to resolve a paste ----
                paste_text = None   # clipboard text to match if found

                # 1) Did the clipboard change since our last known sequence?
                if _pre_clip_seq[0] > 0:
                    current_seq = _get_clipboard_sequence_number()
                    clip_changed = current_seq != _pre_clip_seq[0]
                    _pre_clip_seq[0] = current_seq
                else:
                    # Non-Windows fallback: buffer grew by 3+ chars
                    current_seq = 0
                    clip_changed = (len(current) - len(_prev_text[0])) >= 3

                if clip_changed:
                    # Clipboard changed — read it and try to match
                    clip_raw = _ctypes_read_clipboard()
                    clip_text = clip_raw.strip() if clip_raw else ""
                    if clip_text and len(clip_text) >= 3:
                        if clip_text in current and clip_text not in _prev_text[0]:
                            paste_text = clip_text
                            _pending_paste[0] = None
                            _pending_count[0] = 0
                        else:
                            # Clipboard changed but text not found yet —
                            # enter pending state so we keep trying on
                            # subsequent keystrokes.
                            _pending_paste[0] = clip_text
                            _last_clip_seq[0] = current_seq
                            _pending_count[0] = 0

                elif _pending_paste[0] is not None:
                    # Clipboard didn't change, but we have a pending paste.
                    # Check if clipboard changed *again* (new paste attempt).
                    if _pre_clip_seq[0] > 0:
                        current_seq = _get_clipboard_sequence_number()
                        if current_seq != _last_clip_seq[0]:
                            # Clipboard changed — update pending content
                            new_raw = _ctypes_read_clipboard()
                            new_clip = new_raw.strip() if new_raw else ""
                            if new_clip and len(new_clip) >= 3:
                                _pending_paste[0] = new_clip
                                _last_clip_seq[0] = current_seq
                                _pre_clip_seq[0] = current_seq
                                _pending_count[0] = 0

                    # Try to match the pending text in the current buffer
                    clip_text = _pending_paste[0]
                    if clip_text and clip_text in current and clip_text not in _prev_text[0]:
                        paste_text = clip_text
                        _pending_paste[0] = None
                        _pending_count[0] = 0
                    else:
                        _pending_count[0] += 1
                        if _pending_count[0] > 100:
                            _pending_paste[0] = None
                            _pending_count[0] = 0
                        else:
                            return

                if paste_text is None:
                    _prev_text[0] = current
                    return

                # ---- Replace the pasted text with a token ----
                clip_raw = _ctypes_read_clipboard() or paste_text

                _handling[0] = True
                try:
                    token = _make_text_token()
                    _paste_registry[token] = {
                        "type": "text",
                        "text": clip_raw,
                        "text_stripped": paste_text,
                    }
                    idx = current.find(paste_text)
                    if idx == -1:
                        return
                    b.text = (current[:idx] + token
                              + current[idx + len(paste_text):])
                    b.cursor_position = idx + len(token)
                    _prev_text[0] = b.text
                finally:
                    _handling[0] = False

            buf.on_text_changed += _on_text_changed

            # ---- Run the prompt ----
            try:
                result = self._prompt_session.prompt(
                    HTML(f'<style class="prompt">{prefix}</style>'),
                    style=PROMPT_STYLE,
                ).strip()
            finally:
                buf.on_text_changed -= _on_text_changed

            # Note: paste token resolution happens in main.py, not here.
            # main.py handles both text and image tokens in one pass.
            # Images are pasted explicitly via Alt+V keybinding.

            return result

        except (EOFError, KeyboardInterrupt):
            return None
