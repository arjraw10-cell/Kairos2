import threading
import time
import sys
import os
import io
import base64
from contextlib import contextmanager
from typing import Optional, Callable, Dict

import subprocess
import tempfile
from pathlib import Path

from rich.console import Console, ConsoleOptions, RenderResult
from rich.panel import Panel
from rich.markdown import Markdown, MarkdownElement, MarkdownContext, TableElement as _BaseTableElement
from rich.markup import escape
from rich.segment import Segment
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
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.filters import Condition
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import ANSI, HTML, FormattedText, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.keys import Keys

console = Console()

PROMPT_STYLE = Style.from_dict({
    "prompt": "ansicyan bold",
    "rprompt": "ansibrightblack",
    "bottom-toolbar": "noreverse ansibrightblack",
    "": "ansidefault",
})

# Lines of a wake reply previewed above the input while it streams.
PREVIEW_HEIGHT = 10


def describe_notification(notification: dict) -> str:
    """``sub-agent a3f2 finished`` from a background notification."""
    if notification.get("subagent_id"):
        who = f"sub-agent {notification['subagent_id']}"
    elif notification.get("terminal_id") is not None:
        who = f"terminal {notification['terminal_id']}"
    else:
        who = "background work"
    # subagent_finished -> finished, terminal_matched -> matched, ...
    verb = str(notification.get("kind") or "finished").rsplit("_", 1)[-1]
    return f"{who} {verb}"


