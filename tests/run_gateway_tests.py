"""Dependency-light gateway smoke tests.

Run with ``python tests/run_gateway_tests.py`` when pytest is not installed.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from kairos.gateway.history import sanitize_for_resume
from kairos.gateway.manager import GatewayManager
from kairos.gateway.repository import GatewayRepository
from kairos.gateway.server import create_app

# The gateway refuses non-loopback Host headers (TestClient defaults to
# "testserver") unless a token is configured.
BASE_URL = "http://127.0.0.1:8765"
WS_URL = "ws://127.0.0.1:8765/api/v1/ws"


class FakeAgent:
    created = 0

    def __init__(self, workspace: str):
        type(self).created += 1
        self.workspace = workspace
        self.conversation_history = [{"role": "system", "content": "system"}]
        self.browser_manager = type("Browser", (), {"is_open": False})()
        self.terminal_manager = type("Terminal", (), {"terminals": {}})()
        self.subagent_tool = None
        self.on_stream_start = self.on_stream_token = self.on_stream_end = None
        self.on_tool_call = self.on_tool_result = self.on_token_update = self.on_compact = None

    def get_history(self):
        return list(self.conversation_history)

    def run(self, content, image_url=None):
        self.conversation_history.extend([
            {"role": "user", "content": content},
            {"role": "assistant", "content": f"done: {content}"},
        ])
        return f"done: {content}"

    def interrupt(self):
        return None

    def compact(self):
        return "nothing to compact"


class MinimalFakeAgent:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.conversation_history = [{"role": "system", "content": "system"}]
        self.browser_manager = object()
        self.terminal_manager = object()
        self.subagent_tool = None

    def get_history(self):
        return list(self.conversation_history)

    def run(self, content, image_url=None):
        self.conversation_history.extend([
            {"role": "user", "content": content},
            {"role": "assistant", "content": "minimal"},
        ])
        return "minimal"

    def interrupt(self):
        return None


def terminal_test() -> None:
    from kairos.terminal_manager import TerminalManager

    manager = TerminalManager()
    terminal_id = manager.create_terminal(background=False)
    success, message = manager.execute_command(terminal_id, "echo ok")
    assert not success and "timeout is required" in message.lower()
    success, message = manager.execute_command(terminal_id, "echo ok", timeout=21)
    assert success and "ok" in message
    success, output = manager.execute_command(terminal_id, "echo ok", timeout=1)
    assert success and "ok" in output
    manager.close_terminal(terminal_id)


def repository_test() -> None:
    with tempfile.TemporaryDirectory() as directory:
        repo = GatewayRepository(directory)
        conversation = repo.create_conversation(str(Path(directory) / "project"))
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        repo.replace_messages(conversation.id, history)
        assert repo.load_history(conversation.id) == history
        assert repo.get_conversation(conversation.id).preview == "hello"
        assert [message.role for message in repo.list_messages(conversation.id)] == ["user", "assistant"]
        assert repo.append_event(conversation.id, "one", {})["event_id"] == 1
        assert repo.append_event(conversation.id, "two", {})["event_id"] == 2
        assert repo.delete_conversation(conversation.id) is True
        assert repo.get_conversation(conversation.id) is None
        conversation = repo.create_conversation(str(Path(directory) / "private"))
        run = repo.create_run(conversation.id, "secret prompt", "data:image/png;base64,abc")
        assert "request_content" not in run.to_dict()
        assert run.to_dict(include_request=True)["request_content"] == "secret prompt"


def history_test() -> None:
    user_only, needs_continue = sanitize_for_resume(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "last request"}],
        prefer_incomplete=True,
    )
    assert user_only[-1]["content"] == "last request"
    assert needs_continue is True

    dirty_history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_2", "function": {"name": "read", "arguments": "{}"}}]},
    ]
    clean, needs_continue = sanitize_for_resume(dirty_history)
    assert clean == dirty_history[:3]
    assert needs_continue is False

    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect files"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "function": {"name": "read", "arguments": "{}"}}],
        },
    ]
    sanitized, needs_continue = sanitize_for_resume(history, prefer_incomplete=True)
    assert needs_continue is True
    assert sanitized[-1]["tool_call_id"] == "call_1"


def manager_test() -> None:
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            manager = GatewayManager(
                GatewayRepository(Path(directory) / "data"),
                max_concurrent_runs=1,
                agent_factory=FakeAgent,
            )
            conversation = manager.create_conversation(directory)
            run = await manager.submit_message(conversation.id, "hello")
            for _ in range(100):
                if manager.repository.get_run(run.id).status == "completed":
                    break
                await asyncio.sleep(0.01)
            assert manager.repository.get_run(run.id).status == "completed"
            assert manager.repository.load_history(conversation.id)[-1]["content"] == "done: hello"
            await manager.shutdown()

    asyncio.run(scenario())


def minimal_agent_resource_test() -> None:
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            manager = GatewayManager(
                GatewayRepository(Path(directory) / "data"),
                max_concurrent_runs=1,
                agent_factory=MinimalFakeAgent,
            )
            conversation = manager.create_conversation(directory)
            await manager.load_runtime(conversation.id)
            await manager.unload_runtime(conversation.id)
            assert manager.get_runtime(conversation.id) is None
            await manager.shutdown()

    asyncio.run(scenario())


def unload_rejection_test() -> None:
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            manager = GatewayManager(
                GatewayRepository(Path(directory) / "data"),
                max_concurrent_runs=1,
                agent_factory=FakeAgent,
            )
            conversation = manager.create_conversation(directory)
            await manager.load_runtime(conversation.id)
            run = await manager.submit_message(conversation.id, "hello")
            try:
                await manager.unload_runtime(conversation.id)
            except RuntimeError:
                pass
            else:
                raise AssertionError("unload should reject an active or queued run")
            assert manager.get_runtime(conversation.id) is not None
            for _ in range(100):
                if manager.repository.get_run(run.id).status == "completed":
                    break
                await asyncio.sleep(0.01)
            await manager.shutdown()

    asyncio.run(scenario())


def concurrent_load_test() -> None:
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            FakeAgent.created = 0
            manager = GatewayManager(
                GatewayRepository(Path(directory) / "data"),
                max_concurrent_runs=1,
                agent_factory=FakeAgent,
            )
            conversation = manager.create_conversation(directory)
            runtimes = await asyncio.gather(
                *(manager.load_runtime(conversation.id) for _ in range(10))
            )
            assert len({id(runtime) for runtime in runtimes}) == 1
            assert FakeAgent.created == 1
            await manager.shutdown()

    asyncio.run(scenario())


def config_test() -> None:
    import os
    from kairos.config import Config

    previous = os.environ.get("KAIROS_GATEWAY_PORT")
    os.environ["KAIROS_GATEWAY_PORT"] = "65535"
    Config.reload()
    assert Config.KAIROS_GATEWAY_PORT() == 65535
    os.environ["KAIROS_GATEWAY_PORT"] = "0"
    Config.reload()
    try:
        Config.KAIROS_GATEWAY_PORT()
    except ValueError as exc:
        assert "between 1 and 65535" in str(exc)
    else:
        raise AssertionError("invalid gateway port should fail")
    if previous is None:
        os.environ.pop("KAIROS_GATEWAY_PORT", None)
    else:
        os.environ["KAIROS_GATEWAY_PORT"] = previous
    Config.reload()


def auth_test() -> None:
    import os
    from kairos.config import Config

    previous = os.environ.get("KAIROS_AUTH_TOKEN")
    os.environ["KAIROS_AUTH_TOKEN"] = "secret"
    Config.reload()
    with tempfile.TemporaryDirectory() as directory:
        manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1)
        app = create_app(manager)
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/readyz").status_code == 200
            assert client.get("/api/v1/capabilities").status_code == 401
            assert client.get("/healthz/anything").status_code == 401
            assert client.get("/api/v1/capabilities", headers={"Authorization": "Bearer secret"}).status_code == 200
    if previous is None:
        os.environ.pop("KAIROS_AUTH_TOKEN", None)
    else:
        os.environ["KAIROS_AUTH_TOKEN"] = previous
    Config.reload()


def api_test() -> None:
    import json
    from unittest import mock

    with tempfile.TemporaryDirectory() as directory, mock.patch(
        "kairos.providers.config.config_path", return_value=Path(directory) / "provider.json"
    ):
        # A real Agent, configured from a temporary provider.json.
        (Path(directory) / "provider.json").write_text(json.dumps(MODEL_CONFIG), encoding="utf-8")
        manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1)
        app = create_app(manager)
        with TestClient(app, base_url=BASE_URL) as client:
            response = client.post("/api/v1/conversations", json={"workspace": directory, "title": "Test"})
            assert response.status_code == 201, response.text
            conversation_id = response.json()["id"]
            assert client.get("/api/v1/conversations").status_code == 200
            loaded = client.post(f"/api/v1/conversations/{conversation_id}/runtime/load")
            assert loaded.status_code == 200, loaded.text
            assert loaded.json()["conversation"]["runtime_loaded"] is True
            unloaded = client.post(f"/api/v1/conversations/{conversation_id}/runtime/unload")
            assert unloaded.status_code == 200, unloaded.text


MODEL_CONFIG = {
    "default_setup": "luna",
    "providers": {
        "openai": {"format": "responses", "base_url": "https://api.openai.com/v1", "api_keys": ["sk-test"]},
        "openrouter": {"format": "completions", "base_url": "https://openrouter.ai/api/v1", "api_keys": ["or-test"]},
    },
    "setups": {
        "luna": {"provider": "openai", "model": "gpt-6-luna", "reasoning_efforts": ["low", "high"], "default_effort": "high"},
        "mimo": {"provider": "openrouter", "model": "xiaomi/mimo", "reasoning_efforts": ["on", "off"], "default_effort": "on"},
    },
}


class ModelFakeAgent(FakeAgent):
    """FakeAgent with the Agent model-selection API backed by providers."""

    def __init__(self, workspace: str):
        super().__init__(workspace)
        self.set_model()

    def set_model(self, setup=None, model=None, provider=None):
        from kairos import providers

        self.spec = providers.resolve(setup, model=model, provider=provider)
        self.effort = self.spec.default_effort
        return "ok"

    def set_effort(self, effort):
        from kairos import providers

        self.effort = providers.validate_effort(self.spec, effort)
        return "ok"

    def model_info(self):
        return self.spec.info(self.effort)


def model_config_test() -> None:
    import json
    from unittest import mock

    async def scenario(directory: str):
        manager = GatewayManager(
            GatewayRepository(Path(directory) / "data"),
            max_concurrent_runs=1,
            agent_factory=ModelFakeAgent,
        )
        assert manager.list_models()["default_setup"] == "luna"
        assert [s["name"] for s in manager.list_models()["setups"]] == ["luna", "mimo"]
        conversation = manager.create_conversation(directory)
        info = await manager.configure_conversation(conversation.id)
        assert (info["setup"], info["effort"]) == ("luna", "high")
        info = await manager.configure_conversation(conversation.id, setup="mimo", effort="off")
        assert (info["setup"], info["model"], info["effort"]) == ("mimo", "xiaomi/mimo", "off")
        try:
            await manager.configure_conversation(conversation.id, effort="high")
        except ValueError:
            pass
        else:
            raise AssertionError("an unsupported effort must be rejected")
        # Applied when the runtime loads later...
        runtime = await manager.load_runtime(conversation.id)
        assert runtime.agent.model_info()["effort"] == "off"
        # ...and immediately to a loaded runtime; persisted across reloads.
        info = await manager.configure_conversation(conversation.id, model="gpt-6-sol", provider="openai")
        assert (info["setup"], info["model"], info["effort"]) == (None, "gpt-6-sol", None)
        assert runtime.agent.model_info()["model"] == "gpt-6-sol"
        await manager.unload_runtime(conversation.id)
        runtime = await manager.load_runtime(conversation.id)
        assert runtime.agent.model_info()["model"] == "gpt-6-sol"
        try:
            await manager.configure_conversation("conv_missing")
        except KeyError:
            pass
        else:
            raise AssertionError("unknown conversation must raise KeyError")
        await manager.shutdown()

    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "provider.json"
        config.write_text(json.dumps(MODEL_CONFIG), encoding="utf-8")
        with mock.patch("kairos.providers.config.config_path", return_value=config):
            asyncio.run(scenario(directory))

            # WebSocket commands.
            manager = GatewayManager(
                GatewayRepository(Path(directory) / "ws-data"),
                max_concurrent_runs=1,
                agent_factory=ModelFakeAgent,
            )
            with TestClient(create_app(manager)) as client, client.websocket_connect(WS_URL) as ws:
                def command(name, conversation_id=None, **payload):
                    ws.send_json({"type": "command", "command": name, "request_id": name,
                                  "conversation_id": conversation_id, "payload": payload})
                    return ws.receive_json()

                listed = command("model.list")
                assert listed["ok"] and listed["data"]["default_setup"] == "luna"
                created = command("conversation.create", workspace=directory)["data"]["conversation"]
                reply = command("conversation.configure", created["id"], setup="mimo")
                assert reply["ok"] and reply["data"]["setup"] == "mimo", reply
                reply = command("conversation.configure", conversation_id=created["id"])  # id in payload
                assert reply["data"]["effort"] == "on", reply
                reply = command("conversation.configure", created["id"], effort="max")
                assert reply["type"] == "error" and reply["code"] == "invalid_request", reply
                reply = command("conversation.configure", "conv_missing")
                assert reply["type"] == "error" and reply["code"] == "not_found", reply


def model_settings_migration_test() -> None:
    import sqlite3

    with tempfile.TemporaryDirectory() as directory:
        # A database created before the model_settings column existed.
        conn = sqlite3.connect(str(Path(directory) / "kairos.sqlite3"))
        conn.execute(
            "CREATE TABLE conversations (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, workspace_path TEXT NOT NULL, "
            "title TEXT NOT NULL DEFAULT '', preview TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'idle', "
            "archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "message_count INTEGER NOT NULL DEFAULT 0, last_event_id INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("INSERT INTO conversations(id, workspace_id, workspace_path, created_at, updated_at) VALUES ('conv_old', 'ws', ?, 'now', 'now')", (directory,))
        conn.commit()
        conn.close()
        repo = GatewayRepository(directory)
        assert repo.get_model_settings("conv_old") == {}
        repo.set_model_settings("conv_old", {"setup": "mimo", "effort": "off"})
        assert GatewayRepository(directory).get_model_settings("conv_old") == {"setup": "mimo", "effort": "off"}
        assert repo.get_conversation("conv_old").id == "conv_old"


def fresh_workspace_test() -> None:
    from kairos import workspaces

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "kairos_workspace"
        previous_root = workspaces.WORKSPACE_ROOT
        workspaces.WORKSPACE_ROOT = root
        try:
            manager = GatewayManager(
                GatewayRepository(Path(directory) / "data"),
                default_workspace=str(Path(directory) / "old-default"),
                max_concurrent_runs=1,
            )
            fresh = Path(manager.create_conversation().workspace_path)
            assert fresh.parent == root.resolve() and fresh.is_dir(), fresh
            assert Path(manager.create_conversation().workspace_path) != fresh
            explicit = manager.create_conversation(directory).workspace_path
            assert Path(explicit) == Path(directory).resolve(), explicit
            asyncio.run(manager.shutdown())  # releases the data-dir lock
        finally:
            workspaces.WORKSPACE_ROOT = previous_root


class BackgroundFakeAgent(FakeAgent):
    """FakeAgent with the Agent background-event API (see kairos/agent.py)."""

    instances: list = []

    def __init__(self, workspace: str):
        super().__init__(workspace)
        self.on_background_event = None
        self.on_subagent_status = None
        self.held: list = []
        self.busy = False
        self.runs: list = []
        self.subagent_tool = type("Subagents", (), {
            "mark_lost": lambda _self: self._lose(),
            "child_agents": lambda _self: [],
        })()
        type(self).instances.append(self)

    def _lose(self):
        if self.busy:
            self.busy = False
            self.on_subagent_status({"subagent_id": "s1", "state": "lost", "elapsed_s": 1.0, "prompt": "p", "recent_actions": []})
            self.on_background_event(bg_event("lost"))
        return 0

    def add_background_events(self, events):
        self.held.extend(events)

    def drain_background_events(self):
        events, self.held = self.held, []
        return events

    def has_background_work(self):
        return self.busy

    def subagent_statuses(self):
        return [{"subagent_id": "s1", "state": "running" if self.busy else "idle", "elapsed_s": 1.0, "prompt": "p", "recent_actions": []}]

    def stop_subagents(self):
        stopped, self.busy = int(self.busy), False
        return stopped

    def run(self, content, image_url=None):
        self.runs.append((content, [e["id"] for e in self.drain_background_events()]))
        return super().run(content, image_url)


def bg_event(name: str) -> dict:
    return {"id": name, "kind": "subagent_finished", "subagent_id": "s1", "text": f"result {name}", "created_at": 0.0}


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def background_manager(directory: str, **kwargs) -> GatewayManager:
    BackgroundFakeAgent.instances = []
    return GatewayManager(
        GatewayRepository(Path(directory) / "data"),
        max_concurrent_runs=1,
        agent_factory=BackgroundFakeAgent,
        wake_debounce=kwargs.pop("wake_debounce", 0.05),
        **kwargs,
    )


def wake_run_test() -> None:
    from kairos.wake import NOTIFICATION_PREFIX

    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            manager = background_manager(directory, wake_debounce=0.2)
            conversation = manager.create_conversation(directory)
            runtime = await manager.load_runtime(conversation.id)
            agent = runtime.agent
            # Two events inside one debounce window: one wake run with both.
            agent.on_background_event(bg_event("e1"))
            agent.on_background_event(bg_event("e2"))
            assert await wait_until(lambda: agent.runs and manager.get_runtime(conversation.id).is_running is False and len(agent.conversation_history) == 3)
            await asyncio.sleep(0.3)
            assert len(agent.runs) == 1, agent.runs
            content, _held = agent.runs[0]
            assert content.startswith(NOTIFICATION_PREFIX) and "result e1" in content and "result e2" in content
            events = manager.events_after(conversation.id)
            names = [e["event"] for e in events]
            assert names.count("background.notification") == 2
            note = next(e for e in events if e["event"] == "background.notification")["data"]
            assert note["conversation_id"] == conversation.id and note["notifications"][0]["id"] == "e1"
            started = next(e for e in events if e["event"] == "run.started")["data"]
            assert started["origin"] == "wake" and started["run"]["origin"] == "wake"
            completed = next(e for e in events if e["event"] == "run.completed")["data"]
            assert completed["origin"] == "wake"

            # A user message sent while the conversation is busy takes the
            # pending notification with it instead of a separate wake.
            runtime.is_running = True  # simulate an active run
            agent.on_background_event(bg_event("e3"))
            await asyncio.sleep(0.3)
            assert len(agent.runs) == 1
            runtime.is_running = False
            run = await manager.submit_message(conversation.id, "hi")
            assert await wait_until(lambda: manager.repository.get_run(run.id).status == "completed")
            assert agent.runs[-1] == ("hi", ["e3"]), agent.runs
            assert manager.repository.get_run(run.id).origin == "user"
            await asyncio.sleep(0.3)
            assert len(agent.runs) == 2  # e3 was not also delivered by a wake
            await manager.shutdown()

    asyncio.run(scenario())


def wake_cap_test() -> None:
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            manager = background_manager(directory, wake_cap=1)
            conversation = manager.create_conversation(directory)
            agent = (await manager.load_runtime(conversation.id)).agent
            agent.on_background_event(bg_event("e1"))
            assert await wait_until(lambda: len(agent.runs) == 1)
            assert await wait_until(lambda: not manager.get_runtime(conversation.id).is_running)
            agent.on_background_event(bg_event("e2"))
            assert await wait_until(lambda: any(e["event"] == "wake.paused" for e in manager.events_after(conversation.id)))
            paused = next(e for e in manager.events_after(conversation.id) if e["event"] == "wake.paused")["data"]
            assert paused == {"conversation_id": conversation.id, "pending": 1}, paused
            await asyncio.sleep(0.2)
            assert len(agent.runs) == 1
            run = await manager.submit_message(conversation.id, "back")
            assert await wait_until(lambda: manager.repository.get_run(run.id).status == "completed")
            assert agent.runs[-1] == ("back", ["e2"]), agent.runs
            # The user message reset the counter: waking works again.
            agent.on_background_event(bg_event("e3"))
            assert await wait_until(lambda: len(agent.runs) == 3)
            await manager.shutdown()

    asyncio.run(scenario())


def unload_protection_and_lost_test() -> None:
    from unittest import mock

    real_sleep = asyncio.sleep

    async def fast_sleep(seconds, *args, **kwargs):
        await real_sleep(0.01)

    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            manager = background_manager(directory)
            conversation = manager.create_conversation(directory)
            agent = (await manager.load_runtime(conversation.id)).agent
            agent.busy = True
            manager.get_runtime(conversation.id).last_activity = 0
            with mock.patch("kairos.gateway.manager.asyncio.sleep", fast_sleep):
                task = asyncio.create_task(manager.cleanup_idle(max_idle_seconds=1))
                await real_sleep(0.2)
                assert manager.get_runtime(conversation.id) is not None  # busy: kept
                agent.busy = False
                assert await wait_until(lambda: manager.get_runtime(conversation.id) is None)
                task.cancel()

            # Shutdown with a running sub-agent: lost status + saved notification.
            agent = (await manager.load_runtime(conversation.id)).agent
            agent.busy = True
            await manager.shutdown()
            names = [e["event"] for e in manager.events_after(conversation.id)]
            assert "subagent.status" in names and "background.notification" in names
            status = next(e for e in manager.events_after(conversation.id) if e["event"] == "subagent.status")["data"]
            assert status["state"] == "lost" and status["conversation_id"] == conversation.id
            restarted = background_manager(directory)
            agent = (await restarted.load_runtime(conversation.id)).agent
            assert [e["id"] for e in agent.held] == ["lost"], agent.held
            await restarted.shutdown()

    asyncio.run(scenario())


def subagent_commands_test() -> None:
    with tempfile.TemporaryDirectory() as directory:
        manager = background_manager(directory)
        with TestClient(create_app(manager)) as client, client.websocket_connect(WS_URL) as ws:
            def command(name, conversation_id=None, **payload):
                ws.send_json({"type": "command", "command": name, "request_id": name,
                              "conversation_id": conversation_id, "payload": payload})
                return ws.receive_json()

            created = command("conversation.create", workspace=directory)["data"]["conversation"]
            reply = command("subagent.list", created["id"])
            assert reply["ok"] and reply["data"] == {"subagents": []}, reply  # not loaded
            command("conversation.load", created["id"])
            BackgroundFakeAgent.instances[-1].busy = True
            reply = command("subagent.list", created["id"])
            assert reply["data"]["subagents"][0]["state"] == "running", reply
            reply = command("subagent.stop_all", created["id"])
            assert reply["ok"] and reply["data"] == {"stopped": 1}, reply
            reply = command("subagent.stop_all", created["id"])
            assert reply["data"] == {"stopped": 0}, reply
            reply = command("subagent.list", "conv_missing")
            assert reply["type"] == "error" and reply["code"] == "not_found", reply


def _set_env(**values) -> dict:
    """Set (or, with None, remove) environment variables; return the old values."""
    import os
    from kairos.config import Config

    previous = {name: os.environ.get(name) for name in values}
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    Config.reload()
    return previous


def _ws_rejected(client, url: str, headers: dict | None = None) -> bool:
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect(url, headers=headers or {}):
            return False
    except WebSocketDisconnect:
        return True


def cross_site_test() -> None:
    """Fix 1: a web page must not reach the gateway through the browser."""
    import hmac
    from unittest import mock

    evil = {"origin": "https://evil.example"}
    previous = _set_env(KAIROS_AUTH_TOKEN=None)
    try:
        with tempfile.TemporaryDirectory() as directory:
            manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1, agent_factory=FakeAgent)
            with TestClient(create_app(manager), base_url=BASE_URL) as client:
                # WebSocket: a cross-site Origin is refused; loopback or no Origin (CLI) is fine.
                assert _ws_rejected(client, WS_URL, evil)
                assert not _ws_rejected(client, WS_URL, {"origin": "http://localhost:5173"})
                assert not _ws_rejected(client, WS_URL)
                # DNS rebinding: a non-loopback Host is refused without a token.
                assert _ws_rejected(client, "ws://evil.example:8765/api/v1/ws")
                assert client.get("/api/v1/capabilities").status_code == 200
                assert client.get("/api/v1/capabilities", headers=evil).status_code == 403
                assert client.get("http://evil.example:8765/api/v1/capabilities").status_code == 403
                assert client.get("/healthz", headers={"host": "[::1]:8765"}).status_code == 200
                assert client.get("http://localhost/healthz").status_code == 200

        _set_env(KAIROS_AUTH_TOKEN="secret")
        with tempfile.TemporaryDirectory() as directory:
            manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1, agent_factory=FakeAgent)
            with TestClient(create_app(manager), base_url="http://kairos.lan:8765") as client:
                # With a token the token is the gate: any Host, constant-time compare.
                with mock.patch("hmac.compare_digest", wraps=hmac.compare_digest) as compare:
                    assert client.get("/api/v1/capabilities", headers={"Authorization": "Bearer secret"}).status_code == 200
                    assert compare.called
                assert client.get("/api/v1/capabilities", headers={"Authorization": "Bearer wrong"}).status_code == 401
                assert not _ws_rejected(client, "ws://kairos.lan:8765/api/v1/ws?token=secret")
                assert _ws_rejected(client, "ws://kairos.lan:8765/api/v1/ws?token=secret", evil)
    finally:
        _set_env(**previous)


def bind_refusal_test() -> None:
    """Fix 1: no non-loopback bind without KAIROS_AUTH_TOKEN."""
    from unittest import mock
    from kairos import gateway_main
    from kairos.config import Config

    previous = _set_env(KAIROS_GATEWAY_HOST="0.0.0.0", KAIROS_AUTH_TOKEN=None)
    try:
        with mock.patch.object(Config, "validate"), mock.patch.object(sys, "argv", ["gateway_main"]), \
                mock.patch.object(gateway_main, "GatewayManager") as manager_cls, \
                mock.patch.object(gateway_main, "create_app"), \
                mock.patch.object(gateway_main.uvicorn, "run") as run:
            try:
                gateway_main.main()
            except SystemExit as exc:
                assert exc.code == 1
            else:
                raise AssertionError("a non-loopback bind without a token must be refused")
            assert not manager_cls.called and not run.called
            _set_env(KAIROS_AUTH_TOKEN="secret")
            gateway_main.main()
            assert run.called
    finally:
        _set_env(**previous)


def token_redaction_test() -> None:
    """Fix 1: ?token= never reaches uvicorn's log lines."""
    import logging
    import logging.config
    import uvicorn.config
    from kairos.gateway.server import RedactTokenFilter

    with tempfile.TemporaryDirectory() as directory:
        manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1, agent_factory=FakeAgent)
        create_app(manager)
        asyncio.run(manager.shutdown())

    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    for name, message, args in (
        ("uvicorn.error", '%s - "WebSocket %s" [accepted]', ("127.0.0.1:5000", "/api/v1/ws?token=secret")),
        ("uvicorn.access", '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:5000", "GET", "/api/v1/x?a=1&token=secret", "1.1", 200)),
    ):
        logger = logging.getLogger(name)
        handler, level = Capture(), logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            logger.info(message, *args)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(level)
    assert len(lines) == 2 and all("secret" not in line and "token=***" in line for line in lines), lines
    # uvicorn configures its loggers at startup; the filter must survive that.
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    assert any(isinstance(f, RedactTokenFilter) for f in logging.getLogger("uvicorn.access").filters)


def event_order_test() -> None:
    """Fix 2: an event whose ID was assigned first is never dropped by a
    subscriber that already received a later one."""
    import threading
    import time

    with tempfile.TemporaryDirectory() as directory:
        manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1, agent_factory=FakeAgent)
        conversation = manager.create_conversation(directory)
        real_append = manager.repository.append_event
        first_has_id = threading.Event()

        def slow_append(conversation_id, event, data, run_id=None):
            payload = real_append(conversation_id, event, data, run_id)
            if event == "first":
                first_has_id.set()
                time.sleep(0.3)  # its ID is assigned; its delivery is late
            return payload

        manager.repository.append_event = slow_append
        with TestClient(create_app(manager), base_url=BASE_URL) as client, client.websocket_connect(WS_URL) as ws:
            ws.send_json({"type": "command", "command": "conversation.subscribe", "request_id": "s",
                          "conversation_id": conversation.id, "payload": {}})
            assert ws.receive_json()["type"] == "ack"
            first = threading.Thread(target=manager.emit, args=(conversation.id, "first", {}))
            first.start()
            assert first_has_id.wait(5)
            second = threading.Thread(target=manager.emit, args=(conversation.id, "second", {}))
            second.start()
            first.join()
            second.join()
            manager.emit(conversation.id, "last", {})
            names = []
            while not names or names[-1] != "last":
                names.append(ws.receive_json()["event"])
        assert names == ["first", "second", "last"], names


