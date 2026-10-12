"""Kairos CLI entry point.

Gateway mode (default) is a thin client of the gateway's WebSocket API.
Offline mode (``--offline``) runs the Agent in-process. Both modes feed the
same agent events (``assistant.token``, ``tool.started``, ...) to
``CLI.handle_event``, so they look the same.
"""
import argparse
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from rich.text import Text

from kairos.cli import CLI, _paste_registry
from kairos.gateway_client import GatewayClient, GatewayError
from kairos.resume import sanitize_history_for_resume

# Backwards-compatible private name for callers that imported the original
# helper from kairos.main before the shared resume module was introduced.
_sanitize_history_for_resume = sanitize_history_for_resume

COMMANDS = ("/quit", "/clear", "/reset", "/compact", "/resume", "/model", "/effort")
PROVIDERS = ("openai", "anthropic", "openrouter")
CONTINUE_INSTRUCTION = "Continue where you left off. Pick up the next step."
_RUN_ENDED = frozenset(("run.completed", "run.interrupted", "run.failed", "run.cancelled"))


def _resolve_paste_input(user_input: str) -> tuple[str, str | None]:
    """Replace visible paste tokens before command dispatch.

    Command aliases must be checked after paste resolution. Otherwise a
    pasted ``/quit`` (or ``/resume``, ``/compact``, etc.) is mistaken for a
    normal agent request because the prompt returns its placeholder token.
    """
    paste_image_data_url = None
    for token, data in list(_paste_registry.items()):
        if token not in user_input:
            continue
        if data["type"] == "text":
            user_input = user_input.replace(token, data["text_stripped"])
        elif data["type"] == "image":
            paste_image_data_url = data["data_url"]
            user_input = user_input.replace(token, "")
    _paste_registry.clear()
    return user_input, paste_image_data_url


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """Return ``(command, args)`` for a slash command, else None.

    Only a first word made of letters counts, so ``/etc/hosts ...`` is sent
    to the agent as a message. Unknown commands are returned too, so the
    caller can report them instead of sending a typo to the model.
    """
    parts = text.strip().split()
    if not parts or not re.fullmatch(r"/[A-Za-z]+", parts[0]):
        return None
    return parts[0].lower(), parts[1:]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kairos", description="Kairos coding agent.")
    parser.add_argument(
        "workspace", nargs="?",
        help="workspace folder ('.' = current folder); omit it to start in a new folder",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="run the agent in this process instead of through the gateway",
    )
    args = parser.parse_args(argv)
    if args.workspace is not None:
        path = Path(args.workspace).expanduser().resolve()
        if not path.is_dir():
            parser.error(f"not a folder: {path}")
        args.workspace = str(path)
    return args


def _error_text(exc: BaseException) -> str:
    # APIRequestError already carries concise diagnostics; never print a
    # traceback (gateway failures can dump pages of httpx internals).
    detail = str(exc)
    if type(exc).__name__ not in ("Exception", "GatewayError"):
        detail = f"{type(exc).__name__}: {detail}"
    return detail


def _same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def _local_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso or ""


def _text(content) -> str:
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return content or ""


def _remember(configured: dict, changes: dict) -> None:
    """Track /model and /effort choices so a new conversation keeps them."""
    if "setup" in changes or "model" in changes:
        # A different model has its own efforts; keep only the new choice.
        configured.clear()
    configured.update(changes)


def _close_agent(agent) -> None:
    for terminal_id in list(agent.terminal_manager.terminals):
        agent.terminal_manager.close_terminal(terminal_id)
    if agent.browser_manager.is_open:
        try:
            agent.browser_manager.close()
        except Exception:
            pass


# ------------------------------------------------------------------ #
#  Backends: the REPL talks to one of these                            #
# ------------------------------------------------------------------ #

