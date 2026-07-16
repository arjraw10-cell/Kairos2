import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import List, Dict, Any, Optional


# Direct callers historically used the source tree's chats/ directory.  The
# workspace-aware CLI now stores sessions in ~/.kairos, while still reading
# that old location as a compatibility fallback.
LEGACY_CHATS_DIR = Path(os.path.dirname(os.path.dirname(__file__))).parent / "chats"
# Backwards-compatible alias for code that imported the old constant.
CHATS_DIR = LEGACY_CHATS_DIR


class SessionManager:
    """Manage chat sessions isolated by their absolute workspace path.

    New sessions are stored at::

        ~/.kairos/chats/<directory>--<stable-path-id>/chats.json

    The stable ID is derived from the normalized absolute workspace path, so
    two workspaces with the same final directory name cannot collide.  The
    JSON file also stores the absolute workspace path in its ``workspace``
    field as human-readable verification metadata.

    ``<workspace>/chats/chats.json`` remains a read-compatible legacy source.
    Legacy sessions are merged with the new store (new-store IDs win) and are
    written to the new store on the next save; the legacy file is never
    deleted or modified by this class.
    """

    _PATH_ID_LENGTH = 12
    _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")

    def __init__(self, workspace: str | os.PathLike[str] | None = None):
        # A missing workspace means the caller's current directory.  This
        # makes the new ~/.kairos location the default even for direct users
        # of SessionManager, while preserving the explicit workspace API.
        self._workspace = Path(workspace or os.getcwd()).expanduser().resolve()
        self._workspace_path = str(self._workspace)
        self._workspace_id = self._stable_workspace_id(self._workspace_path)
        self._workspace_label = self._display_workspace_name(self._workspace)

        self._chats_dir = (
            Path.home() / ".kairos" / "chats" /
            f"{self._workspace_label}--{self._workspace_id}"
        )
        self._legacy_chats_dir = self._workspace / "chats"
        self._chats_dir.mkdir(parents=True, exist_ok=True)
        self._current_session_id: Optional[str] = None

    @classmethod
    def _stable_workspace_id(cls, workspace_path: str) -> str:
        """Return a deterministic short ID for a normalized absolute path."""
        normalized = os.path.normcase(os.path.normpath(workspace_path))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:cls._PATH_ID_LENGTH]

    @classmethod
    def _display_workspace_name(cls, workspace: Path) -> str:
        """Make the workspace basename safe and readable as a folder name."""
        name = workspace.name or workspace.anchor.replace("\\", "").replace("/", "")
        name = cls._SAFE_NAME_RE.sub("_", name).strip(" .")
        return name or "workspace"

    def _chat_file(self) -> Path:
        """Return the new canonical ~/.kairos chat file."""
        return self._chats_dir / "chats.json"

    def _legacy_chat_file(self) -> Path:
        """Return the old workspace-local chat file."""
        return self._legacy_chats_dir / "chats.json"

    @staticmethod
    def _sessions_from_json(raw: Any) -> Dict[str, Any]:
        """Extract sessions from either the new envelope or old flat format."""
        if not isinstance(raw, dict):
            return {}
        sessions = raw.get("sessions") if isinstance(raw.get("sessions"), dict) else raw
        # The envelope's metadata must not accidentally become a session if a
        # malformed file has no sessions object.
        if sessions is raw:
            sessions = {key: value for key, value in raw.items() if key != "workspace"}
        return {
            str(session_id): session
            for session_id, session in sessions.items()
            if isinstance(session, dict) and isinstance(session.get("messages"), list)
        }

    def _read_store(self, path: Path, canonical: bool) -> Dict[str, Any]:
        """Read one store, recovering valid JSON where possible."""
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if canonical and isinstance(raw, dict) and raw.get("workspace"):
                stored_workspace = str(Path(raw["workspace"]).expanduser().resolve())
                if stored_workspace != self._workspace_path:
                    # Do not accidentally display another workspace's chats
                    # if a file was copied into this hashed directory.
                    return {}
            return self._sessions_from_json(raw)
        except json.JSONDecodeError:
            # File is corrupted (e.g. from an interrupted write). Try to
            # recover by parsing up to the first valid JSON boundary.
            content = path.read_text(encoding="utf-8")
            decoder = json.JSONDecoder()
            try:
                raw, _ = decoder.raw_decode(content)
                sessions = self._sessions_from_json(raw)
                if sessions:
                    self._repair_store(path, sessions, canonical)
                    return sessions
            except json.JSONDecodeError:
                pass

            # Last resort: try each adjacent JSON block and merge any session
            # dictionaries that can be parsed.
            recovered: Dict[str, Any] = {}
            pos = 0
            while pos < len(content):
                while pos < len(content) and content[pos] in " \t\n\r":
                    pos += 1
                if pos >= len(content):
                    break
                try:
                    raw, end = decoder.raw_decode(content, pos)
                    recovered.update(self._sessions_from_json(raw))
                    pos = end
                except json.JSONDecodeError:
                    break
            if recovered:
                self._repair_store(path, recovered, canonical)
                return recovered
            return {}

    def _repair_store(self, path: Path, sessions: Dict[str, Any], canonical: bool):
        """Repair a recoverable file without changing legacy file format."""
        if canonical:
            self._write_store(path, sessions)
        else:
            self._atomic_write(path, sessions)

    def _load_all(self) -> Dict[str, Any]:
        """Load this workspace's canonical sessions plus legacy fallbacks."""
        canonical = self._read_store(self._chat_file(), canonical=True)
        legacy = self._read_store(self._legacy_chat_file(), canonical=False)
        # Prefer canonical copies when a legacy session has already been
        # migrated, while retaining legacy-only sessions for resume.
        merged = dict(legacy)
        merged.update(canonical)
        return merged

    @staticmethod
    def _atomic_write(path: Path, data: Dict[str, Any]):
        """Atomically write JSON through a same-directory temporary file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix="chats_"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
                tmp_f.write(json.dumps(data, indent=2, ensure_ascii=False))
                tmp_f.flush()
                os.fsync(tmp_f.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp_path, str(path))
                    break
                except PermissionError:
                    if attempt < 4:
                        time.sleep(0.1 * (attempt + 1))
                    else:
                        # Keep the complete temp file available if a Windows
                        # antivirus/indexer prevents replacement.
                        with open(tmp_path, "r", encoding="utf-8") as src:
                            content = src.read()
                        with open(str(path), "w", encoding="utf-8") as dst:
                            dst.write(content)
                            dst.flush()
                            os.fsync(dst.fileno())
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _write_store(self, path: Path, sessions: Dict[str, Any]):
        """Write the canonical store with workspace verification metadata."""
        self._atomic_write(path, {
            "workspace": self._workspace_path,
            "sessions": sessions,
        })

    def _save_all(self, data: Dict[str, Any]):
        """Write sessions to the new canonical ~/.kairos location."""
        self._write_store(self._chat_file(), data)

    @staticmethod
    def _extract_preview(conversation_history: List[Dict[str, Any]]) -> str:
        """Find the first user message to create a preview."""
        for msg in conversation_history:
            if msg.get("role") == "user":
                content = msg.get("content", "")
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
        """Save or update the current session in the canonical home store."""
        if len(conversation_history) <= 1:
            return

        data = self._load_all()
        preview = self._extract_preview(conversation_history)
        session_id = None

        if self._current_session_id and self._current_session_id in data:
            session_id = self._current_session_id

        if not session_id:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            session_id = f"chat_{timestamp}"

        timestamp = data.get(session_id, {}).get(
            "timestamp", time.strftime("%Y-%m-%d %H:%M:%S")
        )
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
        """List only sessions belonging to this workspace."""
        data = self._load_all()
        sessions = []
        for sid in sorted(data.keys(), reverse=True):
            session = data[sid]
            sessions.append({
                "id": sid,
                "timestamp": session.get("timestamp", "unknown"),
                "preview": session.get("preview", ""),
            })
        return sessions

    def load_session(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        """Load a specific session from this workspace's stores."""
        data = self._load_all()
        session = data.get(session_id)
        if session:
            return session.get("messages")
        return None
