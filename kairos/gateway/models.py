"""Dependency-free data models used at the gateway boundary."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass
class Conversation:
    id: str
    workspace_id: str
    workspace_path: str
    title: str = ""
    preview: str = ""
    status: str = "idle"
    runtime_loaded: bool = False
    active_run_id: Optional[str] = None
    archived: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    message_count: int = 0
    last_event_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "workspace_path": self.workspace_path,
            "title": self.title,
            "preview": self.preview,
            "status": self.status,
            "runtime_loaded": self.runtime_loaded,
            "active_run_id": self.active_run_id,
            "archived": self.archived,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "last_event_id": self.last_event_id,
        }


@dataclass
class Run:
    id: str
    conversation_id: str
    status: str = "queued"
    source: str = "api"
    client_id: Optional[str] = None
    input_message_id: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    request_content: Optional[str] = None
    request_image_url: Optional[str] = None

    def to_dict(self, include_request: bool = False) -> dict[str, Any]:
        result = {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "status": self.status,
            "source": self.source,
            "client_id": self.client_id,
            "input_message_id": self.input_message_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }
        if include_request:
            result["request_content"] = self.request_content
            result["request_image_url"] = self.request_image_url
        return result


@dataclass
class StoredMessage:
    id: str
    conversation_id: str
    sequence: int
    role: str
    content: Any
    run_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    internal: bool = False
    created_at: str = field(default_factory=utc_now)

    def to_dict(self, include_internal: bool = False) -> dict[str, Any]:
        result = {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "sequence": self.sequence,
            "role": self.role,
            "content": self.content,
            "run_id": self.run_id,
            "created_at": self.created_at,
        }
        if include_internal:
            result.update({
                "tool_call_id": self.tool_call_id,
                "name": self.name,
                "internal": self.internal,
            })
        return result