class OfflineBackend:
    """In-process Agent with sessions under ~/.kairos (the old behavior)."""

    mode = "offline"

    def __init__(self, workspace: str | None):
        from kairos.tools.session import SessionManager

        self._explicit = workspace is not None
        if workspace is None:
            from kairos.workspaces import new_workspace

            workspace = str(new_workspace())
            # Fresh folders are siblings; one chat list covers all of them.
            store = str(Path(workspace).parent)
        else:
            store = workspace
        self.workspace = workspace
        self.sessions = SessionManager(store)
        self._configured: dict = {}
        self._on_event = None
        self._muted = False
        self._worker: threading.Thread | None = None
        self._save_lock = threading.Lock()
        self.agent = self._new_agent(workspace)

    def _new_agent(self, workspace: str):
        from kairos.agent import Agent

        agent = Agent(workspace)
        emit = self._emit

        def tokens(counter) -> dict:
            return {
                "session_input": counter.session_input,
                "session_output": counter.session_output,
                "context_pct": counter.context_pct,
                "turn_input": counter.turn_input,
                "turn_output": counter.turn_output,
            }

        # Same event names and payloads as the gateway manager emits.
        agent.on_stream_start = lambda: emit("assistant.stream_started", {})
        agent.on_stream_token = lambda token: emit("assistant.token", {"delta": token})
        agent.on_stream_end = lambda text, has_tools: emit(
            "assistant.stream_ended", {"content": text, "has_tool_calls": has_tools}
        )
        agent.on_tool_call = lambda name, args: emit(
            "tool.started", {"name": name, "summary": agent._tool_summary(name, args)}
        )
        agent.on_token_update = lambda counter: emit("token.updated", tokens(counter))
        agent.on_compact = lambda message: emit("conversation.compacted", {"message": message})
        # Background completions show immediately; the agent also keeps them
        # queued for the next API turn.
        agent.on_background_notification = lambda message: emit("terminal.completed", {"message": message})
        subagents = agent.subagent_tool
        subagents._tool_printer = lambda summary: emit("tool.started", {"name": "subagent", "summary": summary})
        subagents._stream_start = lambda: emit("assistant.stream_started", {"subagent": True})
        subagents._stream_token = lambda token: emit("assistant.token", {"delta": token, "subagent": True})
        subagents._stream_end = lambda text, has_tools: emit(
            "assistant.stream_ended", {"content": text, "has_tool_calls": has_tools, "subagent": True}
        )
        subagents._token_update = lambda subagent_id, counter: emit(
            "token.updated", {**tokens(counter), "subagent_id": subagent_id}
        )
        if self._configured:
            self._apply(agent, **self._configured)
        return agent

    @staticmethod
    def _apply(agent, setup=None, model=None, provider=None, effort=None) -> None:
        if setup or model:
            agent.set_model(setup=setup, model=model, provider=provider)
        if effort:
            agent.set_effort(effort)

    def _emit(self, event: str, data: dict) -> None:
        # After Esc the prompt is already back; drop the stopped run's output.
        if not self._muted and self._on_event:
            self._on_event(event, data)

    def _wait_previous(self) -> None:
        """Let a run stopped with Esc reach its cleanup before reusing the Agent."""
        if self._worker is not None:
            self._worker.join()
            self._worker = None
            self.save()

    def save(self) -> None:
        with self._save_lock:
            self.sessions.save_chat(self.agent.get_history())

    def run(self, text: str, image_url: str | None, stop: threading.Event, on_event):
        self._wait_previous()
        self._on_event, self._muted = on_event, False
        result: dict = {}

        def work():
            try:
                result["response"] = self.agent.run(text, image_url=image_url)
            except Exception as exc:
                result["error"] = exc

        worker = threading.Thread(target=work, daemon=True)
        self._worker = worker
        worker.start()
        while worker.is_alive():
            try:
                if stop.wait(0.05):
                    break
            except KeyboardInterrupt:
                stop.set()
        if worker.is_alive():
            # Return to the prompt at once; the next command joins the worker.
            self._muted = True
            self.agent.interrupt()
            return "interrupted", None
        self._worker = None
        self.save()
        if "error" in result:
            return "failed", _error_text(result["error"])
        response = result.get("response") or ""
        if response == "[Interrupted]" or response.startswith("[Stopped"):
            return "interrupted", None
        return "completed", response

    def continue_run(self, stop: threading.Event, on_event):
        return self.run(CONTINUE_INSTRUCTION, None, stop, on_event)

    def compact(self) -> str:
        self._wait_previous()
        result = self.agent.compact()
        self.save()
        return result

    def reset(self) -> None:
        self._wait_previous()
        self.save()
        self.sessions.new_session()
        if self._explicit:
            self.agent.reset()
            return
        from kairos.workspaces import new_workspace

        _close_agent(self.agent)
        self.workspace = str(new_workspace())
        self.agent = self._new_agent(self.workspace)

    def list_conversations(self) -> list[dict]:
        return self.sessions.list_sessions()

    def resume(self, conversation_id: str) -> tuple[str, bool]:
        self._wait_previous()
        history = self.sessions.load_session(conversation_id)
        sanitized, last, mid_execution = (
            sanitize_history_for_resume(history) if history else (None, "", False)
        )
        if sanitized is None:
            raise ValueError("That chat has no resumable state.")
        self.save()
        self.agent.reset()
        self.agent.conversation_history = sanitized
        self.sessions.set_current_session(conversation_id)
        return last, mid_execution

    def models(self) -> tuple[list[dict], dict]:
        from kairos.providers import list_setups

        return list_setups(), self.agent.model_info()

    def model_info(self) -> dict:
        return self.agent.model_info()

    def configure(self, **changes) -> dict:
        self._wait_previous()
        self._apply(self.agent, **changes)
        _remember(self._configured, changes)
        return self.agent.model_info()

    def close(self) -> None:
        self._wait_previous()
        self.save()
        # Background shells are owned by the agent and should not survive
        # CLI shutdown. Close them only after the final save.
        _close_agent(self.agent)
        self.sessions.new_session()


