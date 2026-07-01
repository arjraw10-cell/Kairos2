"""FastAPI application — WebSocket + REST routes for the Kairos gateway.

Usage:
    from kairos.gateway.server import create_app
    app = create_app(default_workspace="/path/to/project")
    uvicorn.run(app, host="127.0.0.1", port=8765)
"""
import asyncio
import json
import logging
import traceback
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..agent import Agent
from .manager import GatewayManager

log = logging.getLogger("kairos.gateway")


def _sanitize_for_client(history: list) -> list:
    """Strip tool results and incomplete assistant messages.

    Returns only clean message pairs (user ↔ assistant without tool_calls)
    suitable for initial client display. The full history stays in the
    Agent for context continuity.
    """
    if not history:
        return []

    result = [history[0]]  # always keep system prompt

    i = 1
    while i < len(history):
        msg = history[i]
        role = msg.get("role", "")

        if role == "user":
            # Keep user messages unless they're screenshot injections
            content = msg.get("content", "")
            is_screenshot = False
            if isinstance(content, list) and len(content) > 0:
                first = content[0]
                if isinstance(first, dict) and first.get("type") == "text":
                    if first.get("text", "").startswith("[Screenshot captured"):
                        is_screenshot = True
            if not is_screenshot:
                result.append(msg)

        elif role == "assistant":
            if msg.get("tool_calls"):
                # Incomplete — skip this and all following tool results
                # until the next clean assistant message or user message
                i += 1
                while i < len(history):
                    next_role = history[i].get("role", "")
                    if next_role == "user":
                        # Back up one so the user message is processed
                        i -= 1
                        break
                    if next_role == "assistant" and not history[i].get("tool_calls"):
                        # Found a clean assistant response
                        i -= 1  # back up so this gets processed in outer loop
                        break
                    i += 1
                i += 1
                continue
            else:
                # Clean assistant message
                result.append(msg)

        elif role == "tool":
            # Skip tool results
            pass

        i += 1

    return result


def _last_assistant_message(history: list) -> Optional[str]:
    """Find the last clean assistant response for display."""
    for msg in reversed(history):
        if msg.get("role") == "assistant" and not msg.get("tool_calls"):
            content = msg.get("content", "")
            if content:
                return content
    return None


