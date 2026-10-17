"""The fixed answers a turn returns when it cannot finish normally."""

from __future__ import annotations

TIMED_OUT = "The API request timed out."
UNREACHABLE = "Could not reach the API."
REJECTED_PREFIX = "The API rejected the request: "
MAX_TURNS_REACHED = "Max turns reached."
EMPTY_ANSWER = "The model returned an empty answer."

_FIXED = frozenset({TIMED_OUT, UNREACHABLE, MAX_TURNS_REACHED, EMPTY_ANSWER})


def rejected(text: str) -> str:
    return f"{REJECTED_PREFIX}{text}"


def is_failure_answer(text: object) -> bool:
    if not isinstance(text, str):
        return False
    return text in _FIXED or text.startswith(REJECTED_PREFIX)
