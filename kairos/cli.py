import threading
import time
import sys
import os
import base64
from typing import Optional, Callable, Dict, List, Tuple

import hashlib
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

    This is a single ctypes call — virtually free. The number increments
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

def _get_image_hash(data: Optional[bytes]) -> Optional[str]:
    """Return MD5 hex digest of image bytes, or None."""
    if data and len(data) > 0:
        return hashlib.md5(data).hexdigest()
    return None


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

    On Windows Terminal, Ctrl+V is intercepted by the terminal itself
    and this handler is NEVER called.  For that case, ``get_user_input()``
    uses ``Buffer.on_text_changed`` to detect text pastes and clipboard
    polling to detect image pastes.
    """
    try:
        buf = event.app.current_buffer

        # 1. Try image first
        image_data = _check_clipboard_has_image()
        if image_data and len(image_data) > 0:
            data_url = _image_data_to_url(image_data)
            token = _make_image_token()
            _paste_registry[token] = {"type": "image", "data_url": data_url}
            buf.insert_text(token)
            return

        # 2. Otherwise, try text
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
        self._last_clipboard_hash: Optional[str] = None

        # PromptSession with paste support (Ctrl+V)
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
        banner = Text()
        banner.append(
            "\u2588\u2584\u2588\u2584\u2588 \u2588\u2580\u2588 \u2588\u2584\u2588 \u2588\u2580\u2580 \u2588\u2580\u2588 \u2588\u2584\u2588\u2584\u2588\n\u2588\u2581\u2580\u2588\u2581\u2588 \u2588\u2588 \u2588\u2581\u2580 \u2588\u2588\u2584 \u2588\u2588\u2588 \u2588\u2581\u2580\u2588\u2581\u2588",
            style="bold cyan",
        )
        banner.append("\n", style="dim")
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
    #  Clipboard image detection (for auto-detect / post-prompt)          #
    # ------------------------------------------------------------------ #

    def check_clipboard_image(self) -> Optional[str]:
        """Check clipboard for image data. Returns a base64 data URL, or None.

        Deduplicates by content hash so the same image isn't returned twice
        in a row.
        """
        image_data = _check_clipboard_has_image()
        if image_data is None or len(image_data) == 0:
            return None
        h = hashlib.md5(image_data).hexdigest()
        if h == self._last_clipboard_hash:
            return None
        self._last_clipboard_hash = h
        return _image_data_to_url(image_data)

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

            # ---- 1. Snapshot clipboard state BEFORE the prompt ----
            pre_image_data = _check_clipboard_has_image()
            pre_image_hash = _get_image_hash(pre_image_data)
            _pre_clip_seq = [_get_clipboard_sequence_number()]

            # ---- 2. Set up paste detection via on_text_changed ----
            # Windows Terminal intercepts Ctrl+V and pastes raw characters
            # before prompt_toolkit sees a key event.  We detect this by:
            #   a) Watching for clipboard sequence number changes (cheap!)
            #   b) Then reading clipboard only when a change is detected
            _handling = [False]           # prevent recursion flag
            _prev_text = [buf.text]       # last known buffer state

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
                    return

                # Check if the clipboard changed since the prompt started.
                # On Windows this is a single ctypes call (< 1 microsecond).
                # On other platforms, fall back to buffer-growth heuristic.
                clip_changed = False
                if _pre_clip_seq[0] > 0:
                    clip_changed = _get_clipboard_sequence_number() != _pre_clip_seq[0]
                else:
                    # Non-Windows: assume paste if buffer grew by 3+ chars
                    clip_changed = (len(current) - len(_prev_text[0])) >= 3

                if not clip_changed:
                    _prev_text[0] = current
                    return

                # Clipboard changed — read the text content (expensive, but
                # only happens once per paste, not per keystroke)
                clip_text = _get_clipboard_text_clean()
                if not clip_text or len(clip_text) < 3:
                    _prev_text[0] = current
                    return

                # Check that the clipboard text appears in the NEW content
                # but NOT in the previous content — it was just added.
                if clip_text not in current or clip_text in _prev_text[0]:
                    _prev_text[0] = current
                    return

                # Read raw (non-stripped) version for the registry
                clip_raw = _read_system_clipboard() or clip_text

                _handling[0] = True
                try:
                    token = _make_text_token()
                    _paste_registry[token] = {
                        "type": "text",
                        "text": clip_raw,
                        "text_stripped": clip_text,
                    }
                    idx = current.find(clip_text)
                    if idx == -1:
                        return
                    b.text = (current[:idx] + token
                              + current[idx + len(clip_text):])
                    b.cursor_position = idx + len(token)
                    _prev_text[0] = b.text
                    # Update sequence number so we don't re-detect
                    _pre_clip_seq[0] = _get_clipboard_sequence_number()
                finally:
                    _handling[0] = False

            buf.on_text_changed += _on_text_changed

            # ---- 2. Image paste poller (for Windows Terminal) ----
            # When Ctrl+V pastes an image, Windows Terminal ignores it
            # (no buffer change), so on_text_changed never fires.
            # We poll the cheap clipboard sequence number every 200ms
            # and insert the image token when an image paste is detected.
            _prompt_active = [True]
            _image_paste_thread = [None]

            def _image_paste_poller():
                """Background thread: poll clipboard for image pastes."""
                last_seq = _pre_clip_seq[0]
                while _prompt_active[0]:
                    time.sleep(0.2)
                    if not _prompt_active[0]:
                        break
                    cur_seq = _get_clipboard_sequence_number()
                    if cur_seq == last_seq or cur_seq == 0:
                        continue
                    # Clipboard changed — check if it's an image
                    try:
                        img_data = _check_clipboard_has_image()
                        if img_data and len(img_data) > 0:
                            # Don't double-detect: check if this image
                            # was already the pre-prompt image
                            img_hash = _get_image_hash(img_data)
                            if img_hash != pre_image_hash:
                                data_url = _image_data_to_url(img_data)
                                token = _make_image_token()
                                _paste_registry[token] = {
                                    "type": "image", "data_url": data_url
                                }
                                if not _handling[0]:
                                    _handling[0] = True
                                    try:
                                        buf.insert_text(" " + token)
                                        _prev_text[0] = buf.text
                                    finally:
                                        _handling[0] = False
                    except Exception:
                        pass
                    last_seq = cur_seq

            _img_thread = threading.Thread(
                target=_image_paste_poller, daemon=True
            )
            _img_thread.start()
            _image_paste_thread[0] = _img_thread

            # ---- 3. Run the prompt ----
            try:
                result = self._prompt_session.prompt(
                    HTML(f'<style class="prompt">{prefix}</style>'),
                    style=PROMPT_STYLE,
                ).strip()
            finally:
                _prompt_active[0] = False
                buf.on_text_changed -= _on_text_changed
                # Wait for poller thread to notice and exit
                if _image_paste_thread[0]:
                    _image_paste_thread[0].join(timeout=0.5)

            # ---- 4. Post-prompt: detect IMAGE paste (fallback) ----
            # Images can't be pasted as text, so on_text_changed won't
            # catch them.  We detect the clipboard change here.
            try:
                post_image_data = _check_clipboard_has_image()
                post_image_hash = _get_image_hash(post_image_data)
                if post_image_hash and post_image_hash != pre_image_hash:
                    data_url = _image_data_to_url(post_image_data)
                    token = _make_image_token()
                    _paste_registry[token] = {"type": "image", "data_url": data_url}
                    if result:
                        result = result + " " + token
                    else:
                        result = token
            except Exception:
                pass

            # Note: paste token resolution happens in main.py, not here.
            # main.py handles both text and image tokens in one pass.

            return result

        except (EOFError, KeyboardInterrupt):
            return None
