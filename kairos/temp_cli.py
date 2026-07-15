"""
TempCLI -- a Textual-based alternative frontend for the Kairos agent.

Launch with:  python run_temp_cli.py [workspace]
"""

import os
import sys
import subprocess
import threading
import uuid
import base64
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Footer, Input, RichLog, Static
from rich.panel import Panel
from rich.markdown import Markdown
from rich.text import Text

from kairos.config import Config
from kairos.agent import Agent
from kairos.resume import sanitize_history_for_resume

PASTE_TEXT_MASK = "(Pasted text)"
PASTE_IMAGE_MASK = "(Pasted image)"


# ------------------------------------------------------------------ #
#  Clipboard helpers                                                  #
# ------------------------------------------------------------------ #

def _check_clipboard_has_image():
    """Quick check if the clipboard contains an image. Returns raw bytes or None."""
    import tempfile
    import time
    from pathlib import Path
    try:
        if sys.platform == "win32":
            try:
                import ctypes
                user32 = ctypes.windll.user32
                CF_DIB = 8
                if not user32.IsClipboardFormatAvailable(CF_DIB):
                    return None
            except Exception:
                pass
            tmp_path = Path(tempfile.gettempdir()) / f"kairos_paste_{int(time.time() * 1000)}.png"
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -Assembly System.Windows.Forms; "
                 "$img = [System.Windows.Forms.Clipboard]::GetImage(); "
                 f"if ($img) {{ $img.Save('{tmp_path}') }}"],
                capture_output=True, text=True, timeout=5,
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
            import tempfile as _tmp
            import time as _t
            from pathlib import Path as _P
            tmp_path = _P(_tmp.gettempdir()) / f"kairos_paste_{int(_t.time() * 1000)}.png"
            result = subprocess.run(["pngpaste", str(tmp_path)], capture_output=True, timeout=5)
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


def _read_clipboard_text() -> str:
    """Read text from the OS clipboard."""
    try:
        if sys.platform == "win32":
            check = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -Assembly System.Windows.Forms; "
                 "if ([System.Windows.Forms.Clipboard]::ContainsText()) { 'yes' }"],
                capture_output=True, text=True, timeout=3,
            )
            if "yes" not in check.stdout.lower():
                return ""
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -Assembly System.Windows.Forms; "
                 "[System.Windows.Forms.Clipboard]::GetText()"],
                capture_output=True, text=True, timeout=3,
            )
            return r.stdout
        elif sys.platform == "darwin":
            r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3)
            if r.returncode != 0:
                return ""
            return r.stdout
        else:
            r = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                               capture_output=True, text=True, timeout=3)
            if r.returncode != 0:
                return ""
            return r.stdout
    except Exception:
        return ""


# ------------------------------------------------------------------ #
#  Styling                                                            #
# ------------------------------------------------------------------ #

CSS = """
#chat {
    height: 1fr;
    overflow-y: scroll;
    padding: 0 1;
}

#streaming {
    display: none;
    padding: 0 1;
    max-height: 40;
    overflow-y: auto;
}

#streaming.visible {
    display: block;
}

#input-container {
    dock: bottom;
    height: auto;
    padding: 0 1;
}

#input {
    width: 100%;
}

#status {
    dock: bottom;
    height: 1;
    padding: 0 1;
    color: $text-muted;
}
"""


# ------------------------------------------------------------------ #
#  TempCLI App                                                        #
# ------------------------------------------------------------------ #

