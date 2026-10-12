"""Web search through Tavily's REST API (POST https://api.tavily.com/search)."""
import time

import httpx

from .base import ToolResult

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
USE_BROWSER = ("Use the browser to search instead "
               "(sub-agents have no browser: say in your result that web search was unavailable).")
SNIPPET_CHARS = 300


def _api_key() -> str:
    from ..providers import load_config
    try:
        config = load_config() or {}
    except Exception:
        return ""
    return config.get("tavily_api_key") or ""


def _error_text(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
        if isinstance(detail, dict):
            return str(detail.get("error") or detail)
        return str(detail or response.text)
    except ValueError:
        return response.text


class WebSearchTool:
    """Search the web with Tavily and return a compact answer plus results."""

    RATE_LIMIT_RETRY_DELAY = 2.0
    # Set when Tavily reports 432 (plan limit) or 433 (pay-as-you-go limit);
    # shared by every agent so the process stops calling Tavily.
    out_of_credits = False

    def __call__(self, query: str, max_results: int = 5) -> ToolResult:
        if WebSearchTool.out_of_credits:
            return ToolResult(False, "", f"Tavily is out of credits. {USE_BROWSER}")
        api_key = _api_key()
        if not api_key:
            return ToolResult(False, "", f"web_search is not configured (no tavily_api_key in provider.json). {USE_BROWSER}")

        body = {
            "query": query,
            "max_results": max(1, min(int(max_results), 10)),
            "include_answer": "basic",
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            response = httpx.post(TAVILY_SEARCH_URL, json=body, headers=headers, timeout=30)
            if response.status_code == 429:
                time.sleep(self.RATE_LIMIT_RETRY_DELAY)
                response = httpx.post(TAVILY_SEARCH_URL, json=body, headers=headers, timeout=30)
        except httpx.HTTPError as e:
            return ToolResult(False, "", f"web_search request failed: {e}. {USE_BROWSER}")

        status = response.status_code
        if status in (432, 433):
            WebSearchTool.out_of_credits = True
            return ToolResult(False, "", f"Tavily is out of credits ({_error_text(response)}). {USE_BROWSER}")
        if status == 429:
            return ToolResult(False, "", f"Tavily is rate limiting requests. {USE_BROWSER}")
        if status != 200:
            return ToolResult(False, "", f"Tavily error {status}: {_error_text(response)}")

        data = response.json()
        lines = []
        if data.get("answer"):
            lines += [f"Answer: {data['answer']}", ""]
        for i, result in enumerate(data.get("results") or [], 1):
            snippet = " ".join((result.get("content") or "").split())
            if len(snippet) > SNIPPET_CHARS:
                snippet = snippet[:SNIPPET_CHARS] + "..."
            lines += [f"{i}. {result.get('title', '')}", f"   {result.get('url', '')}", f"   {snippet}"]
        return ToolResult(True, "\n".join(lines) or "No results.")
