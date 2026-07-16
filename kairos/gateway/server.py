"""FastAPI application — WebSocket + REST routes for the Kairos gateway.

Usage:
    from kairos.gateway.server import create_app
    app = create_app(default_workspace="/path/to/project")
    uvicorn.run(app, host="127.0.0.1", port=8765)
"""
import asyncio
import json
import logging
import os
import traceback
from pathlib import Path
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


def create_app(default_workspace: str = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        default_workspace: Optional fallback workspace path. If None,
            clients must specify a workspace for each new session.
    """
    app = FastAPI(title="Kairos Gateway")
    manager = GatewayManager(default_workspace)

    # Prevent garbage-collection of background tasks started by the WS handler.
    _bg_tasks: set = set()

    @app.on_event("startup")
    async def startup():
        asyncio.create_task(manager.cleanup_idle())
        ws_label = default_workspace or "(none \u2014 workspace required per session)"
        log.info(f"Gateway started. Default workspace: {ws_label}")

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

    @app.get("/api/workspaces")
    async def api_list_workspaces():
        return {"workspaces": manager.list_workspaces()}

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        active_sessions: set = set()  # sessions loaded by this client
        loop = asyncio.get_event_loop()
        home_dir = str(Path.home())

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

        async def _unload_old_sessions_list(sids: list):
            """Unload a list of sessions in the background."""
            for sid in sids:
                try:
                    await manager.unload_session(sid)
                except Exception as e:
                    log.warning(f"Failed to unload {sid}: {e}")
                try:
                    await safe_send({
                        "type": "unloaded",
                        "session_id": sid,
                    })
                except Exception:
                    pass

        try:
            while True:
                raw = await websocket.receive_json()
                msg_type = raw.get("type")
                msg_sid = raw.get("session_id")  # session_id from client message

                # -- Connect -------------------------------------------
                if msg_type == "connect":
                    session_id = raw.get("session_id")
                    if session_id:
                        try:
                            session = await manager.load_session(session_id)
                            active_sessions.add(session.session_id)
                            await safe_send({
                                "type": "connected",
                                "session_id": session.session_id,
                                "workspace": session.workspace,
                                "history": _sanitize_for_client(
                                    session.agent.conversation_history
                                ),
                                "workspaces": manager.list_workspaces(),
                                "home_dir": home_dir,
                            })
                        except ValueError as e:
                            await safe_send({"type": "error", "message": str(e)})
                    else:
                        await safe_send({
                            "type": "connected",
                            "session_id": None,
                            "workspace": manager.default_workspace,
                            "workspaces": manager.list_workspaces(),
                            "home_dir": home_dir,
                        })

                # -- New session ---------------------------------------
                elif msg_type == "new_session":
                    # 1) Create the new session IMMEDIATELY so the client
                    #    gets a response fast (workspace picker disappears).
                    old_sids = list(active_sessions)
                    try:
                        session = await manager.create_session(raw.get("workspace"))
                        active_sessions.add(session.session_id)
                        await safe_send({
                            "type": "new_session_created",
                            "session_id": session.session_id,
                            "workspace": session.workspace,
                        })
                        await safe_send({
                            "type": "sessions_list",
                            "sessions": manager.list_sessions(),
                        })
                    except ValueError as e:
                        await safe_send({"type": "error", "message": str(e)})
                        continue

                    # 2) Unload old sessions in the background so
                    #    browser_close / save don't block the user.
                    if old_sids:
                        task = asyncio.create_task(_unload_old_sessions_list(old_sids))
                        _bg_tasks.add(task)
                        task.add_done_callback(_bg_tasks.discard)
                        for sid in old_sids:
                            active_sessions.discard(sid)

                # -- Load a saved session ------------------------------
                elif msg_type == "load_session":
                    target_id = raw["session_id"]
                    try:
                        session = await manager.load_session(target_id)
                        active_sessions.add(session.session_id)
                        await safe_send({
                            "type": "connected",
                            "session_id": session.session_id,
                            "workspace": session.workspace,
                            "history": _sanitize_for_client(
                                session.agent.conversation_history
                            ),
                            "workspaces": manager.list_workspaces(),
                            "home_dir": home_dir,
                        })
                    except ValueError as e:
                        await safe_send({"type": "error", "message": str(e)})

                # -- Unload a specific session -------------------------
                elif msg_type == "unload":
                    target = msg_sid
                    if target:
                        await manager.unload_session(target)
                        active_sessions.discard(target)
                        await safe_send({
                            "type": "unloaded",
                            "session_id": target,
                        })

                # -- List sessions -------------------------------------
                elif msg_type == "list_sessions":
                    sessions = manager.list_sessions()
                    await safe_send({
                        "type": "sessions_list",
                        "sessions": sessions,
                    })

                # -- List workspaces -----------------------------------
                elif msg_type == "list_workspaces":
                    workspaces = manager.list_workspaces()
                    await safe_send({
                        "type": "workspaces_list",
                        "workspaces": workspaces,
                    })

                # -- Send a message ------------------------------------
                elif msg_type == "message":
                    target_sid = msg_sid

                    # Auto-recover: create session if target is unloaded
                    if not target_sid or manager.get_session(target_sid) is None:
                        msg_workspace = raw.get("workspace")
                        try:
                            session = await manager.create_session(msg_workspace)
                            target_sid = session.session_id
                            active_sessions.add(target_sid)
                            await safe_send({
                                "type": "new_session_created",
                                "session_id": session.session_id,
                                "workspace": session.workspace,
                            })
                            await safe_send({
                                "type": "sessions_list",
                                "sessions": manager.list_sessions(),
                            })
                        except ValueError as e:
                            await safe_send({"type": "error", "message": str(e)})
                            continue

                    content = raw["content"]
                    image_url = raw.get("image_url")
                    image_urls = raw.get("image_urls")
                    if image_urls:
                        effective_image_url = image_urls[0] if image_urls else None
                    else:
                        effective_image_url = image_url
                    content_stripped = content.strip()

                    # Handle /compact
                    if content_stripped == "/compact":
                        result = await manager.compact(target_sid)
                        await safe_send({
                            "type": "compacted",
                            "session_id": target_sid,
                            "message": result,
                        })
                        continue

                    # Handle /reset — preserve workspace, unload all, create new
                    if content_stripped == "/reset":
                        reset_workspace = None
                        if target_sid:
                            active_session = manager.get_session(target_sid)
                            if active_session:
                                reset_workspace = active_session.workspace
                        old_sids = list(active_sessions)
                        try:
                            session = await manager.create_session(reset_workspace)
                            active_sessions.add(session.session_id)
                            await safe_send({
                                "type": "new_session_created",
                                "session_id": session.session_id,
                                "workspace": session.workspace,
                            })
                        except ValueError as e:
                            await safe_send({"type": "error", "message": str(e)})
                        if old_sids:
                            task = asyncio.create_task(_unload_old_sessions_list(old_sids))
                            _bg_tasks.add(task)
                            task.add_done_callback(_bg_tasks.discard)
                            for sid in old_sids:
                                active_sessions.discard(sid)
                        continue

                    # Handle /exit, exit, quit
                    if content_stripped in ("/exit", "exit", "quit"):
                        for sid in list(active_sessions):
                            await manager.unload_session(sid)
                        active_sessions.clear()
                        await safe_send({"type": "exit"})
                        break

                    # -- Send to agent (non-blocking) ------------------
                    # Run send_message as a background task so the WS
                    # handler loop is free to process interrupt, new_session,
                    # load_session, etc. while the agent is still streaming.
                    # Without this, all other messages are stuck in the
                    # WebSocket buffer until the agent finishes.
                    async def _run_message():
                        try:
                            await manager.send_message(
                                target_sid,
                                content,
                                effective_image_url,
                                callbacks={
                                    "on_stream_start": lambda: thread_safe_send({
                                        "type": "stream_start",
                                        "session_id": target_sid,
                                    }),
                                    "on_token": lambda t: thread_safe_send({
                                        "type": "stream_token",
                                        "session_id": target_sid,
                                        "content": t,
                                    }),
                                    "on_stream_end": lambda c, h: thread_safe_send({
                                        "type": "stream_end",
                                        "session_id": target_sid,
                                        "content": c,
                                        "has_tool_calls": h,
                                    }),
                                    "on_tool_call": lambda n, a: thread_safe_send({
                                        "type": "tool_call",
                                        "session_id": target_sid,
                                        "name": n,
                                        "args": a,
                                        "summary": Agent._tool_summary(n, a),
                                    }),
                                    "on_token_update": lambda tc: thread_safe_send({
                                        "type": "token_update",
                                        "session_id": target_sid,
                                        "session_input": tc.session_input,
                                        "session_output": tc.session_output,
                                        "context_pct": round(tc.context_pct, 1),
                                        "turn_input": tc.turn_input,
                                        "turn_output": tc.turn_output,
                                    }),
                                    "on_compact": lambda m: thread_safe_send({
                                        "type": "compacted",
                                        "session_id": target_sid,
                                        "message": m,
                                    }),
                                    "on_done": lambda r: (
                                        thread_safe_send({
                                            "type": "done",
                                            "session_id": target_sid,
                                            "response": r,
                                        }),
                                        thread_safe_send({
                                            "type": "sessions_list",
                                            "sessions": manager.list_sessions(),
                                        }),
                                    ),
                                    "on_error": lambda e: thread_safe_send({
                                        "type": "error",
                                        "message": e,
                                    }),
                                    # Sub-agent callbacks (flow through same stream)
                                    "on_subagent_tool": lambda summary: thread_safe_send({
                                        "type": "tool_call",
                                        "session_id": target_sid,
                                        "name": "subagent",
                                        "args": {},
                                        "summary": f"\u2193 subagent: {summary}",
                                    }),
                                    "on_subagent_stream_start": lambda: thread_safe_send({
                                        "type": "stream_start",
                                        "session_id": target_sid,
                                    }),
                                    "on_subagent_stream_token": lambda t: thread_safe_send({
                                        "type": "stream_token",
                                        "session_id": target_sid,
                                        "content": t,
                                    }),
                                    "on_subagent_stream_end": lambda _c, _h: thread_safe_send({
                                        "type": "stream_end",
                                        "session_id": target_sid,
                                        "content": "",
                                        "has_tool_calls": True,
                                    }),
                                },
                            )
                        except ValueError as e:
                            await safe_send({"type": "error", "message": str(e)})
                        except Exception as e:
                            log.error(f"Background message error in {target_sid}: {e}")

                    task = asyncio.create_task(_run_message())
                    _bg_tasks.add(task)
                    task.add_done_callback(_bg_tasks.discard)

                # -- Interrupt -----------------------------------------
                elif msg_type == "interrupt":
                    if msg_sid:
                        await manager.interrupt(msg_sid)

                # -- Stop (treated as interrupt for instant response) ---
                elif msg_type == "stop":
                    if msg_sid:
                        await manager.interrupt(msg_sid)

                # -- Compact -------------------------------------------
                elif msg_type == "compact":
                    if msg_sid:
                        result = await manager.compact(msg_sid)
                        await safe_send({
                            "type": "compacted",
                            "session_id": msg_sid,
                            "message": result,
                        })

                # -- Ping ----------------------------------------------
                elif msg_type == "ping":
                    await safe_send({"type": "pong"})

        except WebSocketDisconnect:
            log.info(f"Client disconnected (sessions: {active_sessions})")
        except Exception as e:
            log.error(f"WebSocket error: {e}\n{traceback.format_exc()}")
        finally:
            # Cleanup: save and unload all active sessions
            for sid in list(active_sessions):
                try:
                    await manager.unload_session(sid)
                except Exception:
                    pass

    return app
