import threading
import time
import sys
import os
import base64
from typing import Optional, Callable, Dict

import subprocess
import tempfile
from pathlib import Path

from rich.console import Console, ConsoleOptions, RenderResult
from rich.panel import Panel
from rich.markdown import Markdown, MarkdownElement, MarkdownContext, TableElement as _BaseTableElement
from rich.syntax import Syntax
from rich.text import Text
from rich.live import Live
from rich.table import Table
from rich import box


class _EnhancedTableElement(MarkdownElement):
    """TableElement replacement that renders markdown tables with rounded boxes,
    bold headers on a subtle background, and alternating row shading."""

    def __init__(self) -> None:
        self.header = None
        self.body = None

    @classmethod
    def create(cls, markdown, token):
        return cls()

    def on_child_close(self, context: MarkdownContext, child: MarkdownElement) -> bool:
        from rich.markdown import TableHeaderElement, TableBodyElement
        if isinstance(child, TableHeaderElement):
            self.header = child
        elif isinstance(child, TableBodyElement):
            self.body = child
        else:
            raise RuntimeError("Couldn't process markdown table.")
        return False

    def on_enter(self, context: MarkdownContext) -> None:
        pass

    def on_leave(self, context: MarkdownContext) -> None:
        pass

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        table = Table(
            box=box.ROUNDED,
            pad_edge=False,
            border_style="cyan",
            show_edge=True,
            collapse_padding=True,
            header_style="bold bright_white on grey15",
            row_styles=["", "dim"],
            title_style="bold cyan",
        )

        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                heading = column.content.copy()
                heading.stylize("markdown.table.header")
                table.add_column(heading)

        if self.body is not None:
            for row in self.body.rows:
                row_content = [element.content for element in row.cells]
                table.add_row(*row_content)

        yield table


class KairosMarkdown(Markdown):
    """Enhanced Markdown renderer with polished table rendering.

    Replaces the default ``TableElement`` with a version that uses rounded
    boxes, bold headers on a subtle dark background, and alternating row
    shading for better readability.
    """

    elements = dict(Markdown.elements)
    elements["table_open"] = _EnhancedTableElement
from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

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


def _insert_text_paste(buf, text: str) -> None:
    """Store pasted text and insert a visible token into the prompt buffer."""
    if not text:
        return
    token = _make_text_token()
    _paste_registry[token] = {
        "type": "text",
        "text": text,
        "text_stripped": text.strip(),
    }
    buf.insert_text(token)


# ------------------------------------------------------------------ #
#  Key bindings (text/image paste, backspace deletes paste tokens)    #
# ------------------------------------------------------------------ #

@_kb.add(Keys.BracketedPaste)
def _bracketed_paste_handler(event):
    """Handle terminals that send a bracketed-paste event."""
    try:
        # Match prompt_toolkit's normal bracketed-paste line-ending behavior.
        text = event.data.replace("\r\n", "\n").replace("\r", "\n")
        _insert_text_paste(event.current_buffer, text)
    except Exception:
        pass


