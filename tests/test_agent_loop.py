"""Agent loop robustness: tool-chain repair, background-event hand-back,
stops during compaction, provider-reported context, compaction prompt and
tool error messages. No network: adapters are scripted."""
import json
import sys
import threading
import unittest
from pathlib import Path

import httpx
from datetime import date
from unittest import mock

from kairos import providers
from kairos.agent import Agent, APIRequestError, TRUNCATED_CALL_ERROR
from kairos.providers.base import EventStream
from kairos.resume import date_note, repair_tool_chains
from kairos.tokens import TokenCounter
from kairos.wake import WakeScheduler

from test_background import ProviderConfigMixin, ScriptAdapter, bg_event, wait_for


def tool_call(call_id, name, args):
    return ("tool_call", 0, call_id, name, json.dumps(args))


def assistant_calls(*ids):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": i, "type": "function", "function": {"name": "read", "arguments": "{}"}} for i in ids]}


def tool_result(call_id):
    return {"role": "tool", "tool_call_id": call_id, "name": "read", "content": "{}"}


def assert_valid_chains(test, messages):
    """Every assistant tool call is answered right after it; no orphans."""
    i = 0
    while i < len(messages):
        message = messages[i]
        test.assertNotEqual(message.get("role"), "tool", f"orphaned tool message at {i}")
        i += 1
        if message.get("role") == "assistant" and message.get("tool_calls"):
            expected = [c["id"] for c in message["tool_calls"]]
            answered = [m.get("tool_call_id") for m in messages[i:i + len(expected)]]
            test.assertEqual(answered, expected, f"tool chain at {i - 1} is incomplete")
            i += len(expected)


class ToolChainTests(ProviderConfigMixin, unittest.TestCase):
    def test_tool_summary_never_raises(self):
        names = [t["function"]["name"] for t in Agent(self.workspace)._get_tool_schema(all_groups=True)]
        for name in names:
            for args in ({}, None, [], "text", {"message": None, "prompt": None, "text": None, "expression": None,
                                                "command": "commit", "fields": 5, "ref": None}):
                self.assertIsInstance(Agent._tool_summary(name, args), str, name)
        self.assertEqual(Agent._tool_summary("git", {"command": "commit", "message": None}), "git commit: ")

    def test_null_argument_and_failing_observer_do_not_wedge_the_conversation(self):
        # Reproduction: the gateway's on_tool_call summarizes the call; a null
        # message crashed it after the assistant tool call was in history.
        agent = Agent(self.workspace)
        agent.on_tool_call = lambda name, args: Agent._tool_summary(name, args)
        agent.adapter = ScriptAdapter(
            lambda m: [tool_call("call_1", "message_subagent", {"subagent_id": "nope", "message": None})],
            lambda m: [("text", "Done here.")],
        )
        self.assertEqual(agent.run("go"), "Done here.")
        assert_valid_chains(self, agent.adapter.requests[1])

        def broken_observer(name, args):
            raise RuntimeError("observer bug")

        agent.on_tool_call = broken_observer
        agent.adapter = ScriptAdapter(lambda m: [tool_call("call_2", "list_skills", {})],
                                      lambda m: [("text", "Still fine.")])
        self.assertEqual(agent.run("again"), "Still fine.")
        assert_valid_chains(self, agent.adapter.requests[1])

    def test_any_exception_leaving_run_repairs_the_tool_chain(self):
        agent = Agent(self.workspace)
        agent.adapter = ScriptAdapter(lambda m: [tool_call("call_1", "list_skills", {})])

        def crash(name, args, max_chars=None):
            raise RuntimeError("tool loop bug")

        agent._execute_tool = crash
        with self.assertRaises(RuntimeError):
            agent.run("go")
        del agent._execute_tool
        assert_valid_chains(self, agent.conversation_history)

        agent.adapter = ScriptAdapter(lambda m: [("text", "Recovered.")])
        self.assertEqual(agent.run("next"), "Recovered.")
        assert_valid_chains(self, agent.adapter.requests[0])

    def test_validation_repairs_a_gap_before_a_later_message(self):
        agent = Agent(self.workspace)
        agent.conversation_history += [
            {"role": "user", "content": "go"}, assistant_calls("a", "b"), tool_result("a"),
            {"role": "user", "content": "later"},
        ]
        notices = []
        agent.on_compact = notices.append
        agent.adapter = ScriptAdapter(lambda m: [("text", "Okay then.")])
        self.assertEqual(agent.run("now"), "Okay then.")
        sent = agent.adapter.requests[0]
        assert_valid_chains(self, sent)
        self.assertEqual([m.get("tool_call_id") for m in sent[3:5]], ["a", "b"])
        self.assertIn("Tool was not executed", sent[4]["content"])
        self.assertTrue(any("Repaired" in n for n in notices))

    def test_repair_tool_chains_orders_results_and_drops_orphans(self):
        history = [{"role": "system", "content": "s"}, tool_result("x"), {"role": "user", "content": "u"},
                   assistant_calls("a", "b"), tool_result("b"), tool_result("zz"), tool_result("a")]
        self.assertEqual(repair_tool_chains(history), 2)
        self.assertEqual([m.get("tool_call_id") for m in history], [None, None, None, "a", "b"])
        self.assertEqual(repair_tool_chains(history), 0)