class GatewayBackend:
    """Thin client: the gateway owns the conversation, Agent and history."""

    mode = "gateway"

    def __init__(self, workspace: str | None, client: GatewayClient | None = None):
        self.client = client or GatewayClient()
        self.client.connect()
        self._explicit = workspace
        self._configured: dict = {}
        self._abandoned: set[str] = set()
        self.conversation: dict | None = None
        self._open(self._create())

    @property
    def workspace(self) -> str:
        return self.conversation["workspace_path"]

    @property
    def _id(self) -> str:
        return self.conversation["id"]

    def _create(self) -> dict:
        # Without a workspace the gateway creates a fresh folder.
        payload = {"workspace": self._explicit} if self._explicit else {}
        return self.client.request("conversation.create", payload=payload)["conversation"]

    def _open(self, conversation: dict) -> None:
        """Make *conversation* current and subscribe to its new events."""
        if self.conversation is not None:
            self.client.request("conversation.unsubscribe", self._id)
        self._abandoned.clear()
        self.conversation = conversation
        self.client.request(
            "conversation.subscribe", self._id,
            {"after_event_id": conversation.get("last_event_id", 0)},
        )

    def _wait(self, run_id: str, stop: threading.Event, on_event):
        """Render this run's events until it ends or the user stops it."""
        while True:
            try:
                if stop.is_set():
                    # Do not wait for the run to wind down: the prompt comes
                    # back now and later events for this run are dropped.
                    self._abandoned.add(run_id)
                    try:
                        self.client.request("run.cancel", self._id, {"run_id": run_id})
                    except GatewayError:
                        pass
                    return "interrupted", None
                event = self.client.next_event(0.05)
            except KeyboardInterrupt:
                stop.set()
                continue
            if event is None:
                continue
            name, data = event.get("event"), event.get("data") or {}
            if event.get("run_id") != run_id:
                if name in _RUN_ENDED:
                    self._abandoned.discard(event.get("run_id"))
                continue
            if name == "run.completed":
                return "completed", data.get("response")
            if name == "run.failed":
                return "failed", data.get("error") or "Run failed."
            if name in _RUN_ENDED:
                return "interrupted", None
            on_event(name, data)

    def _settle(self, timeout: float = 30.0) -> None:
        """Wait for runs stopped with Esc to end before a control command."""
        deadline = time.monotonic() + timeout
        while self._abandoned and time.monotonic() < deadline:
            event = self.client.next_event(0.1)
            if event and event.get("event") in _RUN_ENDED:
                self._abandoned.discard(event.get("run_id"))

    def run(self, text: str, image_url: str | None, stop: threading.Event, on_event):
        payload = {"content": text, "source": "cli"}
        if image_url:
            payload["image_url"] = image_url
        ack = self.client.request("message.send", self._id, payload)
        return self._wait(ack["run_id"], stop, on_event)

    def continue_run(self, stop: threading.Event, on_event):
        ack = self.client.request(
            "conversation.continue", self._id,
            {"instruction": CONTINUE_INSTRUCTION, "source": "cli"},
        )
        return self._wait(ack["run_id"], stop, on_event)

    def compact(self) -> str:
        self._settle()
        return self.client.request("conversation.compact", self._id, timeout=600)["message"]

    def reset(self) -> None:
        self._settle()
        self._open(self._create())
        if self._configured:
            self.configure(**self._configured)

    def list_conversations(self) -> list[dict]:
        items = self.client.request("conversation.list", payload={"limit": 500})["items"]
        here = Path(self.workspace)

        def belongs(conversation: dict) -> bool:
            path = Path(conversation["workspace_path"])
            if self._explicit:
                return _same_path(path, here)
            # No path given: fresh folders are siblings, list all of them.
            return _same_path(path.parent, here.parent)

        return [
            {
                "id": c["id"],
                "timestamp": _local_time(c.get("updated_at", "")),
                "preview": c.get("title") or c.get("preview") or "",
            }
            for c in items
            if c.get("message_count") and belongs(c)
        ]

    def resume(self, conversation_id: str) -> tuple[str, bool]:
        self._settle()
        data = self.client.request("conversation.load", conversation_id)
        self._open(data["conversation"])
        last = next(
            (_text(m.get("content")) for m in reversed(data.get("history") or []) if m.get("role") == "assistant"),
            "",
        )
        return last, bool(data.get("needs_continue"))

    def models(self) -> tuple[list[dict], dict]:
        setups = self.client.request("model.list").get("setups") or []
        return setups, self.model_info()

    def model_info(self) -> dict:
        return self.client.request(
            "conversation.configure", self._id, {"conversation_id": self._id}
        )

    def configure(self, **changes) -> dict:
        self._settle()
        info = self.client.request(
            "conversation.configure", self._id, {"conversation_id": self._id, **changes}
        )
        _remember(self._configured, changes)
        return info

    def save(self) -> None:
        """The gateway persists the conversation."""

    def close(self) -> None:
        self.client.close()