def subagent_line(status: dict, now: float) -> str:
    """One compact status line: id, state, current tool, elapsed time."""
    elapsed = int(float(status.get("elapsed_s") or 0) + now - status.get("_seen", now))
    minutes, seconds = divmod(max(elapsed, 0), 60)
    parts = ["●", str(status.get("subagent_id")), str(status.get("state") or "")]
    if status.get("current_tool"):
        parts.append(str(status["current_tool"]))
    parts.append(f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s")
    return "  ".join(parts)


class _AbovePrompt:
    """Rich's output file while a prompt is open.

    Rich renders as usual; each flush prints the text above the prompt with
    prompt_toolkit's ``run_in_terminal`` on the prompt's event loop, which
    redraws the input line (and the user's half-typed text) below it. Text
    whose flush could not be scheduled is written by ``drain()`` once the
    prompt has closed.
    """

    def __init__(self, app, original):
        self._app = app
        self._original = original
        self._parts: list[str] = []
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self._parts.append(text)
        return len(text)

    def flush(self) -> None:
        try:
            self._app.loop.call_soon_threadsafe(
                lambda: run_in_terminal(self.drain, in_executor=False)
            )
        except (AttributeError, RuntimeError):
            pass  # the prompt is not running; drain() prints it later

    def drain(self) -> None:
        with self._lock:
            text, self._parts = "".join(self._parts), []
        if text:
            output = self._app.output
            # Same steps as prompt_toolkit's StdoutProxy (Windows resets autowrap).
            output.enable_autowrap()
            output.write_raw(text)
            output.flush()

    def isatty(self) -> bool:
        return self._original.isatty()

    @property
    def encoding(self) -> str:
        return getattr(self._original, "encoding", None) or "utf-8"


class _StreamView:
    """Live renderable for the grey streaming panel.

    It reads the CLI's text at render time, so a token only appends to a
    string. While streaming it shows the bottom of the panel that fits the
    terminal: with Rich's default ``ellipsis`` overflow the view froze at the
    top once a reply grew taller than the screen. ``full`` renders everything
    for the final frame printed when the stream ends.
    """

    def __init__(self, cli: "CLI"):
        self._cli = cli
        self.full = False

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self._cli._stream_text
        if not self.full:
            # The last height*width characters always contain every line
            # that can be visible, so rendering cost stays bounded.
            text = text[-(console.width * console.height):]
        panel = Panel(Text(text or "...", style="italic dim"), border_style="dim", padding=(0, 1))
        if self.full:
            yield panel
            return
        lines = console.render_lines(panel, options, pad=False)
        for line in lines[-max(3, console.height - 1):]:
            yield from line
            yield Segment.line()

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


def _read_windows_clipboard_text() -> str:
    """Read CF_UNICODETEXT through the Win32 API.

    This replaced two PowerShell launches that made Ctrl+V take ~2 s.
    """
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return ""
    # Another program can hold the clipboard briefly; retry a few times.
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _read_system_clipboard() -> str:
    """Read text from the OS clipboard (cross-platform)."""
    try:
        if sys.platform == "win32":
            return _read_windows_clipboard_text()
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
        self._stream_view: Optional[_StreamView] = None
        self._stream_text = ""
        self._streaming = False
        self._skip_print_response = False

        # Background work (wake turns, sub-agent status) renders from other
        # threads, also while the user is typing at the prompt.
        self._lock = threading.RLock()
        self._above: Optional[_AbovePrompt] = None  # set while a prompt is open
        self._wake = False  # a wake turn is rendering
        self._subagents: Dict[str, dict] = {}  # running sub-agents by id
        self._preview_key = None
        self._preview: list = []
        # Esc at the prompt while a wake turn runs (set by main.py).
        self.on_prompt_escape: Optional[Callable[[], None]] = None

        wake_keys = KeyBindings()

        @wake_keys.add("escape", filter=Condition(lambda: self._wake))
        def _stop_wake(event):
            if self.on_prompt_escape:
                self.on_prompt_escape()

        # PromptSession with paste support (Alt+V for images)
        self._prompt_session = PromptSession(
            key_bindings=merge_key_bindings([_kb, wake_keys]),
            enable_open_in_editor=False,
            refresh_interval=1.0,  # sub-agent elapsed times in the toolbar
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

    def print_banner(self, workspace: str = "", mode: str = ""):
        """Print a one-line banner: name, mode, workspace."""
        banner = Text()
        banner.append("KAIROS", style="bold cyan")
        if mode:
            banner.append(f"  {mode}", style="cyan")
        if workspace:
            banner.append(f"  {workspace}", style="dim")
        self.console.print(banner)
        self.console.print()

    def print_workspace(self, path: str):
        self.console.print(f"[dim]Workspace:[/dim] [bold cyan]{path}[/bold cyan]")

    def print_info(self, message: str):
        self.console.print(Text(message, style="dim"))

    def print_error(self, message: str):
        # Text(), not markup: error text often contains [brackets].
        if "\n" in message:
            self.console.print(
                Panel(Text(message, style="red"), border_style="red",
                      title="Error", padding=(1, 2))
            )
        else:
            self.console.print(Text.assemble(("Error: ", "red"), message))

    def print_exit(self):
        self.console.print("\n[dim]Goodbye![/dim]\n")

    def clear_screen(self):
        self.console.clear()

    # ------------------------------------------------------------------ #
    #  Animated thinking dots                                              #
    # ------------------------------------------------------------------ #

    def start_thinking(self):
        if self._wake:
            return  # a wake turn's output owns the screen until it ends
        self._stop_thinking.clear()
        self._thinking_thread = threading.Thread(target=self._thinking_animation, daemon=True)
        self._thinking_thread.start()

    def stop_thinking(self):
        self._stop_thinking.set()
        if self._thinking_thread is not None:
            self._thinking_thread.join(timeout=0.5)
            self._thinking_thread = None
            # Erase the dots only if they were drawn. Writing this while the
            # Live panel is active printed a stray blank line above it.
            sys.stdout.write("\r" + " " * 30 + "\r")
            sys.stdout.flush()
        with self._lock:
            # A wake reply keeps streaming; it moves to the prompt preview.
            if self._live and not self._wake:
                self._stream_view.full = True
                self._live.stop()
                self._live = None
                self._stream_text = ""
                self._streaming = False

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
        with self._lock:
            self._stream_text = ""
            self._streaming = True
            if self._above is not None:
                # At the prompt the reply streams in a preview above the input.
                self._invalidate()
                return
            self.stop_thinking()
            self._start_live()

    def _start_live(self):
        self._stream_view = _StreamView(self)
        self._live = Live(
            self._stream_view,
            console=self.console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()

    def on_stream_token(self, token: str):
        # Live's refresh thread (or the prompt's redraw) renders the text.
        self._stream_text += token
        self._invalidate()

    def finish_stream(self) -> str:
        with self._lock:
            text = self._stream_text
            if self._above is not None:
                if text.strip():
                    view = _StreamView(self)
                    view.full = True
                    self.console.print(view)
            elif self._live:
                if not text.strip() or text.strip() == "...":
                    self._live.update(Text(""))
                else:
                    self._stream_view.full = True
                self._live.stop()
                self._live = None
            self._stream_text = ""
            self._streaming = False
            self._invalidate()
            return text

    def finalize_stream_as_response(self):
        with self._lock:
            text = self._stream_text
            if self._above is not None:
                self.print_response(text)
                self._skip_print_response = True
            elif self._live:
                try:
                    display = KairosMarkdown(text) if text else Text("")
                except Exception:
                    display = Text(text)
                self._live.update(Panel(display, border_style="green", padding=(1, 2)))
                self._live.stop()
                self._live = None
                self._skip_print_response = True
            self._stream_text = ""
            self._streaming = False
            self._invalidate()

    def finish_turn(self, outcome: str, payload: Optional[str]):
        """Print how a turn ended (user turns and wake turns alike)."""
        if outcome == "completed":
            if payload and not self._skip_print_response:
                self.print_response(payload)
        elif outcome == "failed":
            self.print_error(payload or "Request failed.")
        else:
            self.print_info("Interrupted.")
        self._skip_print_response = False

    # ------------------------------------------------------------------ #
    #  Background work: wake turns, sub-agent status                      #
    # ------------------------------------------------------------------ #

    def _begin_wake(self, notifications: list):
        with self._lock:
            self._wake = True
            self._skip_print_response = False
            what = ", ".join(describe_notification(n) for n in notifications) or "background work finished"
            self.console.print(Text.assemble(("» woke: ", "cyan"), (what, "dim")))
            self._invalidate()

    def _end_wake(self, outcome: str, payload: Optional[str]):
        with self._lock:
            if not self._wake:
                return
            if self._streaming:
                self.finish_stream()  # a stopped reply stays as the grey trace
            self._wake = False
            self.finish_turn(outcome, payload)
            self._invalidate()

    def _update_subagents(self, statuses: list, replace: bool = False):
        with self._lock:
            if replace:
                self._subagents.clear()
            for status in statuses:
                subagent_id = status.get("subagent_id")
                if status.get("state") == "running":
                    self._subagents[subagent_id] = {**status, "_seen": time.monotonic()}
                else:
                    # Finished ones drop off; their notification says how it went.
                    self._subagents.pop(subagent_id, None)
            self._prompt_session.bottom_toolbar = self._toolbar if self._subagents else None
            self._invalidate()

    def _toolbar(self):
        now = time.monotonic()
        lines = [subagent_line(status, now) for status in list(self._subagents.values())]
        return FormattedText([("", "\n".join(lines))])

    def _invalidate(self):
        """Redraw the open prompt (preview, toolbar); thread-safe."""
        if self._above is not None:
            self._prompt_session.app.invalidate()

    def handle_event(self, event: str, data: dict):
        """Render one event; offline and gateway modes share this.

        Background threads call it too (wake turns, sub-agent status), so it
        is serialized with the prompt's open/close.
        """
        with self._lock:
            self._handle_event(event, data)

    def _handle_event(self, event: str, data: dict):
        if event == "assistant.stream_started":
            self.start_stream()
        elif event == "assistant.token":
            self.on_stream_token(data.get("delta") or "")
        elif event == "assistant.stream_ended":
            content = data.get("content") or ""
            if data.get("has_tool_calls") or not content.strip():
                # Tool-call preamble stays as the grey thinking trace; an
                # empty panel is cleared.
                self.finish_stream()
            else:
                self.finalize_stream_as_response()
        elif event == "tool.started":
            summary = escape(str(data.get("summary") or data.get("name") or ""))
            self.print_tool_summary(summary)
        elif event == "token.updated":
            self.print_token_status(data)
        elif event == "conversation.compacted":
            self.print_info(str(data.get("message") or ""))
        elif event == "wake.started":
            self._begin_wake(data.get("notifications") or [])
        elif event == "wake.ended":
            self._end_wake(data.get("outcome") or "interrupted", data.get("payload"))
        elif event == "wake.paused":
            pending = int(data.get("pending") or 0)
            self.print_info(
                f"paused auto-wake: {pending} background result{'s' if pending != 1 else ''} "
                "will be sent with your next message"
            )
        elif event == "subagent.status":
            self._update_subagents([data])
        elif event == "subagent.list":
            self._update_subagents(data.get("subagents") or [], replace=True)

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

    def print_token_status(self, data: dict):
        """Print a ``token.updated`` payload (same layout as TokenCounter)."""
        from kairos.tokens import format_status_line

        self.console.print(Text(format_status_line(data), style="dim"))

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
            preview = escape(s.get("preview", ""))
            self.console.print(f"  [cyan]{i + 1}.[/cyan] {ts}  {preview}...")
        self.console.print()

        choice = initial_choice
        while True:
            if choice is None:
                choice = self.ask("Pick a chat (number) or press Enter to cancel: ")
                if choice is None:
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

    def ask(self, label: str) -> Optional[str]:
        """Prompt for one line (also taking input typed during a handoff).

        Returns None on Ctrl+C / Ctrl+D.
        """
        pending_line, pending_default = self._take_pending_input_for_prompt()
        if pending_line is not None:
            return pending_line.strip()
        try:
            return self._prompt(
                FormattedText([("class:prompt", label)]),
                default=pending_default,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None

    # ------------------------------------------------------------------ #
    #  Prompt that coexists with background output                        #
    # ------------------------------------------------------------------ #

    def _prompt(self, message, **kwargs) -> str:
        """``PromptSession.prompt`` while background output prints above it."""
        with self._prompting():
            return self._prompt_session.prompt(
                self._prompt_message(message),
                style=PROMPT_STYLE,
                pre_run=self._above.flush,  # output that came before the loop ran
                **kwargs,
            )

    @contextmanager
    def _prompting(self):
        with self._lock:
            if self._live:
                # A wake reply streaming directly continues in the preview;
                # erase the Live tail instead of leaving a duplicate.
                self._live.transient = True
                self._live.stop()
                self._live = None
            original = self.console.file
            self._above = _AbovePrompt(self._prompt_session.app, original)
            self.console.file = self._above
        try:
            yield
        finally:
            with self._lock:
                self.console.file = original
                above, self._above = self._above, None
                above.drain()
                if self._streaming:
                    self._start_live()  # the wake reply keeps streaming below

    def _prompt_message(self, message):
        fragments = to_formatted_text(message)

        def get():
            # The final frame (left in the scrollback) shows just the prompt.
            if not self._wake or self._prompt_session.app.is_done:
                return fragments
            return self._render_preview() + fragments

        return get

    def _render_preview(self) -> list:
        """The streaming wake reply (or "Thinking...") rendered by Rich."""
        width = self.console.width
        streaming = self._streaming
        key = (streaming, len(self._stream_text), width)
        if key != self._preview_key:
            out = io.StringIO()
            preview = Console(
                file=out, width=width, height=PREVIEW_HEIGHT, force_terminal=True,
                color_system=self.console.color_system, legacy_windows=False,
            )
            if streaming:
                preview.print(_StreamView(self))
            else:
                preview.print(Text("Thinking...", style="italic dim"))
            self._preview_key = key
            self._preview = to_formatted_text(ANSI(out.getvalue()))
        return self._preview

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

    def get_user_input(self, prefix: str = "kairos> ", status: str = "") -> Optional[str]:
        """Read one request; ``status`` (model · effort) shows right-aligned."""
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
            result = self._prompt(
                HTML(f'<style class="prompt">{prefix}</style>'),
                rprompt=FormattedText([("class:rprompt", status)]),
                default=pending_default,
            ).strip()

            # Note: paste token resolution happens in main.py, not here.
            # main.py handles both text and image tokens in one pass.
            # Images are pasted explicitly via Alt+V keybinding.

            return result

        except (EOFError, KeyboardInterrupt):
            return None