class StopAwareAgent(FakeAgent):
    """Mimics Agent.run's stop flag: a stop pending at the start returns
    "[Stopped ...]" and the end of run() clears it."""

    def __init__(self, workspace: str):
        import threading

        super().__init__(workspace)
        self._interrupt_event = threading.Event()
        self.after_run = None  # called once at the very end of run()

    def interrupt(self):
        self._interrupt_event.set()

    def clear_stop(self):
        # Same public contract as Agent.clear_stop(), which the gateway calls
        # to drop a stop that arrived after its run had ended.
        self._interrupt_event.clear()

    def run(self, content, image_url=None):
        if self._interrupt_event.is_set():
            self._interrupt_event.clear()
            return "[Stopped — waiting for your input]"
        result = super().run(content, image_url)
        self._interrupt_event.clear()
        hook, self.after_run = self.after_run, None
        if hook:
            hook()
        return result


def late_interrupt_test() -> None:
    """Fix 3: an Esc processed after Agent.run returned must not stop the next run."""
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1, agent_factory=StopAwareAgent)
            conversation = manager.create_conversation(directory)
            agent = (await manager.load_runtime(conversation.id)).agent
            loop = asyncio.get_running_loop()
            # The interrupt reaches the loop before the gateway handles the
            # run's return (the window a late run.cancel/run.interrupt hits).
            agent.after_run = lambda: loop.call_soon_threadsafe(
                lambda: loop.create_task(manager.interrupt_run(conversation.id))
            )
            first = await manager.submit_message(conversation.id, "one")
            assert await wait_until(lambda: manager.repository.get_run(first.id).status == "completed")
            second = await manager.submit_message(conversation.id, "two")
            assert await wait_until(lambda: manager.repository.get_run(second.id).status != "queued" and not manager.get_runtime(conversation.id).is_running)
            assert manager.repository.get_run(second.id).status == "completed", manager.repository.get_run(second.id)
            assert agent.conversation_history[-1]["content"] == "done: two"
            await manager.shutdown()

    asyncio.run(scenario())


