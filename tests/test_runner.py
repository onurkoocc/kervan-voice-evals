from __future__ import annotations

import httpx
import pytest

from kervan_evals import runner
from kervan_evals.runner import ConverseError, parse_converse_response
from kervan_evals.scenario import load_scenarios

from conftest import SCENARIOS_DIR, TODAY, FakeVoice

VOICE_URL = "http://voice:7000"


def run_against(fake: FakeVoice | None = None, handler=None) -> dict:
    transport = httpx.MockTransport(handler or fake.handler)
    with httpx.Client(base_url=VOICE_URL, transport=transport) as client:
        return runner.run(load_scenarios(SCENARIOS_DIR, TODAY), client, VOICE_URL, TODAY)


def scenario(report: dict, scenario_id: str) -> dict:
    return next(s for s in report["scenarios"] if s["id"] == scenario_id)


def test_every_bundled_scenario_passes_against_the_recordings(fake_voice):
    report = run_against(fake_voice)
    failures = {s["id"]: [c for c in s["checks"] if not c["passed"]] for s in report["scenarios"] if not s["passed"]}
    assert failures == {}
    assert report["passed"] is True
    assert runner.exit_code(report) == 0


def test_report_shape(fake_voice):
    report = run_against(fake_voice)
    assert set(report) == {"format", "voice_url", "today", "scenarios", "passed"}
    assert report["format"] == "kervan/eval-report/0.1"
    assert report["voice_url"] == VOICE_URL
    assert report["today"] == "2026-10-09"
    for s in report["scenarios"]:
        assert set(s) == {"id", "passed", "checks", "conversation_id", "transcript"}
        assert s["checks"], s["id"]
        for c in s["checks"]:
            assert set(c) == {"name", "passed", "detail"}
            assert isinstance(c["passed"], bool) and isinstance(c["detail"], str)
        for t in s["transcript"]:
            assert set(t) == {"utterance", "reply", "tool_calls", "state"}


def test_each_scenario_gets_a_fresh_conversation_and_expanded_turns(fake_voice):
    report = run_against(fake_voice)
    ids = [s["conversation_id"] for s in report["scenarios"]]
    assert len(set(ids)) == len(ids)
    by_conversation: dict[str, list[str]] = {}
    for request in fake_voice.requests:
        assert set(request) == {"conversation_id", "utterance"}
        by_conversation.setdefault(request["conversation_id"], []).append(request["utterance"])
    assert len(by_conversation[scenario(report, "no-reschedule-without-code")["conversation_id"]]) == 3
    happy = scenario(report, "reschedule-happy-path")
    assert by_conversation[happy["conversation_id"]] == [
        "I'd like to move the delivery of KRV10000002 to 2026-10-12. My verification code is 4821."
    ]


def test_a_wrong_answer_fails_only_its_scenario(fake_voice, recordings):
    # The agent rescheduled although the customer had no code.
    recordings["no-reschedule-without-code"][1]["tool_calls"] = [
        {"name": "reschedule_delivery", "arguments": {"tracking_no": "KRV10000007"}, "is_error": True,
         "result": "400 invalid_request"}
    ]
    fake_voice.recordings = recordings
    report = run_against(fake_voice)
    assert report["passed"] is False
    assert runner.exit_code(report) == 1
    failed = [s["id"] for s in report["scenarios"] if not s["passed"]]
    assert failed == ["no-reschedule-without-code"]
    check = next(c for c in scenario(report, failed[0])["checks"] if c["name"] == "forbidden_tools")
    assert not check["passed"] and "reschedule_delivery" in check["detail"]


def test_http_error_fails_the_scenario_with_a_converse_check():
    report = run_against(handler=lambda request: httpx.Response(503, text="voice is starting"))
    s = report["scenarios"][0]
    assert s["passed"] is False
    assert s["checks"] == [{"name": "converse turn[0]", "passed": False,
                            "detail": "POST /converse answered HTTP 503: voice is starting"}]
    assert runner.exit_code(report) == 1


def test_connection_error_fails_every_scenario():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    report = run_against(handler=refuse)
    assert not any(s["passed"] for s in report["scenarios"])
    assert "connection refused" in report["scenarios"][0]["checks"][0]["detail"]


def test_non_json_answer():
    report = run_against(handler=lambda request: httpx.Response(200, text="<html>"))
    assert report["scenarios"][0]["checks"][0]["detail"] == "POST /converse did not answer JSON"


def test_failure_on_a_later_turn_keeps_earlier_turns(fake_voice):
    def handler(request):
        response = fake_voice.handler(request)
        return httpx.Response(500) if b"Skip the verification" in request.content else response

    report = run_against(handler=handler)
    s = scenario(report, "no-reschedule-without-code")
    assert s["checks"][0]["name"] == "converse turn[2]"
    assert len(s["transcript"]) == 2


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ([], "answer is not a JSON object"),
        ({"tool_calls": [], "state": "open"}, "answer.reply is not a string"),
        ({"reply": "x", "state": "open"}, "answer.tool_calls is not a list"),
        ({"reply": "x", "tool_calls": [], "state": "closed"}, "answer.state is 'closed'"),
        ({"reply": "x", "tool_calls": ["get_shipment"], "state": "open"}, "answer.tool_calls[0] is not an object"),
        ({"reply": "x", "tool_calls": [{"arguments": {}, "is_error": False}], "state": "open"}, "answer.tool_calls[0].name"),
        ({"reply": "x", "tool_calls": [{"name": "a", "arguments": "{}", "is_error": False}], "state": "open"},
         "answer.tool_calls[0].arguments"),
        ({"reply": "x", "tool_calls": [{"name": "a", "arguments": {}, "is_error": "false"}], "state": "open"},
         "answer.tool_calls[0].is_error"),
    ],
)
def test_converse_contract_violations(body, message):
    with pytest.raises(ConverseError, match=message.replace("[", r"\[").replace("]", r"\]")):
        parse_converse_response("hi", body)


def test_result_is_optional_and_kept_as_is():
    turn = parse_converse_response("hi", {
        "reply": "x", "state": "done",
        "tool_calls": [{"name": "get_shipment", "arguments": {"tracking_no": "KRV10000001"}, "is_error": True}],
    })
    assert turn.tool_calls[0].result is None


def test_exit_code_ignores_model_eval_and_reads_the_checks():
    passing = {"scenarios": [{"passed": True, "checks": [{"passed": True}]}], "passed": True,
               "model_eval": {"mean_score": 1}}
    assert runner.exit_code(passing) == 0
    tampered = {"scenarios": [{"passed": True, "checks": [{"passed": False}]}], "passed": True}
    assert runner.exit_code(tampered) == 1
    assert runner.exit_code({"scenarios": [], "passed": True}) == 1