class BackgroundHandBackTests(ProviderConfigMixin, unittest.TestCase):
    def test_events_of_a_failed_run_go_back_to_the_host(self):
        agent = Agent(self.workspace)
        seen = []
        agent.on_background_event = seen.append

        def first(messages):
            agent._push_background_event(bg_event(1))  # arrives in-run
            raise providers.ProviderError("HTTP 400: bad request", 400)

        agent.adapter = ScriptAdapter(first)
        with self.assertRaises(APIRequestError):
            agent.run("go")
        self.assertEqual(seen[0]["in_run"], True)
        self.assertEqual(seen[-1], bg_event(1))  # handed back without in_run
        self.assertEqual(agent.drain_background_events(), [])

    def test_events_of_an_interrupted_run_wake_the_host(self):
        agent = Agent(self.workspace)
        woken = []
        scheduler = WakeScheduler(start_wake=woken.append, is_idle=lambda: not agent.is_processing, debounce=0.01)
        agent.on_background_event = scheduler.push

        def first(messages):
            agent._push_background_event(bg_event(1))
            agent.interrupt()
            return [("text", "never shown")]

        agent.adapter = ScriptAdapter(first)
        self.assertEqual(agent.run("go"), "[Interrupted]")
        self.assertTrue(wait_for(lambda: woken))
        self.assertEqual(woken[0], [bg_event(1)])

    def test_reset_forgets_the_old_conversations_events(self):
        agent = Agent(self.workspace)
        agent.add_background_events([bg_event(1)])
        agent.reset()
        self.assertEqual(agent.drain_background_events(), [])


class BlockingSummaryAdapter:
    """Normal requests answer at once; the summary request blocks until closed."""

    def __init__(self):
        self.opened = threading.Event()
        self.closed = threading.Event()

    def stream(self, messages, tools, effort):
        if tools is not None:
            return iter([("text", "unused")])
        self.opened.set()

        def events():
            self.closed.wait(5)
            return
            yield

        return EventStream(events(), self.closed.set)