def reload_keeps_interrupted_turn_test() -> None:
    """Fix 4: loading an interrupted conversation keeps the user's request and
    the executed tool calls, and the next save does not erase them."""
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1, agent_factory=FakeAgent)
            conversation = manager.create_conversation(directory)
            manager.repository.replace_messages(conversation.id, [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "second request"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_a", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                    {"id": "call_b", "type": "function", "function": {"name": "edit", "arguments": "{}"}},
                ]},
                {"role": "tool", "tool_call_id": "call_a", "name": "read", "content": "A result"},
            ])
            runtime = await manager.load_runtime(conversation.id)
            for history in (runtime.agent.conversation_history, None):
                if history is None:
                    await manager.unload_runtime(conversation.id)
                    history = manager.repository.load_history(conversation.id)
                assert any(m.get("content") == "second request" for m in history), history
                assert any(m.get("tool_call_id") == "call_a" and m.get("content") == "A result" for m in history), history
                assert history[-1]["tool_call_id"] == "call_b"  # repaired, API-valid
            await manager.shutdown()

    asyncio.run(scenario())


def data_dir_lock_test() -> None:
    """Fix 5: a second gateway on the same data dir refuses to start and does
    not mark the live gateway's runs interrupted."""
    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory) / "data"
        first = GatewayManager(GatewayRepository(data), max_concurrent_runs=1, agent_factory=FakeAgent)
        conversation = first.create_conversation(directory)
        run = first.repository.create_run(conversation.id, "live queued run")
        try:
            GatewayManager(GatewayRepository(data), max_concurrent_runs=1, agent_factory=FakeAgent)
        except RuntimeError as exc:
            assert "already using" in str(exc), exc
        else:
            raise AssertionError("a second gateway on the same data dir must refuse to start")
        assert first.repository.get_run(run.id).status == "queued"
        asyncio.run(first.shutdown())
        # Released at shutdown: the next gateway starts and recovers the run.
        second = GatewayManager(GatewayRepository(data), max_concurrent_runs=1, agent_factory=FakeAgent)
        assert second.repository.get_run(run.id).status == "interrupted"
        asyncio.run(second.shutdown())