class TempCLI(App):
    """A Textual-based chat interface for the Kairos agent."""

    CSS = CSS
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Interrupt", show=True),
        Binding("ctrl+l", "clear_chat", "Clear Chat", show=True),
        Binding("ctrl+v", "paste_clipboard", "Paste", show=True),
        Binding("pageup", "scroll_chat_up", "Scroll Up", show=True),
        Binding("pagedown", "scroll_chat_down", "Scroll Down", show=True),
        Binding("up", "scroll_chat_up", show=False),
        Binding("down", "scroll_chat_down", show=False),
    ]

    TITLE = "Kairos (Textual)"

    def __init__(self, agent: Agent, **kwargs):
        super().__init__(**kwargs)
        self.agent = agent
        self._stream_text = ""
        self._processing = False
        self._awaiting_resume = False
        self._resume_sessions: list = []
        self._session_mgr = None  # Initialized on mount
        self._session_save_lock = threading.Lock()
        # Token registry: maps token string -> content dict
        self._paste_registry: dict = {}

        # Wire agent callbacks -- these are called from the agent's background
        # thread, so they use call_from_thread() to update the UI safely.
        agent.on_stream_start = self._cb_stream_start
        agent.on_stream_token = self._cb_stream_token
        agent.on_stream_end = self._cb_stream_end
        agent.on_tool_call = self._cb_tool_call
        agent.on_token_update = self._cb_token_update
        agent.on_compact = self._cb_compact
        if agent.subagent_tool:
            agent.subagent_tool._token_update = self._cb_subagent_token_update

    # ---- Compose ----

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="chat", auto_scroll=True, markup=True, highlight=False)
        yield Static("", id="streaming")
        yield Static("", id="status")
        yield Input(placeholder="kairos> ", id="input")
        yield Footer()

    def on_mount(self):
        from kairos.tools.session import SessionManager
        # Use the same workspace-specific chat store as the standard CLI.
        # Otherwise /resume can show a different set of chats depending on
        # which frontend was launched.
        self._session_mgr = SessionManager(self.agent.cwd)
        self.query_one("#input", Input).focus()
        chat = self.query_one("#chat", RichLog)
        chat.write(Panel(
            "[bold cyan]Kairos[/bold cyan] [dim](Textual mode)[/dim]\n"
            "[dim]Type your request. Ctrl+C to interrupt. 'exit' to quit.[/dim]\n"
            "[dim]/resume  load saved chat  |  /compact  compress context[/dim]\n"
            "[dim]PageUp/PageDown to scroll chat  |  Ctrl+V to paste text or images[/dim]",
            border_style="cyan",
            padding=(0, 1),
        ))

    # ---- Input handling ----

    def on_input_submitted(self, event: Input.Submitted):
        """Handle user pressing Enter."""
        user_input = event.value.strip()
        event.input.value = ""  # Clear input immediately

        if not user_input:
            return

        # Resolve paste tokens -> real content; extract image data if present
        paste_image_data_url = None
        for token, data in list(self._paste_registry.items()):
            if token in user_input:
                if data["type"] == "text":
                    user_input = user_input.replace(token, data["text_stripped"])
                elif data["type"] == "image":
                    paste_image_data_url = data["data_url"]
                    user_input = user_input.replace(token, "")
        self._paste_registry.clear()

        if not user_input and not paste_image_data_url:
            return
        # If only an image was pasted (no text), use a default prompt
        if not user_input.strip() and paste_image_data_url:
            user_input = "Describe this image."

        # If we're waiting for a resume session selection, handle it first
        if self._awaiting_resume:
            self._handle_resume_selection(user_input)
            return

        if user_input.lower() in ("exit", "quit", "q", "/exit", "/quit"):
            self.exit()
            return

        if user_input.lower() == "clear":
            self.action_clear_chat()
            return

        if user_input.lower() == "reset":
            self._session_mgr.save_chat(self.agent.get_history())
            self._session_mgr.new_session()
            self.agent.reset()
            self._log_info("Conversation history reset")
            return

        if user_input.lower() == "/compact":
            self._log_info("Compacting conversation...")
            def _do_compact():
                result = self.agent.compact()
                self.call_from_thread(self._log_info, result)
            threading.Thread(target=_do_compact, daemon=True).start()
            return

        if user_input.lower() == "/paste":
            self._log_info("Ctrl+V pastes text or images from your clipboard.")
            self._log_info("Text shows as (Pasted text), images show as (Pasted image).")
            self._log_info("The actual content is sent to the API.")
            return

        if user_input.lower() == "/resume":
            self._start_resume()
            return

        # Display user message
        chat = self.query_one("#chat", RichLog)
        chat.write(Panel(
            Markdown(user_input) if "```" in user_input or "#" in user_input else user_input,
            border_style="cyan",
            title="[bold]You[/bold]",
            padding=(0, 1),
        ))

        # Run agent in background thread
        self._processing = True
        self.query_one("#input", Input).disabled = True
        _image_url = paste_image_data_url  # capture for closure

        def _run():
            try:
                self.agent.run(user_input, image_url=_image_url)
            except InterruptedError:
                self.call_from_thread(self._log_info, "[Interrupted]")
            except Exception as e:
                self.call_from_thread(self._log_error, f"Agent error: {e}")
            finally:
                self.call_from_thread(self._finish_processing)

        threading.Thread(target=_run, daemon=True).start()

    def _start_resume(self):
        """List saved sessions and wait for user selection."""
        sessions = self._session_mgr.list_sessions()
        if not sessions:
            self._log_info("No saved chats found.")
            return

        self._awaiting_resume = True
        self._resume_sessions = sessions

        chat = self.query_one("#chat", RichLog)
        lines = "[bold]Saved chats:[/bold]\n"
        for i, s in enumerate(sessions, 1):
            preview = s.get("preview", "")
            lines += f"  [cyan]{i}.[/cyan] {preview}...\n"
        lines += "[dim]Type a number to load, or 'cancel' to go back.[/dim]"
        chat.write(Panel(lines, border_style="cyan", padding=(0, 1)))

    def _handle_resume_selection(self, user_input: str):
        """Handle the user's session selection after /resume."""
        choice = user_input.strip().casefold()
        if choice in ("cancel", "c", "q", "exit"):
            self._awaiting_resume = False
            self._resume_sessions = []
            self._log_info("Resume cancelled.")
            return

        try:
            idx = int(choice)
            if idx < 1 or idx > len(self._resume_sessions):
                self._log_error(
                    f"Invalid selection. Enter a number between 1 and {len(self._resume_sessions)}."
                )
                return
        except ValueError:
            self._log_error("Invalid input. Enter a number or 'cancel'.")
            return

        # list_sessions() returns metadata dictionaries; load_session() needs
        # the actual string ID. Keep selection mode active until the selected
        # chat has been accepted so a bad entry can never fall through as an
        # ordinary agent prompt.
        selected_id = self._resume_sessions[idx - 1]["id"]
        history = self._session_mgr.load_session(selected_id)
        if not history:
            self._log_error(f"Could not load chat '{selected_id}'.")
            return

        sanitized, last_msg, mid_exec = sanitize_history_for_resume(history)
        if sanitized is None:
            self._log_info(f"Chat '{selected_id}' has no resumable state.")
            return

        self._awaiting_resume = False
        self._resume_sessions = []
        self._session_mgr.save_chat(self.agent.get_history())
        self._session_mgr.new_session()
        self.agent.reset()
        self.agent.conversation_history = sanitized
        self._session_mgr.set_current_session(selected_id)
        self._log_info(f"Loaded chat: {selected_id}")

        if last_msg:
            self._log_info("Restored the last completed agent response.")
        if mid_exec:
            self._log_info(
                "Resuming mid-execution — continuing where the previous run stopped..."
            )
            self._processing = True
            self.query_one("#input", Input).disabled = True

            def _continue():
                try:
                    self.agent.run("Continue where you left off. Pick up the next step.")
                except Exception as e:
                    self.call_from_thread(self._log_error, f"Resume error: {e}")
                finally:
                    # Persist the continuation under the selected session,
                    # then re-enable input on the Textual/UI thread.
                    with self._session_save_lock:
                        self._session_mgr.save_chat(self.agent.get_history())
                    self.call_from_thread(self._finish_processing)

            threading.Thread(target=_continue, daemon=True).start()

    def _finish_processing(self):
        self._processing = False
        self.query_one("#input", Input).disabled = False
        self.query_one("#input", Input).focus()

    # ---- Agent callbacks (called from background thread) ----

    def _cb_stream_start(self):
        self.call_from_thread(self._handle_stream_start)

    def _cb_stream_token(self, token: str):
        self.call_from_thread(self._handle_stream_token, token)

    def _cb_stream_end(self, content: str, has_tool_calls: bool):
        self.call_from_thread(self._handle_stream_end, content, has_tool_calls)

    def _cb_tool_call(self, name: str, args: dict):
        self.call_from_thread(self._handle_tool_call, name, args)

    def _cb_token_update(self, token_counter):
        self.call_from_thread(self._handle_token_update, token_counter)

    def _cb_subagent_token_update(self, subagent_id: str, token_counter):
        self.call_from_thread(
            self._handle_subagent_token_update, subagent_id, token_counter
        )

    def _cb_compact(self, msg: str):
        self.call_from_thread(self._log_info, msg)

    # ---- Stream handlers (run on main thread via call_from_thread) ----

    def _handle_stream_start(self):
        self._stream_text = ""
        streaming = self.query_one("#streaming", Static)
        streaming.add_class("visible")
        streaming.update(Text("...", style="italic dim"))

    def _handle_stream_token(self, token: str):
        self._stream_text += token
        streaming = self.query_one("#streaming", Static)
        streaming.update(Text(self._stream_text, style="italic dim"))

    def _handle_stream_end(self, content: str, has_tool_calls: bool):
        streaming = self.query_one("#streaming", Static)
        streaming.remove_class("visible")
        streaming.update(Text(""))

        if not content:
            return

        chat = self.query_one("#chat", RichLog)

        if has_tool_calls:
            # Thinking trace -- dim panel
            chat.write(Panel(
                content,
                border_style="dim",
                title="[dim]thinking[/dim]",
                padding=(0, 1),
            ))
        else:
            # Final response -- green panel with markdown
            try:
                display = Markdown(content)
            except Exception:
                display = content
            chat.write(Panel(
                display,
                border_style="green",
                padding=(0, 1),
            ))

        self._stream_text = ""

    def _handle_tool_call(self, name: str, args: dict):
        summary = self.agent._tool_summary(name, args)
        chat = self.query_one("#chat", RichLog)
        chat.write(Text(f"  -> {summary}", style="dim"))

    def _handle_token_update(self, token_counter):
        status = token_counter.format_status()
        self.query_one("#status", Static).update(Text(status, style="dim"))

    def _handle_subagent_token_update(self, subagent_id: str, token_counter):
        status = token_counter.format_status()
        self.query_one("#chat", RichLog).write(
            Text(f"Subagent {subagent_id}: {status}", style="dim")
        )

    # ---- Actions ----

    def action_interrupt(self):
        """Interrupt the running agent."""
        if self._processing:
            self.agent.interrupt()
            self._log_info("Interrupted")

    def action_clear_chat(self):
        """Clear the chat log."""
        chat = self.query_one("#chat", RichLog)
        chat.clear()
        chat.write(Panel(
            "[dim]Chat cleared.[/dim]",
            border_style="dim",
        ))

    def action_scroll_chat_up(self):
        """Scroll the chat log up by one page."""
        chat = self.query_one("#chat", RichLog)
        chat.scroll_page_up()

    def action_scroll_chat_down(self):
        """Scroll the chat log down by one page."""
        chat = self.query_one("#chat", RichLog)
        chat.scroll_page_down()

    def action_paste_clipboard(self):
        """Read clipboard content and insert a placeholder token into the input.

        Detects whether the clipboard has an image or text, and inserts the
        appropriate placeholder. The actual content is resolved on submit.
        """
        try:
            # 1. Try image first
            image_data = _check_clipboard_has_image()
            if image_data and len(image_data) > 0:
                mime = _detect_mime(image_data)
                b64 = base64.b64encode(image_data).decode("ascii")
                data_url = f"data:{mime};base64,{b64}"
                token_id = uuid.uuid4().hex[:8]
                token = f"({PASTE_IMAGE_MASK} {token_id})"
                self._paste_registry[token] = {"type": "image", "data_url": data_url}
                inp = self.query_one("#input", Input)
                inp.value += token
                inp.cursor_position = len(inp.value)
                return

            # 2. Otherwise, try text
            text = _read_clipboard_text()
            if text:
                token_id = uuid.uuid4().hex[:8]
                token = f"({PASTE_TEXT_MASK} {token_id})"
                self._paste_registry[token] = {
                    "type": "text",
                    "text": text,
                    "text_stripped": text.strip(),
                }
                inp = self.query_one("#input", Input)
                inp.value += token
                inp.cursor_position = len(inp.value)
        except Exception:
            pass

    # ---- Helpers ----

    def _log_info(self, msg: str):
        chat = self.query_one("#chat", RichLog)
        chat.write(Text(f"Info: {msg}", style="cyan"))

    def _log_error(self, msg: str):
        chat = self.query_one("#chat", RichLog)
        chat.write(Text(f"Error: {msg}", style="red"))

    def exit(self, **kwargs):
        """Save and exit."""
        self._session_mgr.save_chat(self.agent.get_history())
        self._session_mgr.new_session()
        super().exit(**kwargs)


# ------------------------------------------------------------------ #
#  Entry point                                                        #
# ------------------------------------------------------------------ #

def main():
    try:
        Config.validate()
    except ValueError as e:
        print(f"Configuration error: {e}")
        print("Please create a .env file with your OpenAI API key.")
        sys.exit(1)

    workspace = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    agent = Agent(workspace)

    app = TempCLI(agent)
    app.run()


if __name__ == "__main__":
    main()
