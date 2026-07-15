import os
import sys
import signal
import threading
import time

from kairos.config import Config
from kairos.agent import Agent
from kairos.cli import CLI, _paste_registry
from kairos.tools.session import SessionManager
from kairos.resume import sanitize_history_for_resume

# Backwards-compatible private name for callers that imported the original
# helper from kairos.main before the shared resume module was introduced.
_sanitize_history_for_resume = sanitize_history_for_resume

_EXIT_COMMANDS = frozenset(("exit", "quit", "q", "/exit", "/quit"))


def _resolve_paste_input(user_input: str) -> tuple[str, str | None]:
    """Replace visible paste tokens before command dispatch.

    Command aliases must be checked after paste resolution. Otherwise a
    pasted ``/exit`` (or ``/resume``, ``/compact``, etc.) is mistaken for a
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


def _is_exit_command(user_input: str) -> bool:
    """Return whether input is one of the CLI's exit aliases."""
    return user_input.strip().casefold() in _EXIT_COMMANDS


# ------------------------------------------------------------------ #
#  Auto-save state (shared between REPL and signal handlers)           #
# ------------------------------------------------------------------ #

_session_mgr: SessionManager | None = None
_agent: Agent | None = None
_auto_save_lock = threading.Lock()


def _save_now():
    """Save current chat history to disk (thread-safe)."""
    with _auto_save_lock:
        if _session_mgr and _agent:
            _session_mgr.save_chat(_agent.get_history())


def _handle_sigint(sig, frame):
    """Ctrl+C at the OS level -- save and exit."""
    _save_now()
    sys.exit(0)


def _handle_sigterm(sig, frame):
    """SIGTERM (window close / task kill) -- save and exit."""
    _save_now()
    sys.exit(0)


def _handle_sighup(sig, frame):
    """SIGHUP (terminal hangup / window close on Unix) -- save and exit."""
    _save_now()
    sys.exit(0)


def _start_signal_handlers():
    """Install signal handlers so chat is saved when the window closes."""
    # SIGTERM / SIGINT -- works on all platforms
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigint)

    # SIGHUP only exists on Unix
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle_sighup)


def _start_auto_save(agent: Agent, interval_seconds: int = 60):
    """Periodically save chat history in a background thread."""

    def _loop():
        while True:
            time.sleep(interval_seconds)
            _save_now()

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def process_request(
    cli: CLI, agent: Agent, user_input: str, image_url: str | None = None
) -> str | None:
    """
    Run the agent in a background thread so the main thread can catch
    KeyboardInterrupt cleanly.

    The Escape key listener is active only during agent execution. Because it
    shares stdin with prompt_toolkit, ordinary characters captured during the
    handoff are buffered and replayed by the next prompt instead of discarded.
    """
    result = [None]
    exception_holder = [None]

    def _run():
        try:
            result[0] = agent.run(user_input, image_url=image_url)
        except Exception as e:
            exception_holder[0] = e

    # Start Escape listener -- only while agent runs
    cli.start_escape_listener(on_escape=agent.request_stop)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    try:
        while t.is_alive():
            try:
                t.join(timeout=0.15)
            except KeyboardInterrupt:
                agent.interrupt()
                t.join(timeout=2)
                return "[Interrupted]"

        if exception_holder[0]:
            raise exception_holder[0]
        return result[0]
    finally:
        # Stop Escape listener before returning to prompt
        cli.stop_escape_listener()