# ------------------------------------------------------------------ #
#  Offline auto-save state (shared with signal handlers)               #
# ------------------------------------------------------------------ #

_backend = None


def _save_now():
    if _backend is not None:
        _backend.save()


def _handle_sigterm(sig, frame):
    """SIGTERM (window close / task kill) -- save and exit."""
    _save_now()
    sys.exit(0)


def _handle_sighup(sig, frame):
    """SIGHUP (terminal hangup / window close on Unix) -- save and exit."""
    _save_now()
    sys.exit(0)


def _start_signal_handlers():
    """Save the chat when the window closes. Ctrl+C arrives as
    KeyboardInterrupt and stops the running request."""
    signal.signal(signal.SIGTERM, _handle_sigterm)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle_sighup)


def _start_auto_save(interval_seconds: int = 60):
    """Periodically save chat history in a background thread."""

    def _loop():
        while True:
            time.sleep(interval_seconds)
            _save_now()

    threading.Thread(target=_loop, daemon=True).start()


# ------------------------------------------------------------------ #
#  REPL                                                                #
# ------------------------------------------------------------------ #

def _status_text(backend) -> str:
    """Compact ``model · effort`` for the prompt (empty if unavailable)."""
    try:
        info = backend.model_info() or {}
    except Exception:
        # An older gateway may not know conversation.configure.
        return ""
    return " · ".join(str(part) for part in (info.get("model") or info.get("setup"), info.get("effort")) if part)


def _describe(info: dict) -> str:
    text = f"{info.get('provider', '?')}/{info.get('model', '?')}"
    if info.get("setup"):
        text = f"{info['setup']} ({text})"
    if info.get("effort"):
        text += f" · effort {info['effort']}"
    return text


def run_turn(cli: CLI, start) -> None:
    """Run one agent turn; Esc or Ctrl+C stops it and returns to the prompt."""
    stop = threading.Event()
    cli.start_thinking()
    cli.start_escape_listener(on_escape=stop.set)
    try:
        outcome, payload = start(stop)
    except Exception as exc:
        outcome, payload = "failed", _error_text(exc)
    finally:
        cli.stop_escape_listener()
        cli.stop_thinking()
    if outcome == "completed":
        if payload and not cli._skip_print_response:
            cli.print_response(payload)
    elif outcome == "failed":
        cli.print_error(payload or "Request failed.")
    else:
        cli.print_info("Interrupted.")
    cli._skip_print_response = False


def _resume(cli: CLI, backend, args: list[str]) -> None:
    sessions = backend.list_conversations()
    if not sessions:
        cli.print_info("No saved chats for this workspace.")
        return
    chosen = cli.pick_session(sessions, initial_choice=args[0] if args else None)
    if not chosen:
        return
    last, needs_continue = backend.resume(chosen)
    preview = next((s.get("preview", "") for s in sessions if s["id"] == chosen), "")
    cli.print_info(f"Resumed: {preview}")
    if last:
        cli.print_response(last)
    if needs_continue:
        run_turn(cli, lambda stop: backend.continue_run(stop, cli.handle_event))


