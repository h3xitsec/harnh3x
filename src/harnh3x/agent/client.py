import json
import re
import urllib.error
import urllib.request
from typing import NamedTuple

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from harnh3x import anthropic_api, config
from harnh3x.anthropic_api import AnthropicClient, status_message
from harnh3x.failures import TIMED_OUT, UNREACHABLE

_client = None


def get_client() -> OpenAI | AnthropicClient:
    global _client
    if _client is None:
        kwargs = {}
        if config.API_KEY:
            kwargs["api_key"] = config.API_KEY
        if config.API_URL:
            kwargs["base_url"] = config.API_URL
        if config.PROVIDER == "anthropic":
            _client = AnthropicClient(anthropic_api.Anthropic(**kwargs))
        else:
            _client = OpenAI(**kwargs)
    return _client


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
