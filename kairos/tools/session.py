import json
import os
import time
from pathlib import Path
from typing import List, Dict, Any, Optional


CHATS_DIR = Path(os.path.dirname(os.path.dirname(__file__))).parent / "chats"


class SessionManager:
    """Manages saving and loading of chat sessions."""

    def __init__(self):
        CHATS_DIR.mkdir(exist_ok=True)
        self._current_session_id: Optional[str] = None

    def _chat_file(self) -> Path:
        return CHATS_DIR / "chats.json"

    def _load_all(self) -> Dict[str, Any]:
        path = self._chat_file()
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {}

    def _save_all(self, data: Dict[str, Any]):
        path = self._chat_file()
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _extract_preview(conversation_history: List[Dict[str, Any]]) -> str:
        """Find the first user message to create a preview."""
        for msg in conversation_history:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                # Handle vision content arrays (list of text + image blocks)
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
                    return " ".join(parts)[:20]
                elif isinstance(content, str):
                    return content[:20]
        return ""

    def save_chat(self, conversation_history: List[Dict[str, Any]]):
        """Save (or update) the current chat session.

        Strategy:
        1. If _current_session_id is set and still on disk → update it.
        2. Otherwise → create a brand-new entry.
        """
        if len(conversation_history) <= 1:
            return  # Don't save empty sessions (only system prompt)

        data = self._load_all()
        preview = self._extract_preview(conversation_history)
        session_id = None

        # 1. Try the tracked in-memory ID first
        if self._current_session_id and self._current_session_id in data:
            session_id = self._current_session_id

        # 2. Create brand-new entry if no tracked session
        if not session_id:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            session_id = f"chat_{timestamp}"

        # Preserve the original timestamp for existing sessions
        timestamp = data.get(session_id, {}).get("timestamp", time.strftime("%Y-%m-%d %H:%M:%S"))

        # Track for next call
        self._current_session_id = session_id

        data[session_id] = {
            "timestamp": timestamp,
            "preview": preview,
            "messages": conversation_history,
        }

        self._save_all(data)

    def new_session(self):
        """Reset the current session so the next save creates a new entry."""
        self._current_session_id = None

    def set_current_session(self, session_id: str):
        """Set the current session ID (used when resuming an existing session)."""
        self._current_session_id = session_id

    def list_sessions(self) -> List[Dict[str, str]]:
        """List all sessions with their IDs, timestamps, and previews."""
        data = self._load_all()
        sessions = []
        for sid in sorted(data.keys(), reverse=True):
            session = data[sid]
            sessions.append(
                {
                    "id": sid,
                    "timestamp": session.get("timestamp", "unknown"),
                    "preview": session.get("preview", ""),
                }
            )
        return sessions

    def load_session(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        """Load a specific session by its ID."""
        data = self._load_all()
        session = data.get(session_id)
        if session:
            return session.get("messages")
        return None
