"""Token counting and display using tiktoken."""

import tiktoken


class TokenCounter:
    """Tracks token usage across a session and per-turn."""

    def __init__(self, model: str = "gpt-4o"):
        # Try to get the encoding for the model; fall back to cl100k_base
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

        # Estimated max context (conservative default)
        self.context_window = 262_000

    @staticmethod
    def _extract_text(content) -> str:
        """Extract plain text from message content (string or vision content array)."""
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

        Uses data URL length as a proxy for image size. Not exact, but
        close enough for context tracking — and when the API reports
        real usage, those numbers override this estimate entirely.
        """
        tokens = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "image_url":
                continue
            url = block.get("image_url", {}).get("url", "")
            if url.startswith("data:"):
                # Rough estimate: 85 base tokens + ~1 token per 2000 chars of data URL
                # Yields ~200 tokens for small images, ~1500-3000 for large photos
                tokens += 85 + max(0, len(url) // 2000)
            else:
                # External URL — can't know actual size, assume base tokens
                tokens += 85
        return tokens

    def count_message(self, msg: dict) -> int:
        """Count tokens in a single message dict.

        NOTE: Tool call arguments on assistant messages are intentionally
        NOT counted here. They were already counted as output tokens when
        generated (via add_output_tokens in step()). Counting them again
        here as input would double-count the same bytes across turns.
        """
        content = msg.get("content", "")
        # Extract text content
        text = self._extract_text(content)

        # Count image tokens if content is a vision content array
        image_tokens = 0
        if isinstance(content, list):
            image_tokens = self._estimate_image_tokens(content)

        # For messages with no extractable text at all, use str() as fallback
        if not text:
            text = str(msg)
        return len(self._enc.encode(text)) + image_tokens

    def count_history(self, messages: list[dict]) -> int:
        """Count total tokens in a list of messages."""
        total = 0
        for msg in messages:
            total += self.count_message(msg)
            # Overhead for message structure (~4 tokens per message)
            total += 4
        return total

    def start_turn(self, messages: list[dict]):
        """Called at the start of a turn — count input tokens."""
        self.turn_input = self.count_history(messages)
        self.context_tokens = self.turn_input
        self.turn_output = 0

    def add_output_tokens(self, text: str):
        """Accumulate output tokens during streaming (tiktoken estimate)."""
        n = len(self._enc.encode(text or ""))
        self.turn_output += n

    def set_turn_from_api(self, prompt_tokens: int, completion_tokens: int):
        """Override turn counters with ground-truth values from the API.

        Called when stream_options={"include_usage": True} returns real counts.
        The API's prompt_tokens replaces the tiktoken estimate entirely,
        and completion_tokens replaces the accumulated output estimate.
        """
        self.turn_input = prompt_tokens
        self.context_tokens = prompt_tokens
        self.turn_output = completion_tokens

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
        """Format a status line showing all token info.

        Layout:
          Session: X in / Y out  |  Context: Z%  |  Turn: A in / B out
        """
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