class CompactionTests(ProviderConfigMixin, unittest.TestCase):
    def test_stop_during_auto_compaction_is_immediate_and_keeps_the_prompt(self):
        agent = Agent(self.workspace)
        agent.tokens.context_window = 1_000  # the tool schema alone is over 80%
        agent.conversation_history += [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old reply"}]
        agent.adapter = BlockingSummaryAdapter()
        result = []
        worker = threading.Thread(target=lambda: result.append(agent.run("new request")))
        worker.start()
        self.assertTrue(agent.adapter.opened.wait(5))
        agent.interrupt()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["[Interrupted]"])
        self.assertEqual(agent.conversation_history[-1], {"role": "user", "content": "new request" + date_note(date.today())})
        self.assertEqual(agent.conversation_history[1:3], [{"role": "user", "content": "old"},
                                                           {"role": "assistant", "content": "old reply"}])
        self.assertTrue(agent._interrupted_pending)

    def test_summary_request_is_retried_like_normal_requests(self):
        agent = Agent(self.workspace)
        agent.API_RETRY_BASE_DELAY = 0
        agent.conversation_history += [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]

        def overloaded(messages):
            raise providers.ProviderError("overloaded_error: busy", 529)

        agent.adapter = ScriptAdapter(overloaded, lambda m: [("text", "SUMMARY")])
        self.assertTrue(agent.compact().startswith("Compacted"))
        self.assertEqual(agent.conversation_history[1], {"role": "user", "content": "q1"})
        self.assertIn("SUMMARY", agent.conversation_history[2]["content"])

    def test_previous_summary_is_sent_once(self):
        agent = Agent(self.workspace)
        agent.tokens.context_window = 5_000  # keeps ~1,000 tokens: the last reply
        agent.conversation_history += [
            {"role": "user", "content": "[Conversation compacted — summary of prior history]\n\nOLD-SUMMARY-TEXT"},
            {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "q3"}, {"role": "assistant", "content": "word " * 1500},
        ]
        agent.adapter = ScriptAdapter(lambda m: [("text", "NEW SUMMARY")])
        self.assertTrue(agent.compact().startswith("Compacted"))
        prompt = agent.adapter.requests[0][1]["content"]
        self.assertEqual(prompt.count("OLD-SUMMARY-TEXT"), 1)
        self.assertIn("<previous-summary>", prompt)
        self.assertIn("User: q2", prompt)

    def test_auto_compaction_uses_provider_reported_prompt_tokens(self):
        # Screenshots cost far more than the tiktoken estimate: the provider
        # said 97% while the estimate said a few percent.
        agent = Agent(self.workspace)
        window = agent.tokens.context_window
        notices = []
        agent.on_compact = notices.append
        agent.adapter = ScriptAdapter(
            lambda m: [tool_call("call_1", "list_skills", {}), ("usage", int(window * 0.97), 5, 0)],
            lambda m: [("text", "SUMMARY")],
            lambda m: [("text", "All done.")],
        )
        self.assertEqual(agent.run("look at the screenshot"), "All done.")
        self.assertTrue(any(n.startswith("Compacted") for n in notices))
        self.assertEqual(agent.adapter.requests[1][0]["content"], Agent.COMPACT_SYSTEM_PROMPT)
        self.assertIsNone(agent._api_context)  # reset by compaction

    def test_images_without_low_detail_count_as_high_detail(self):
        def image(detail=None):
            url = {"url": "data:image/png;base64,AAAA"}
            if detail:
                url["detail"] = detail
            return [{"type": "image_url", "image_url": url}]

        self.assertEqual(TokenCounter._estimate_image_tokens(image()), 765)
        self.assertEqual(TokenCounter._estimate_image_tokens(image("auto")), 765)
        self.assertEqual(TokenCounter._estimate_image_tokens(image("high")), 765)
        self.assertEqual(TokenCounter._estimate_image_tokens(image("low")), 85)


