import os
import sys
import signal
import threading
import time
import traceback
import json

from kairos.config import Config
from kairos.agent import Agent
from kairos.cli import CLI, _paste_registry
from kairos.tools.session import SessionManager


# ------------------------------------------------------------------ #
#  Auto-save state (shared between REPL and signal handlers)           #
# ------------------------------------------------------------------ #

_session_mgr: SessionManager | None = None
_agent: Agent | None = None
_auto_save_lock = threading.Lock()


def _is_screenshot_injection(msg: dict) -> bool:
    """Check if a user message is a screenshot injection from the agent
    (e.g. '[Screenshot captured — ...]'), not a real user message."""
    content = msg.get("content", "")
    if isinstance(content, list) and len(content) > 0:
        first_block = content[0]
        if isinstance(first_block, dict) and first_block.get("type") == "text":
            text = first_block.get("text", "")
            if text.startswith("[Screenshot captured"):
                return True
    return False


def _sanitize_history_for_resume(
    history: list[dict],
) -> tuple[list[dict] | None, str, bool]:
    """Walk backward through conversation history to find the last resumable
    point — either a clean agent response or a mid-execution state.

    Returns (sanitized_history, last_agent_content, is_mid_execution):
      - Normal resume: history ends at last clean assistant response,
        is_mid_execution=False
      - Mid-execution resume: history has incomplete work (tool calls in
        progress), is_mid_execution=True. The incomplete chain is completed
        with synthetic tool results so the API sees valid message ordering.
      - No resumable state: (None, "", False)
    """
    if not history or len(history) <= 1:
        return None, "", False

    # --- Pass 1: try to find a clean assistant response (normal resume) ---
    i = len(history) - 1
    while i > 0:  # index 0 is always the system prompt — never skip it
        msg = history[i]
        role = msg.get("role", "")

        # Tool messages → always dirty, skip
        if role == "tool":
            i -= 1
            continue

        # User message → screenshot injection is dirty, real user is a hard stop
        if role == "user":
            if _is_screenshot_injection(msg):
                i -= 1
                continue
            # Real user message — no clean agent response exists above this
            break

        # Assistant message
        if role == "assistant":
            if msg.get("tool_calls"):
                # Dirty: agent called tools but execution never completed
                i -= 1
                continue
            # Clean: final response with no tool calls
            sanitized = history[: i + 1]
            content = msg.get("content") or ""
            return sanitized, content, False

    # --- Pass 2: no clean response — try mid-execution resume ---
    result = list(history)  # work on a copy

    # Strip trailing screenshot injection messages (dirty user messages)
    while len(result) > 1 and _is_screenshot_injection(result[-1]):
        result.pop()

    if len(result) <= 1:
        return None, "", False

    last = result[-1]
    last_role = last.get("role", "")

    def _make_synthetic_result(tc_id: str, tc_name: str) -> dict:
        """Create a synthetic tool result for an interrupted tool call."""
        return {
            "tool_call_id": tc_id,
            "role": "tool",
            "name": tc_name,
            "content": json.dumps({
                "success": False,
                "output": "",
                "error": "Tool was not executed — execution was interrupted.",
            }),
        }

    def _extract_tc_info(tool_call: dict) -> tuple[str, str]:
        """Extract (id, name) from a tool_call entry in conversation history."""
        tc_id = tool_call.get("id", "")
        func = tool_call.get("function", {})
        if isinstance(func, dict):
            tc_name = func.get("name", "unknown")
        else:
            tc_name = tool_call.get("name", "unknown")
        return tc_id, tc_name

    if last_role == "assistant" and last.get("tool_calls"):
        # History ends with assistant that called tools but got NO results
        # at all (interrupted right after streaming). Add synthetic results
        # so the history is API-valid and the agent sees its own intent.
        for tc in last["tool_calls"]:
            tc_id, tc_name = _extract_tc_info(tc)
            result.append(_make_synthetic_result(tc_id, tc_name))
        return result, "", True

    elif last_role == "tool":
        # History ends with tool result(s). Walk backward to find the
        # corresponding assistant message with tool_calls.
        j = len(result) - 1
        trailing_ids = []
        while j > 0 and result[j].get("role") == "tool":
            trailing_ids.append(result[j].get("tool_call_id", ""))
            j -= 1

        if j > 0 and result[j].get("role") == "assistant" and result[j].get("tool_calls"):
            tc_map = {}
            for tc in result[j]["tool_calls"]:
                tc_id, tc_name = _extract_tc_info(tc)
                tc_map[tc_id] = tc_name

            present = set(trailing_ids)
            expected = set(tc_map.keys())

            if present == expected:
                # Complete chain — all tool results present. Valid seam.
                pass
            elif present.issubset(expected):
                # Partial results — add synthetic results for missing ones
                for mid in sorted(expected - present):
                    result.append(_make_synthetic_result(mid, tc_map[mid]))
            else:
                # Orphaned tool results with no matching assistant —
                # strip everything after the assistant message
                del result[j + 1:]
                if len(result) <= 1:
                    return None, "", False

        return result, "", True

    # For any other last role (user message, clean assistant), the history
    # is already in a valid state. The "Continue" message will be appended
    # by the caller.
    return result, "", True


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

    The Escape key listener is active only during agent execution so it
    doesn't conflict with prompt_toolkit while the user is typing.
    """
    result = [None]
    exception_holder = [None]

    def _run():
        try:
            result[0] = agent.run(user_input, image_url=image_url)
        except Exception as e:
            exception_holder[0] = e

    # Start Escape listener (raw terminal input) -- only while agent runs
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
    session_mgr = SessionManager()

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

            if user_input.lower() in ("exit", "quit", "q", "/exit", "/quit"):
                break

            if user_input.lower() == "clear":
                cli.clear_screen()
                cli.print_banner()
                continue

            if user_input.lower() == "reset":
                _save_now()
                session_mgr.new_session()
                agent.reset()
                cli.print_info("Conversation history reset")
                continue

            if user_input.lower() == "/compact":
                cli.print_info("Compacting conversation...")
                result = agent.compact()
                cli.print_info(result)
                continue

            if user_input.lower() == "/paste":
                cli.print_info("Paste text directly — it's detected automatically.")
                cli.print_info("Alt+V pastes images from your clipboard.")
                cli.print_info(
                    "Text shows as (Pasted Text #N), images show as (Pasted Image #N)."
                )
                cli.print_info(
                    "Backspace removes an entire paste token. The actual content is sent to the API."
                )
                continue

            if user_input.lower() == "/resume":
                sessions = session_mgr.list_sessions()
                if not sessions:
                    cli.print_info("No saved chats found.")
                    continue
                selected_id = cli.pick_session(sessions)
                if selected_id:
                    history = session_mgr.load_session(selected_id)
                    if history:
                        sanitized, last_msg, mid_exec = _sanitize_history_for_resume(history)
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
                                cli.console.print(f"[dim]{traceback.format_exc()}[/dim]")
                                response = None
                            finally:
                                cli.stop_thinking()
                            if response and not cli._skip_print_response:
                                cli.print_response(response)
                            cli._skip_print_response = False
                            _save_now()
                continue

            # --- Resolve paste tokens -> extract text + images ----------
            paste_image_data_url = None
            for token, data in list(_paste_registry.items()):
                if token in user_input:
                    if data["type"] == "text":
                        user_input = user_input.replace(token, data["text_stripped"])
                    elif data["type"] == "image":
                        paste_image_data_url = data["data_url"]
                        user_input = user_input.replace(token, "")
            _paste_registry.clear()

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
                # Show the full error with details
                error_detail = str(e)
                # If the error has extra info (like from _format_api_error), show it
                if type(e).__name__ != "Exception":
                    error_detail = f"{type(e).__name__}: {error_detail}"
                cli.print_error(error_detail)
                # Also show traceback for debugging
                cli.console.print(f"[dim]{traceback.format_exc()}[/dim]")
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