class BlockingAgent(FakeAgent):
    def __init__(self, workspace: str):
        import threading

        super().__init__(workspace)
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, content, image_url=None):
        self.started.set()
        self.release.wait(5)
        return super().run(content, image_url)


def queued_run_status_change_test() -> None:
    """Fix 5: a queued run whose status changed underneath it ends with a
    terminal run event instead of silence."""
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1, agent_factory=BlockingAgent)
            conversation = manager.create_conversation(directory)
            agent = (await manager.load_runtime(conversation.id)).agent
            await manager.submit_message(conversation.id, "one")
            assert await wait_until(agent.started.is_set)
            second = await manager.submit_message(conversation.id, "two")
            manager.repository.update_run(second.id, "interrupted", "Gateway restarted.", expected_status="queued")
            agent.release.set()
            assert await wait_until(lambda: any(
                e["event"] == "run.interrupted" and e["run_id"] == second.id
                for e in manager.events_after(conversation.id)
            ))
            await manager.shutdown()

    asyncio.run(scenario())


class SlowCloseAgent(FakeAgent):
    def __init__(self, workspace: str):
        import time

        super().__init__(workspace)
        self.closed = []
        closed = self.closed

        class Browser:
            is_open = False  # the worker can be alive while no page is open

            def close(self):
                time.sleep(0.5)
                closed.append("browser")

        class Desktop:
            def close(self):
                closed.append("desktop")

        self.browser_manager = Browser()
        self.desktop_manager = Desktop()