class ToolErrorTests(ProviderConfigMixin, unittest.TestCase):
    def test_bad_value_key_error_is_not_reported_as_a_missing_argument(self):
        agent = Agent(self.workspace)
        result = json.loads(agent._execute_tool("desktop_window_action", {"action": "resize", "pid": 1}))
        self.assertFalse(result["success"])
        self.assertNotIn("Missing required argument", result["error"])
        self.assertIn("resize", result["error"])
        missing = json.loads(agent._execute_tool("desktop_window_action", {"action": "activate_window"}))
        self.assertIn("Missing required argument 'pid' for tool desktop_window_action", missing["error"])

    def test_repeated_identical_failures_warn_then_stop_the_run(self):
        # Reproduction: glm-5.3-flash sent execute_command {"is_background": false}
        # 301 times in one run.
        bad = {"is_background": False}
        agent = Agent(self.workspace)
        notices = []
        agent.on_compact = notices.append
        agent.adapter = ScriptAdapter(*[
            (lambda m, i=i: [tool_call(f"call_{i}", "execute_command", bad)]) for i in range(10)
        ])
        reply = agent.run("run the tests")
        self.assertEqual(len(agent.adapter.requests), 5)
        self.assertTrue(reply.startswith("[Stopped: execute_command failed 5 times in a row"), reply)
        self.assertIn("'command'", reply)
        # execute_command results are plain text; the warning is a "Warning:" paragraph.
        results = [m["content"] for m in agent.conversation_history if m.get("role") == "tool"]
        self.assertEqual(["Warning:" in r for r in results], [False, False, True, False, False])
        self.assertIn("2 more identical failures", results[2])
        self.assertEqual(len(notices), 1)
        # The stop is the run's reply, so a resume does not continue the loop.
        self.assertEqual(agent.conversation_history[-1], {"role": "assistant", "content": reply})

    def test_stop_after_a_failing_command_quotes_its_exit_code_and_output(self):
        # A command that ran and failed has no "error", only its output.
        agent = Agent(self.workspace)
        agent.execute_command_tool = mock.Mock(return_value=mock.Mock(to_dict=lambda: {
            "success": False, "output": "Exit code: 2\nWall time: 0.1s\nOutput:\nboom", "error": None}))
        agent.adapter = ScriptAdapter(*[
            (lambda m, i=i: [tool_call(f"call_{i}", "execute_command", {"command": "make"})]) for i in range(5)])
        reply = agent.run("build")
        self.assertTrue(reply.startswith("[Stopped: execute_command failed 5 times in a row"), reply)
        self.assertIn("Last error: Exit code: 2\nWall time: 0.1s\nOutput:\nboom]", reply)

    def test_a_different_call_resets_the_failure_count(self):
        bad = {"is_background": False}
        steps = [bad, bad, {"terminal_id": 99, "command": "x", "is_background": False}, bad, bad, bad]
        agent = Agent(self.workspace)
        agent.adapter = ScriptAdapter(
            *[(lambda m, i=i, a=a: [tool_call(f"call_{i}", "execute_command", a)]) for i, a in enumerate(steps)],
            lambda m: [("text", "Gave up.")],
        )
        self.assertEqual(agent.run("go"), "Gave up.")
        results = [m["content"] for m in agent.conversation_history if m.get("role") == "tool"]
        # Calls 0-1 and 3-5 are separate streaks; only the 3rd of the second one warns.
        self.assertEqual(["Warning:" in r for r in results], [False, False, False, False, False, True])

    def test_stop_mid_batch_answers_every_call(self):
        bad = {"is_background": False}
        agent = Agent(self.workspace)
        agent.adapter = ScriptAdapter(
            *[(lambda m, i=i: [tool_call(f"call_{i}", "execute_command", bad)]) for i in range(4)],
            lambda m: [("tool_call", 0, "call_a", "execute_command", json.dumps(bad)),
                       ("tool_call", 1, "call_b", "list_skills", "{}")],
        )
        self.assertTrue(agent.run("go").startswith("[Stopped:"))
        assert_valid_chains(self, agent.conversation_history)


if __name__ == "__main__":
    unittest.main()


class HostStopAndResetTests(ProviderConfigMixin, unittest.TestCase):
    def test_clear_stop_drops_a_stale_stop(self):
        # The gateway calls clear_stop() when a stop arrives after its run ended,
        # so the next run is not cancelled by it.
        agent = Agent(self.workspace)
        agent.interrupt()
        self.assertTrue(agent._interrupt_event.is_set())
        agent.clear_stop()
        self.assertFalse(agent._interrupt_event.is_set())

    def test_reset_closes_the_browser_even_without_an_open_window(self):
        # The browser worker can be alive with no window (failed launch, window closed by hand).
        agent = Agent(self.workspace)
        closed = []
        agent.browser_manager.close = lambda: closed.append(True)
        self.assertFalse(agent.browser_manager.is_open)
        agent.reset()
        self.assertEqual(closed, [True])


