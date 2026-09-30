"""Choose the model provider used for intent parsing.

``SPENDDAT_LLM_PROVIDER``:
* ``openrouter`` - OpenRouter (OpenAI-compatible); secret ``openrouter_api_key``.
  Default model ``anthropic/claude-sonnet-5.5``.
* ``anthropic``  - Anthropic Messages API directly; secret ``anthropic_api_key``.
  Default model ``claude-sonnet-5-5``.
* ``auto`` (default) - OpenRouter if an OpenRouter key is present, else Anthropic.

``SPENDDAT_LLM_MODEL`` overrides the model for whichever provider is chosen. Remember
that OpenRouter ids are prefixed (``anthropic/...``) and Anthropic ids are not.
"""

from __future__ import annotations

from dataclasses import dataclass

from spenddat.intent import AnthropicClient, LlmClient, OpenRouterClient
from spenddat.secrets import SecretsProvider

DEFAULT_MODELS = {
    "openrouter": "anthropic/claude-sonnet-5.5",
    "anthropic": "claude-sonnet-5-5",
}
SECRET_NAMES = {"openrouter": "openrouter_api_key", "anthropic": "anthropic_api_key"}


@dataclass(frozen=True)
class LlmChoice:
    provider: str | None  # None: no key available
    model: str | None
    secret_name: str | None

    @property
    def label(self) -> str:
        if not self.provider:
            return "offline demo parser"
        via = "OpenRouter" if self.provider == "openrouter" else "Anthropic API"
        return f"{self.model} via {via}"


def choose_llm(env: dict[str, str], secrets: SecretsProvider) -> LlmChoice:
    want = env.get("SPENDDAT_LLM_PROVIDER", "auto").strip().lower()
    if want not in ("auto", "openrouter", "anthropic"):
        raise ValueError(
            f"SPENDDAT_LLM_PROVIDER must be auto, openrouter or anthropic, got {want!r}"
        )
    order = ["openrouter", "anthropic"] if want == "auto" else [want]
    for p in order:
        if secrets.get(SECRET_NAMES[p]):
            return LlmChoice(p, env.get("SPENDDAT_LLM_MODEL") or DEFAULT_MODELS[p], SECRET_NAMES[p])
    if want != "auto":
        return LlmChoice(
            want, env.get("SPENDDAT_LLM_MODEL") or DEFAULT_MODELS[want], SECRET_NAMES[want]
        )
    return LlmChoice(None, None, None)


def build_llm(choice: LlmChoice, secrets: SecretsProvider) -> LlmClient:
    if not choice.provider or not choice.secret_name or not choice.model:
        raise ValueError("no model provider configured")
    key = secrets.get(choice.secret_name)
    if not key:
        raise ValueError(f"missing secret {choice.secret_name}")
    if choice.provider == "openrouter":
        return OpenRouterClient(key, choice.model)
    return AnthropicClient(key, choice.model)