def _model(cli: CLI, backend, args: list[str]) -> None:
    usage = "Usage: /model [number|name] or /model <model-id> <openai|anthropic|openrouter>"
    if len(args) > 2:
        cli.print_error(usage)
        return
    if len(args) == 2:
        model, provider = args[0], args[1].lower()
        if provider not in PROVIDERS:
            cli.print_error(usage)
            return
        info = backend.configure(model=model, provider=provider)
    else:
        setups, current = backend.models()
        if args:
            choice = args[0]
        else:
            for index, setup in enumerate(setups, 1):
                mark = "  (current)" if setup.get("name") == current.get("setup") else ""
                cli.console.print(Text.assemble(
                    (f"  {index}. ", "cyan"),
                    str(setup.get("name")),
                    (f"  {setup.get('provider')}/{setup.get('model')}{mark}", "dim"),
                ))
            cli.print_info(f"Current: {_describe(current)}")
            choice = cli.ask("Pick a model (number or name), Enter to cancel: ")
            if not choice:
                return
        names = [setup.get("name") for setup in setups]
        if choice.isdigit() and 1 <= int(choice) <= len(setups):
            name = names[int(choice) - 1]
        elif choice in names:
            name = choice
        else:
            cli.print_error(f"No saved setup '{choice}'. {usage}")
            return
        info = backend.configure(setup=name)
    cli.print_info(f"Model: {_describe(info)}")


def _effort(cli: CLI, backend, args: list[str]) -> None:
    if not args:
        info = backend.model_info()
        allowed = ", ".join(info.get("reasoning_efforts") or []) or "none"
        cli.print_info(f"Effort: {info.get('effort') or 'default'} (allowed: {allowed})")
        return
    info = backend.configure(effort=args[0].lower())
    cli.print_info(f"Effort: {info.get('effort')}")


def handle_command(cli: CLI, backend, name: str, args: list[str]) -> bool:
    """Run a slash command (not /quit); return True if model/effort may have changed."""
    if name == "/clear":
        cli.clear_screen()
        return False
    if name == "/reset":
        backend.reset()
        cli.print_info(f"New conversation · {backend.workspace}")
        return True
    if name == "/compact":
        cli.print_info("Compacting...")
        cli.print_info(backend.compact())
        return False
    if name == "/resume":
        _resume(cli, backend, args)
        return True
    if name == "/model":
        _model(cli, backend, args)
        return True
    if name == "/effort":
        _effort(cli, backend, args)
        return True
    cli.print_error(f"Unknown command {name}. Commands: {' '.join(COMMANDS)}")
    return False


def main(argv: list[str] | None = None):
    global _backend

    args = parse_args(argv)
    cli = CLI()
    try:
        if args.offline:
            from kairos.config import Config

            try:
                Config.validate()
            except ValueError as e:
                cli.print_error(f"Configuration error: {e}")
                sys.exit(1)
            backend = OfflineBackend(args.workspace)
        else:
            backend = GatewayBackend(args.workspace)
    except GatewayError as exc:
        hint = " Start Kairos with kairos.bat, or use kairos_offline.bat." if exc.code == "disconnected" else ""
        cli.print_error(f"{exc}{hint}")
        sys.exit(1)

    _backend = backend
    if args.offline:
        _start_signal_handlers()
        _start_auto_save(interval_seconds=60)

    cli.print_banner(backend.workspace, backend.mode)
    status = _status_text(backend)

    while True:
        if backend.mode == "gateway" and backend.client.closed:
            cli.print_error("Lost connection to the gateway.")
            break
        try:
            user_input = cli.get_user_input(status=status)
            if user_input is None:
                break

            # Resolve paste placeholders before dispatching commands. A
            # pasted command is returned as a token (for example,
            # ``(Pasted Text #1)``), so checking commands first would send
            # the pasted command to the agent instead of handling it locally.
            user_input, paste_image_data_url = _resolve_paste_input(user_input)

            command = parse_command(user_input)
            if command:
                name, command_args = command
                if name == "/quit":
                    break
                try:
                    if handle_command(cli, backend, name, command_args):
                        status = _status_text(backend)
                except Exception as exc:
                    cli.print_error(_error_text(exc))
                continue

            if not user_input.strip():
                if not paste_image_data_url:
                    continue
                user_input = "Describe this image."

            run_turn(
                cli,
                lambda stop: backend.run(user_input, paste_image_data_url, stop, cli.handle_event),
            )

        except KeyboardInterrupt:
            cli.console.print()
            continue

    backend.close()
    cli.print_exit()


if __name__ == "__main__":
    main()