class BrokenToolCallTests(ProviderConfigMixin, unittest.TestCase):
    BROKEN = '{"path": "out.txt", "content": "line one\nline two'  # raw newline, no closing quote

    def results(self, agent):
        return [json.loads(m["content"]) for m in agent.conversation_history if m.get("role") == "tool"]

    def test_unparseable_arguments_are_reported_and_not_run(self):
        agent = Agent(self.workspace)
        agent.adapter = ScriptAdapter(lambda m: [("tool_call", 0, "call_1", "write", self.BROKEN)],
                                      lambda m: [("text", "Fixed.")])
        self.assertEqual(agent.run("write it"), "Fixed.")
        error = self.results(agent)[0]["error"]
        self.assertTrue(error.startswith("Could not parse the arguments for write as JSON ("), error)
        self.assertNotIn("Missing required argument", error)
        self.assertFalse((Path(self.workspace) / "out.txt").exists())
        # The stored call has valid (empty) arguments, so the history stays sendable.
        call = agent.conversation_history[2]["tool_calls"][0]["function"]
        self.assertEqual(call["arguments"], "{}")
        assert_valid_chains(self, agent.adapter.requests[1])

    def test_arguments_that_are_not_an_object_are_reported_and_not_run(self):
        agent = Agent(self.workspace)
        agent.adapter = ScriptAdapter(lambda m: [("tool_call", 0, "call_1", "write", "null")],
                                      lambda m: [("tool_call", 0, "call_2", "write", '["out.txt", "x"]')],
                                      lambda m: [("text", "Fixed.")])
        self.assertEqual(agent.run("write it"), "Fixed.")
        for result in self.results(agent):
            self.assertTrue(result["error"].startswith("The arguments for write must be a JSON object."), result)
        calls = [m["tool_calls"][0]["function"]["arguments"] for m in agent.conversation_history if m.get("tool_calls")]
        self.assertEqual(calls, ["{}", "{}"])
        self.assertFalse((Path(self.workspace) / "out.txt").exists())

    def test_repeated_unparseable_arguments_stop_the_run(self):
        agent = Agent(self.workspace)
        agent.adapter = ScriptAdapter(*[
            (lambda m, i=i: [("tool_call", 0, f"call_{i}", "write", self.BROKEN)]) for i in range(10)])
        reply = agent.run("write it")
        self.assertTrue(reply.startswith("[Stopped: write failed 5 times in a row"), reply)
        self.assertEqual(len(agent.adapter.requests), 5)

    def test_calls_of_a_cut_off_reply_are_not_run(self):
        agent = Agent(self.workspace)
        agent.adapter = ScriptAdapter(
            lambda m: [tool_call("call_1", "write", {"path": "out.txt", "content": "x"}), ("truncated",)],
            lambda m: [("text", "Split it.")])
        self.assertEqual(agent.run("write it"), "Split it.")
        self.assertEqual(self.results(agent)[0]["error"], TRUNCATED_CALL_ERROR)
        self.assertFalse((Path(self.workspace) / "out.txt").exists())

    def test_light_repair_against_the_schema(self):
        agent = Agent(self.workspace)
        repair = agent._repair_tool_call
        self.assertEqual(repair("Read_Logs", {"terminal_id": "2", "tail": " 20 ", "start_line": None}),
                         ("read_logs", {"terminal_id": 2, "tail": 20}))
        self.assertEqual(repair("new_terminal", {"background": "False"}), ("new_terminal", {"background": False}))
        self.assertEqual(repair("desktop_hotkey", {"pid": 1, "keys": '["ctrl", "c"]', "x": "1.5"}),
                         ("desktop_hotkey", {"pid": 1, "keys": ["ctrl", "c"], "x": 1.5}))
        # Values that are not the schema's type stay as sent; a required null stays.
        self.assertEqual(repair("read_logs", {"terminal_id": None, "tail": "true", "end_line": "ten"}),
                         ("read_logs", {"terminal_id": None, "tail": "true", "end_line": "ten"}))
        self.assertEqual(repair("search", {"pattern": "20"}), ("search", {"pattern": "20"}))
        self.assertEqual(repair("no_such_tool", "x"), ("no_such_tool", "x"))
        # Through dispatch: the repaired call runs.
        result = json.loads(agent._execute_tool("New_Terminal", {"background": "false"}))
        self.assertTrue(result["success"], result)


