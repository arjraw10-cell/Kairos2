import unittest
from unittest.mock import patch

import httpx

from kairos.agent import Agent
from kairos.tools.web_search import WebSearchTool


def _response(status, body):
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://api.tavily.com/search"))


class WebSearchToolTests(unittest.TestCase):
    def setUp(self):
        WebSearchTool.out_of_credits = False
        self.tool = WebSearchTool()
        self.tool.RATE_LIMIT_RETRY_DELAY = 0
        key = patch("kairos.tools.web_search._api_key", return_value="tvly-test")
        self.api_key = key.start()
        self.addCleanup(key.stop)
        self.addCleanup(setattr, WebSearchTool, "out_of_credits", False)

    def test_success_formats_answer_and_compact_results(self):
        body = {
            "answer": "Paris.",
            "results": [
                {"title": "France", "url": "https://example.com/fr", "content": "Paris is\n the capital. " + "x" * 500, "score": 0.9},
                {"title": "Europe", "url": "https://example.com/eu", "content": "Short."},
            ],
        }
        with patch("httpx.post", return_value=_response(200, body)) as post:
            result = self.tool("capital of France", max_results=50)

        self.assertTrue(result.success)
        lines = result.output.splitlines()
        self.assertEqual(lines[0], "Answer: Paris.")
        self.assertIn("1. France", lines)
        self.assertIn("   https://example.com/fr", lines)
        self.assertIn("   Paris is the capital. ", result.output)
        self.assertIn("2. Europe", lines)
        self.assertLess(len(result.output), 1000)
        sent = post.call_args.kwargs
        self.assertEqual(sent["headers"], {"Authorization": "Bearer tvly-test"})
        self.assertEqual(sent["json"]["max_results"], 10)
        self.assertEqual(sent["json"]["query"], "capital of France")

    def test_missing_key_points_to_browser(self):
        self.api_key.return_value = ""
        with patch("httpx.post") as post:
            result = self.tool("anything")
        self.assertFalse(result.success)
        self.assertIn("tavily_api_key", result.error)
        self.assertIn("browser", result.error)
        post.assert_not_called()

    def test_out_of_credits_disables_tavily_for_the_process(self):
        for status in (432, 433):
            WebSearchTool.out_of_credits = False
            body = {"detail": {"error": "This request exceeds your plan's set usage limit."}}
            with patch("httpx.post", return_value=_response(status, body)) as post:
                first = self.tool("one")
                second = WebSearchTool()("two")  # a different agent's tool
            self.assertFalse(first.success)
            self.assertIn("out of credits", first.error)
            self.assertIn("browser", first.error)
            self.assertIn("out of credits", second.error)
            self.assertEqual(post.call_count, 1)

    def test_rate_limit_retries_once_then_succeeds(self):
        responses = [_response(429, {"detail": {"error": "slow down"}}), _response(200, {"results": []})]
        with patch("httpx.post", side_effect=responses) as post:
            result = self.tool("query")
        self.assertTrue(result.success)
        self.assertEqual(post.call_count, 2)

    def test_rate_limit_twice_gives_browser_advice(self):
        with patch("httpx.post", return_value=_response(429, {"detail": {"error": "slow down"}})) as post:
            result = self.tool("query")
        self.assertFalse(result.success)
        self.assertIn("rate limiting", result.error)
        self.assertIn("browser", result.error)
        self.assertEqual(post.call_count, 2)
        self.assertFalse(WebSearchTool.out_of_credits)

    def test_other_errors_are_concise(self):
        with patch("httpx.post", return_value=_response(401, {"detail": {"error": "Unauthorized: missing or invalid API key."}})):
            result = self.tool("query")
        self.assertEqual(result.error, "Tavily error 401: Unauthorized: missing or invalid API key.")
        with patch("httpx.post", side_effect=httpx.ConnectError("no route")):
            result = self.tool("query")
        self.assertIn("request failed: no route", result.error)


class WebSearchWiringTests(unittest.TestCase):
    def test_schema_for_agents_and_subagents_and_summary(self):
        agent = Agent.__new__(Agent)
        agent.web_search_tool = WebSearchTool()
        for is_subagent in (False, True):
            agent._is_subagent = is_subagent
            names = [tool["function"]["name"] for tool in agent._get_tool_schema()]
            self.assertIn("web_search", names)
        self.assertEqual(Agent._tool_summary("web_search", {"query": "news"}), "web search: 'news'")


if __name__ == "__main__":
    unittest.main()
