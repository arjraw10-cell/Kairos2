"""Background work: sub-agents, background-event routing in Agent, terminals."""
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from kairos import providers
from kairos.agent import SUBAGENT_TOOL_NAMES, Agent
from kairos.terminal_manager import TerminalManager
from kairos.tools.subagent import SubAgentTool, _SubAgent
from kairos.wake import NOTIFICATION_PREFIX

CONFIG = {
    "default_setup": "fake",
    "providers": {"local": {"format": "completions", "base_url": "http://127.0.0.1:9/v1", "api_keys": ["k"]}},
    "setups": {"fake": {"provider": "local", "model": "fake-model"}},
}


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class ProviderConfigMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "provider.json"
        path.write_text(json.dumps(CONFIG), encoding="utf-8")
        patcher = mock.patch("kairos.providers.config.config_path", return_value=path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.workspace = self._tmp.name


class FakeChild:
    """Stands in for the child Agent created by SubAgentTool."""

    _tool_summary = staticmethod(Agent._tool_summary)
    instances = []
    TOOLS = [("read", {"path": "a.py"}), ("edit", {"path": "a.py"}),
             ("search", {"pattern": "x", "path": "src"}), ("git", {"command": "status"})]

    def __init__(self, workspace):
        self._interrupt_event = threading.Event()
        self.release = threading.Event()
        self.stubborn = False  # True: a tool call in flight ignores the stop
        self.on_tool_call = None
        self.prompts = []
        FakeChild.instances.append(self)

    def _use_spec(self, spec, effort):
        pass

    def _setup_system_prompt(self):
        pass

    def run(self, prompt):
        self.prompts.append(prompt)
        for name, args in self.TOOLS:
            self.on_tool_call(name, args)
        while not self.release.is_set():
            if self._interrupt_event.is_set() and not self.stubborn:
                return "[Interrupted]"
            time.sleep(0.01)
        self.release.clear()
        if prompt == "boom":
            raise RuntimeError("api down")
        return f"result for {prompt}"

    def interrupt(self):
        self._interrupt_event.set()


class SubAgentToolTests(unittest.TestCase):
    def setUp(self):
        FakeChild.instances = []
        patcher = mock.patch("kairos.agent.Agent", FakeChild)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events, self.statuses = [], []
        self.tool = SubAgentTool("C:/ws", lambda: (None, None), self.events.append, self.statuses.append)

    def spawn(self, prompt="task"):
        result = self.tool.spawn(prompt)
        self.assertTrue(result.success, result.error)
        return result.output.split()[1]

    def test_spawn_returns_immediately_and_result_arrives_as_event(self):
        sub_id = self.spawn("write the parser")
        self.assertEqual(self.tool.statuses()[0]["state"], "running")
        self.assertEqual(self.events, [])
        refused = self.tool.message(sub_id, "more")
        self.assertFalse(refused.success)
        self.assertIn("still running", refused.error)

        FakeChild.instances[0].release.set()
        self.assertTrue(wait_for(lambda: self.events))
        event = self.events[0]
        self.assertEqual((event["kind"], event["subagent_id"]), ("subagent_finished", sub_id))
        self.assertIn("result for write the parser", event["text"])
        self.assertEqual(set(event), {"id", "kind", "subagent_id", "text", "created_at"})
        self.assertEqual(self.statuses[-1]["state"], "idle")
        self.assertEqual(self.statuses[0]["state"], "running")  # spawn status
        self.assertTrue(any(s.get("current_tool") for s in self.statuses))

        # Continue it with its context, again in the background.
        self.assertTrue(self.tool.message(sub_id, "now test it").success)
        FakeChild.instances[0].release.set()
        self.assertTrue(wait_for(lambda: len(self.events) == 2))
        self.assertEqual(FakeChild.instances[0].prompts, ["write the parser", "now test it"])
        self.assertEqual(len(FakeChild.instances), 1)

    def test_failure_produces_subagent_failed(self):
        self.spawn("boom")
        FakeChild.instances[0].release.set()
        self.assertTrue(wait_for(lambda: self.events))
        self.assertEqual(self.events[0]["kind"], "subagent_failed")
        self.assertIn("api down", self.events[0]["text"])
        self.assertEqual(self.tool.statuses()[0]["state"], "failed")

    def test_list_shows_recent_actions_oldest_first(self):
        sub_id = self.spawn("long task with a fairly long description " * 5)
        self.assertTrue(wait_for(lambda: len(self.tool.statuses()[0]["recent_actions"]) == 3))
        status = self.tool.statuses()[0]
        self.assertEqual(status["recent_actions"],
                         ["edited file: a.py", "search: 'x' in src (timeout: 10.0s)", "git status"])
        self.assertEqual(status["current_tool"], "git status")
        self.assertLessEqual(len(status["prompt"]), SubAgentTool.PROMPT_PREVIEW_CHARS)
        listing = self.tool.list().output
        self.assertIn(sub_id, listing)
        self.assertIn("running", listing)
        self.assertIn("edited file: a.py | search: 'x' in src (timeout: 10.0s) | git status", listing)
        self.tool.stop_all()

    def test_message_waits_for_a_terminated_run_to_finish(self):
        # Reproduction: terminate then message ran two threads on one Agent;
        # the old run reported its result as the reply and the new one was lost.
        sub_id = self.spawn("first")
        child = FakeChild.instances[0]
        child.stubborn = True
        self.tool.STOP_WAIT_SECONDS = 0.05
        self.assertTrue(self.tool.terminate(sub_id).success)
        refused = self.tool.message(sub_id, "second")
        self.assertFalse(refused.success)
        self.assertIn("still stopping", refused.error)
        self.assertEqual(child.prompts, ["first"])

        # A run that finishes within the wait is waited for; its own result
        # is not reported (it was terminated).
        self.tool.STOP_WAIT_SECONDS = 5
        threading.Timer(0.05, child.release.set).start()
        self.assertTrue(self.tool.message(sub_id, "second").success)
        self.assertTrue(wait_for(lambda: child.prompts == ["first", "second"]))
        child.release.set()
        self.assertTrue(wait_for(lambda: self.events))
        time.sleep(0.05)
        self.assertEqual(len(self.events), 1)
        self.assertIn("result for second", self.events[0]["text"])

    def test_cap_terminate_stop_all_and_lost(self):
        ids = [self.spawn(f"t{i}") for i in range(SubAgentTool.MAX_RUNNING)]
        over = self.tool.spawn("one too many")
        self.assertFalse(over.success)
        self.assertIn("maximum", over.error)

        self.assertTrue(self.tool.terminate(ids[0]).success)
        self.assertFalse(self.tool.terminate(ids[0]).success)  # not running any more
        self.assertEqual(self.statuses[-1]["state"], "terminated")
        self.assertTrue(FakeChild.instances[0]._interrupt_event.is_set())

        self.assertEqual(self.tool.stop_all(), 2 * 2)  # ids[1..4]
        self.assertEqual(self.tool.running_count(), 0)
        time.sleep(0.1)
        self.assertEqual(self.events, [])  # terminations do not wake the parent

        lost_id = self.spawn("survive?")
        self.assertEqual(self.tool.mark_lost(), 1)
        self.assertEqual(self.statuses[-1], {**self.statuses[-1], "subagent_id": lost_id, "state": "lost"})
        self.assertEqual(self.events[-1]["kind"], "subagent_failed")
        self.assertIn("lost", self.events[-1]["text"])


class ScriptAdapter:
    """Provider adapter whose replies come from per-step callables."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    def stream(self, messages, tools, effort):
        self.requests.append([dict(m) for m in messages])
        events = self.steps[len(self.requests) - 1](messages)
        return (e for e in events)


def bg_event(i):
    return {"id": f"e{i}", "kind": "subagent_finished", "subagent_id": "s1", "text": f"done {i}", "created_at": 0.0}


class AgentBackgroundTests(ProviderConfigMixin, unittest.TestCase):
    def test_subagent_tools_excluded_from_child_schema(self):
        parent = {t["function"]["name"] for t in Agent._get_tool_schema(types.SimpleNamespace(_is_subagent=False))}
        child = {t["function"]["name"] for t in Agent._get_tool_schema(types.SimpleNamespace(_is_subagent=True))}
        self.assertTrue(SUBAGENT_TOOL_NAMES <= parent)
        self.assertFalse(SUBAGENT_TOOL_NAMES & child)
        self.assertNotIn("get_subagent_result", parent)
        self.assertIn("stop_command", child)

    def test_subagent_prompt_describes_only_its_tools(self):
        tool = SubAgentTool(self.workspace, lambda: (providers.resolve(), None), lambda e: None, lambda s: None)
        child = tool._create_sub_agent(_SubAgent(id="x", prompt="p"))
        for absent in ("## Browser Tools", "browser_", "browse the web", "when a sub-agent finishes"):
            self.assertNotIn(absent, child.system_prompt)
        self.assertIn("## Native Desktop Tools", child.system_prompt)
        self.assertEqual(child.conversation_history, [{"role": "system", "content": child.system_prompt}])
        self.assertIn("## Browser Tools", Agent(self.workspace).system_prompt)

    def test_child_cannot_call_subagent_tools_directly(self):
        agent = Agent(self.workspace)
        agent._is_subagent = True  # even with a tool object still attached
        for name, args in (("spawn_subagent", {"prompt": "x"}), ("message_subagent", {"subagent_id": "a", "message": "m"}),
                           ("terminate_subagent", {"subagent_id": "a"}), ("list_subagents", {})):
            result = json.loads(agent._execute_tool(name, args))
            self.assertFalse(result["success"])
            self.assertIn("Sub-agents cannot", result["error"])
        agent.subagent_tool = None
        agent._is_subagent = False
        self.assertIn("Sub-agents cannot", json.loads(agent._execute_tool("list_subagents", {}))["error"])

    def test_event_during_final_step_is_injected_into_the_same_run(self):
        agent = Agent(self.workspace)
        seen = []
        agent.on_background_event = seen.append

        def first(_messages):
            agent._push_background_event(bg_event(1))  # sub-agent finishes mid-step
            return [("text", "Two sub-agents are running; I will report back.")]

        agent.adapter = ScriptAdapter(first, lambda m: [("text", "Both are done now.")])
        self.assertEqual(agent.run("start two"), "Both are done now.")
        self.assertEqual(seen[0]["in_run"], True)
        last = agent.adapter.requests[1][-1]
        self.assertEqual(last["role"], "user")
        self.assertTrue(last["content"].startswith(NOTIFICATION_PREFIX))
        self.assertIn("done 1", last["content"])

        # Idle: the host owns the event; the Agent keeps nothing.
        agent._push_background_event(bg_event(2))
        self.assertNotIn("in_run", seen[-1])
        self.assertEqual(agent.drain_background_events(), [])

    def test_without_a_host_idle_events_ride_with_the_next_message(self):
        agent = Agent(self.workspace)
        agent._push_background_event(bg_event(1))
        agent.add_background_events([bg_event(2)])
        agent.adapter = ScriptAdapter(lambda m: [("text", "Noted both results.")])
        agent.run("hello")
        messages = agent.adapter.requests[0]
        self.assertEqual(messages[-2], {"role": "user", "content": "hello"})
        self.assertIn("done 1", messages[-1]["content"])
        self.assertIn("done 2", messages[-1]["content"])

    def test_hard_stop_of_parent_does_not_stop_subagents(self):
        agent = Agent(self.workspace)
        FakeChild.instances = []
        with mock.patch("kairos.agent.Agent", FakeChild):
            self.assertTrue(agent.subagent_tool.spawn("work").success)
        child = FakeChild.instances[0]
        agent.interrupt()
        time.sleep(0.05)
        self.assertFalse(child._interrupt_event.is_set())
        self.assertTrue(agent.has_background_work())
        self.assertEqual(agent.subagent_statuses()[0]["state"], "running")
        self.assertEqual(agent.stop_subagents(), 1)
        self.assertTrue(child._interrupt_event.is_set())
        self.assertFalse(agent.has_background_work())


PY = f'"{sys.executable}"'


class TerminalBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.manager = TerminalManager()
        self.events = []
        self.manager.set_completion_callback(self.events.append)
        self.tid = self.manager.create_terminal(background=True)
        self.addCleanup(self.manager.close_terminal, self.tid)

    def test_until_match_then_stop_command_keeps_the_shell(self):
        ok, msg = self.manager.execute_command(
            self.tid, f'{PY} -u -c "print(1); print(\'Listening on 8000\'); import time; time.sleep(60)"',
            is_background=True, until=r"Listening on \d+",
        )
        self.assertTrue(ok, msg)
        self.assertTrue(wait_for(lambda: self.events))
        match = self.events[0]
        self.assertEqual(match["kind"], "terminal_matched")
        self.assertEqual(match["line"], "Listening on 8000")
        self.assertIn("still running", TerminalManager.format_background_completion(match))
        self.assertEqual(self.manager.running_background_commands(), 1)

        ok, msg = self.manager.stop_command(self.tid)
        self.assertTrue(ok, msg)
        self.assertTrue(wait_for(lambda: len(self.events) == 2, timeout=10))
        finished = self.events[1]
        self.assertEqual((finished["kind"], finished["stopped"], finished["success"]), ("terminal_finished", True, False))
        self.assertIn("stopped by stop_command", TerminalManager.format_background_completion(finished))
        self.assertEqual(len([e for e in self.events if e["kind"] == "terminal_matched"]), 1)

        ok, _ = self.manager.execute_command(self.tid, "echo still-alive", is_background=True)
        self.assertTrue(ok)
        self.assertTrue(wait_for(lambda: len(self.events) == 3))
        self.assertTrue(self.events[2]["success"])
        self.assertIn("still-alive", self.events[2]["output"])
        self.assertFalse(self.manager.stop_command(self.tid)[0])  # nothing running

    def test_until_rejected_for_blocking_and_bad_regex(self):
        blocking = self.manager.create_terminal(background=False)
        self.assertIn("only supported", self.manager.execute_command(blocking, "echo x", 5, False, "x")[1])
        self.assertIn("Invalid until", self.manager.execute_command(self.tid, "echo x", None, True, "(")[1])

    def test_read_logs_tail_default_and_ranges(self):
        self.assertEqual(self.manager.read_logs(self.tid), (True, "[no output yet]"))
        self.manager.execute_command(
            self.tid, f'{PY} -c "print(chr(10).join(f\'line{{i}}\' for i in range(1, 151)))"', is_background=True
        )
        self.assertTrue(wait_for(lambda: self.events))
        # The shell may add prompt lines, so compare with the buffer itself.
        lines = self.manager.terminals[self.tid].output_buffer.splitlines()
        n = len(lines)
        self.assertIn("line150", lines)
        ok, out = self.manager.read_logs(self.tid, tail=2)
        self.assertEqual(out.splitlines(), [f"[lines {n - 1}-{n} of {n}]", *lines[-2:]])
        ok, out = self.manager.read_logs(self.tid)
        self.assertTrue(out.startswith(f"[lines {n - 99}-{n} of {n}]"))
        self.assertEqual(len(out.splitlines()), 101)
        first = next(i for i, line in enumerate(lines) if line.endswith("line1"))  # may follow a prompt
        ok, out = self.manager.read_logs(self.tid, start_line=first + 2, end_line=first + 3)
        self.assertEqual(out.splitlines(), [f"[lines {first + 2}-{first + 3} of {n}]", "line2", "line3"])
        ok, out = self.manager.read_logs(self.tid, start_line=n)
        self.assertEqual(out.splitlines()[1:], [lines[-1]])
        self.assertFalse(self.manager.read_logs(self.tid, start_line=n + 1)[0])
        self.assertFalse(self.manager.read_logs(self.tid, start_line=5, end_line=4)[0])
        self.assertFalse(self.manager.read_logs(self.tid, tail=3, start_line=1)[0])

    def test_events_queue_without_a_consumer_and_are_handed_over_once(self):
        manager = TerminalManager()
        tid = manager.create_terminal(background=True)
        self.addCleanup(manager.close_terminal, tid)
        manager.execute_command(tid, "echo queued", is_background=True)
        self.assertTrue(wait_for(lambda: manager._completed_background_commands))
        received = []
        manager.set_completion_callback(received.append)
        self.assertEqual(len(received), 1)
        self.assertEqual(list(manager._completed_background_commands), [])


if __name__ == "__main__":
    unittest.main()


class ToolArgumentErrorTests(ProviderConfigMixin, unittest.TestCase):
    def test_missing_required_argument_is_named(self):
        # Models sometimes drop a required argument; a bare "'terminal_id'" made
        # them retry the same broken call, so the error must name what is missing.
        agent = Agent(self.workspace)
        result = json.loads(agent._execute_tool("execute_command", {"terminal_id": 1}))
        self.assertFalse(result["success"])
        self.assertIn("Missing required argument 'command' for tool execute_command", result["error"])