@_kb.add("c-v")
def _paste_handler(event):
    """Handle Ctrl+V when the terminal passes the key to prompt_toolkit.

    Some terminals intercept Ctrl+V and emit a bracketed-paste event instead;
    those terminals are handled by ``_bracketed_paste_handler`` above.
    """
    try:
        # Text is the normal Ctrl+V payload. Images remain explicit via Alt+V.
        _insert_text_paste(event.current_buffer, _read_system_clipboard())
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

        # PromptSession with paste support (Alt+V for images)
        self._prompt_session = PromptSession(
            key_bindings=_kb,
            enable_open_in_editor=False,
        )

        # Escape key listener (active during agent execution). The listener
        # shares stdin with the next prompt, so ordinary characters captured
        # during the response-to-prompt handoff are buffered and replayed
        # instead of being silently discarded.
        self._escape_listening = False
        self._escape_stop_event = threading.Event()
        self._escape_listener_thread: Optional[threading.Thread] = None
        self._on_escape: Optional[Callable[[], None]] = None
        self._pending_input = ""
        self._pending_input_lock = threading.Lock()

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

    def print_background_notification(self, message: str):
        """Show a completed background command without hiding its details."""
        self.console.print(
            Panel(
                message,
                border_style="yellow",
                title="Background terminal",
                padding=(0, 1),
            )
        )

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
                display = KairosMarkdown(text) if text else Text("")
            except Exception:
                display = text or ""
            self._live.update(Panel(display, border_style="green", padding=(1, 2)))
            self._live.stop()
            self._live = None
        self._stream_text = ""
        self._skip_print_response = True

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

    def print_subagent_token_status(self, subagent_id: str, token_counter):
        """Print a child agent's token status without mixing it with the parent."""
        status = token_counter.format_status()
        self.console.print(
            f"[dim]Subagent {subagent_id}: {status}[/dim]"
        )

    # ------------------------------------------------------------------ #
    #  Final response (green panel)                                        #
    # ------------------------------------------------------------------ #

    def print_response(self, content: str):
        try:
            md = KairosMarkdown(content)
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

    def pick_session(
        self, sessions: list, initial_choice: Optional[str] = None
    ) -> Optional[str]:
        """Prompt for and return a saved-session ID.

        ``initial_choice`` supports commands such as ``/resume 1``. Pending
        input captured by the Escape handoff is consumed here as well as by
        the normal prompt, so a quickly entered ``/resume`` followed by
        ``1`` cannot fall through to the agent as an ordinary request.
        """
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

        choice = initial_choice
        while True:
            if choice is None:
                pending_line, pending_default = self._take_pending_input_for_prompt()
                if pending_line is not None:
                    choice = pending_line
                else:
                    try:
                        choice = self._prompt_session.prompt(
                            HTML(
                                '<style class="prompt">Pick a chat (number) '
                                'or press Enter to cancel: </style>'
                            ),
                            style=PROMPT_STYLE,
                            default=pending_default,
                        ).strip()
                    except (EOFError, KeyboardInterrupt):
                        return None

            choice = choice.strip()
            if not choice:
                return None
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(sessions):
                    return sessions[idx]["id"]
            except ValueError:
                pass

            self.console.print("[red]Invalid choice. Enter a chat number or press Enter to cancel.[/red]")
            choice = None

    # ------------------------------------------------------------------ #
    #  Escape key listener                                                 #
    # ------------------------------------------------------------------ #

    def start_escape_listener(self, on_escape: Callable[[], None]):
        """Start watching for Escape without losing ordinary terminal input.

        The listener necessarily shares stdin with ``PromptSession``. If a
        user types the next command while an agent response is finishing, the
        listener may see those bytes first. It therefore buffers every
        non-Escape byte for the next prompt instead of silently discarding it.
        """
        self._escape_stop_event.clear()
        self._on_escape = on_escape
        self._escape_listening = True
        self._escape_listener_thread = threading.Thread(
            target=self._escape_listener_loop, daemon=True
        )
        self._escape_listener_thread.start()

    def stop_escape_listener(self):
        """Stop the Escape listener before the next prompt owns stdin."""
        self._escape_listening = False
        self._escape_stop_event.set()
        thread = self._escape_listener_thread
        if thread and thread.is_alive():
            # Both listener loops use short polling timeouts. Wait for the
            # thread to finish before the next prompt owns stdin; otherwise a
            # late listener read could still consume the next command.
            thread.join()
        self._escape_listener_thread = None
        self._on_escape = None

    def _buffer_intercepted_input(self, text: str) -> None:
        """Save input consumed by the Escape listener for the next prompt."""
        if not text:
            return
        with self._pending_input_lock:
            self._pending_input += text

    def _take_pending_input_for_prompt(self) -> tuple[Optional[str], str]:
        """Take a complete intercepted line or return a partial line as default.

        ``PromptSession`` must remain the owner of normal editing, but a user
        can press keys during the response-to-prompt handoff. Complete lines
        are returned directly; a partial line is pre-filled into the next
        prompt so it can be edited or submitted normally.
        """
        with self._pending_input_lock:
            pending = self._pending_input
            self._pending_input = ""

        if not pending:
            return None, ""

        for index, char in enumerate(pending):
            if char not in "\r\n":
                continue
            remainder_start = index + 1
            if char == "\r" and remainder_start < len(pending) and pending[remainder_start] == "\n":
                remainder_start += 1
            with self._pending_input_lock:
                self._pending_input = pending[remainder_start:]
            return pending[:index], ""

        return None, pending

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
        while not self._escape_stop_event.is_set() and self._escape_listening:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch == "\x1b":
                    if self._on_escape:
                        self._on_escape()
                    return
                # Extended-key prefixes are not useful as text in the next
                # prompt. Preserve ordinary characters, including Enter.
                if ch not in ("\x00", "\xe0"):
                    self._buffer_intercepted_input(ch)
            self._escape_stop_event.wait(0.05)

    def _escape_listener_unix(self):
        import tty, select, termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            # cbreak keeps Ctrl+C signal handling enabled while still making
            # individual Escape and character bytes observable.
            tty.setcbreak(fd)
            while not self._escape_stop_event.is_set() and self._escape_listening:
                rlist, _, _ = select.select([fd], [], [], 0.05)
                if rlist:
                    ch = os.read(fd, 1)
                    if ch == b"\x1b":
                        if self._on_escape:
                            self._on_escape()
                        return
                    if ch:
                        self._buffer_intercepted_input(
                            ch.decode(sys.stdin.encoding or "utf-8", errors="replace")
                        )
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    # ------------------------------------------------------------------ #
    #  User input  (the main entry point — handles all paste detection)   #
    # ------------------------------------------------------------------ #

    def get_user_input(self, prefix: str = "kairos> ") -> Optional[str]:
        try:
            # Reset paste token counters for this prompt
            _reset_paste_counters()
            _paste_registry.clear()

            pending_line, pending_default = self._take_pending_input_for_prompt()
            if pending_line is not None:
                # The Escape listener may have captured a complete command
                # while the previous request was finishing. Return it as the
                # next prompt submission instead of making the user type it
                # again.
                return pending_line.strip()

            # Text and image pastes are handled by explicit key bindings above.
            # Bracketed-paste events carry their own payload, while Ctrl+V
            # reads the clipboard only because the user explicitly pressed it.
            # We intentionally do not infer a paste from arbitrary buffer
            # changes or from the Windows clipboard sequence number: copying
            # new content changes that number without performing a paste.

            # ---- Run the prompt ----
            result = self._prompt_session.prompt(
                HTML(f'<style class="prompt">{prefix}</style>'),
                style=PROMPT_STYLE,
                default=pending_default,
            ).strip()

            # Note: paste token resolution happens in main.py, not here.
            # main.py handles both text and image tokens in one pass.
            # Images are pasted explicitly via Alt+V keybinding.

            return result

        except (EOFError, KeyboardInterrupt):
            return None
