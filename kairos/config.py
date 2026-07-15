import os
from functools import lru_cache

# Lazy-load .env file at first access instead of at import time
_load_dotenv_done = False


def _ensure_dotenv():
    global _load_dotenv_done
    if not _load_dotenv_done:
        from dotenv import load_dotenv

        load_dotenv()
        _load_dotenv_done = True


class Config:
    """Lazy-loaded configuration. .env is loaded on first attribute access."""

    DEFAULT_MAX_TOOL_RESULT_CHARS = 20_000
    DEFAULT_CONTEXT_WINDOW = 262_000
    DEFAULT_CONTEXT_RESERVE_TOKENS = 16_384

    @staticmethod
    def _get(key: str, default=None):
        _ensure_dotenv()
        return os.getenv(key, default)

    @classmethod
    @lru_cache(maxsize=1)
    def OPENAI_API_KEY(cls) -> str:
        _ensure_dotenv()
        key = os.getenv("OPENAI_API_KEY")
        # Don't cache failures
        if not key:
            cls.OPENAI_API_KEY.cache_clear()
        return key

    @classmethod
    @lru_cache(maxsize=1)
    def OPENAI_BASE_URL(cls) -> str:
        _ensure_dotenv()
        url = os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8082/v1")
        return url

    @classmethod
    @lru_cache(maxsize=1)
    def OPENAI_MODEL(cls) -> str:
        _ensure_dotenv()
        model = os.getenv("OPENAI_MODEL", "gpt-4o")
        return model

    @classmethod
    @lru_cache(maxsize=1)
    def MAX_TOOL_RESULT_CHARS(cls) -> int:
        """Maximum text characters retained for one model-facing tool result."""
        _ensure_dotenv()
        raw_value = os.getenv(
            "KAIROS_MAX_TOOL_RESULT_CHARS",
            str(cls.DEFAULT_MAX_TOOL_RESULT_CHARS),
        )
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return cls.DEFAULT_MAX_TOOL_RESULT_CHARS
        if value < 1:
            return cls.DEFAULT_MAX_TOOL_RESULT_CHARS
        return value

    @classmethod
    @lru_cache(maxsize=1)
    def CONTEXT_WINDOW(cls) -> int:
        """Configured context budget used by compaction and token display."""
        _ensure_dotenv()
        raw_value = os.getenv(
            "KAIROS_CONTEXT_WINDOW",
            str(cls.DEFAULT_CONTEXT_WINDOW),
        )
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return cls.DEFAULT_CONTEXT_WINDOW
        if value < 1:
            return cls.DEFAULT_CONTEXT_WINDOW
        return value

    @classmethod
    def validate(cls):
        _ensure_dotenv()
        if not cls.OPENAI_API_KEY():
            raise ValueError("OPENAI_API_KEY not found in .env file")
        return True

    @classmethod
    def reload(cls):
        """Force reload of configuration (clears cache and re-reads .env)."""
        global _load_dotenv_done
        _load_dotenv_done = False
        cls.OPENAI_API_KEY.cache_clear()
        cls.OPENAI_BASE_URL.cache_clear()
        cls.OPENAI_MODEL.cache_clear()
        cls.MAX_TOOL_RESULT_CHARS.cache_clear()
        cls.CONTEXT_WINDOW.cache_clear()
