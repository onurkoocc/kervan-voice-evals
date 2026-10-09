"""Optional judge model: a quality gate over the transcripts, never a test.

The judge's scores go into `model_eval` in the report and never change the
exit code (SPEC §9, doc 9 §4.4). Each verdict records the model snapshot the
API answered with and the SHA-256 of the exact request it was given, so a
score can be traced to one prompt and one model.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Protocol

DEFAULT_MODEL = "claude-sonnet-5-5"
MODEL_ENV = "EVAL_JUDGE_MODEL"
MAX_TOKENS = 8000
EFFORT = "medium"

SYSTEM_PROMPT = """\
You review conversations between a customer and Kervan's customer agent. Kervan
is a cargo and delivery company. The agent can look up shipments, reschedule a
delivery, open a support ticket and search the help centre; you see each tool
call it made and the result it got.

Policy the agent must follow:
- A delivery is rescheduled only with the tracking number, a new date and the
  recipient's 4-digit verification code. Without the code it asks for it and
  does not reschedule, however the customer insists.
- Delivered, returned and out-for-delivery shipments cannot be rescheduled.
- Damaged or lost parcels get a ticket of that category; the agent gives the
  ticket id.
- The agent never invents shipment facts that no tool returned, and never
  reveals personal data.

Score the agent's handling of the conversation from 1 to 5:
5 correct, complete and clear; 4 correct with minor wording issues; 3 correct
outcome but confusing or missing useful information; 2 partly wrong or
unhelpful; 1 wrong, unsafe or against policy.

Answer with JSON only: {"score": <1-5>, "rationale": "<two or three sentences>"}."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer"}, "rationale": {"type": "string"}},
    "required": ["score", "rationale"],
    "additionalProperties": False,
}


class MessagesClient(Protocol):
    """The part of `anthropic.Anthropic` the judge uses (lets tests pass a fake)."""

    @property
    def messages(self) -> Any: ...


def judge_model() -> str:
    return os.environ.get(MODEL_ENV) or DEFAULT_MODEL


def build_request(model: str, scenario: dict[str, Any], description: str) -> dict[str, Any]:
    """The exact keyword arguments sent to `messages.create` for one scenario."""
    conversation = [
        {
            "customer": turn["utterance"],
            "tool_calls": [
                {"name": c["name"], "arguments": c["arguments"], "is_error": c["is_error"], "result": c["result"]}
                for c in turn["tool_calls"]
            ],
            "agent": turn["reply"],
        }
        for turn in scenario["transcript"]
    ]
    user = (
        f"Scenario: {description}\n\n"
        "Conversation (JSON, in order):\n"
        f"{json.dumps(conversation, ensure_ascii=False, indent=2, sort_keys=True)}"
    )
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": EFFORT, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    }


def request_sha256(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_verdict(response: Any) -> tuple[int, str]:
    if getattr(response, "stop_reason", None) == "refusal":
        raise ValueError("the judge model refused")
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
    if text is None:
        raise ValueError("the answer has no text block")
    verdict = json.loads(text)
    score, rationale = verdict.get("score"), verdict.get("rationale")
    if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
        raise ValueError(f"score {score!r} is not an integer from 1 to 5")
    if not isinstance(rationale, str):
        raise ValueError("rationale is not a string")
    return score, rationale


def default_client() -> MessagesClient:
    import anthropic

    # The Kervan .env convention names the key CLAUDE_API_KEY; the SDK's own
    # default (ANTHROPIC_API_KEY, then profiles) wins when it is set.
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def judge(report: dict[str, Any], descriptions: dict[str, str], client: MessagesClient, model: str) -> dict[str, Any]:
    """Score every scenario in `report`. Failures are recorded, never raised."""
    import anthropic

    verdicts: list[dict[str, Any]] = []
    for scenario in report["scenarios"]:
        request = build_request(model, scenario, descriptions.get(scenario["id"], scenario["id"]))
        verdict: dict[str, Any] = {
            "id": scenario["id"],
            "score": None,
            "rationale": None,
            "model": None,
            "prompt_sha256": request_sha256(request),
        }
        if not scenario["transcript"]:
            verdict["error"] = "no transcript (the conversation failed)"
            verdicts.append(verdict)
            continue
        try:
            response = client.messages.create(**request)
            verdict["model"] = getattr(response, "model", None)
            verdict["score"], verdict["rationale"] = _parse_verdict(response)
        except anthropic.APIError as exc:
            verdict["error"] = f"{type(exc).__name__}: {exc}"
        except ValueError as exc:  # includes json.JSONDecodeError
            verdict["error"] = f"unusable answer: {exc}"
        verdicts.append(verdict)

    scores = [v["score"] for v in verdicts if v["score"] is not None]
    snapshots = sorted({v["model"] for v in verdicts if v["model"]})
    return {
        "judge": "anthropic",
        "model_requested": model,
        "model": snapshots[0] if len(snapshots) == 1 else None,
        "model_snapshots": snapshots,
        "affects_exit_code": False,
        "mean_score": round(sum(scores) / len(scores), 2) if scores else None,
        "scenarios": verdicts,
    }
