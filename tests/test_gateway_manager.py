from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from kairos.gateway.manager import GatewayManager
from kairos.gateway.repository import GatewayRepository


class FakeAgent:
    created = 0

    def __init__(self, workspace: str):
        type(self).created += 1
        self.workspace = workspace
        self.conversation_history = [{"role": "system", "content": "system"}]
        self.browser_manager = SimpleNamespace(is_open=False)
        self.terminal_manager = SimpleNamespace(terminals={})
        self.subagent_tool = None
        self.on_stream_start = None
        self.on_stream_token = None
        self.on_stream_end = None
        self.on_tool_call = None
        self.on_tool_result = None
        self.on_token_update = None
        self.on_compact = None
        self.interrupted = False

    def get_history(self):
        return list(self.conversation_history)

    def run(self, content: str, image_url=None):
        self.conversation_history.append({"role": "user", "content": content})
        self.conversation_history.append({"role": "assistant", "content": f"done: {content}"})
        return f"done: {content}"

    def interrupt(self):
        self.interrupted = True
        return None

    def compact(self):
        return "nothing to compact"


def test_manager_runs_and_persists_with_fake_agent(tmp_path: Path):
    async def scenario():
        manager = GatewayManager(
            GatewayRepository(tmp_path / "data"),
            max_concurrent_runs=1,
            agent_factory=FakeAgent,
        )
        conversation = manager.create_conversation(str(tmp_path / "workspace"))
        run = await manager.submit_message(conversation.id, "hello")
        for _ in range(100):
            if manager.repository.get_run(run.id).status == "completed":
                break
            await asyncio.sleep(0.01)
        assert manager.repository.get_run(run.id).status == "completed"
        history = manager.repository.load_history(conversation.id)
        assert history[-1]["content"] == "done: hello"
        await manager.shutdown()

    asyncio.run(scenario())


def test_non_forced_unload_rejection_does_not_poison_runtime(tmp_path: Path):
    async def scenario():
        manager = GatewayManager(
            GatewayRepository(tmp_path / "data"),
            max_concurrent_runs=1,
            agent_factory=FakeAgent,
        )
        conversation = manager.create_conversation(str(tmp_path / "workspace"))
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


def test_concurrent_loads_create_one_runtime(tmp_path: Path):
    async def scenario():
        FakeAgent.created = 0
        manager = GatewayManager(
            GatewayRepository(tmp_path / "data"),
            max_concurrent_runs=1,
            agent_factory=FakeAgent,
        )
        conversation = manager.create_conversation(str(tmp_path / "workspace"))
        runtimes = await asyncio.gather(
            *(manager.load_runtime(conversation.id) for _ in range(10))
        )
        assert len({id(runtime) for runtime in runtimes}) == 1
        assert FakeAgent.created == 1
        await manager.shutdown()

    asyncio.run(scenario())
