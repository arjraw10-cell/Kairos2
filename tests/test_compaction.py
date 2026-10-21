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
        agent._api_context = None
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


    def make_compacting_shell(self, history, context_window=5_000):
        """A shell whose compact() records the summarizer's input."""
        agent = self.make_agent_shell(context_window=context_window)
        agent._is_subagent = False
        agent.enabled_tool_groups = set()
        agent.conversation_history = history
        agent._get_tool_schema = lambda: []
        agent.summarized = []

        def generate(messages, previous_summary=None):
            agent.summarized.append((messages, previous_summary))
            return "NEW SUMMARY"

        agent._generate_summary = generate
        return agent

    def test_compaction_keeps_user_messages_verbatim_before_the_summary(self):
        call = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}
        dated = "second: keep THIS exact text\n\n[Current date: 2026-10-20 (Tuesday)]"
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "first request"},
            call,
            {"role": "tool", "name": "read", "tool_call_id": "c1", "content": "file text"},
            {"role": "user", "content": "[Background notification]\nSub-agent finished"},
            {"role": "user", "content": [{"type": "text", "text": "[Screenshot captured]"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]},
            {"role": "user", "content": dated},
            {"role": "assistant", "content": "done"},
            {"role": "assistant", "content": "word " * 1500},  # over the 1,000-token keep
        ]
        agent = self.make_compacting_shell(history)
        self.assertTrue(agent.compact().startswith("Compacted"))
        compacted = agent.conversation_history
        self.assertEqual(compacted[1:3], [{"role": "user", "content": "first request"},
                                          {"role": "user", "content": dated}])
        self.assertTrue(compacted[3]["content"].startswith(Agent.COMPACT_SUMMARY_HEADER))
        self.assertIn("NEW SUMMARY", compacted[3]["content"])
        self.assertEqual(compacted[4:], history[8:])  # the recent tail is untouched
        messages, previous = agent.summarized[0]
        self.assertIsNone(previous)
        self.assertEqual(messages, history[1:8])  # the summarizer still sees everything

    def test_oldest_user_messages_are_dropped_beyond_the_budget(self):
        agent = self.make_agent_shell()
        users = [{"role": "user", "content": f"request {i} " + "x " * 50} for i in range(4)]
        agent.COMPACT_USER_MESSAGES_TOKENS = agent.tokens.count_message(users[0]) * 2 + 1
        kept = agent._compacted_user_messages(users)
        self.assertEqual(kept, [{"role": "user", "content": "[Conversation compacted: 2 earlier user messages omitted]"},
                                users[2], users[3]])
        # A later compaction adds to the count of an earlier note.
        kept = agent._compacted_user_messages(kept + [users[0]])
        self.assertEqual(kept[0]["content"], "[Conversation compacted: 3 earlier user messages omitted]")
        self.assertEqual(kept[1:], [users[3], users[0]])

    def test_user_message_budget_is_capped_by_the_context_window(self):
        agent = self.make_agent_shell(context_window=1_000)  # a fifth: 200 tokens
        users = [{"role": "user", "content": "x " * 150} for _ in range(2)]
        self.assertEqual(len(agent._compacted_user_messages(users)), 2)  # note + newest

    def test_recompaction_keeps_earlier_user_messages_and_sends_the_summary_once(self):
        previous = f"{Agent.COMPACT_SUMMARY_HEADER}\n\nOLD SUMMARY"
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "u1"},
            {"role": "user", "content": previous},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "assistant", "content": "word " * 1500},  # over the 1,000-token keep
        ]
        agent = self.make_compacting_shell(history)
        agent.compact()
        messages, previous_summary = agent.summarized[0]
        self.assertEqual(previous_summary, previous)
        self.assertNotIn(history[2], messages)
        contents = [m["content"] for m in agent.conversation_history]
        self.assertEqual(contents[1:3], ["u1", "u2"])
        self.assertEqual(sum(c.startswith("[Conversation compacted") for c in contents), 1)

    def test_compacting_again_with_only_recent_messages_does_nothing(self):
        # The boundary fell among the kept user messages: summarizing them
        # alone added one more summary message per /compact.
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "u1"},
            {"role": "user", "content": "u2"},
            {"role": "user", "content": f"{Agent.COMPACT_SUMMARY_HEADER}\n\nOLD SUMMARY"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u3"},
            {"role": "assistant", "content": "a3"},
        ]
        agent = self.make_compacting_shell(list(history))
        self.assertEqual(agent.compact(), "Nothing to compact — all messages are recent.")
        self.assertEqual(agent.summarized, [])
        self.assertEqual(agent.conversation_history, history)

    def test_summary_prompt_no_longer_asks_for_user_messages(self):
        for prompt in (Agent.COMPACT_SUMMARY_PROMPT, Agent.COMPACT_UPDATE_PROMPT):
            self.assertNotIn("User Messages", prompt)
            self.assertIn("## Next Steps", prompt)


if __name__ == "__main__":
    unittest.main()
