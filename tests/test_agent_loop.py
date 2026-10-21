"""Agent loop robustness: tool-chain repair, background-event hand-back,
stops during compaction, provider-reported context, compaction prompt and
tool error messages. No network: adapters are scripted."""
import json
import threading
import unittest
from pathlib import Path

import httpx

from kairos import providers
from kairos.agent import Agent, APIRequestError, TRUNCATED_CALL_ERROR
from kairos.providers.base import EventStream
from kairos.resume import repair_tool_chains
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
        names = [t["function"]["name"] for t in Agent(self.workspace)._get_tool_schema()]
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
        self.assertEqual(agent.conversation_history[-1], {"role": "user", "content": "new request"})
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
        self.assertIn("SUMMARY", agent.conversation_history[1]["content"])

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
        self.assertIn("terminal_id", reply)
        results = [json.loads(m["content"]) for m in agent.conversation_history if m.get("role") == "tool"]
        self.assertEqual(["warning" in r for r in results], [False, False, True, False, False])
        self.assertIn("2 more identical failures", results[2]["warning"])
        self.assertEqual(len(notices), 1)
        # The stop is the run's reply, so a resume does not continue the loop.
        self.assertEqual(agent.conversation_history[-1], {"role": "assistant", "content": reply})

    def test_a_different_call_resets_the_failure_count(self):
        bad = {"is_background": False}
        steps = [bad, bad, {"terminal_id": 99, "command": "x", "is_background": False}, bad, bad, bad]
        agent = Agent(self.workspace)
        agent.adapter = ScriptAdapter(
            *[(lambda m, i=i, a=a: [tool_call(f"call_{i}", "execute_command", a)]) for i, a in enumerate(steps)],
            lambda m: [("text", "Gave up.")],
        )
        self.assertEqual(agent.run("go"), "Gave up.")
        results = [json.loads(m["content"]) for m in agent.conversation_history if m.get("role") == "tool"]
        # Calls 0-1 and 3-5 are separate streaks; only the 3rd of the second one warns.
        self.assertEqual(["warning" in r for r in results], [False, False, False, False, False, True])

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
