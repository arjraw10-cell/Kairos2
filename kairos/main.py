"""Kairos CLI — thin WebSocket client connected to the gateway.

Usage:
    python -m kairos.main [workspace]
"""
import asyncio
import json
import os
import sys

import websockets

from kairos.config import Config
from kairos.cli import CLI, _paste_registry


async def _ws_send(ws, data: dict):
    """Send a JSON dict over websockets (v15+ compat)."""
    await ws.send(json.dumps(data))


async def _ws_recv(ws) -> dict:
    """Receive a JSON dict over websockets (v15+ compat)."""
    raw = await ws.recv()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


async def _async_main():
    """Async main — connects to gateway and runs REPL."""
    try:
        Config.validate()
    except ValueError as e:
        print(f"Configuration error: {e}")
        print("Please create a .env file with your OPENAI_API_KEY.")
        sys.exit(1)

    workspace = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    port = Config.KAIROS_GATEWAY_PORT()
    host = Config.KAIROS_GATEWAY_HOST()
    uri = f"ws://{host}:{port}/ws"

    cli = CLI()
    cli.print_banner()

    try:
        async with websockets.connect(uri, max_size=2**24) as ws:
            # ── List sessions on startup ──────────────────────────
            await _ws_send(ws, {"type": "list_sessions"})
            resp = await _ws_recv(ws)
            sessions = resp.get("sessions", [])

            cli.print_info(f"Found {len(sessions)} saved conversation(s)")
            cli.print_info("Commands: 'exit' to quit, '/compact' to compact, '/reset' for new chat")

            # ── Resume or new session ─────────────────────────────
            session_id = None
            if sessions:
                choice = cli.pick_session(sessions)
                if choice:
                    await _ws_send(ws, {"type": "load_session", "session_id": choice})
                    resp = await _ws_recv(ws)
                    if resp["type"] == "connected":
                        session_id = resp["session_id"]
                        cli.print_info(f"Loaded: {session_id} ({resp['workspace']})")
                        history = resp.get("history", [])
                        if history:
                            last = _last_assistant_message(history)
                            if last:
                                cli.print_response(last)

            if not session_id:
                await _ws_send(ws, {"type": "new_session", "workspace": workspace})
                resp = await _ws_recv(ws)
                session_id = resp["session_id"]
                cli.print_info(f"New session: {session_id}")

            # ── REPL loop ─────────────────────────────────────────
            while True:
                try:
                    user_input = await asyncio.get_event_loop().run_in_executor(
                        None, cli.get_user_input
                    )
                except (KeyboardInterrupt, EOFError):
                    break

                if user_input is None:
                    break

                # Handle exit commands
                if user_input.lower() in ("exit", "quit", "q", "/exit", "/quit"):
                    await _ws_send(ws, {"type": "message", "content": "exit"})
                    break

                # Handle clear
                if user_input.lower() == "clear":
                    cli.clear_screen()
                    cli.print_banner()
                    continue

                # Handle special commands (send as messages to gateway)
                if user_input.lower() in ("reset", "/reset", "/compact"):
                    await _ws_send(ws, {"type": "message", "content": user_input})
                    # Receive confirmation
                    resp = await _ws_recv(ws)
                    if resp["type"] == "new_session_created":
                        session_id = resp["session_id"]
                        cli.print_info(f"New session: {session_id}")
                    elif resp["type"] == "compacted":
                        cli.print_info(resp["message"])
                    continue

                # Resolve paste tokens
                paste_image_data_url = None
                for token, data in list(_paste_registry.items()):
                    if token in user_input:
                        if data["type"] == "text":
                            user_input = user_input.replace(token, data["text_stripped"])
                        elif data["type"] == "image":
                            paste_image_data_url = data["data_url"]
                            user_input = user_input.replace(token, "")
                _paste_registry.clear()

                if not user_input.strip():
                    if not paste_image_data_url:
                        continue
                    user_input = "Describe this image."

                # Send message
                msg_payload = {"type": "message", "content": user_input}
                if paste_image_data_url:
                    msg_payload["image_url"] = paste_image_data_url
                await _ws_send(ws, msg_payload)

                # ── Receive stream ────────────────────────────────
                cli.start_thinking()
                try:
                    while True:
                        raw = await _ws_recv(ws)
                        msg_type = raw["type"]

                        if msg_type == "stream_start":
                            cli.stop_thinking()
                            cli.start_stream()
                        elif msg_type == "stream_token":
                            cli.on_stream_token(raw["content"])
                        elif msg_type == "tool_call":
                            cli.print_tool_summary(raw["summary"])
                        elif msg_type == "stream_end":
                            if not raw["has_tool_calls"]:
                                cli.finalize_stream_as_response()
                            else:
                                cli.finish_stream()  # thinking trace
                        elif msg_type == "token_update":
                            status = (
                                f"Session: {raw['session_input']} in / {raw['session_output']} out"
                                f"  |  Context: {raw['context_pct']}%"
                                f"  |  Turn: {raw['turn_input']} in / {raw['turn_output']} out"
                            )
                            cli.console.print(f"[dim]{status}[/dim]")
                        elif msg_type == "compacted":
                            cli.print_info(raw["message"])
                        elif msg_type == "done":
                            cli.stop_thinking()
                            if raw.get("response") and not cli._skip_print_response:
                                cli.print_response(raw["response"])
                            cli._skip_print_response = False
                            break
                        elif msg_type == "error":
                            cli.stop_thinking()
                            cli.print_error(raw["message"])
                            break
                        elif msg_type == "exit":
                            cli.stop_thinking()
                            return
                        elif msg_type == "unloaded":
                            continue

                except asyncio.CancelledError:
                    await _ws_send(ws, {"type": "interrupt"})
                    cli.stop_thinking()
                    cli.print_info("[Interrupted]")
                    continue

            # ── Shutdown ──────────────────────────────────────────
            if session_id:
                await _ws_send(ws, {"type": "unload"})
            cli.print_exit()

    except (ConnectionRefusedError, OSError):
        print(f"Cannot connect to gateway at {uri}")
        print(f"Start it with: python -m kairos.main_gateway [workspace]")
        sys.exit(1)


def _last_assistant_message(history):
    """Find the last clean assistant response for display."""
    for msg in reversed(history):
        if msg.get("role") == "assistant" and not msg.get("tool_calls"):
            return msg.get("content", "")
    return None


def main():
    """Sync entry point for console_scripts."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