def create_app(default_workspace: str) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        default_workspace: Fallback workspace path for sessions
            that don't have one stored.
    """
    app = FastAPI(title="Kairos Gateway")
    manager = GatewayManager(default_workspace)

    @app.on_event("startup")
    async def startup():
        asyncio.create_task(manager.cleanup_idle())
        log.info(f"Gateway started. Default workspace: {default_workspace}")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "active_sessions": len(manager._sessions),
            "default_workspace": manager.default_workspace,
        }

    @app.get("/api/sessions")
    async def api_list_sessions():
        return manager.list_sessions()

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        current_session_id: Optional[str] = None
        loop = asyncio.get_event_loop()  # capture for thread-safe sends

        async def safe_send(data: dict):
            """Send JSON to WebSocket, swallowing errors if disconnected."""
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json(data)
            except Exception:
                pass

        def thread_safe_send(data: dict):
            """Schedule a send from a background thread (agent callbacks).

            Uses run_coroutine_threadsafe to safely schedule the coroutine
            on the event loop from any thread.
            """
            try:
                asyncio.run_coroutine_threadsafe(safe_send(data), loop)
            except Exception:
                pass

        try:
            while True:
                raw = await websocket.receive_json()
                msg_type = raw.get("type")

                # ── Connect to existing session ────────────────────
                if msg_type == "connect":
                    session_id = raw.get("session_id")
                    if session_id:
                        try:
                            session = await manager.load_session(session_id)
                            current_session_id = session.session_id
                            await safe_send({
                                "type": "connected",
                                "session_id": session.session_id,
                                "workspace": session.workspace,
                                "history": _sanitize_for_client(
                                    session.agent.conversation_history
                                ),
                            })
                        except ValueError as e:
                            await safe_send({"type": "error", "message": str(e)})
                    else:
                        await safe_send({
                            "type": "connected",
                            "session_id": None,
                            "workspace": manager.default_workspace,
                        })

                # ── New session ────────────────────────────────────
                elif msg_type == "new_session":
                    if current_session_id:
                        await manager.unload_session(current_session_id)
                    session = await manager.create_session(raw.get("workspace"))
                    current_session_id = session.session_id
                    await safe_send({
                        "type": "new_session_created",
                        "session_id": session.session_id,
                        "workspace": session.workspace,
                    })

                # ── Load a saved session ───────────────────────────
                elif msg_type == "load_session":
                    if current_session_id:
                        await manager.unload_session(current_session_id)
                    target_id = raw["session_id"]
                    try:
                        session = await manager.load_session(target_id)
                        current_session_id = session.session_id
                        await safe_send({
                            "type": "connected",
                            "session_id": session.session_id,
                            "workspace": session.workspace,
                            "history": _sanitize_for_client(
                                session.agent.conversation_history
                            ),
                        })
                    except ValueError as e:
                        await safe_send({"type": "error", "message": str(e)})

                # ── Unload current session ─────────────────────────
                elif msg_type == "unload":
                    if current_session_id:
                        await manager.unload_session(current_session_id)
                        await safe_send({
                            "type": "unloaded",
                            "session_id": current_session_id,
                        })
                        current_session_id = None

                # ── List sessions ──────────────────────────────────
                elif msg_type == "list_sessions":
                    sessions = manager.list_sessions()
                    await safe_send({
                        "type": "sessions_list",
                        "sessions": sessions,
                    })

                # ── Send a message ─────────────────────────────────
                elif msg_type == "message":
                    if not current_session_id:
                        await safe_send({
                            "type": "error",
                            "message": "No session loaded",
                        })
                        continue

                    content = raw["content"]
                    image_url = raw.get("image_url")
                    content_stripped = content.strip()

                    # Handle /compact
                    if content_stripped == "/compact":
                        result = await manager.compact(current_session_id)
                        await safe_send({
                            "type": "compacted",
                            "session_id": current_session_id,
                            "message": result,
                        })
                        continue

                    # Handle /reset
                    if content_stripped == "/reset":
                        await manager.unload_session(current_session_id)
                        session = await manager.create_session()
                        current_session_id = session.session_id
                        await safe_send({
                            "type": "new_session_created",
                            "session_id": session.session_id,
                            "workspace": session.workspace,
                        })
                        continue

                    # Handle /exit, exit, quit
                    if content_stripped in ("/exit", "exit", "quit"):
                        if current_session_id:
                            await manager.unload_session(current_session_id)
                            current_session_id = None
                        await safe_send({"type": "exit"})
                        break

                    # ── Send to agent ──
                    try:
                        await manager.send_message(
                            current_session_id,
                            content,
                            image_url,
                            callbacks={
                                "on_stream_start": lambda: thread_safe_send({
                                    "type": "stream_start",
                                    "session_id": current_session_id,
                                }),
                                "on_token": lambda t: thread_safe_send({
                                    "type": "stream_token",
                                    "session_id": current_session_id,
                                    "content": t,
                                }),
                                "on_stream_end": lambda c, h: thread_safe_send({
                                    "type": "stream_end",
                                    "session_id": current_session_id,
                                    "content": c,
                                    "has_tool_calls": h,
                                }),
                                "on_tool_call": lambda n, a: thread_safe_send({
                                    "type": "tool_call",
                                    "session_id": current_session_id,
                                    "name": n,
                                    "args": a,
                                    "summary": Agent._tool_summary(n, a),
                                }),
                                "on_token_update": lambda tc: thread_safe_send({
                                    "type": "token_update",
                                    "session_id": current_session_id,
                                    "session_input": tc.session_input,
                                    "session_output": tc.session_output,
                                    "context_pct": round(tc.context_pct, 1),
                                    "turn_input": tc.turn_input,
                                    "turn_output": tc.turn_output,
                                }),
                                "on_compact": lambda m: thread_safe_send({
                                    "type": "compacted",
                                    "session_id": current_session_id,
                                    "message": m,
                                }),
                                "on_done": lambda r: thread_safe_send({
                                    "type": "done",
                                    "session_id": current_session_id,
                                    "response": r,
                                }),
                                "on_error": lambda e: thread_safe_send({
                                    "type": "error",
                                    "message": e,
                                }),
                                # Sub-agent callbacks (flow through same stream)
                                "on_subagent_tool": lambda summary: thread_safe_send({
                                    "type": "tool_call",
                                    "session_id": current_session_id,
                                    "name": "subagent",
                                    "args": {},
                                    "summary": f"↓ subagent: {summary}",
                                }),
                                "on_subagent_stream_start": lambda: thread_safe_send({
                                    "type": "stream_start",
                                    "session_id": current_session_id,
                                }),
                                "on_subagent_stream_token": lambda t: thread_safe_send({
                                    "type": "stream_token",
                                    "session_id": current_session_id,
                                    "content": t,
                                }),
                                "on_subagent_stream_end": lambda _c, _h: thread_safe_send({
                                    "type": "stream_end",
                                    "session_id": current_session_id,
                                    "content": "",
                                    "has_tool_calls": True,
                                }),
                            },
                        )
                    except ValueError as e:
                        await safe_send({"type": "error", "message": str(e)})

                # ── Interrupt ──────────────────────────────────────
                elif msg_type == "interrupt":
                    if current_session_id:
                        await manager.interrupt(current_session_id)

                # ── Stop ───────────────────────────────────────────
                elif msg_type == "stop":
                    if current_session_id:
                        await manager.stop(current_session_id)

                # ── Compact ────────────────────────────────────────
                elif msg_type == "compact":
                    if current_session_id:
                        result = await manager.compact(current_session_id)
                        await safe_send({
                            "type": "compacted",
                            "session_id": current_session_id,
                            "message": result,
                        })

                # ── Ping ───────────────────────────────────────────
                elif msg_type == "ping":
                    await safe_send({"type": "pong"})

        except WebSocketDisconnect:
            log.info(f"Client disconnected (session: {current_session_id})")
        except Exception as e:
            log.error(f"WebSocket error: {e}\n{traceback.format_exc()}")
        finally:
            # Cleanup: save and unload the active session
            if current_session_id:
                await manager.unload_session(current_session_id)

    return app
