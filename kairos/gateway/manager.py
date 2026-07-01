"""GatewayManager — owns conversations, routes messages to Agents.

The gateway itself holds zero workspace state. Every ManagedSession
carries its own workspace and Agent instance.
"""
import asyncio
import logging
import time
from pathlib import Path
from typing import Dict, Optional, Callable, Any

from ..agent import Agent
from ..tools.session import SessionManager

log = logging.getLogger("kairos.gateway")


class ManagedSession:
    """One conversation: one workspace, one Agent, one state."""

    def __init__(self, session_id: str, workspace: str):
        self.session_id = session_id
        self.workspace = workspace
        self.agent = Agent(workspace)
        self.is_running = False
        self.last_activity = time.time()


class GatewayManager:
    """Routes messages to conversation-specific Agents.

    The manager owns no workspace itself. Each conversation carries
    its own workspace and creates its own Agent instance.
    """

    def __init__(self, default_workspace: str):
        self.default_workspace = str(Path(default_workspace).resolve())
        self._persistence = SessionManager()
        self._sessions: Dict[str, ManagedSession] = {}
        self._lock = asyncio.Lock()

    def _resolve_workspace(self, workspace: Optional[str]) -> str:
        """Return given workspace, or fall back to default."""
        if workspace and Path(workspace).is_dir():
            return str(Path(workspace).resolve())
        return self.default_workspace

    # ── Session lifecycle ──────────────────────────────────────────

    async def create_session(self, workspace: str = None) -> ManagedSession:
        """Create a brand-new conversation in the given workspace."""
        resolved = self._resolve_workspace(workspace)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        session_id = f"chat_{timestamp}"
        session = ManagedSession(session_id, resolved)
        async with self._lock:
            self._sessions[session_id] = session
        log.info(f"Created session {session_id} in {resolved}")
        return session

    async def load_session(self, session_id: str) -> ManagedSession:
        """Pull full history from disk, create Agent, restore state."""
        # Check if already loaded in memory
        async with self._lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                session.last_activity = time.time()
                return session

        # Load from disk
        history = self._persistence.load_session(session_id)
        if history is None:
            raise ValueError(f"Session not found: {session_id}")

        workspace = self._persistence.get_workspace(session_id)
        resolved = self._resolve_workspace(workspace)

        session = ManagedSession(session_id, resolved)
        # Repair any broken tool chains from interrupted execution
        sanitized, last_response = Agent._sanitize_history_for_resume(history)
        session.agent.conversation_history = sanitized
        try:
            session.agent.tokens.start_turn(history)
            session.agent.tokens.finish_turn()
        except Exception:
            pass  # Token counting is best-effort on load

        async with self._lock:
            self._sessions[session_id] = session
        log.info(f"Loaded session {session_id} from disk (workspace: {resolved})")
        return session

    async def unload_session(self, session_id: str):
        """Save + destroy Agent + release memory."""
        async with self._lock:
            session = self._sessions.pop(session_id, None)

        if session is None:
            return

        # Save to disk
        try:
            self._persistence.save_chat(
                session.agent.get_history(),
                workspace=session.workspace,
                session_id=session.session_id,
            )
        except Exception as e:
            log.error(f"Failed to save session {session_id}: {e}")

        # Close browser if open
        try:
            if session.agent.browser_manager.is_open:
                session.agent.browser_manager.close()
        except Exception:
            pass

        # Close background terminals
        try:
            for tid in list(session.agent.terminal_manager._terminals.keys()):
                session.agent.terminal_manager.close_terminal(tid)
        except Exception:
            pass

        session.agent = None  # release reference
        log.info(f"Unloaded session {session_id}")

    # ── Message handling ───────────────────────────────────────────

    async def send_message(
        self,
        session_id: str,
        content: str,
        image_url: str = None,
        callbacks: Dict[str, Callable] = None,
    ):
        """Run agent.run() in a thread, piping events to callbacks."""
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Session not loaded: {session_id}")
        if session.is_running:
            raise ValueError("Session is already processing a message")

        session.is_running = True
        cb = callbacks or {}
        try:
            # Wire agent callbacks
            session.agent.on_stream_token = cb.get("on_token")
            session.agent.on_stream_start = cb.get("on_stream_start")
            session.agent.on_stream_end = cb.get("on_stream_end")
            session.agent.on_tool_call = cb.get("on_tool_call")
            session.agent.on_token_update = cb.get("on_token_update")
            session.agent.on_compact = cb.get("on_compact")

            # Wire sub-agent callbacks
            session.agent.subagent_tool._tool_printer = cb.get("on_subagent_tool")
            session.agent.subagent_tool._stream_start = cb.get("on_subagent_stream_start")
            session.agent.subagent_tool._stream_token = cb.get("on_subagent_stream_token")
            session.agent.subagent_tool._stream_end = cb.get("on_subagent_stream_end")

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: session.agent.run(content, image_url=image_url)
            )

            if cb.get("on_done"):
                cb["on_done"](response)

            # Auto-save after every exchange
            self._persistence.save_chat(
                session.agent.get_history(),
                workspace=session.workspace,
                session_id=session.session_id,
            )

        except Exception as e:
            log.error(f"Error in session {session_id}: {e}")
            if cb.get("on_error"):
                cb["on_error"](str(e))
        finally:
            session.is_running = False
            session.last_activity = time.time()

    # ── Commands ───────────────────────────────────────────────────

    async def compact(self, session_id: str) -> str:
        """Compact a session's conversation history."""
        session = self._sessions.get(session_id)
        if not session:
            return "No session loaded"
        result = session.agent.compact()
        self._persistence.save_chat(
            session.agent.get_history(),
            workspace=session.workspace,
            session_id=session.session_id,
        )
        return result

    async def interrupt(self, session_id: str):
        """Hard interrupt (Ctrl+C equivalent)."""
        session = self._sessions.get(session_id)
        if session and session.is_running:
            session.agent.interrupt()

    async def stop(self, session_id: str):
        """Graceful stop (Escape equivalent — finishes current step)."""
        session = self._sessions.get(session_id)
        if session:
            session.agent.request_stop()

    # ── Queries ────────────────────────────────────────────────────

    def list_sessions(self):
        """List all persisted sessions from disk."""
        return self._persistence.list_sessions()

    def get_session(self, session_id: str) -> Optional[ManagedSession]:
        """Get a currently-loaded session, or None."""
        return self._sessions.get(session_id)

    # ── Cleanup ────────────────────────────────────────────────────

    async def cleanup_idle(self, max_idle_seconds: int = 1800):
        """Background task: unload sessions idle for more than max_idle_seconds."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            to_unload = []
            async with self._lock:
                for sid, session in self._sessions.items():
                    if (
                        not session.is_running
                        and now - session.last_activity > max_idle_seconds
                    ):
                        to_unload.append(sid)
            for sid in to_unload:
                log.info(f"Auto-unloading idle session {sid}")
                await self.unload_session(sid)