def main():
    global _session_mgr, _agent

    # Validate config
    try:
        Config.validate()
    except ValueError as e:
        print(f"Configuration error: {e}")
        print("Please create a .env file with your OpenAI API key.")
        print("See .env.example for reference.")
        sys.exit(1)

    workspace = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()

    cli = CLI()
    agent = Agent(workspace)
    # Persist sessions under the active workspace. The PATH launcher invokes
    # this source tree while preserving the caller's CWD, so Agent2Gateway
    # chats are not mixed with Agent2's own legacy chats.
    session_mgr = SessionManager(workspace)

    # Share globals with signal handlers and auto-save
    _session_mgr = session_mgr
    _agent = agent

    # Install signal handlers so closing the window saves the chat
    _start_signal_handlers()

    # Start periodic auto-save (every 60 seconds)
    _start_auto_save(agent, interval_seconds=60)

    cli.print_banner()
    cli.print_info("Type your request, 'exit' to quit, '/resume' to load a chat")
    cli.print_info("'/compact' to compact conversation, Ctrl+C to hard-interrupt")
    cli.print_info("Escape to stop after current step, paste text directly or Alt+V for images")
    cli.print_info("Commands: 'clear', 'reset', '/exit', '/quit'")
    cli.print_info("/resume now also resumes mid-execution chats automatically")
    cli.console.print()

    # Wire agent callbacks to CLI
    # Tool call summaries (basic one-liners)
    agent.on_tool_call = lambda name, args: cli.print_tool_summary(
        agent._tool_summary(name, args)
    )
    # Streaming display
    agent.on_stream_start = lambda: cli.start_stream()
    agent.on_stream_token = cli.on_stream_token

    # Wire subagent callbacks so their tool calls & streaming are visible
    agent.subagent_tool._tool_printer = lambda summary: cli.console.print(
        f"  [dim]\u2193[/dim] [italic dim]subagent:[/italic dim] {summary}"
    )
    agent.subagent_tool._stream_start = lambda: cli.start_stream()
    agent.subagent_tool._stream_token = cli.on_stream_token
    # Print child token accounting separately from the parent agent's status.
    agent.subagent_tool._token_update = lambda subagent_id, tc: cli.print_subagent_token_status(
        subagent_id, tc
    )
    # For subagent stream end, always finish the stream panel (keep as thinking trace)
    agent.subagent_tool._stream_end = lambda _content, _has_tools: cli.finish_stream()

    def _on_stream_end(content: str, has_tool_calls: bool):
        if not content:
            # No text (model went straight to tool calls) -- clean up empty panel
            cli.finish_stream()
            return
        if has_tool_calls:
            # Tool-call thinking -- grey panel stays on screen as thinking trace
            cli.finish_stream()
        else:
            # Final response -- upgrade streaming panel to green
            cli.finalize_stream_as_response()
            cli._skip_print_response = True

    agent.on_stream_end = _on_stream_end
    # Token status after each turn
    agent.on_token_update = lambda tc: cli.print_token_status(tc)
    # Compaction status
    agent.on_compact = lambda msg: cli.print_info(msg)
    # Background completions are visible immediately. The completion remains
    # queued inside the agent so it is also inserted into the next API turn.
    agent.on_background_notification = cli.print_background_notification

    while True:
        try:
            user_input = cli.get_user_input()

            if user_input is None:
                break

            # Resolve paste placeholders before dispatching commands. A
            # pasted command is returned as a token (for example,
            # ``(Pasted Text #1)``), so checking aliases first would send the
            # pasted command to the agent instead of handling it locally.
            user_input, paste_image_data_url = _resolve_paste_input(user_input)

            if _is_exit_command(user_input):
                break

            if user_input.strip().casefold() == "clear":
                cli.clear_screen()
                cli.print_banner()
                continue

            if user_input.strip().casefold() == "reset":
                _save_now()
                session_mgr.new_session()
                agent.reset()
                cli.print_info("Conversation history reset")
                continue

            command = user_input.strip().casefold()

            if command == "/compact":
                cli.print_info("Compacting conversation...")
                result = agent.compact()
                cli.print_info(result)
                continue

            if command == "/paste":
                cli.print_info("Paste text directly — it's detected automatically.")
                cli.print_info("Alt+V pastes images from your clipboard.")
                cli.print_info(
                    "Text shows as (Pasted Text #N), images show as (Pasted Image #N)."
                )
                cli.print_info(
                    "Backspace removes an entire paste token. The actual content is sent to the API."
                )
                continue

            if command == "/resume" or command.startswith("/resume "):
                sessions = session_mgr.list_sessions()
                if not sessions:
                    cli.print_info("No saved chats found.")
                    continue
                # Accept both the interactive form (/resume, then 1) and the
                # convenient single-line form (/resume 1).
                initial_choice = user_input.strip()[len("/resume"):].strip() or None
                selected_id = cli.pick_session(sessions, initial_choice=initial_choice)
                if selected_id:
                    history = session_mgr.load_session(selected_id)
                    if history:
                        sanitized, last_msg, mid_exec = sanitize_history_for_resume(history)
                        if sanitized is None:
                            cli.print_info(
                                f"Chat '{selected_id}' has no resumable state."
                            )
                            cli.print_info("Skipping this chat.")
                            continue
                        _save_now()
                        agent.reset()
                        agent.conversation_history = sanitized
                        session_mgr.set_current_session(selected_id)
                        cli.print_info(f"Loaded chat: {selected_id}")
                        if last_msg:
                            cli.print_response(last_msg)
                        if mid_exec:
                            # Mid-execution resume — auto-continue the agent
                            cli.print_info(
                                "Resuming mid-execution — agent will continue where it left off..."
                            )
                            cli.start_thinking()
                            try:
                                response = process_request(
                                    cli, agent,
                                    "Continue where you left off. Pick up the next step.",
                                )
                            except Exception as e:
                                cli.stop_thinking()
                                cli.print_error(str(e))
                                response = None
                            finally:
                                cli.stop_thinking()
                            if response and not cli._skip_print_response:
                                cli.print_response(response)
                            cli._skip_print_response = False
                            _save_now()
                continue

            # Empty input: if no image was pasted, just skip
            if not user_input.strip():
                if not paste_image_data_url:
                    continue
                user_input = "Describe this image."

            # Show animated "Thinking..." while agent processes
            cli.start_thinking()

            try:
                response = process_request(
                    cli, agent, user_input, image_url=paste_image_data_url
                )
            except Exception as e:
                cli.stop_thinking()
                # APIRequestError already contains concise diagnostics. Avoid
                # printing chained tracebacks: gateway failures can otherwise
                # dump many pages of httpx/httpcore internals into the CLI.
                error_detail = str(e)
                if type(e).__name__ != "Exception":
                    error_detail = f"{type(e).__name__}: {error_detail}"
                cli.print_error(error_detail)
                response = None
            finally:
                cli.stop_thinking()

            # Print final response in green panel (skip if streaming already handled it)
            if response and not cli._skip_print_response:
                cli.print_response(response)
            cli._skip_print_response = False

            # Auto-save after each successful exchange so nothing is lost
            _save_now()

        except KeyboardInterrupt:
            cli.console.print()
            cli.print_info("Type 'exit' to quit.")
            continue

    # Final save
    _save_now()
    # Background shells are owned by the agent and should not survive CLI
    # shutdown. Close them only after the final conversation save.
    for terminal_id in list(agent.terminal_manager.terminals):
        agent.terminal_manager.close_terminal(terminal_id)
    session_mgr.new_session()
    cli.print_exit()


if __name__ == "__main__":
    main()
