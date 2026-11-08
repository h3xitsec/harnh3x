import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

from anthropic import AnthropicError
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    OpenAIError,
)

from harnh3x import anthropic_api, config
from harnh3x.anthropic_api import AnthropicClient, status_message
from harnh3x.config import Provider
from harnh3x.failures import TIMED_OUT, UNREACHABLE

_clients: dict[Provider, OpenAI | AnthropicClient] = {}


class ModelRef(NamedTuple):
    """A model id and the configured provider that serves it (None when unqualified)."""

    provider: str | None
    model: str


class ProviderModels(NamedTuple):
    """One provider's model list, or the error text when it could not be listed."""

    provider: str
    models: list[str]
    error: str | None = None


def find_provider(name: str | None = None) -> Provider | None:
    """The provider called `name`, or the first configured one when `name` is None."""
    providers = config.PROVIDERS
    if name is None:
        return providers[0] if providers else None
    for entry in providers:
        if entry.name == name:
            return entry
    return None


def default_provider() -> str | None:
    entry = find_provider()
    return None if entry is None else entry.name


def parse_model_ref(text: str) -> ModelRef:
    """Split `provider/model` when the first segment names a configured provider."""
    head, sep, rest = text.partition("/")
    if sep and rest and find_provider(head) is not None:
        return ModelRef(head, rest)
    return ModelRef(None, text)


def model_label(provider: str | None, model: str) -> str:
    """`provider/model` when more than one provider is configured, else the model."""
    if provider is None or len(config.PROVIDERS) <= 1:
        return model
    return f"{provider}/{model}"


def get_client(provider: str | None = None) -> OpenAI | AnthropicClient:
    entry = find_provider(provider)
    if entry is None:
        if provider is None:
            raise ValueError("No provider is configured.")
        raise ValueError(f"Provider {provider} is not configured.")
    client = _clients.get(entry)
    if client is None:
        kwargs = {"api_key": entry.api_key, "base_url": entry.api_url}
        if entry.kind == "anthropic":
            client = AnthropicClient(anthropic_api.Anthropic(**kwargs))
        else:
            client = OpenAI(**kwargs)
        _clients[entry] = client
    return client


def _api_failure(exc: APIConnectionError) -> str:
    if isinstance(exc, APITimeoutError):
        return TIMED_OUT
    return UNREACHABLE


def list_models(client: OpenAI | AnthropicClient | None = None) -> list[str]:
    api = client or get_client()
    if isinstance(api, AnthropicClient):
        return api.list_model_ids()
    try:
        names = {model.id for model in api.models.list() if getattr(model, "id", None)}
    except APIConnectionError as exc:
        raise SystemExit(_api_failure(exc)) from None
    except APIStatusError as exc:
        raise SystemExit(status_message(exc)) from None
    return sorted(names)


def _provider_models(entry: Provider) -> ProviderModels:
    if entry.models is not None:
        return ProviderModels(entry.name, list(entry.models))
    try:
        return ProviderModels(entry.name, list_models(get_client(entry.name)))
    except SystemExit as exc:
        return ProviderModels(entry.name, [], str(exc.code))
    except (OpenAIError, AnthropicError, OSError, ValueError) as exc:
        return ProviderModels(entry.name, [], f"Could not list models: {exc}")


def list_provider_models(only: str | None = None) -> list[ProviderModels]:
    """Every configured provider's models (or just `only`'s), in config order."""
    providers = [
        entry for entry in config.PROVIDERS if only is None or entry.name == only
    ]
    if len(providers) <= 1:
        return [_provider_models(entry) for entry in providers]
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        return list(pool.map(_provider_models, providers))


_SHOW_TIMEOUT = 3
_NUM_CTX = re.compile(r"(?m)^num_ctx\s+(\d+)\s*$")


def _show_url(client) -> str | None:
    base = getattr(client, "base_url", None)
    if base is None:
        return None
    text = str(base).strip().rstrip("/")
    if not text:
        return None
    text = text.removesuffix("/v1")
    return f"{text}/api/show"


def _context_limit_from_show(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    parameters = payload.get("parameters")
    if isinstance(parameters, str):
        match = _NUM_CTX.search(parameters)
        if match:
            return int(match.group(1))
    info = payload.get("model_info")
    if not isinstance(info, dict):
        return None
    lengths = [
        value
        for key, value in info.items()
        if isinstance(key, str)
        and key.endswith(".context_length")
        and isinstance(value, int)
        and not isinstance(value, bool)
    ]
    if not lengths:
        return None
    return max(lengths)


def model_context_limit(
    model: str, client: OpenAI | AnthropicClient | None = None
) -> int | None:
    configured = _count(config.CONTEXT_LIMIT)
    if configured is not None:
        return configured
    api = client or get_client()
    if isinstance(api, AnthropicClient):
        return config.ANTHROPIC_CONTEXT_LIMIT
    url = _show_url(api)
    if url is None:
        return None
    body = json.dumps({"model": model}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_SHOW_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.URLError):
        return None
    return _context_limit_from_show(payload)


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _stored_context_tokens(data: dict) -> int | None:
    return _count(data.get("context_tokens"))


def _cache_stats(response) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    stats = {}
    read = _count(getattr(usage, "cache_read_tokens", None))
    if read is None:
        details = getattr(usage, "prompt_tokens_details", None)
        read = _count(getattr(details, "cached_tokens", None))
    if read is not None:
        stats["cache_read"] = read
    write = _count(getattr(usage, "cache_write_tokens", None))
    if write is not None:
        stats["cache_write"] = write
    return stats


class CacheRound(NamedTuple):
    """One completion's prompt-cache numbers; None where the server sent none.

    `reusable` is the previous completion's prompt plus reply, the most this
    one could read back from the cache.
    """

    read: int | None
    write: int | None
    reusable: int | None
    prompt: int | None


def _stored_cache_rounds(data: dict) -> list[CacheRound]:
    stored = data.get("cache_rounds")
    if not isinstance(stored, list):
        return []
    rounds = []
    for entry in stored:
        if not isinstance(entry, dict):
            continue
        values = {name: entry.get(name) for name in CacheRound._fields}
        if any(
            value is not None and _count(value) is None for value in values.values()
        ):
            continue
        rounds.append(CacheRound(**values))
    return rounds


def _remember_context(session, response, client) -> None:
    usage = getattr(response, "usage", None)
    total = _count(getattr(usage, "total_tokens", None))
    stats = _cache_stats(response)
    session.cache_rounds.append(
        CacheRound(
            read=stats.get("cache_read"),
            write=stats.get("cache_write"),
            reusable=_count(session.context_tokens),
            prompt=_count(getattr(usage, "prompt_tokens", None)),
        )
    )
    if total is not None:
        session.context_tokens = total
    if session.context_limit is None:
        limit = model_context_limit(session.model, client)
        if limit is not None:
            session.context_limit = limit