class RetryPolicyTests(ProviderConfigMixin, unittest.TestCase):
    def test_only_transient_errors_are_retried(self):
        from openai import RateLimitError

        error = providers.ProviderError
        for status in (408, 429, 500, 520, 529):
            self.assertTrue(Agent._is_retryable_error(error("HTTP error", status)), status)
        for status in (400, 401, 402, 403, 404):
            self.assertFalse(Agent._is_retryable_error(error("HTTP error", status)), status)
        self.assertFalse(Agent._is_retryable_error(error("HTTP 400: prompt is too long", 400)))
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        quota = RateLimitError("Error code: 429 - You exceeded your current quota (insufficient_quota)",
                               response=httpx.Response(429, request=request), body=None)
        self.assertFalse(Agent._is_retryable_error(quota))
        limited = RateLimitError("Rate limit reached", response=httpx.Response(429, request=request), body=None)
        self.assertTrue(Agent._is_retryable_error(limited))

    def test_backoff_and_retry_after(self):
        from openai import RateLimitError

        agent = Agent(self.workspace)
        self.assertEqual(agent.API_MAX_RETRIES, 5)
        delays = [agent._retry_delay(n) for n in range(6)]
        self.assertTrue(1.6 <= delays[0] <= 2.4, delays)
        self.assertTrue(all(24 <= d <= 36 for d in delays[4:]), delays)  # capped near 30 s
        error = providers.ProviderError
        self.assertEqual(agent._retry_delay(0, error=error("slow", 429, {"retry-after": "3"})), 3.0)
        self.assertEqual(agent._retry_delay(0, error=error("slow", 429, {"retry-after-ms": "1500"})), 1.5)
        self.assertLess(agent._retry_delay(0, error=error("slow", 429, {"retry-after": "3600"})), 3)  # too long: backoff
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        sdk = RateLimitError("slow", response=httpx.Response(429, request=request, headers={"retry-after": "4"}), body=None)
        self.assertEqual(agent._retry_delay(0, error=sdk), 4.0)

    def test_session_id_is_sent_and_changes_on_reset(self):
        agent = Agent(self.workspace)
        agent.adapter = ScriptAdapter(lambda m: [("text", "One.")], lambda m: [("text", "Two.")])
        agent.run("first")
        first = agent.adapter.session_id
        self.assertEqual(first, agent.session_id)
        agent.run("second")
        self.assertEqual(agent.adapter.session_id, first)
        agent.reset()
        self.assertNotEqual(agent.session_id, first)


class ExecuteCommandResultTests(ProviderConfigMixin, unittest.TestCase):
    def test_result_is_plain_text_and_terminal_id_is_optional(self):
        agent = Agent(self.workspace)
        agent.adapter = ScriptAdapter(
            lambda m: [tool_call("c1", "execute_command", {"command": f'"{sys.executable}" -c "print(1); print(2)"'})],
            lambda m: [tool_call("c2", "execute_command", {"terminal_id": 99, "command": "echo x"})],
            lambda m: [("text", "done")],
        )
        self.assertEqual(agent.run("go"), "done")
        ran, missing = [m["content"] for m in agent.conversation_history if m.get("role") == "tool"]
        # Real newlines, not a JSON blob; the default blocking terminal ran it.
        self.assertTrue(ran.startswith("Exit code: 0\nWall time: "), ran)
        self.assertTrue(ran.endswith("Output:\n1\n2\n"), ran)
        self.assertEqual(missing, "Error: Terminal 99 not found")
        # Other tools keep their JSON results.
        self.assertEqual(json.loads(agent._execute_tool("list_skills", {}))["success"], True)
        self.assertEqual(Agent._tool_summary("execute_command", {"command": "dir"}), "run: dir")

    def test_warned_plain_text_result_keeps_its_saved_output_marker(self):
        # A capped plain-text result plus the repeated-failure warning is a
        # little over the cap; history normalization must not truncate it
        # again (that cut out the saved-output path).
        agent = Agent(self.workspace)
        agent.max_tool_result_chars = 2000
        notices = []
        agent.on_compact = notices.append
        failing = mock.Mock(return_value=mock.Mock(to_dict=lambda: {
            "success": False, "output": "Exit code: 1\nWall time: 0.1s\nOutput:\n" + "x" * 5000, "error": None}))
        agent.execute_command_tool = failing
        agent.adapter = ScriptAdapter(
            *[(lambda m, i=i: [tool_call(f"c{i}", "execute_command", {"command": "fail"})]) for i in range(3)],
            lambda m: [("text", "done")])
        with mock.patch.object(Agent, "TOOL_OUTPUT_DIR", Path(self.workspace) / "tool-output"):
            self.assertEqual(agent.run("go"), "done")
        sent = [m for m in agent.adapter.requests[3] if m.get("role") == "tool"][-1]["content"]
        self.assertIn("Warning: This exact call has now failed 3 times", sent)
        self.assertIn("full output saved to", sent)
        self.assertFalse([n for n in notices if "normalized" in n], notices)


