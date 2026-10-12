"""Direct model providers: provider.json setups and wire-format adapters."""
from .anthropic import AnthropicAdapter
from .base import EventStream, KeyPool, ProviderError
from .completions import CompletionsAdapter
from .config import (
    FORMATS,
    ModelSpec,
    ProviderConfigError,
    config_path,
    default_setup,
    list_setups,
    load_config,
    resolve,
    validate_effort,
)
from .responses import ResponsesAdapter

_ADAPTERS = {
    "completions": CompletionsAdapter,
    "responses": ResponsesAdapter,
    "anthropic": AnthropicAdapter,
}


def create_adapter(spec: ModelSpec):
    return _ADAPTERS[spec.format](spec)


__all__ = [
    "FORMATS", "ModelSpec", "ProviderConfigError", "ProviderError", "EventStream", "KeyPool",
    "CompletionsAdapter", "ResponsesAdapter", "AnthropicAdapter",
    "config_path", "load_config", "list_setups", "resolve", "default_setup", "validate_effort",
    "create_adapter",
]
