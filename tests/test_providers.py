"""Provider layer: config resolution, wire-format translation, streaming
normalization, key rotation and Agent model selection. No network."""
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import httpx

from kairos import providers
from kairos.agent import Agent
from kairos.providers import base as provider_base
from kairos.providers.anthropic import AnthropicAdapter
from kairos.providers.completions import CompletionsAdapter
from kairos.providers.responses import ResponsesAdapter, translate_messages as responses_messages

CONFIG = {
    "default_setup": "luna",
    "providers": {
        "openai": {"format": "responses", "base_url": "https://api.openai.com/v1", "api_keys": ["sk-a"]},
        "openrouter": {"format": "completions", "base_url": "https://openrouter.ai/api/v1", "api_keys": ["or-1", "or-2"]},
        "anthropic": {"format": "anthropic", "base_url": "https://api.anthropic.com", "api_keys": ["ant-1"]},
    },
    "setups": {
        "luna": {"provider": "openai", "model": "gpt-6-luna", "reasoning_efforts": ["none", "low", "high"], "default_effort": "high"},
        "glm": {"provider": "openrouter", "model": "z-ai/glm", "reasoning_efforts": ["low", "high"], "default_effort": "high"},
        "mimo": {"provider": "openrouter", "model": "xiaomi/mimo", "reasoning_efforts": ["on", "off"], "default_effort": "on"},
        "luna-chat": {"provider": "openai", "model": "gpt-6-luna", "format": "completions",
                      "base_url": "http://localhost:9/v1", "api_keys": ["sk-b"], "context_window": 100000},
    },
}


@contextmanager
def provider_config(config=CONFIG):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "provider.json"
        if config is not None:
            path.write_text(json.dumps(config), encoding="utf-8")
        with mock.patch("kairos.providers.config.config_path", return_value=path):
            yield path


