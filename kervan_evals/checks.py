"""Deterministic checks of one conversation against a scenario's expectations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from .scenario import Expectation


@dataclass(frozen=True)
class ToolCall:
    """One tool call as kervan-voice reports it in a /converse answer (SPEC §8)."""

    name: str
    arguments: dict[str, Any]
    is_error: bool
    result: Any


@dataclass(frozen=True)
class Turn:
    utterance: str
    reply: str
    tool_calls: tuple[ToolCall, ...]
    state: str


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def is_subset(expected: Any, actual: Any) -> bool:
    """True when every key of `expected` is in `actual` with a matching value.

    Mappings match recursively; any other value must be equal.
    """
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and is_subset(value, actual[key]) for key, value in expected.items()
        )
    return type(expected) is type(actual) and expected == actual


def _brief(value: Any, limit: int = 200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe(index: int, call: ToolCall) -> str:
    return f"call #{index} {call.name}({_brief(call.arguments)}) is_error={str(call.is_error).lower()}"


def evaluate(expect: Expectation, turns: list[Turn]) -> list[Check]:
    """Run every deterministic check. The conversation must have at least one turn."""
    calls = [call for turn in turns for call in turn.tool_calls]
    checks: list[Check] = []

    # Expected tool calls must appear in order (other calls may sit in between).
    cursor = 0
    for i, exp in enumerate(expect.tool_calls):
        name = f"tool_call[{i}] {exp.name}"
        candidates = [
            j for j in range(cursor, len(calls))
            if calls[j].name == exp.name and is_subset(exp.arguments, calls[j].arguments)
        ]
        full = next((j for j in candidates if calls[j].is_error == exp.expect_error), None)
        if full is not None:
            cursor = full + 1
            checks.append(Check(name, True, _describe(full, calls[full])))
        elif candidates:
            j = candidates[0]
            want = "an error" if exp.expect_error else "success"
            checks.append(Check(name, False, f"expected {want}, got {_describe(j, calls[j])}: {_brief(calls[j].result)}"))
        else:
            same_name = [j for j in range(cursor, len(calls)) if calls[j].name == exp.name]
            if same_name:
                got = "; ".join(_describe(j, calls[j]) for j in same_name)
                detail = f"no call matched arguments {_brief(exp.arguments)}; got {got}"
            else:
                detail = f"{exp.name} was not called" + (f" after call #{cursor - 1}" if cursor else "")
            checks.append(Check(name, False, detail))

    if expect.forbidden_tools:
        hits = [(j, c) for j, c in enumerate(calls) if c.name in expect.forbidden_tools]
        if hits:
            detail = "called: " + "; ".join(_describe(j, c) for j, c in hits)
        else:
            detail = "none of " + ", ".join(expect.forbidden_tools) + " was called"
        checks.append(Check("forbidden_tools", not hits, detail))

    final_reply = turns[-1].reply
    for needle in expect.reply_contains:
        found = needle.casefold() in final_reply.casefold()
        detail = "found" if found else f"not in final reply: {_brief(final_reply)}"
        checks.append(Check(f"reply_contains {needle!r}", found, detail))

    if expect.final_state is not None:
        state = turns[-1].state
        checks.append(Check("final_state", state == expect.final_state, f"expected {expect.final_state}, got {state}"))

    return checks
