import json
import re
import urllib.error
import urllib.request

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from harnh3x import anthropic_api
from harnh3x import config
from harnh3x.anthropic_api import AnthropicClient, status_message

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
        return "The API request timed out."
    return "Could not reach the API."


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
    if text.endswith("/v1"):
        text = text[: -len("/v1")]
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
    api = client or get_client()
    if isinstance(api, AnthropicClient):
        configured = _count(config.CONTEXT_LIMIT)
        if configured is not None:
            return configured
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


def _remember_context(session, response, client) -> None:
    usage = getattr(response, "usage", None)
    total = _count(getattr(usage, "total_tokens", None))
    if total is not None:
        session.context_tokens = total
    if session.context_limit is None:
        limit = model_context_limit(session.model, client)
        if limit is not None:
            session.context_limit = limit