TOOLS = [{"type": "function", "function": {"name": "read", "description": "Read a file",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
HISTORY = [
    {"role": "system", "content": "You are Kairos."},
    {"role": "user", "content": [
        {"type": "text", "text": "What is in this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]},
    {"role": "assistant", "content": "Let me read it.", "_thinking": [{"type": "thinking", "thinking": "hm", "signature": "sig"}],
     "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{\"path\": \"a.txt\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": "{\"success\": true}"},
    {"role": "user", "content": [{"type": "text", "text": "[Screenshot captured]"},
                                 {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}]},
]


class ResolveTests(unittest.TestCase):
    def test_default_named_and_override(self):
        with provider_config():
            spec = providers.resolve()
            self.assertEqual((spec.setup, spec.format, spec.model, spec.default_effort), ("luna", "responses", "gpt-6-luna", "high"))
            chat = providers.resolve("luna-chat")
            self.assertEqual((chat.format, chat.base_url, chat.api_keys, chat.context_window),
                             ("completions", "http://localhost:9/v1", ("sk-b",), 100000))
            self.assertIsNone(chat.reasoning_efforts)

    def test_ad_hoc_model(self):
        with provider_config():
            spec = providers.resolve(model="claude-opus-5-5", provider="anthropic")
            self.assertEqual((spec.setup, spec.format, spec.reasoning_efforts), (None, "anthropic", None))
            with self.assertRaises(providers.ProviderConfigError):
                providers.resolve(model="x")

    def test_errors_and_secrets(self):
        with provider_config():
            with self.assertRaisesRegex(ValueError, "Unknown setup 'nope'"):
                providers.resolve("nope")
            self.assertNotIn("sk-a", repr(providers.resolve()))
        with provider_config(None):
            with self.assertRaisesRegex(ValueError, "provider.example.json"):
                providers.load_config()

    def test_list_setups(self):
        with provider_config():
            setups = {s["name"]: s for s in providers.list_setups()}
            self.assertEqual(set(setups), set(CONFIG["setups"]))
            self.assertEqual(setups["mimo"], {"name": "mimo", "provider": "openrouter", "model": "xiaomi/mimo",
                                              "format": "completions", "reasoning_efforts": ["on", "off"], "default_effort": "on"})

    def test_validate_effort(self):
        with provider_config():
            self.assertEqual(providers.validate_effort(providers.resolve("glm"), "low"), "low")
            with self.assertRaisesRegex(ValueError, "Choose from: low, high"):
                providers.validate_effort(providers.resolve("glm"), "max")
            self.assertEqual(providers.validate_effort(providers.resolve("luna-chat"), "anything"), "anything")


class Status(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class KeyPoolTests(unittest.TestCase):
    def test_rotates_on_rate_limit_and_cools_key(self):
        pool = provider_base.KeyPool(["a", "b"])
        seen = []

        def call(key):
            seen.append(key)
            if key == "a":
                raise Status(429)
            return key

        self.assertEqual(pool.call(call), "b")
        self.assertEqual(pool.call(call), "b")  # "a" is cooling down
        self.assertEqual(seen, ["a", "b", "b"])

    def test_other_errors_do_not_rotate(self):
        pool = provider_base.KeyPool(["a", "b"])
        with self.assertRaises(Status):
            pool.call(lambda key: (_ for _ in ()).throw(Status(400)))

    def test_all_cooling_tries_soonest_and_raises_last(self):
        pool = provider_base.KeyPool(["a", "b"])
        with self.assertRaises(Status):
            pool.call(lambda key: (_ for _ in ()).throw(Status(402 if key == "a" else 401)))
        seen = []

        def ok(key):
            seen.append(key)
            return key

        self.assertEqual(pool.call(ok), "a")  # "a" recovers first
        self.assertEqual(seen, ["a"])

    def test_no_keys(self):
        with self.assertRaisesRegex(providers.ProviderError, "No API keys"):
            provider_base.KeyPool([]).call(lambda key: key)


def chunk(content=None, tool_calls=None, usage=None, choices=True):
    delta = NS(content=content, tool_calls=tool_calls)
    return NS(choices=[NS(delta=delta)] if choices else [], usage=usage)


def tc(index, id=None, name=None, arguments=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=arguments))


class CompletionsTests(unittest.TestCase):
    def setUp(self):
        self.ctx = provider_config()
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)

    def test_request_effort_mapping_and_private_fields(self):
        adapter = CompletionsAdapter(providers.resolve("glm"))
        kwargs = adapter.request_kwargs(HISTORY, TOOLS, "high")
        self.assertEqual(kwargs["reasoning_effort"], "high")
        self.assertEqual((kwargs["tools"], kwargs["tool_choice"], kwargs["stream"]), (TOOLS, "auto", True))
        self.assertNotIn("_thinking", kwargs["messages"][2])
        self.assertEqual(kwargs["messages"][2]["tool_calls"], HISTORY[2]["tool_calls"])
        off = adapter.request_kwargs(HISTORY, None, "off")
        self.assertEqual(off["extra_body"], {"reasoning": {"enabled": False}})
        self.assertNotIn("reasoning_effort", off)
        self.assertNotIn("tools", off)
        plain = adapter.request_kwargs(HISTORY, None, None)
        self.assertNotIn("reasoning_effort", plain)
        self.assertNotIn("extra_body", plain)

    def test_stream_events(self):
        chunks = [
            chunk(content="Hel"), chunk(content="lo"),
            chunk(tool_calls=[tc(0, "call_1", "read", '{"pa')]),
            chunk(tool_calls=[tc(0, None, None, 'th": "a"}')]),
            chunk(choices=False, usage=NS(prompt_tokens=10, completion_tokens=5)),
        ]
        self.assertEqual(list(CompletionsAdapter.events(chunks)), [
            ("text", "Hel"), ("text", "lo"),
            ("tool_call", 0, "call_1", "read", '{"pa'), ("tool_call", 0, "", "", 'th": "a"}'),
            ("usage", 10, 5, 0),
        ])

    def test_rate_limited_key_rotates(self):
        adapter = CompletionsAdapter(providers.resolve("glm"))
        used = []

        def create(key, **kwargs):
            used.append(key)
            if key == "or-1":
                raise Status(429)
            return NS(close=lambda: None, __iter__=None)

        class FakeClient:
            def __init__(self, key):
                self.chat = NS(completions=NS(create=lambda **kw: create(key, **kw)))

        with mock.patch("kairos.providers.completions.openai_client", side_effect=lambda spec, key, cache: FakeClient(key)):
            stream = adapter.stream(HISTORY, TOOLS, "high")
        self.assertEqual(used, ["or-1", "or-2"])
        stream.close()


class ResponsesTests(unittest.TestCase):
    def test_translate_messages(self):
        items = responses_messages(HISTORY)
        self.assertEqual(items[0], {"role": "system", "content": "You are Kairos."})
        self.assertEqual(items[1]["content"], [
            {"type": "input_text", "text": "What is in this?"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"},
        ])
        self.assertEqual(items[2], {"role": "assistant", "content": "Let me read it."})
        self.assertEqual(items[3], {"type": "function_call", "id": "fc_call_1", "call_id": "call_1",
                                    "name": "read", "arguments": "{\"path\": \"a.txt\"}"})
        self.assertEqual(items[4], {"type": "function_call_output", "call_id": "call_1", "output": "{\"success\": true}"})
        self.assertEqual(items[5]["content"][1]["image_url"], "https://example.com/x.png")

    def test_request_tools_and_effort(self):
        with provider_config():
            kwargs = ResponsesAdapter(providers.resolve()).request_kwargs(HISTORY, TOOLS, "high")
        self.assertEqual(kwargs["reasoning"], {"effort": "high"})
        self.assertEqual(kwargs["tools"], [{"type": "function", "name": "read", "description": "Read a file",
                                            "parameters": TOOLS[0]["function"]["parameters"]}])
        with provider_config():
            self.assertNotIn("reasoning", ResponsesAdapter(providers.resolve()).request_kwargs(HISTORY, None, None))

    def test_stream_events(self):
        events = [
            NS(type="response.created"),
            NS(type="response.output_item.added", output_index=0, item=NS(type="reasoning")),
            NS(type="response.output_text.delta", delta="Hi"),
            NS(type="response.output_item.added", output_index=2, item=NS(type="function_call", call_id="call_9", name="read")),
            NS(type="response.function_call_arguments.delta", output_index=2, delta='{"path":'),
            NS(type="response.function_call_arguments.delta", output_index=2, delta='"b"}'),
            NS(type="response.output_item.done", output_index=2, item=NS(type="function_call", arguments='{"path":"b"}')),
            NS(type="response.output_item.added", output_index=3, item=NS(type="function_call", call_id="call_10", name="write")),
            NS(type="response.output_item.done", output_index=3, item=NS(type="function_call", arguments="{}")),
            NS(type="response.completed", response=NS(usage=NS(input_tokens=20, output_tokens=7))),
        ]
        self.assertEqual(list(ResponsesAdapter.events(events)), [
            ("text", "Hi"),
            ("tool_call", 2, "call_9", "read", ""), ("tool_call", 2, "", "", '{"path":'), ("tool_call", 2, "", "", '"b"}'),
            ("tool_call", 3, "call_10", "write", ""), ("tool_call", 3, "", "", "{}"),
            ("usage", 20, 7, 0),
        ])

    def test_failed_response_raises(self):
        with self.assertRaisesRegex(providers.ProviderError, "boom"):
            list(ResponsesAdapter.events([NS(type="response.failed", response=NS(error=NS(message="boom")))]))


def sse(*payloads):
    return "".join(f"event: {p['type']}\ndata: {json.dumps(p)}\n\n" for p in payloads)


RECORDED_SSE = sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 30, "cache_read_input_tokens": 70, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Need the file."}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "EqQB"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "ping"},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Reading"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"path\": "}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "\"a.txt\"}"}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
)


class AnthropicTests(unittest.TestCase):
    def setUp(self):
        self.ctx = provider_config()
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.spec = providers.resolve(model="claude-opus-5-5", provider="anthropic")

    def test_request_translation(self):
        body = AnthropicAdapter(self.spec).request_body(HISTORY, TOOLS, "high")
        self.assertEqual(body["system"], "You are Kairos.")
        self.assertEqual((body["thinking"], body["output_config"]), ({"type": "adaptive"}, {"effort": "high"}))
        self.assertEqual(body["tools"], [{"name": "read", "description": "Read a file",
                                          "input_schema": TOOLS[0]["function"]["parameters"]}])
        messages = body["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[0]["content"][1], {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}})
        self.assertEqual(messages[1]["content"], [
            {"type": "thinking", "thinking": "hm", "signature": "sig"},
            {"type": "text", "text": "Let me read it."},
            {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "a.txt"}},
        ])
        # The tool result and the following screenshot merge into one user turn.
        self.assertEqual(messages[2]["content"][0], {"type": "tool_result", "tool_use_id": "call_1", "content": "{\"success\": true}"})
        self.assertEqual(messages[2]["content"][2], {"type": "image", "source": {"type": "url", "url": "https://example.com/x.png"}})
        self.assertNotIn("thinking", AnthropicAdapter(self.spec).request_body(HISTORY, None, None))

    def test_recorded_stream(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, text=RECORDED_SSE, headers={"content-type": "text/event-stream"})

        adapter = AnthropicAdapter(self.spec, transport=httpx.MockTransport(handler))
        stream = adapter.stream(HISTORY, TOOLS, "high")
        events = list(stream)
        stream.close()
        self.assertEqual(requests[0].url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(requests[0].headers["x-api-key"], "ant-1")
        self.assertEqual(events, [
            ("text", "Reading"),
            ("tool_call", 2, "toolu_1", "read", ""),
            ("tool_call", 2, "", "", "{\"path\": "),
            ("tool_call", 2, "", "", "\"a.txt\"}"),
            ("usage", 100, 42, 70),
            ("message_extra", "_thinking", [{"type": "thinking", "thinking": "Need the file.", "signature": "EqQB"}]),
        ])

    def test_http_error_and_stream_error(self):
        adapter = AnthropicAdapter(self.spec, transport=httpx.MockTransport(
            lambda request: httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}})))
        with self.assertRaises(providers.ProviderError) as caught:
            adapter.stream(HISTORY, None, None)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn("slow down", str(caught.exception))
        with self.assertRaises(providers.ProviderError) as caught:
            list(AnthropicAdapter.events([{"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}]))
        self.assertEqual(caught.exception.status_code, 529)
        self.assertTrue(Agent._is_retryable_error(caught.exception))

    def test_adapters_share_one_http_client(self):
        # One client per adapter leaked an open client per sub-agent.
        self.assertIs(AnthropicAdapter(self.spec)._client, AnthropicAdapter(self.spec)._client)
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        self.assertIsNot(AnthropicAdapter(self.spec, transport=transport)._client, AnthropicAdapter(self.spec)._client)


class FakeAdapter:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def stream(self, messages, tools, effort):
        self.calls.append((messages, tools, effort))
        return provider_base.EventStream(self.events, lambda: None)


def bare_agent(spec, events):
    agent = object.__new__(Agent)
    agent.spec, agent.model, agent.effort = spec, spec.model, spec.default_effort
    agent.adapter = FakeAdapter(events)
    agent.conversation_history = [{"role": "user", "content": "hi"}]
    agent._interrupt_event = threading.Event()
    agent._active_stream_lock = threading.Lock()
    agent._active_stream = None
    agent._get_tool_schema = lambda: TOOLS
    agent.on_stream_start = agent.on_stream_end = None
    agent.tokens_streamed = []
    agent.on_stream_token = agent.tokens_streamed.append
    return agent


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.ctx = provider_config()
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)

    def test_stream_response_assembles_normalized_events(self):
        agent = bare_agent(providers.resolve(), [
            ("text", "Let me "), ("text", "check."),
            ("tool_call", 1, "call_b", "write", '{"path": "x", '), ("tool_call", 1, "", "", '"content": "y"}'),
            ("tool_call", 0, "call_a", "read", '{"path": "a"}'),
            ("usage", 11, 3), ("message_extra", "_thinking", ["block"]),
        ])
        content, calls, usage = agent._stream_response()
        self.assertEqual(content, "Let me check.")
        self.assertEqual(agent.tokens_streamed, ["Let me ", "check."])
        self.assertEqual(calls, [
            {"id": "call_a", "name": "read", "arguments": {"path": "a"}},
            {"id": "call_b", "name": "write", "arguments": {"path": "x", "content": "y"}},
        ])
        self.assertEqual(usage, {"prompt_tokens": 11, "completion_tokens": 3, "cached_tokens": 0})
        self.assertEqual(agent._stream_extras, {"_thinking": ["block"]})
        self.assertEqual(agent.adapter.calls[0][1:], (TOOLS, "high"))

    def test_error_hints_for_keys_and_rate_limits(self):
        # AuthenticationError/RateLimitError are APIStatusErrors; the status
        # branch used to swallow their hints.
        from openai import AuthenticationError, RateLimitError

        agent = bare_agent(providers.resolve(), [])
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        auth = AuthenticationError("Incorrect API key", response=httpx.Response(401, request=request), body=None)
        text = agent._format_api_error(auth)
        self.assertIn("Status Code: 401", text)
        self.assertIn("Hint: Check the 'openai' API keys in provider.json", text)
        limited = RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)
        self.assertIn("rate-limited", agent._format_api_error(limited))
        self.assertIn("API keys", agent._format_api_error(providers.ProviderError("HTTP 401: invalid x-api-key", 401)))

    def test_model_selection(self):
        agent = bare_agent(providers.resolve(), [])
        agent.tokens = NS(context_window=1)
        self.assertEqual(agent.model_info(), {"setup": "luna", "provider": "openai", "model": "gpt-6-luna", "format": "responses",
                                              "effort": "high", "reasoning_efforts": ["none", "low", "high"]})
        agent.set_effort("none")
        with self.assertRaises(ValueError):
            agent.set_effort("max")
        agent.set_model("luna-chat")
        self.assertEqual((agent.model, agent.effort, agent.tokens.context_window), ("gpt-6-luna", None, 100000))
        self.assertIsInstance(agent.adapter, CompletionsAdapter)
        agent.set_model(model="claude-x", provider="anthropic")
        self.assertIsInstance(agent.adapter, AnthropicAdapter)
        self.assertEqual(agent.set_effort("max"), "Effort: max")  # unknown efforts pass through


if __name__ == "__main__":
    unittest.main()


class ContextAndCostTests(unittest.TestCase):
    def test_context_window_is_capped_at_300k_or_the_model_max(self):
        config = json.loads(json.dumps(CONFIG))
        config["setups"]["luna"]["max_context"] = 1_050_000
        with provider_config(config):
            self.assertEqual(providers.resolve("luna").context_window, 300_000)
            self.assertEqual(providers.resolve("luna-chat").context_window, 100_000)  # older "context_window" key
            self.assertEqual(providers.resolve(model="x", provider="openai").context_window, 300_000)

    def test_session_cost_uses_cached_rate(self):
        from kairos.tokens import TokenCounter, format_status_line

        counter = TokenCounter("model", 300_000, {"input": 2.0, "cached_input": 0.1, "output": 10.0})
        counter.set_turn_from_api(100_000, 1_000, 40_000)
        counter.finish_turn()
        data = counter.status_data()
        self.assertAlmostEqual(data["session_cost"], (60_000 * 2.0 + 40_000 * 0.1 + 1_000 * 10.0) / 1e6)
        self.assertEqual((data["context_tokens"], data["context_window"], data["turn_output"]), (100_000, 300_000, 1_000))
        self.assertEqual(format_status_line(data),
                         "Context 33.3% · 100,000 / 300,000 tokens · this call: 1,000 out · session: 100,000 in / 1,000 out · $0.1340")
        no_price = TokenCounter("model", 300_000)
        no_price.set_turn_from_api(10, 2)
        no_price.finish_turn()
        self.assertIsNone(no_price.status_data()["session_cost"])
        self.assertTrue(format_status_line(no_price.status_data()).endswith("cost n/a"))

    def test_cached_tokens_are_reported_by_each_format(self):
        completions = list(CompletionsAdapter.events([chunk(choices=False, usage=NS(
            prompt_tokens=10, completion_tokens=5, prompt_tokens_details=NS(cached_tokens=4)))]))
        self.assertEqual(completions, [("usage", 10, 5, 4)])
        responses = list(ResponsesAdapter.events([NS(type="response.completed", response=NS(usage=NS(
            input_tokens=20, output_tokens=7, input_tokens_details=NS(cached_tokens=12))))]))
        self.assertEqual(responses, [("usage", 20, 7, 12)])