def schema_names(agent, **kwargs):
    return {t["function"]["name"] for t in agent._get_tool_schema(**kwargs)}


class ToolGroupTests(ProviderConfigMixin, unittest.TestCase):
    def test_browser_and_desktop_tools_are_off_by_default(self):
        agent = Agent(self.workspace)
        names = schema_names(agent)
        self.assertFalse([n for n in names if n.startswith(("browser_", "desktop_"))])
        self.assertLessEqual({"read", "edit", "execute_command", "spawn_subagent", "load_skill", "enable_tools"}, names)
        # The guides moved to the enable_tools result; one line remains.
        self.assertNotIn("## Browser Tools", agent.system_prompt)
        self.assertNotIn("## Native Desktop Tools", agent.system_prompt)
        self.assertIn("`enable_tools`", agent.system_prompt)

    def test_enable_tools_turns_a_group_on_and_returns_its_guide(self):
        agent = Agent(self.workspace)
        result = json.loads(agent._execute_tool("enable_tools", {"group": "browser"}))
        self.assertTrue(result["success"])
        self.assertIn("browser_navigate", result["output"])
        names = schema_names(agent)
        self.assertEqual(len([n for n in names if n.startswith("browser_")]), 20)
        self.assertFalse([n for n in names if n.startswith("desktop_")])
        bad = json.loads(agent._execute_tool("enable_tools", {"group": "printer"}))
        self.assertFalse(bad["success"])
        self.assertIn("browser, desktop", bad["error"])

    def test_calling_a_tool_of_a_disabled_group_enables_it_and_runs(self):
        agent = Agent(self.workspace)
        result = json.loads(agent._execute_tool("desktop_window_action", {"action": "activate_window"}))
        self.assertIn("Missing required argument 'pid'", result["error"])  # it ran
        self.assertIn("desktop_window_action", schema_names(agent))

    def test_groups_are_restored_from_history(self):
        agent = Agent(self.workspace)

        def call(name, args):
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}

        agent.conversation_history += [{"role": "user", "content": "go"}, call("browser_snapshot", {}), tool_result("c")]
        self.assertIn("browser_snapshot", schema_names(agent))
        agent.conversation_history += [call("enable_tools", {"group": "desktop"}), tool_result("c")]
        self.assertIn("desktop_click", schema_names(agent))

    def test_groups_survive_compaction_and_reload(self):
        agent = Agent(self.workspace)
        agent._execute_tool("enable_tools", {"group": "desktop"})
        agent.conversation_history += [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
        agent.adapter = ScriptAdapter(lambda m: [("text", "SUMMARY")])
        self.assertTrue(agent.compact().startswith("Compacted"))
        reloaded = Agent(self.workspace)
        reloaded.conversation_history = list(agent.conversation_history)
        self.assertIn("desktop_click", schema_names(reloaded))
        self.assertNotIn("browser_snapshot", schema_names(reloaded))

    def test_subagents_can_enable_desktop_but_not_browser(self):
        agent = Agent(self.workspace)
        agent._is_subagent = True
        enable = next(t for t in agent._get_tool_schema() if t["function"]["name"] == "enable_tools")
        self.assertEqual(enable["function"]["parameters"]["properties"]["group"]["enum"], ["desktop"])
        self.assertFalse(json.loads(agent._execute_tool("enable_tools", {"group": "browser"}))["success"])
        self.assertTrue(json.loads(agent._execute_tool("enable_tools", {"group": "desktop"}))["success"])
        names = schema_names(agent)
        self.assertIn("desktop_click", names)
        self.assertFalse([n for n in names if n.startswith("browser_")])

    def test_reset_turns_groups_off(self):
        agent = Agent(self.workspace)
        agent.browser_manager.close = lambda: None
        agent._execute_tool("enable_tools", {"group": "browser"})
        agent.reset()
        self.assertNotIn("browser_snapshot", schema_names(agent))


class DateNoteTests(ProviderConfigMixin, unittest.TestCase):
    def test_date_is_added_only_when_it_changed(self):
        agent = Agent(self.workspace)
        agent.adapter = ScriptAdapter(lambda m: [("text", "one")], lambda m: [("text", "two")],
                                      lambda m: [("text", "three")])
        with mock.patch("kairos.agent.date") as fake:
            fake.today.return_value = date(2026, 10, 20)
            agent.run("first")
            agent.run("second")
            fake.today.return_value = date(2026, 10, 21)
            agent.run("third")
        users = [m["content"] for m in agent.conversation_history if m["role"] == "user"]
        self.assertEqual(users, ["first\n\n[Current date: 2026-10-20 (Tuesday)]", "second",
                                 "third\n\n[Current date: 2026-10-21 (Wednesday)]"])
        # The system prompt does not change with the date.
        self.assertNotIn("2026-10", agent.system_prompt)

    def test_environment_is_in_the_system_prompt(self):
        agent = Agent(self.workspace)
        self.assertIn("## Environment\n- OS: ", agent.system_prompt)
        self.assertIn("cmd.exe" if sys.platform == "win32" else "bash", agent.system_prompt)
        self.assertIn(f"Your current workspace is: {agent.cwd}", agent.system_prompt)

    def test_displays_strip_the_date_note(self):
        from kairos.gateway.history import visible_history
        from kairos.gateway.repository import GatewayRepository

        history = [{"role": "system", "content": "s"},
                   {"role": "user", "content": "hi" + date_note(date(2026, 10, 20))},
                   {"role": "assistant", "content": "hello"}]
        self.assertEqual(visible_history(history)[0], {"role": "user", "content": "hi"})
        self.assertEqual(GatewayRepository._preview(history), "hi")
        # A message with an image has the note in its text part.
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}
        with_image = [{"role": "user", "content": [
            {"type": "text", "text": "look" + date_note(date(2026, 10, 20))}, image]}]
        self.assertEqual(visible_history(with_image)[0]["content"], [{"type": "text", "text": "look"}, image])


