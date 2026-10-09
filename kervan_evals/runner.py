"""Drive kervan-voice's POST /converse through each scenario and build the report."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict
from typing import Any

import httpx

from .checks import Check, ToolCall, Turn, evaluate
from .scenario import STATES, Scenario

REPORT_FORMAT = "kervan/eval-report/0.1"


class ConverseError(Exception):
    """kervan-voice could not be reached or answered outside the /converse contract."""


def _parse_tool_call(raw: Any, where: str) -> ToolCall:
    if not isinstance(raw, dict):
        raise ConverseError(f"{where} is not an object")
    name, arguments, is_error = raw.get("name"), raw.get("arguments"), raw.get("is_error")
    if not isinstance(name, str):
        raise ConverseError(f"{where}.name is not a string")
    if not isinstance(arguments, dict):
        raise ConverseError(f"{where}.arguments is not an object")
    if not isinstance(is_error, bool):
        raise ConverseError(f"{where}.is_error is not a boolean")
    return ToolCall(name=name, arguments=arguments, is_error=is_error, result=raw.get("result"))


def parse_converse_response(utterance: str, body: Any) -> Turn:
    """Validate one /converse answer (SPEC §8) and turn it into a Turn."""
    if not isinstance(body, dict):
        raise ConverseError("answer is not a JSON object")
    reply, raw_calls, state = body.get("reply"), body.get("tool_calls"), body.get("state")
    if not isinstance(reply, str):
        raise ConverseError("answer.reply is not a string")
    if not isinstance(raw_calls, list):
        raise ConverseError("answer.tool_calls is not a list")
    if state not in STATES:
        raise ConverseError(f"answer.state is {state!r}, not one of {', '.join(STATES)}")
    calls = tuple(_parse_tool_call(c, f"answer.tool_calls[{i}]") for i, c in enumerate(raw_calls))
    return Turn(utterance=utterance, reply=reply, tool_calls=calls, state=state)


def converse(client: httpx.Client, conversation_id: str, utterance: str) -> Turn:
    try:
        response = client.post("/converse", json={"conversation_id": conversation_id, "utterance": utterance})
    except httpx.HTTPError as exc:
        raise ConverseError(f"POST /converse failed: {exc!r}") from exc
    if response.status_code != 200:
        raise ConverseError(f"POST /converse answered HTTP {response.status_code}: {response.text[:200]}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ConverseError("POST /converse did not answer JSON") from exc
    return parse_converse_response(utterance, body)


def _transcript(turns: list[Turn]) -> list[dict[str, Any]]:
    return [asdict(turn) for turn in turns]


def run_scenario(client: httpx.Client, scenario: Scenario) -> dict[str, Any]:
    """Play every turn in a fresh conversation, then run the deterministic checks."""
    conversation_id = f"eval-{scenario.id}-{uuid.uuid4().hex[:12]}"
    turns: list[Turn] = []
    try:
        for utterance in scenario.turns:
            turns.append(converse(client, conversation_id, utterance))
    except ConverseError as exc:
        checks = [Check(f"converse turn[{len(turns)}]", False, str(exc))]
    else:
        checks = evaluate(scenario.expect, turns)
    return {
        "id": scenario.id,
        "passed": all(c.passed for c in checks),
        "checks": [c.to_json() for c in checks],
        "conversation_id": conversation_id,
        "transcript": _transcript(turns),
    }


def run(scenarios: list[Scenario], client: httpx.Client, voice_url: str, today: dt.date) -> dict[str, Any]:
    results = [run_scenario(client, s) for s in scenarios]
    return {
        "format": REPORT_FORMAT,
        "voice_url": voice_url,
        "today": today.isoformat(),
        "scenarios": results,
        "passed": bool(results) and all(r["passed"] for r in results),
    }


def exit_code(report: dict[str, Any]) -> int:
    """0 only when every deterministic check passed. `model_eval` is never consulted."""
    scenarios = report["scenarios"]
    passed = bool(scenarios) and all(check["passed"] for s in scenarios for check in s["checks"])
    return 0 if passed else 1
