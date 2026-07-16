"""Wire-level names and helpers for the versioned gateway protocol."""

PROTOCOL_VERSION = 1


class Command:
    HELLO = "gateway.hello"
    LIST_CONVERSATIONS = "conversation.list"
    CREATE_CONVERSATION = "conversation.create"
    LOAD_CONVERSATION = "conversation.load"
    UNLOAD_CONVERSATION = "conversation.unload"
    SUBSCRIBE = "conversation.subscribe"
    UNSUBSCRIBE = "conversation.unsubscribe"
    GET_MESSAGES = "conversation.messages"
    SEND_MESSAGE = "message.send"
    COMPACT = "conversation.compact"
    CONTINUE = "conversation.continue"
    INTERRUPT = "run.interrupt"
    CANCEL = "run.cancel"
    PING = "ping"


def event_message(event: dict) -> dict:
    """Wrap a repository event in the public WebSocket envelope."""
    return {"type": "event", **event}


def ack(request_id: str | None, data: dict | None = None) -> dict:
    return {
        "type": "ack",
        "request_id": request_id,
        "ok": True,
        "data": data or {},
    }


def error_message(code: str, message: str, request_id: str | None = None, details: dict | None = None) -> dict:
    return {
        "type": "error",
        "request_id": request_id,
        "code": code,
        "message": message,
        "details": details or {},
    }
