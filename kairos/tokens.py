"""Token counting and display using tiktoken."""

import json
from typing import Any

import tiktoken


class TokenCounter:
    """Tracks token usage across a session and per-turn.

    The context count is deliberately conservative.  It includes message
    metadata and assistant tool-call arguments, not only visible text, because
    those fields are sent to the chat-completions endpoint too.  Tool schema
    tokens are supplied by :class:`kairos.agent.Agent` as ``extra_tokens``.
    """

    DEFAULT_CONTEXT_WINDOW = 262_000

    def __init__(self, model: str = "gpt-4o", context_window: int | None = None):
        # Try to get the encoding for the model; fall back to cl100k_base.
        try:
            self._enc = tiktoken.encoding_for_model(model)
        except KeyError:
            self._enc = tiktoken.get_encoding("cl100k_base")

        # Session totals
        self.session_input = 0
        self.session_output = 0

        # Context window usage (tokens currently in conversation_history)
        self.context_tokens = 0

        # Per-turn counters (reset each turn)
        self.turn_input = 0
        self.turn_output = 0

        # The configured budget is intentionally conservative for compatible
        # gateways whose advertised model context may be larger than the
        # deployment actually accepts.
        if context_window is None:
            try:
                from .config import Config

                context_window = Config.CONTEXT_WINDOW()
            except (ImportError, AttributeError):
                context_window = self.DEFAULT_CONTEXT_WINDOW
        try:
            context_window = int(context_window)
        except (TypeError, ValueError):
            context_window = self.DEFAULT_CONTEXT_WINDOW
        self.context_window = max(1, context_window)

    def _encode_len(self, value: Any) -> int:
        """Return the encoded length of a value without raising on odd data."""
        if value is None:
            return 0
        if not isinstance(value, str):
            try:
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                value = str(value)
        return len(self._enc.encode(value))

    @staticmethod
    def _extract_text(content) -> str:
        """Extract plain text from message content (string or vision array)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "\n".join(parts)
        return ""

    @staticmethod
    def _estimate_image_tokens(content: list) -> int:
        """Estimate tokens for image_url blocks in a vision content array.

        Image billing is based on image dimensions/detail rather than the raw
        base64 URL length.  Keep a fixed conservative estimate and do not turn
        the URL into text tokens: an inline screenshot can be hundreds of
        thousands of characters while costing only a bounded vision budget.
        """
        tokens = 0
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            image = block.get("image_url", {})
            if not isinstance(image, dict):
                image = {}
            detail = image.get("detail")
            tokens += 765 if detail == "high" else 85
        return tokens

    def count_message(self, msg: dict) -> int:
        """Count the fields from one message that contribute to API context.

        In particular, assistant ``tool_calls`` and tool message IDs/names are
        included.  They were previously omitted when assistant content was
        non-empty, which could make the displayed context materially smaller
        than the request sent to the provider.
        """
        if not isinstance(msg, dict):
            return self._encode_len(msg) + 4

        total = 0
        content = msg.get("content", "")
        if isinstance(content, str):
            total += self._encode_len(content)
        elif isinstance(content, list):
            total += self._encode_len(self._extract_text(content))
            total += self._estimate_image_tokens(content)
            # Count small non-image fields (for example image detail/type),
            # but never count the raw image URL as text.
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "image_url":
                    image = block.get("image_url", {})
                    if isinstance(image, dict):
                        total += self._encode_len(block.get("type"))
                        total += self._encode_len(image.get("detail"))
                elif block.get("type") != "text":
                    total += self._encode_len(block)
        elif content is not None:
            total += self._encode_len(content)

        # These metadata fields are part of the serialized chat message.
        for key in ("role", "name", "tool_call_id"):
            total += self._encode_len(msg.get(key))

        tool_calls = msg.get("tool_calls") or []
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    total += self._encode_len(tool_call)
                    continue
                total += self._encode_len(tool_call.get("id"))
                total += self._encode_len(tool_call.get("type"))
                function = tool_call.get("function", {})
                if isinstance(function, dict):
                    total += self._encode_len(function.get("name"))
                    total += self._encode_len(function.get("arguments"))
                else:
                    total += self._encode_len(function)

        # ChatML/message JSON framing and field separators.  This is a
        # conservative approximation; exact provider usage replaces it when
        # the streaming response includes usage data.
        return total + 4

    def count_history(self, messages: list[dict]) -> int:
        """Count total message tokens in a conversation history."""
        return sum(self.count_message(msg) for msg in messages)

    def count_tools(self, tools: list[dict] | None) -> int:
        """Count function-tool definitions included alongside a request."""
        if not tools:
            return 0
        try:
            serialized = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            serialized = str(tools)
        # A small framing reserve covers the top-level tools parameter and
        # provider-specific wrappers not represented in the JSON itself.
        return self._encode_len(serialized) + 16

    def count_request(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        """Count messages plus optional function-tool definitions."""
        return self.count_history(messages) + self.count_tools(tools)

    def start_turn(self, messages: list[dict], extra_tokens: int = 0):
        """Called at the start of a turn — count input tokens."""
        self.turn_input = self.count_history(messages) + max(0, int(extra_tokens))
        self.context_tokens = self.turn_input
        self.turn_output = 0

    def add_output_tokens(self, text: str):
        """Accumulate output tokens during streaming (tiktoken estimate)."""
        self.turn_output += len(self._enc.encode(text or ""))

    def set_turn_from_api(self, prompt_tokens: int, completion_tokens: int):
        """Override turn counters with ground-truth API usage when available."""
        self.turn_input = max(0, int(prompt_tokens or 0))
        self.context_tokens = self.turn_input
        self.turn_output = max(0, int(completion_tokens or 0))

    def finish_turn(self):
        """Called when a turn completes — update session totals."""
        self.session_input += self.turn_input
        self.session_output += self.turn_output

    @property
    def context_pct(self) -> float:
        """Percentage of context window used."""
        if self.context_window == 0:
            return 0.0
        return (self.context_tokens / self.context_window) * 100

    def format_status(self) -> str:
        """Format a status line showing all token info."""
        si = f"{self.session_input:,}"
        so = f"{self.session_output:,}"
        ctx = f"{self.context_pct:.1f}%"
        ti = f"{self.turn_input:,}"
        to = f"{self.turn_output:,}"
        return (
            f"Session: {si} in / {so} out"
            f"  |  Context: {ctx}"
            f"  |  Turn: {ti} in / {to} out"
        )

    def format_turn_summary(self) -> str:
        """One-liner after a turn completes."""
        ti = f"{self.turn_input:,}"
        to = f"{self.turn_output:,}"
        return f"Tokens this turn: {ti} in / {to} out"
