import json
import os
import tempfile
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
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # File is corrupted (e.g. from interrupted write). Try to recover
            # by parsing up to the last valid JSON boundary.
            content = path.read_text(encoding="utf-8")
            decoder = json.JSONDecoder()
            try:
                data, _ = decoder.raw_decode(content)
                if isinstance(data, dict):
                    # Re-save the recovered data to fix the file
                    self._save_all(data)
                    return data
            except json.JSONDecodeError:
                pass
            # Last resort: try each JSON block and merge
            all_data: Dict[str, Any] = {}
            pos = 0
            while pos < len(content):
                while pos < len(content) and content[pos] in " \t\n\r":
                    pos += 1
                if pos >= len(content):
                    break
                try:
                    obj, end = decoder.raw_decode(content, pos)
                    if isinstance(obj, dict):
                        all_data.update(obj)
                    pos = end
                except json.JSONDecodeError:
                    break
            if all_data:
                self._save_all(all_data)
                return all_data
            # Completely unrecoverable — start fresh
            return {}

    def _save_all(self, data: Dict[str, Any]):
        """Write chats.json atomically via temp-file + rename.

        This prevents file corruption from interrupted writes (Ctrl+C, crash)
        because the target file is only replaced after the full write succeeds.
        """
        path = self._chat_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix="chats_"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
                tmp_f.write(
                    json.dumps(data, indent=2, ensure_ascii=False)
                )
                tmp_f.flush()
                os.fsync(tmp_f.fileno())
            # Atomic replace via os.replace() — works on both Windows and Unix
            # without requiring exclusive file access. Retry on
            # PermissionError (transient locks from antivirus / indexer / OneDrive).
            for attempt in range(5):
                try:
                    os.replace(tmp_path, str(path))
                    break
                except PermissionError:
                    if attempt < 4:
                        time.sleep(0.1 * (attempt + 1))  # 100ms, 200ms, 300ms, 400ms, 500ms
                    else:
                        # Fallback: write directly to target file (not atomic, but
                        # better than crashing — temp file with full data is still
                        # available for manual recovery).
                        with open(tmp_path, "r", encoding="utf-8") as src:
                            content = src.read()
                        with open(str(path), "w", encoding="utf-8") as dst:
                            dst.write(content)
                            dst.flush()
                            os.fsync(dst.fileno())
        except BaseException:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

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
