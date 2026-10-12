import os
from functools import lru_cache


class Config:
    """Settings read from environment variables (cached on first access).

    Model providers and API keys live in provider.json (kairos.providers).
    """

    DEFAULT_MAX_TOOL_RESULT_CHARS = 20_000
    DEFAULT_CONTEXT_WINDOW = 300_000
    DEFAULT_CONTEXT_RESERVE_TOKENS = 16_384

    @classmethod
    @lru_cache(maxsize=1)
    def MAX_TOOL_RESULT_CHARS(cls) -> int:
        """Maximum text characters retained for one model-facing tool result."""
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
        value = os.getenv("KAIROS_GATEWAY_HOST", "127.0.0.1").strip()
        return value or "127.0.0.1"

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_GATEWAY_PORT(cls) -> int:
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
        value = os.getenv("KAIROS_DEFAULT_WORKSPACE", "").strip()
        return value or None

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_DATA_DIR(cls) -> str:
        return os.getenv("KAIROS_DATA_DIR", "")

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_MAX_CONCURRENT_RUNS(cls) -> int:
        raw = os.getenv("KAIROS_MAX_CONCURRENT_RUNS", "8")
        try:
            return max(1, int(raw))
        except ValueError as exc:
            raise ValueError("KAIROS_MAX_CONCURRENT_RUNS must be an integer") from exc

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_RUNTIME_IDLE_SECONDS(cls) -> int:
        raw = os.getenv("KAIROS_RUNTIME_IDLE_SECONDS", "1800")
        try:
            return max(0, int(raw))
        except ValueError as exc:
            raise ValueError("KAIROS_RUNTIME_IDLE_SECONDS must be an integer") from exc

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_AUTH_TOKEN(cls) -> str | None:
        value = os.getenv("KAIROS_AUTH_TOKEN", "").strip()
        return value or None

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_LEGACY_CHAT_FILE(cls) -> str | None:
        value = os.getenv("KAIROS_LEGACY_CHAT_FILE", "").strip()
        return value or None

    @classmethod
    @lru_cache(maxsize=1)
    def KAIROS_CUA_DRIVER_PATH(cls) -> str | None:
        """Optional path to the Cua Driver executable used by desktop tools."""
        value = os.getenv("KAIROS_CUA_DRIVER_PATH", "").strip()
        return value or None

    @classmethod
    def validate(cls):
        """Raise ValueError if provider.json is missing or its default setup is invalid."""
        from .providers import resolve

        resolve()
        return True

    @classmethod
    def reload(cls):
        """Force reload of configuration (clears cached environment values)."""
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
        cls.KAIROS_CUA_DRIVER_PATH.cache_clear()
