"""SQLite persistence for gateway-owned conversations."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

from .models import Conversation, Run, StoredMessage, new_id, utc_now


class GatewayRepository:
    """Thread-safe repository using one short-lived SQLite connection per call."""

    SCHEMA_VERSION = 2

    def __init__(self, data_dir: str | os.PathLike[str] | None = None):
        if data_dir is None:
            data_dir = os.environ.get("KAIROS_DATA_DIR", "").strip() or None
        if data_dir is None:
            data_dir = Path.home() / ".kairos"
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "kairos.sqlite3"
        self._init_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize(self) -> None:
        with self._init_lock, closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    workspace_path TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    preview TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'idle',
                    active_run_id TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    last_event_id INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_conversations_workspace ON conversations(workspace_id);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'interrupted', 'failed', 'cancelled')),
                    source TEXT NOT NULL DEFAULT 'api',
                    client_id TEXT,
                    input_message_id TEXT,
                    request_content TEXT,
                    request_image_url TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_conversation ON runs(conversation_id);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
                    sequence INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    tool_call_id TEXT,
                    name TEXT,
                    internal INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(conversation_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, sequence);
                CREATE TABLE IF NOT EXISTS events (
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_events_created ON events(conversation_id, event_id);
                """
            )
            # Development databases may have been created before these columns
            # existed.  SQLite has no IF NOT EXISTS for ADD COLUMN, so inspect
            # the table before applying each idempotent migration.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)")}
            if "active_run_id" not in columns:
                conn.execute("ALTER TABLE conversations ADD COLUMN active_run_id TEXT")
            run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            for name, definition in (
                ("request_content", "TEXT"),
                ("request_image_url", "TEXT"),
            ):
                if name not in run_columns:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                (str(self.SCHEMA_VERSION),),
            )

    @staticmethod
    def _workspace_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _conversation_from_row(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=row["id"], workspace_id=row["workspace_id"], workspace_path=row["workspace_path"],
            title=row["title"], preview=row["preview"], status=row["status"],
            active_run_id=row["active_run_id"], archived=bool(row["archived"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            message_count=row["message_count"], last_event_id=row["last_event_id"],
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> Run:
        return Run(
            id=row["id"], conversation_id=row["conversation_id"], status=row["status"],
            source=row["source"], client_id=row["client_id"], input_message_id=row["input_message_id"],
            started_at=row["started_at"], completed_at=row["completed_at"], error=row["error"],
            request_content=row["request_content"], request_image_url=row["request_image_url"],
        )

    def upsert_workspace(self, path: str, display_name: str = "") -> dict[str, Any]:
        resolved = str(Path(path).expanduser().resolve())
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO workspaces(id, path, display_name, created_at, updated_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET display_name = CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE workspaces.display_name END, updated_at = excluded.updated_at, last_used_at = excluded.last_used_at",
                (new_id("ws"), resolved, display_name, now, now, now),
            )
            row = conn.execute("SELECT * FROM workspaces WHERE path = ?", (resolved,)).fetchone()
        return self._workspace_from_row(row)

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        return self._workspace_from_row(row) if row else None

    def list_workspaces(self, search: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM workspaces"
        params: list[Any] = []
        if search:
            query += " WHERE path LIKE ? OR display_name LIKE ?"
            term = f"%{search}%"
            params.extend([term, term])
        query += " ORDER BY last_used_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with closing(self._connect()) as conn:
            return [self._workspace_from_row(row) for row in conn.execute(query, params)]

    def create_conversation(self, workspace_path: str, title: str = "") -> Conversation:
        workspace = self.upsert_workspace(workspace_path)
        now = utc_now()
        conversation = Conversation(
            id=new_id("conv"), workspace_id=workspace["id"], workspace_path=workspace["path"],
            title=title, created_at=now, updated_at=now,
        )
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO conversations(id, workspace_id, workspace_path, title, preview, status, active_run_id, archived, created_at, updated_at, message_count, last_event_id) VALUES (?, ?, ?, ?, '', 'idle', NULL, 0, ?, ?, 0, 0)",
                (conversation.id, workspace["id"], conversation.workspace_path, title, now, now),
            )
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return self._conversation_from_row(row) if row else None

    def list_conversations(self, workspace_id: str | None = None, search: str | None = None, archived: bool = False, limit: int = 100, offset: int = 0) -> list[Conversation]:
        query = "SELECT * FROM conversations WHERE archived = ?"
        params: list[Any] = [int(archived)]
        if workspace_id:
            query += " AND workspace_id = ?"
            params.append(workspace_id)
        if search:
            query += " AND (title LIKE ? OR preview LIKE ? OR workspace_path LIKE ?)"
            term = f"%{search}%"
            params.extend([term, term, term])
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        with closing(self._connect()) as conn:
            return [self._conversation_from_row(row) for row in conn.execute(query, params)]

    def update_conversation(self, conversation_id: str, **changes: Any) -> Conversation | None:
        allowed = {"title", "archived", "status", "preview", "active_run_id", "last_event_id"}
        fields = [(key, value) for key, value in changes.items() if key in allowed]
        if not fields:
            return self.get_conversation(conversation_id)
        assignments = ", ".join(f"{key} = ?" for key, _ in fields) + ", updated_at = ?"
        values = [int(value) if key == "archived" else value for key, value in fields] + [utc_now(), conversation_id]
        with closing(self._connect()) as conn:
            conn.execute(f"UPDATE conversations SET {assignments} WHERE id = ?", values)
        return self.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id: str) -> bool:
        # Explicit child cleanup keeps deletion compatible with databases
        # created by earlier schema versions that did not have CASCADE FKs.
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                exists = conn.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
                ).fetchone()
                if not exists:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute("DELETE FROM events WHERE conversation_id = ?", (conversation_id,))
                conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
                conn.execute("DELETE FROM runs WHERE conversation_id = ?", (conversation_id,))
                conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
                conn.execute("COMMIT")
                return True
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _is_screenshot_injection(message: dict[str, Any]) -> bool:
        content = message.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            return isinstance(first, dict) and first.get("type") == "text" and str(first.get("text", "")).startswith("[Screenshot captured")
        return False

    @classmethod
    def _message_internal(cls, message: dict[str, Any]) -> bool:
        return message.get("role") == "system" or message.get("role") == "tool" or bool(message.get("tool_calls")) or cls._is_screenshot_injection(message)

    def replace_messages(self, conversation_id: str, history: list[dict[str, Any]], run_id: str | None = None) -> None:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
                for sequence, message in enumerate(history):
                    conn.execute(
                        "INSERT INTO messages(id, conversation_id, run_id, sequence, role, content_json, tool_call_id, name, internal, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (new_id("msg"), conversation_id, run_id, sequence, message.get("role", "unknown"), json.dumps(message, ensure_ascii=False), message.get("tool_call_id"), message.get("name"), int(self._message_internal(message)), now),
                    )
                preview = self._preview(history)
                title = self._title(history)
                conn.execute(
                    "UPDATE conversations SET preview = ?, title = CASE WHEN title = '' THEN ? ELSE title END, message_count = ?, updated_at = ? WHERE id = ?",
                    (preview, title, len(history), now, conversation_id),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _text_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
        return ""

    @classmethod
    def _preview(cls, history: list[dict[str, Any]]) -> str:
        for message in history:
            if message.get("role") == "user" and not cls._is_screenshot_injection(message):
                return cls._text_content(message.get("content", "")).strip()[:120]
        return ""

    @classmethod
    def _title(cls, history: list[dict[str, Any]]) -> str:
        preview = cls._preview(history)
        return preview.splitlines()[0][:80] if preview else ""

    @staticmethod
    def _decode_message(row: sqlite3.Row) -> dict[str, Any]:
        decoded = json.loads(row["content_json"])
        if isinstance(decoded, dict) and "role" in decoded:
            return decoded
        return {"role": row["role"], "content": decoded, **({"tool_call_id": row["tool_call_id"]} if row["tool_call_id"] else {}), **({"name": row["name"]} if row["name"] else {})}

    def load_history(self, conversation_id: str) -> list[dict[str, Any]] | None:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY sequence", (conversation_id,)).fetchall()
        if not rows:
            return []
        return [self._decode_message(row) for row in rows]

    def list_messages(self, conversation_id: str, include_internal: bool = False, limit: int = 100, offset: int = 0) -> list[StoredMessage]:
        query = "SELECT * FROM messages WHERE conversation_id = ?"
        params: list[Any] = [conversation_id]
        if not include_internal:
            query += " AND internal = 0"
        query += " ORDER BY sequence LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        result: list[StoredMessage] = []
        for row in rows:
            message = self._decode_message(row)
            result.append(StoredMessage(id=row["id"], conversation_id=row["conversation_id"], sequence=row["sequence"], role=message.get("role", row["role"]), content=message.get("content", ""), run_id=row["run_id"], tool_call_id=message.get("tool_call_id", row["tool_call_id"]), name=message.get("name", row["name"]), internal=bool(row["internal"]), created_at=row["created_at"]))
        return result

    def create_run(self, conversation_id: str, content: str, image_url: str | None = None, source: str = "api", client_id: str | None = None) -> Run:
        run = Run(id=new_id("run"), conversation_id=conversation_id, source=source, client_id=client_id, request_content=content, request_image_url=image_url)
        with closing(self._connect()) as conn:
            conn.execute("INSERT INTO runs(id, conversation_id, status, source, client_id, request_content, request_image_url) VALUES (?, ?, 'queued', ?, ?, ?, ?)", (run.id, run.conversation_id, run.source, run.client_id, content, image_url))
        return run

    def get_run(self, run_id: str) -> Run | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_from_row(row) if row else None

    def update_run(self, run_id: str, status: str, error: str | None = None, expected_status: str | None = None) -> Run | None:
        allowed_statuses = {"queued", "running", "completed", "interrupted", "failed", "cancelled"}
        if status not in allowed_statuses:
            raise ValueError(f"Invalid run status: {status}")
        now = utc_now()
        started = now if status == "running" else None
        completed = now if status in {"completed", "interrupted", "failed", "cancelled"} else None
        query = "UPDATE runs SET status = ?, error = ?, started_at = COALESCE(started_at, ?), completed_at = COALESCE(?, completed_at) WHERE id = ?"
        params: list[Any] = [status, error, started, completed, run_id]
        if expected_status:
            query += " AND status = ?"
            params.append(expected_status)
        with closing(self._connect()) as conn:
            cur = conn.execute(query, params)
        return self.get_run(run_id) if cur.rowcount else None

    def list_running_runs(self) -> list[Run]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM runs WHERE status IN ('queued', 'running')").fetchall()
        return [self._run_from_row(row) for row in rows]

    def append_event(self, conversation_id: str, event_type: str, payload: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT last_event_id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
                if not row:
                    raise KeyError(conversation_id)
                event_id = int(row["last_event_id"]) + 1
                conn.execute("INSERT INTO events(conversation_id, event_id, event_type, run_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)", (conversation_id, event_id, event_type, run_id, json.dumps(payload, ensure_ascii=False), now))
                conn.execute("UPDATE conversations SET last_event_id = ?, updated_at = ? WHERE id = ?", (event_id, now, conversation_id))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return {"event_id": event_id, "event": event_type, "conversation_id": conversation_id, "run_id": run_id, "created_at": now, "data": payload}

    def list_events(self, conversation_id: str, after_event_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM events WHERE conversation_id = ? AND event_id > ? ORDER BY event_id LIMIT ?", (conversation_id, max(0, int(after_event_id)), max(1, min(int(limit), 2000)))).fetchall()
        return [{"event_id": row["event_id"], "event": row["event_type"], "conversation_id": row["conversation_id"], "run_id": row["run_id"], "created_at": row["created_at"], "data": json.loads(row["payload_json"])} for row in rows]

    def mark_stale_runs_interrupted(self) -> list[str]:
        runs = self.list_running_runs()
        interrupted: list[str] = []
        for run in runs:
            updated = self.update_run(
                run.id,
                "interrupted",
                "Gateway restarted before the run completed.",
                expected_status=run.status,
            )
            if updated:
                self.update_conversation(run.conversation_id, status="interrupted", active_run_id=None)
                interrupted.append(run.id)
        return interrupted

    def import_legacy_chats(self, chat_file: str | os.PathLike[str], workspace_path: str) -> dict[str, Any]:
        """Import legacy ``chats.json`` sessions once, preserving message JSON.

        The migration is explicit and idempotent by recording the absolute
        source path in ``schema_meta``. It does not delete or modify the source
        file. Imported conversations use the supplied workspace context.
        """
        source = Path(chat_file).expanduser().resolve()
        if not source.is_file():
            return {"imported": 0, "skipped": 0, "source": str(source), "reason": "file not found"}
        key = f"legacy_import:{source}"
        with closing(self._connect()) as conn:
            if conn.execute("SELECT 1 FROM schema_meta WHERE key = ?", (key,)).fetchone():
                return {"imported": 0, "skipped": 0, "source": str(source), "reason": "already imported"}
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"imported": 0, "skipped": 0, "source": str(source), "reason": str(exc)}
        if not isinstance(raw, dict):
            return {"imported": 0, "skipped": 0, "source": str(source), "reason": "expected an object"}
        imported = 0
        skipped = 0
        for session in raw.values():
            if not isinstance(session, dict) or not isinstance(session.get("messages"), list):
                skipped += 1
                continue
            conversation = self.create_conversation(workspace_path, str(session.get("preview", ""))[:80])
            self.replace_messages(conversation.id, session["messages"])
            imported += 1
        with closing(self._connect()) as conn:
            conn.execute("INSERT INTO schema_meta(key, value) VALUES (?, ?)", (key, utc_now()))
        return {"imported": imported, "skipped": skipped, "source": str(source)}