def unload_off_loop_test() -> None:
    """Fix 6: a slow resource close does not freeze the event loop, and the
    browser and desktop managers are always closed."""
    import time

    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1, agent_factory=SlowCloseAgent)
            conversation = manager.create_conversation(directory)
            agent = (await manager.load_runtime(conversation.id)).agent
            started = time.monotonic()
            unload = asyncio.create_task(manager.unload_runtime(conversation.id))
            await asyncio.sleep(0.05)
            assert time.monotonic() - started < 0.4, "the event loop was blocked by unload"
            assert not unload.done()
            await unload
            assert agent.closed == ["browser", "desktop"], agent.closed
            await manager.shutdown()

    asyncio.run(scenario())


def wake_unload_race_test() -> None:
    """Fix 7: notifications taken by a wake that loses the race with an
    unload are kept for the next load."""
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            manager = background_manager(directory)
            conversation = manager.create_conversation(directory)
            runtime = await manager.load_runtime(conversation.id)
            # The wake timer took the event just before the unload; its
            # enqueue reaches the event loop after the unload began.
            manager._start_wake(runtime, [bg_event("raced")])
            await manager.unload_runtime(conversation.id)
            await asyncio.sleep(0.05)
            agent = (await manager.load_runtime(conversation.id)).agent
            assert [e["id"] for e in agent.held] == ["raced"], agent.held
            await manager.shutdown()

    asyncio.run(scenario())


