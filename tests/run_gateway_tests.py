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
    with tempfile.TemporaryDirectory() as directory:
        manager = GatewayManager(GatewayRepository(Path(directory) / "data"), max_concurrent_runs=1)
        app = create_app(manager)
        with TestClient(app) as client:
            response = client.post("/api/v1/conversations", json={"workspace": directory, "title": "Test"})
            assert response.status_code == 201, response.text
            conversation_id = response.json()["id"]
            assert client.get("/api/v1/conversations").status_code == 200
            loaded = client.post(f"/api/v1/conversations/{conversation_id}/runtime/load")
            assert loaded.status_code == 200, loaded.text
            assert loaded.json()["conversation"]["runtime_loaded"] is True
            unloaded = client.post(f"/api/v1/conversations/{conversation_id}/runtime/unload")
            assert unloaded.status_code == 200, unloaded.text


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
    ]
    for test in tests:
        print(f"running {test.__name__}", flush=True)
        test()
    print("gateway smoke tests passed")


if __name__ == "__main__":
    main()
