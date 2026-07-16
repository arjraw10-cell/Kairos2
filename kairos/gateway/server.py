"""FastAPI HTTP and WebSocket surface for the Kairos gateway."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import Config
from .history import visible_history
from .manager import GatewayManager
from .models import Conversation
from .protocol import Command, PROTOCOL_VERSION, ack, error_message, event_message

log = logging.getLogger("kairos.gateway")


class WorkspaceCreate(BaseModel):
    path: str
    display_name: str = ""


class ConversationCreate(BaseModel):
    workspace: str | None = None
    title: str = ""


class ConversationPatch(BaseModel):
    title: str | None = None
    archived: bool | None = None


class MessageCreate(BaseModel):
    content: str
    image_url: str | None = None
    source: str = "api"
    client_id: str | None = None


class ContinueRequest(BaseModel):
    instruction: str = "Continue where you left off."
    source: str = "api"
    client_id: str | None = None


class RuntimeUnload(BaseModel):
    force: bool = False


def _conversation_response(conversation: Conversation) -> dict[str, Any]:
    return conversation.to_dict()


def _token_is_valid(token: str | None, expected: str | None) -> bool:
    return expected is None or (token is not None and token == expected)


def create_app(manager: GatewayManager | None = None) -> FastAPI:
    gateway = manager or GatewayManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cleanup_task = asyncio.create_task(gateway.cleanup_idle())
        try:
            yield
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            await gateway.shutdown()

    app = FastAPI(title="Kairos Gateway", version="1.0", lifespan=lifespan)
    app.state.gateway = gateway

    def current_auth_token() -> str | None:
        # Resolve at request time so embedded callers can reload config after
        # constructing the app and long-lived processes do not retain a stale
        # module-level credential unexpectedly.
        return Config.KAIROS_AUTH_TOKEN()

    @app.middleware("http")
    async def authenticate_http(request, call_next):
        auth_token = current_auth_token()
        # Health/ready endpoints remain usable for process supervision. Keep
        # this exact-path check narrow so similarly prefixed routes are not
        # accidentally exposed without authentication.
        if auth_token is not None and request.url.path not in {"/healthz", "/readyz"}:
            header = request.headers.get("authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else None
            if not _token_is_valid(supplied, auth_token):
                return JSONResponse(
                    {"detail": "Authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "protocol_version": PROTOCOL_VERSION,
            "active_runtimes": len(gateway._runtimes),
        }

    @app.get("/readyz")
    async def readyz():
        return {"status": "ready", "database": str(gateway.repository.db_path)}

    @app.get("/api/v1/capabilities")
    async def capabilities():
        return {
            "protocol_version": PROTOCOL_VERSION,
            "features": [
                "streaming",
                "multiple_conversations",
                "multiple_workspaces",
                "interrupts",
                "compaction",
                "event_replay",
            ],
            "limits": {"max_concurrent_runs": gateway.max_concurrent_runs},
        }

    @app.get("/api/v1/workspaces")
    async def list_workspaces(
        search: str | None = None,
        limit: int = Query(100, ge=1, le=500),
    ):
        return {"items": gateway.list_workspaces(search=search, limit=limit)}

    @app.post("/api/v1/workspaces")
    async def register_workspace(request: WorkspaceCreate):
        return gateway.repository.upsert_workspace(request.path, request.display_name)

    @app.get("/api/v1/workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str):
        workspace = gateway.repository.get_workspace(workspace_id)
        if not workspace:
            raise HTTPException(404, "Workspace not found")
        return workspace

    @app.get("/api/v1/conversations")
    async def list_conversations(
        workspace_id: str | None = None,
        search: str | None = None,
        archived: bool = False,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        conversations = gateway.list_conversations(
            workspace_id=workspace_id,
            search=search,
            archived=archived,
            limit=limit,
            offset=offset,
        )
        return {"items": [_conversation_response(c) for c in conversations]}

    @app.post("/api/v1/conversations", status_code=201)
    async def create_conversation(request: ConversationCreate):
        try:
            conversation = gateway.create_conversation(request.workspace, request.title)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _conversation_response(conversation)

    @app.get("/api/v1/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str):
        try:
            return _conversation_response(gateway.get_conversation(conversation_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.patch("/api/v1/conversations/{conversation_id}")
    async def patch_conversation(conversation_id: str, request: ConversationPatch):
        try:
            conversation = gateway.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        changes = (
            request.model_dump(exclude_none=True)
            if hasattr(request, "model_dump")
            else request.dict(exclude_none=True)
        )
        updated = gateway.repository.update_conversation(conversation.id, **changes)
        return _conversation_response(updated)

    @app.delete("/api/v1/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str):
        try:
            await gateway.unload_runtime(conversation_id, force=True)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not gateway.repository.delete_conversation(conversation_id):
            raise HTTPException(404, "Conversation not found")
        return {"deleted": True, "conversation_id": conversation_id}

    @app.post("/api/v1/conversations/{conversation_id}/runtime/load")
    async def load_runtime(conversation_id: str):
        try:
            runtime = await gateway.load_runtime(conversation_id)
            conversation = gateway.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        with runtime.state_lock:
            history = list(runtime.agent.get_history())
        return {
            "conversation": _conversation_response(conversation),
            "history": visible_history(history),
        }

    @app.post("/api/v1/conversations/{conversation_id}/runtime/unload")
    async def unload_runtime(
        conversation_id: str,
        request: RuntimeUnload | None = None,
    ):
        try:
            await gateway.unload_runtime(
                conversation_id,
                force=bool(request and request.force),
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"conversation_id": conversation_id, "runtime_loaded": False}

    @app.get("/api/v1/conversations/{conversation_id}/messages")
    async def get_messages(
        conversation_id: str,
        include_internal: bool = False,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        try:
            gateway.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {
            "conversation_id": conversation_id,
            "items": gateway.visible_messages(
                conversation_id, include_internal, limit, offset
            ),
        }

    @app.post("/api/v1/conversations/{conversation_id}/messages", status_code=202)
    async def send_message(conversation_id: str, request: MessageCreate):
        try:
            run = await gateway.submit_message(
                conversation_id,
                request.content,
                request.image_url,
                request.source,
                request.client_id,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return {
            "conversation_id": conversation_id,
            "run_id": run.id,
            "status": run.status,
        }

    @app.post("/api/v1/conversations/{conversation_id}/continue", status_code=202)
    async def continue_conversation(
        conversation_id: str,
        request: ContinueRequest | None = None,
    ):
        request = request or ContinueRequest()
        try:
            run = await gateway.continue_conversation(
                conversation_id,
                request.instruction,
                request.source,
                request.client_id,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return {
            "conversation_id": conversation_id,
            "run_id": run.id,
            "status": run.status,
        }

    @app.post("/api/v1/conversations/{conversation_id}/compact")
    async def compact(conversation_id: str):
        try:
            result = await gateway.compact(conversation_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"conversation_id": conversation_id, "message": result}

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str):
        run = gateway.repository.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        return run.to_dict()

    @app.post("/api/v1/runs/{run_id}/interrupt")
    async def interrupt_run(run_id: str):
        run = gateway.repository.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        interrupted = await gateway.interrupt_run(run.conversation_id, run_id)
        return {"run_id": run_id, "accepted": interrupted}

    @app.post("/api/v1/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        try:
            cancelled = await gateway.cancel_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        run = gateway.repository.get_run(run_id)
        return {
            "run_id": run_id,
            "cancelled": cancelled,
            "status": run.status if run else "unknown",
        }

    @app.websocket("/api/v1/ws")
    async def websocket_endpoint(websocket: WebSocket):
        auth_token = current_auth_token()
        if auth_token is not None:
            header = websocket.headers.get("authorization", "")
            supplied = (
                header[7:]
                if header.startswith("Bearer ")
                else websocket.query_params.get("token")
            )
            if not _token_is_valid(supplied, auth_token):
                await websocket.close(code=1008, reason="Authentication required")
                return
        await websocket.accept()
        subscriptions: set[str] = set()
        replaying: set[str] = set()
        deferred_events: dict[str, list[dict[str, Any]]] = {}
        replay_cursors: dict[str, int] = {}
        replay_locks: dict[str, asyncio.Lock] = {}
        subscriber_id = uuid.uuid4().hex
        send_lock = asyncio.Lock()

        async def send(payload: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_json(payload)

        async def send_event(event: dict[str, Any]) -> None:
            conversation_id = event.get("conversation_id")
            if conversation_id not in subscriptions:
                return
            # Serialize live delivery with replay. The event callback may have
            # been scheduled while replay was reading SQLite; waiting on this
            # lock prevents it from overtaking replay or being lost during the
            # replay-to-live handoff.
            lock = replay_locks.setdefault(conversation_id, asyncio.Lock())
            async with lock:
                if conversation_id not in subscriptions:
                    return
                event_id = int(event.get("event_id") or 0)
                cursor = replay_cursors.get(conversation_id, 0)
                if event_id and event_id <= cursor:
                    return
                if conversation_id in replaying:
                    deferred_events.setdefault(conversation_id, []).append(event)
                    return
                try:
                    await send(event_message(event))
                    if event_id:
                        replay_cursors[conversation_id] = max(cursor, event_id)
                except Exception:
                    # The receive loop will eventually observe disconnect; never
                    # let a dead client break the Agent worker.
                    pass

        async def replay_subscription(conversation_id: str, after_event_id: int) -> None:
            lock = replay_locks.setdefault(conversation_id, asyncio.Lock())
            async with lock:
                replaying.add(conversation_id)
                replay_cursors[conversation_id] = after_event_id
                try:
                    cursor = after_event_id
                    while True:
                        events = gateway.events_after(conversation_id, cursor)
                        if not events:
                            break
                        for event in events:
                            await send(event_message(event))
                            cursor = max(cursor, int(event.get("event_id") or cursor))
                        if len(events) < 500:
                            break
                    replay_cursors[conversation_id] = cursor
                    queued = deferred_events.pop(conversation_id, [])
                    for event in sorted(
                        queued,
                        key=lambda item: int(item.get("event_id") or 0),
                    ):
                        event_id = int(event.get("event_id") or 0)
                        if event_id > cursor:
                            await send(event_message(event))
                            cursor = max(cursor, event_id)
                    replay_cursors[conversation_id] = cursor
                finally:
                    replaying.discard(conversation_id)
                    deferred_events.pop(conversation_id, None)

        try:
            while True:
                message = await websocket.receive_json()
                if message.get("type") != "command":
                    await send(
                        error_message(
                            "invalid_message",
                            "Expected a command envelope.",
                            message.get("request_id"),
                        )
                    )
                    continue

                command = message.get("command")
                request_id = message.get("request_id")
                conversation_id = message.get("conversation_id")
                payload = message.get("payload") or {}

                try:
                    if command == Command.HELLO:
                        await send(
                            ack(
                                request_id,
                                {
                                    "protocol_version": PROTOCOL_VERSION,
                                    "features": ["streaming", "event_replay"],
                                },
                            )
                        )
                    elif command == Command.LIST_CONVERSATIONS:
                        items = [
                            c.to_dict() for c in gateway.list_conversations(**payload)
                        ]
                        await send(ack(request_id, {"items": items}))
                    elif command == Command.CREATE_CONVERSATION:
                        conversation = gateway.create_conversation(
                            payload.get("workspace"), payload.get("title", "")
                        )
                        await send(
                            ack(request_id, {"conversation": conversation.to_dict()})
                        )
                    elif command == Command.LOAD_CONVERSATION:
                        runtime = await gateway.load_runtime(conversation_id)
                        with runtime.state_lock:
                            history = list(runtime.agent.get_history())
                        await send(
                            ack(
                                request_id,
                                {
                                    "conversation": gateway.get_conversation(
                                        conversation_id
                                    ).to_dict(),
                                    "history": visible_history(history),
                                },
                            )
                        )
                    elif command == Command.UNLOAD_CONVERSATION:
                        await gateway.unload_runtime(
                            conversation_id, bool(payload.get("force", False))
                        )
                        await send(
                            ack(
                                request_id,
                                {
                                    "conversation_id": conversation_id,
                                    "runtime_loaded": False,
                                },
                            )
                        )
                    elif command == Command.SUBSCRIBE:
                        gateway.get_conversation(conversation_id)
                        after = max(0, int(payload.get("after_event_id", 0)))
                        # Initialize replay state before registering the live
                        # callback. An event emitted in this small handoff
                        # window must be deduplicated against the replay, not
                        # delivered early and then replayed a second time.
                        replay_locks.setdefault(conversation_id, asyncio.Lock())
                        replaying.add(conversation_id)
                        replay_cursors[conversation_id] = after
                        subscriptions.add(conversation_id)
                        gateway.subscribe(
                            conversation_id,
                            subscriber_id,
                            asyncio.get_running_loop(),
                            send_event,
                        )
                        await replay_subscription(conversation_id, after)
                        await send(
                            ack(
                                request_id,
                                {
                                    "conversation_id": conversation_id,
                                    "subscribed": True,
                                },
                            )
                        )
                    elif command == Command.UNSUBSCRIBE:
                        subscriptions.discard(conversation_id)
                        replaying.discard(conversation_id)
                        deferred_events.pop(conversation_id, None)
                        replay_cursors.pop(conversation_id, None)
                        replay_locks.pop(conversation_id, None)
                        gateway.unsubscribe(conversation_id, subscriber_id)
                        await send(
                            ack(
                                request_id,
                                {
                                    "conversation_id": conversation_id,
                                    "subscribed": False,
                                },
                            )
                        )
                    elif command == Command.GET_MESSAGES:
                        gateway.get_conversation(conversation_id)
                        items = gateway.visible_messages(
                            conversation_id,
                            bool(payload.get("include_internal", False)),
                            int(payload.get("limit", 100)),
                            int(payload.get("offset", 0)),
                        )
                        await send(
                            ack(
                                request_id,
                                {
                                    "conversation_id": conversation_id,
                                    "items": items,
                                },
                            )
                        )
                    elif command == Command.SEND_MESSAGE:
                        # Live events are delivered through the subscription
                        # registry. Do not also pass this socket as a direct
                        # observer, or subscribed clients receive every event
                        # twice.
                        run = await gateway.submit_message(
                            conversation_id,
                            payload["content"],
                            payload.get("image_url"),
                            payload.get("source", "websocket"),
                            payload.get("client_id"),
                        )
                        await send(
                            ack(
                                request_id,
                                {
                                    "conversation_id": conversation_id,
                                    "run_id": run.id,
                                    "status": run.status,
                                },
                            )
                        )
                    elif command == Command.COMPACT:
                        result = await gateway.compact(conversation_id)
                        await send(ack(request_id, {"message": result}))
                    elif command == Command.CONTINUE:
                        run = await gateway.continue_conversation(
                            conversation_id,
                            payload.get(
                                "instruction", "Continue where you left off."
                            ),
                            payload.get("source", "websocket"),
                            payload.get("client_id"),
                        )
                        await send(
                            ack(
                                request_id,
                                {
                                    "conversation_id": conversation_id,
                                    "run_id": run.id,
                                    "status": run.status,
                                },
                            )
                        )
                    elif command == Command.INTERRUPT:
                        run_id = payload.get("run_id")
                        run = gateway.repository.get_run(run_id) if run_id else None
                        if run_id and not run:
                            raise KeyError(f"Run not found: {run_id}")
                        if run and run.conversation_id != conversation_id:
                            raise ValueError("Run does not belong to this conversation.")
                        accepted = await gateway.interrupt_run(
                            conversation_id, run.id if run else None
                        )
                        await send(ack(request_id, {"accepted": accepted}))
                    elif command == Command.CANCEL:
                        run_id = payload.get("run_id", "")
                        run = gateway.repository.get_run(run_id)
                        if not run:
                            raise KeyError(f"Run not found: {run_id}")
                        if conversation_id and run.conversation_id != conversation_id:
                            raise ValueError("Run does not belong to this conversation.")
                        cancelled = await gateway.cancel_run(run_id)
                        await send(ack(request_id, {"cancelled": cancelled}))
                    elif command == Command.PING:
                        await send(ack(request_id, {"pong": True}))
                    else:
                        await send(
                            error_message(
                                "unknown_command",
                                f"Unknown command: {command}",
                                request_id,
                            )
                        )
                except KeyError as exc:
                    await send(error_message("not_found", str(exc), request_id))
                except (ValueError, RuntimeError) as exc:
                    await send(error_message("invalid_request", str(exc), request_id))
                except Exception as exc:
                    log.exception("WebSocket command failed")
                    await send(error_message("internal_error", str(exc), request_id))
        except WebSocketDisconnect:
            log.info("Gateway WebSocket client disconnected")
        finally:
            for conversation_id in list(subscriptions):
                gateway.unsubscribe(conversation_id, subscriber_id)
            replaying.clear()
            deferred_events.clear()
            replay_cursors.clear()
            replay_locks.clear()

    return app