class BlockingModelAgent(ModelFakeAgent):
    def __init__(self, workspace: str):
        import threading

        super().__init__(workspace)
        self.started = threading.Event()
        self.release = threading.Event()
        self.models_seen: list = []

    def run(self, content, image_url=None):
        self.started.set()
        self.release.wait(5)
        self.models_seen.append(self.spec.model)
        return super().run(content, image_url)


def configure_during_run_test() -> None:
    """Fix 8: a model change requested during a run applies before the next run."""
    import json
    from unittest import mock

    async def scenario(directory: str):
        manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1, agent_factory=BlockingModelAgent)
        conversation = manager.create_conversation(directory)
        agent = (await manager.load_runtime(conversation.id)).agent
        first = await manager.submit_message(conversation.id, "one")
        assert await wait_until(agent.started.is_set)
        info = await manager.configure_conversation(conversation.id, setup="mimo")
        assert info["setup"] == "mimo", info
        assert agent.spec.setup == "luna"  # not switched under the running run
        agent.release.set()
        assert await wait_until(lambda: manager.repository.get_run(first.id).status == "completed")
        second = await manager.submit_message(conversation.id, "two")
        assert await wait_until(lambda: manager.repository.get_run(second.id).status == "completed")
        assert agent.models_seen == ["gpt-6-luna", "xiaomi/mimo"], agent.models_seen
        assert (await manager.configure_conversation(conversation.id))["setup"] == "mimo"
        await manager.shutdown()

    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "provider.json"
        config.write_text(json.dumps(MODEL_CONFIG), encoding="utf-8")
        with mock.patch("kairos.providers.config.config_path", return_value=config):
            asyncio.run(scenario(directory))


