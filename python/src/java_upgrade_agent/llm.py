"""LLM selector: builds the LangChain chat model for the provider chosen in config.

Each provider class is imported explicitly (not through `init_chat_model`) so that
packagers such as PyInstaller can see every import, and because each provider
names some settings differently.
"""

from typing import Any, Callable, Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, SecretStr, field_validator

Provider = Literal["anthropic", "openai", "google", "ollama"]


class LLMSettings(BaseModel):
    """The `[llm]` section of the agent config."""

    provider: Provider
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: float | None = None  # seconds
    api_key: SecretStr | None = None  # cloud providers; falls back to the provider's env var
    base_url: str | None = None  # Ollama server, or an OpenAI-compatible endpoint
    extra: dict[str, Any] = {}  # passed to the chat model as-is

    @field_validator("model")
    @classmethod
    def model_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("llm.model must be set, e.g. the model id from your provider")
        return value.strip()


def _set(kwargs: dict[str, Any], name: str, value: Any) -> None:
    """Pass a setting only when it was configured, so provider defaults apply otherwise."""
    if value is not None:
        kwargs[name] = value


def _anthropic(s: LLMSettings) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    kwargs: dict[str, Any] = {"model": s.model}
    _set(kwargs, "temperature", s.temperature)
    _set(kwargs, "max_tokens", s.max_tokens)
    _set(kwargs, "default_request_timeout", s.timeout)
    _set(kwargs, "anthropic_api_key", s.api_key)
    _set(kwargs, "anthropic_api_url", s.base_url)
    return ChatAnthropic(**kwargs, **s.extra)


def _openai(s: LLMSettings) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {"model": s.model}
    _set(kwargs, "temperature", s.temperature)
    _set(kwargs, "max_tokens", s.max_tokens)
    _set(kwargs, "request_timeout", s.timeout)
    _set(kwargs, "openai_api_key", s.api_key)
    _set(kwargs, "openai_api_base", s.base_url)
    return ChatOpenAI(**kwargs, **s.extra)


def _google(s: LLMSettings) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    kwargs: dict[str, Any] = {"model": s.model}
    _set(kwargs, "temperature", s.temperature)
    _set(kwargs, "max_output_tokens", s.max_tokens)
    _set(kwargs, "timeout", s.timeout)
    _set(kwargs, "google_api_key", s.api_key)
    _set(kwargs, "base_url", s.base_url)
    return ChatGoogleGenerativeAI(**kwargs, **s.extra)


def _ollama(s: LLMSettings) -> BaseChatModel:
    from langchain_ollama import ChatOllama

    kwargs: dict[str, Any] = {"model": s.model}
    _set(kwargs, "temperature", s.temperature)
    _set(kwargs, "num_predict", s.max_tokens)
    _set(kwargs, "base_url", s.base_url)
    if s.timeout is not None:
        kwargs["client_kwargs"] = {"timeout": s.timeout}
    return ChatOllama(**kwargs, **s.extra)


_BUILDERS: dict[str, Callable[[LLMSettings], BaseChatModel]] = {
    "anthropic": _anthropic,
    "openai": _openai,
    "google": _google,
    "ollama": _ollama,
}


def create_chat_model(settings: LLMSettings) -> BaseChatModel:
    """Return the chat model for `settings.provider`, configured from `settings`."""
    builder = _BUILDERS.get(settings.provider)
    if builder is None:
        raise ValueError(
            f"Unknown llm.provider {settings.provider!r}; expected one of {', '.join(_BUILDERS)}"
        )
    return builder(settings)
