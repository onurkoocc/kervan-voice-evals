"""The judge path, with a fake Anthropic client. The real API is never called."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from kervan_evals import judge
from kervan_evals.cli import main

from conftest import SCENARIOS_DIR

SNAPSHOT = "claude-sonnet-5-5-20260901"


class FakeMessages:
    def __init__(self, answer):
        self.answer = answer
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        return self.answer(request)


class FakeClient:
    def __init__(self, answer):
        self.messages = FakeMessages(answer)


def text_answer(text: str, *, stop_reason: str = "end_turn", model: str = SNAPSHOT) -> Any:
    return SimpleNamespace(model=model, stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def verdict(score: int, rationale: str = "Handled per policy.") -> Any:
    return lambda request: text_answer(json.dumps({"score": score, "rationale": rationale}))


def run_with_judge(client: FakeClient, *extra: str) -> int:
    return main(
        ["run", "--voice-url", extra[0], "--scenarios", str(SCENARIOS_DIR), "--today", "2026-10-09",
         "--judge", "anthropic", *extra[1:]],
        judge_client_factory=lambda: client,
    )


def test_model_eval_records_snapshot_hash_and_scores(voice_server, capsys, monkeypatch):
    monkeypatch.delenv(judge.MODEL_ENV, raising=False)
    client = FakeClient(verdict(4))
    assert run_with_judge(client, voice_server) == 0
    report = json.loads(capsys.readouterr().out)

    model_eval = report["model_eval"]
    assert model_eval["judge"] == "anthropic"
    assert model_eval["model_requested"] == "claude-sonnet-5-5"
    assert model_eval["model"] == SNAPSHOT
    assert model_eval["model_snapshots"] == [SNAPSHOT]
    assert model_eval["affects_exit_code"] is False
    assert model_eval["mean_score"] == 4
    assert [v["id"] for v in model_eval["scenarios"]] == [s["id"] for s in report["scenarios"]]

    # One request per scenario; each verdict carries the hash of exactly what was sent.
    assert len(client.messages.requests) == len(report["scenarios"])
    for request, v in zip(client.messages.requests, model_eval["scenarios"]):
        assert v["prompt_sha256"] == judge.request_sha256(request)
        assert v["score"] == 4 and v["rationale"] == "Handled per policy." and v["model"] == SNAPSHOT
        assert request["model"] == "claude-sonnet-5-5"
        assert request["system"] == judge.SYSTEM_PROMPT
        assert request["output_config"]["format"]["type"] == "json_schema"


def test_the_prompt_contains_the_transcript_and_description(voice_server, capsys):
    client = FakeClient(verdict(5))
    run_with_judge(client, voice_server)
    capsys.readouterr()
    happy = next(r for r in client.messages.requests if "KRV10000002" in r["messages"][0]["content"])
    content = happy["messages"][0]["content"]
    assert content.startswith("Scenario: The customer gives tracking number")
    assert '"verification_code": "4821"' in content
    assert "rescheduled to 2026-10-12" in content


def test_judge_model_from_environment(voice_server, capsys, monkeypatch):
    monkeypatch.setenv(judge.MODEL_ENV, "claude-opus-5-5")
    client = FakeClient(verdict(5))
    run_with_judge(client, voice_server)
    assert json.loads(capsys.readouterr().out)["model_eval"]["model_requested"] == "claude-opus-5-5"
    assert {r["model"] for r in client.messages.requests} == {"claude-opus-5-5"}


def test_a_perfect_score_does_not_rescue_a_failed_check(voice_server, fake_voice, capsys):
    fake_voice.recordings["status-lookup"][0]["reply"] = "No idea."
    assert run_with_judge(FakeClient(verdict(5)), voice_server) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False and report["model_eval"]["mean_score"] == 5


def test_a_bad_score_does_not_fail_a_passing_run(voice_server, capsys):
    assert run_with_judge(FakeClient(verdict(1, "Terse.")), voice_server) == 0
    assert json.loads(capsys.readouterr().out)["model_eval"]["mean_score"] == 1


def test_api_errors_are_recorded_not_raised(voice_server, capsys):
    def fail(request):
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))

    assert run_with_judge(FakeClient(fail), voice_server) == 0
    model_eval = json.loads(capsys.readouterr().out)["model_eval"]
    assert model_eval["mean_score"] is None and model_eval["model"] is None
    for v in model_eval["scenarios"]:
        assert v["score"] is None and v["error"].startswith("APIConnectionError")
        assert len(v["prompt_sha256"]) == 64


def test_client_creation_failure_is_recorded(voice_server, capsys):
    def factory():
        raise anthropic.AnthropicError("no credentials")

    code = main(["run", "--voice-url", voice_server, "--scenarios", str(SCENARIOS_DIR), "--today", "2026-10-09",
                 "--judge", "anthropic"], judge_client_factory=factory)
    assert code == 0
    model_eval = json.loads(capsys.readouterr().out)["model_eval"]
    assert "no credentials" in model_eval["error"]


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (text_answer("not json"), "unusable answer"),
        (text_answer('{"score": 9, "rationale": "x"}'), "score 9 is not an integer from 1 to 5"),
        (text_answer('{"score": true, "rationale": "x"}'), "is not an integer"),
        (text_answer('{"score": 3}'), "rationale is not a string"),
        (text_answer("", stop_reason="refusal"), "the judge model refused"),
        (SimpleNamespace(model=SNAPSHOT, stop_reason="end_turn", content=[]), "no text block"),
    ],
)
def test_unusable_answers(answer, error):
    report = {"scenarios": [{"id": "s", "transcript": [
        {"utterance": "hi", "reply": "hello", "tool_calls": [], "state": "open"}]}]}
    model_eval = judge.judge(report, {"s": "desc"}, FakeClient(lambda request: answer), "m")
    v = model_eval["scenarios"][0]
    assert v["score"] is None and error in v["error"]
    assert v["model"] == SNAPSHOT  # the snapshot is kept even when the answer is unusable


def test_scenarios_without_a_transcript_are_not_sent():
    client = FakeClient(verdict(5))
    model_eval = judge.judge({"scenarios": [{"id": "s", "transcript": []}]}, {}, client, "m")
    assert client.messages.requests == []
    assert model_eval["scenarios"][0]["error"].startswith("no transcript")


def test_request_hash_is_canonical_and_content_sensitive():
    scenario = {"id": "s", "transcript": [{"utterance": "hi", "reply": "hello", "tool_calls": [], "state": "open"}]}
    a = judge.build_request("m", scenario, "desc")
    b = json.loads(json.dumps(a))  # same content, rebuilt
    assert judge.request_sha256(a) == judge.request_sha256(b)
    scenario["transcript"][0]["reply"] = "hello!"
    assert judge.request_sha256(judge.build_request("m", scenario, "desc")) != judge.request_sha256(a)