def malformed_frame_test() -> None:
    """Fix 9: a malformed frame gets an error reply; the connection stays."""
    with tempfile.TemporaryDirectory() as directory:
        manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1, agent_factory=FakeAgent)
        with TestClient(create_app(manager), base_url=BASE_URL) as client, client.websocket_connect(WS_URL) as ws:
            for frame in ("not json", "[1, 2]", '"text"'):
                ws.send_text(frame)
                reply = ws.receive_json()
                assert reply["type"] == "error" and reply["code"] == "invalid_message", reply
            ws.send_json({"type": "command", "command": "ping", "request_id": "p"})
            assert ws.receive_json()["data"] == {"pong": True}


def main() -> None:
    tests = [
        terminal_test,
        repository_test,
        history_test,
        manager_test,
        minimal_agent_resource_test,
        unload_rejection_test,
        concurrent_load_test,
        config_test,
        auth_test,
        api_test,
        model_config_test,
        model_settings_migration_test,
        fresh_workspace_test,
        wake_run_test,
        wake_cap_test,
        unload_protection_and_lost_test,
        subagent_commands_test,
        cross_site_test,
        bind_refusal_test,
        token_redaction_test,
        event_order_test,
        late_interrupt_test,
        reload_keeps_interrupted_turn_test,
        data_dir_lock_test,
        queued_run_status_change_test,
        unload_off_loop_test,
        wake_unload_race_test,
        configure_during_run_test,
        malformed_frame_test,
    ]
    for test in tests:
        print(f"running {test.__name__}", flush=True)
        test()
    print("gateway smoke tests passed")


if __name__ == "__main__":
    main()
