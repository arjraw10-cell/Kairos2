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
    @lru_cache(maxsize=1)
    def KAIROS_GATEWAY_HOST(cls) -> str:
        _ensure_dotenv()
        value = os.getenv("KAIROS_GATEWAY_HOST", "127.0.0.1").strip()
        return value or "127.0.0.1"

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_GATEWAY_PORT(cls) -> int:
        _ensure_dotenv()
        raw = os.getenv("KAIROS_GATEWAY_PORT", "8765").strip()
        try:
            port = int(raw)
        except ValueError as exc:
            raise ValueError("KAIROS_GATEWAY_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("KAIROS_GATEWAY_PORT must be between 1 and 65535")
        return port

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_DEFAULT_WORKSPACE(cls) -> str | None:
        _ensure_dotenv()
        value = os.getenv("KAIROS_DEFAULT_WORKSPACE", "").strip()
        return value or None

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_DATA_DIR(cls) -> str:
        _ensure_dotenv()
        return os.getenv("KAIROS_DATA_DIR", "")

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_MAX_CONCURRENT_RUNS(cls) -> int:
        _ensure_dotenv()
        raw = os.getenv("KAIROS_MAX_CONCURRENT_RUNS", "8")
        try:
            return max(1, int(raw))
        except ValueError as exc:
            raise ValueError("KAIROS_MAX_CONCURRENT_RUNS must be an integer") from exc

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_RUNTIME_IDLE_SECONDS(cls) -> int:
        _ensure_dotenv()
        raw = os.getenv("KAIROS_RUNTIME_IDLE_SECONDS", "1800")
        try:
            return max(0, int(raw))
        except ValueError as exc:
            raise ValueError("KAIROS_RUNTIME_IDLE_SECONDS must be an integer") from exc

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_AUTH_TOKEN(cls) -> str | None:
        _ensure_dotenv()
        value = os.getenv("KAIROS_AUTH_TOKEN", "").strip()
        return value or None

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_LEGACY_CHAT_FILE(cls) -> str | None:
        _ensure_dotenv()
        value = os.getenv("KAIROS_LEGACY_CHAT_FILE", "").strip()
        return value or None

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
        cls.KAIROS_GATEWAY_HOST.cache_clear()
        cls.KAIROS_GATEWAY_PORT.cache_clear()
        cls.KAIROS_DEFAULT_WORKSPACE.cache_clear()
        cls.KAIROS_DATA_DIR.cache_clear()
        cls.KAIROS_MAX_CONCURRENT_RUNS.cache_clear()
        cls.KAIROS_RUNTIME_IDLE_SECONDS.cache_clear()
        cls.KAIROS_AUTH_TOKEN.cache_clear()
        cls.KAIROS_LEGACY_CHAT_FILE.cache_clear()
