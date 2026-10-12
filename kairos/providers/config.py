"""provider.json loading and model-setup resolution.

``provider.json`` lives at the repository root (it is gitignored because it
holds API keys). ``provider.example.json`` documents the shape.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

FORMATS = ("completions", "responses", "anthropic")


class ProviderConfigError(ValueError):
    """provider.json is missing, malformed, or names an unknown setup/provider."""


def config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "provider.json"


def load_config() -> dict:
    path = config_path()
    if not path.is_file():
        raise ProviderConfigError(
            f"Provider config not found: {path}. Copy provider.example.json to "
            "provider.json and fill in your API keys."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProviderConfigError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        raise ProviderConfigError(f"{path} must contain a 'providers' object.")
    return data


@dataclass(frozen=True)
class ModelSpec:
    """Everything needed to talk to one model. ``setup`` is None for ad-hoc models."""

    setup: Optional[str]
    provider: str
    model: str
    format: str
    base_url: str
    api_keys: tuple = field(default=(), repr=False)  # never printed
    # None means unknown (ad-hoc model): any effort is passed through unvalidated.
    reasoning_efforts: Optional[tuple] = None
    default_effort: Optional[str] = None
    # Effective budget: min(Config.CONTEXT_WINDOW() cap, the model's max_context).
    context_window: Optional[int] = None
    # USD per 1M tokens {"input", "cached_input", "output"}; None = unknown.
    pricing: Optional[dict] = field(default=None, compare=False)

    def info(self, effort: Optional[str]) -> dict[str, Any]:
        return {
            "setup": self.setup,
            "provider": self.provider,
            "model": self.model,
            "format": self.format,
            "effort": effort,
            "reasoning_efforts": list(self.reasoning_efforts) if self.reasoning_efforts is not None else None,
        }


def _build_spec(name: Optional[str], provider_name: str, model: str, overrides: dict, config: dict) -> ModelSpec:
    providers = config["providers"]
    if provider_name not in providers:
        raise ProviderConfigError(
            f"Unknown provider '{provider_name}'. Available: {', '.join(sorted(providers))}"
        )
    merged = {**providers[provider_name], **overrides}
    fmt = merged.get("format")
    if fmt not in FORMATS:
        raise ProviderConfigError(f"Provider '{provider_name}' has invalid format {fmt!r}; expected one of {FORMATS}.")
    efforts = merged.get("reasoning_efforts")
    from ..config import Config  # local import: kairos.config validates provider.json

    cap = Config.CONTEXT_WINDOW()
    # max_context is the model's real limit ("context_window" is the older name).
    max_context = merged.get("max_context") or merged.get("context_window")
    context_window = min(cap, int(max_context)) if max_context else cap
    pricing = merged.get("pricing")
    return ModelSpec(
        setup=name,
        provider=provider_name,
        model=model,
        format=fmt,
        base_url=str(merged.get("base_url") or ""),
        api_keys=tuple(merged.get("api_keys") or ()),
        reasoning_efforts=tuple(efforts) if efforts is not None else None,
        default_effort=merged.get("default_effort"),
        context_window=context_window,
        pricing=dict(pricing) if pricing else None,
    )


def resolve(setup: Optional[str] = None, *, model: Optional[str] = None, provider: Optional[str] = None) -> ModelSpec:
    """Return the spec for a named setup (default setup when omitted), or for an
    ad-hoc ``model`` id on a named ``provider``."""
    config = load_config()
    if model is not None:
        if not provider:
            raise ProviderConfigError("An ad-hoc model needs a provider (openai, openrouter, anthropic, ...).")
        return _build_spec(None, provider, model, {}, config)
    setups = config.get("setups") or {}
    name = setup or config.get("default_setup")
    if not name:
        raise ProviderConfigError("No setup given and provider.json has no default_setup.")
    if name not in setups:
        raise ProviderConfigError(f"Unknown setup '{name}'. Available: {', '.join(sorted(setups))}")
    entry = setups[name]
    overrides = {k: v for k, v in entry.items() if k not in ("provider", "model")}
    return _build_spec(name, entry.get("provider", ""), entry.get("model", ""), overrides, config)


def list_setups() -> list[dict[str, Any]]:
    config = load_config()
    result = []
    for name in config.get("setups") or {}:
        spec = resolve(name)
        result.append({
            "name": name,
            "provider": spec.provider,
            "model": spec.model,
            "format": spec.format,
            "reasoning_efforts": list(spec.reasoning_efforts) if spec.reasoning_efforts is not None else None,
            "default_effort": spec.default_effort,
        })
    return result


def default_setup() -> Optional[str]:
    return load_config().get("default_setup")


def validate_effort(spec: ModelSpec, effort: str) -> str:
    """Return ``effort`` if the spec accepts it, else raise ValueError."""
    if spec.reasoning_efforts is not None and effort not in spec.reasoning_efforts:
        allowed = ", ".join(spec.reasoning_efforts) or "none (this model takes no effort setting)"
        raise ValueError(f"Effort '{effort}' is not supported by {spec.setup or spec.model}. Choose from: {allowed}")
    return effort