class ToolSchemaGuardTests(ProviderConfigMixin, unittest.TestCase):
    # OpenRouter's Wafer host (schema-constrained decoding) broke on a oneOf in
    # execute_command: GLM-5.3-flash produced only {"is_background": false, ...}
    # for 110k characters, in every call. Hosts that compile the schema into a
    # grammar handle combinators and conditionals badly, so none may come back.
    BANNED = {"oneOf", "anyOf", "allOf", "$ref", "if", "then", "else", "not", "const"}

    def find_banned(self, schema, path):
        found = []
        if isinstance(schema, dict):
            for key, value in schema.items():
                if key in self.BANNED:
                    found.append(f"{path}.{key}")
                if key == "properties" and isinstance(value, dict):
                    # Property names are not keywords: check only their schemas.
                    for name, sub in value.items():
                        found += self.find_banned(sub, f"{path}.properties.{name}")
                else:
                    found += self.find_banned(value, f"{path}.{key}")
        elif isinstance(schema, list):
            for i, item in enumerate(schema):
                found += self.find_banned(item, f"{path}[{i}]")
        return found

    def test_no_tool_schema_uses_combinators_or_conditionals(self):
        tools = Agent(self.workspace)._get_tool_schema(all_groups=True)
        self.assertGreater(len(tools), 50)  # every group is checked
        found = []
        for tool in tools:
            found += self.find_banned(tool["function"].get("parameters", {}), tool["function"]["name"])
        self.assertEqual(found, [])

    def test_guard_detects_nested_keywords(self):
        schema = {"type": "object", "properties": {"not": {"type": "array", "items": {"anyOf": []}}}}
        self.assertEqual(self.find_banned(schema, "t"), ["t.properties.not.items.anyOf"])
