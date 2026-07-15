import json
import unittest

from kairos.agent import Agent
from kairos.tokens import TokenCounter


class CompactionAccountingTests(unittest.TestCase):
    def make_agent_shell(self, context_window=262_000):
        agent = object.__new__(Agent)
        agent.tokens = TokenCounter("model", context_window=context_window)
        agent.max_tool_result_chars = 20_000
        agent.COMPACT_KEEP_RECENT_PCT = 0.20
        agent.COMPACT_RESERVE_TOKENS = 1_000
        agent.COMPACT_SUMMARY_PROMPT = "summarize"
        agent.COMPACT_UPDATE_PROMPT = "update"
        return agent

    def test_tool_metadata_and_schema_are_counted(self):
        counter = TokenCounter("model", context_window=262_000)
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "search",
                    "arguments": '{"pattern":"needle"}',
                },
            }],
        }
        tool = {
            "role": "tool",
            "name": "search",
            "tool_call_id": "call_1",
            "content": '{"success":true,"output":"result","error":null}',
        }
        plain_assistant = {"role": "assistant", "content": None}
        plain_tool = {"role": "tool", "content": ""}

        self.assertGreater(counter.count_message(assistant), counter.count_message(plain_assistant))
        self.assertGreater(counter.count_message(tool), counter.count_message(plain_tool))
        self.assertGreater(
            counter.count_tools([{"type": "function", "function": {"name": "search"}}]),
            0,
        )

    def test_inline_image_estimate_does_not_scale_with_base64_length(self):
        counter = TokenCounter("model", context_window=262_000)
        small = {
            "role": "user",
            "content": [{
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + "x" * 100},
            }],
        }
        large = {
            "role": "user",
            "content": [{
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + "x" * 5_000_000},
            }],
        }
        self.assertLess(counter.count_message(large) - counter.count_message(small), 10)

    def test_preflight_includes_pending_user_and_tools(self):
        agent = self.make_agent_shell()
        agent.conversation_history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old"},
        ]
        agent._get_tool_schema = lambda: [{"type": "function", "function": {"name": "x"}}]
        agent._refresh_context_tokens({"role": "user", "content": "new"})
        expected = agent.tokens.count_request(
            agent.conversation_history + [{"role": "user", "content": "new"}],
            agent._get_tool_schema(),
        )
        self.assertEqual(agent.tokens.context_tokens, expected)

    def test_compaction_prompt_is_bounded_with_head_and_tail(self):
        agent = self.make_agent_shell(context_window=20_000)
        prompt = "word " * 100_000
        capped, _ = agent._cap_compaction_prompt(prompt, None)
        fixed = len(agent.tokens._enc.encode("<conversation>\n\n</conversation>\n\nsummarize"))
        self.assertLessEqual(
            len(agent.tokens._enc.encode(capped)),
            20_000 - 1_000 - 1_024 - fixed,
        )
        self.assertIn("older summary input truncated", capped)

    def test_compact_boundary_does_not_split_tool_chain(self):
        agent = self.make_agent_shell(context_window=1_000)
        agent.conversation_history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "goal"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "name": "read",
                "tool_call_id": "call_1",
                "content": "result",
            },
        ]
        self.assertTrue(agent._is_safe_compact_boundary(2))
        self.assertFalse(agent._is_safe_compact_boundary(3))
        self.assertTrue(agent._is_safe_compact_boundary(4))

    def test_legacy_tool_result_is_normalized_before_request(self):
        agent = self.make_agent_shell()
        legacy = {
            "success": True,
            "output": "x" * 100_000,
            "error": None,
            "image_url": "data:image/png;base64," + "y" * 100_000,
        }
        agent.conversation_history = [{
            "role": "tool",
            "content": json.dumps(legacy),
        }]
        self.assertEqual(agent._normalize_history_for_context(), 1)
        normalized = json.loads(agent.conversation_history[0]["content"])
        self.assertNotIn("image_url", normalized)
        self.assertLessEqual(len(normalized["output"]), agent.max_tool_result_chars)
        self.assertIn("legacy image omitted", normalized["output"])


if __name__ == "__main__":
    unittest.main()
