from __future__ import annotations

from kervan_evals.checks import ToolCall, Turn, evaluate, is_subset
from kervan_evals.scenario import ExpectedCall, Expectation


def call(name: str, is_error: bool = False, result: object = None, **arguments: object) -> ToolCall:
    return ToolCall(name=name, arguments=dict(arguments), is_error=is_error, result=result)


def turn(*calls: ToolCall, reply: str = "ok", state: str = "done") -> Turn:
    return Turn(utterance="u", reply=reply, tool_calls=tuple(calls), state=state)


def by_name(checks):
    return {c.name: c for c in checks}


def test_is_subset():
    assert is_subset({}, {"a": "1"})
    assert is_subset({"a": "1"}, {"a": "1", "b": "2"})
    assert is_subset({"a": {"b": "1"}}, {"a": {"b": "1", "c": "2"}})
    assert not is_subset({"a": "1"}, {"b": "1"})
    assert not is_subset({"a": "1"}, {"a": "2"})
    assert not is_subset({"a": {"b": "1"}}, {"a": "x"})
    # No loose typing: an integer never matches the string a tool sends.
    assert not is_subset({"code": 4821}, {"code": "4821"})
    assert not is_subset({"flag": 1}, {"flag": True})


def test_expected_calls_match_in_order_across_turns():
    expect = Expectation(tool_calls=(
        ExpectedCall("get_shipment", {"tracking_no": "KRV10000001"}),
        ExpectedCall("open_ticket", {"category": "LOST"}),
    ))
    turns = [
        turn(call("get_shipment", tracking_no="KRV10000001")),
        turn(call("search_help", query="lost"), call("open_ticket", category="LOST", description="x")),
    ]
    checks = evaluate(expect, turns)
    assert [c.passed for c in checks] == [True, True]
    assert "call #2 open_ticket" in checks[1].detail


def test_out_of_order_call_fails():
    expect = Expectation(tool_calls=(ExpectedCall("get_shipment", {}), ExpectedCall("open_ticket", {})))
    checks = evaluate(expect, [turn(call("open_ticket"), call("get_shipment"))])
    assert checks[0].passed
    assert not checks[1].passed
    assert checks[1].detail == "open_ticket was not called after call #1"


def test_missing_call_and_argument_mismatch():
    expect = Expectation(tool_calls=(
        ExpectedCall("reschedule_delivery", {"verification_code": "4821"}),
        ExpectedCall("open_ticket", {}),
    ))
    checks = evaluate(expect, [turn(call("reschedule_delivery", verification_code="1111"))])
    assert not checks[0].passed
    assert "no call matched arguments" in checks[0].detail and "1111" in checks[0].detail
    assert checks[1].detail == "open_ticket was not called"


def test_is_error_must_match_expectation():
    ok = Expectation(tool_calls=(ExpectedCall("get_shipment", {}),))
    err = Expectation(tool_calls=(ExpectedCall("get_shipment", {}, expect_error=True),))
    failed = [turn(call("get_shipment", is_error=True, result="404 not_found"))]
    succeeded = [turn(call("get_shipment", result={"status": "IN_TRANSIT"}))]

    check = evaluate(ok, failed)[0]
    assert not check.passed and "expected success" in check.detail and "404 not_found" in check.detail
    assert evaluate(err, failed)[0].passed
    check = evaluate(err, succeeded)[0]
    assert not check.passed and "expected an error" in check.detail


def test_a_later_call_with_the_right_outcome_matches():
    expect = Expectation(tool_calls=(ExpectedCall("get_shipment", {"tracking_no": "KRV10000001"}),))
    turns = [turn(call("get_shipment", is_error=True, tracking_no="KRV10000001"),
                  call("get_shipment", tracking_no="KRV10000001"))]
    check = evaluate(expect, turns)[0]
    assert check.passed and check.detail.startswith("call #1")


def test_forbidden_tools():
    expect = Expectation(forbidden_tools=("reschedule_delivery", "open_ticket"))
    check = evaluate(expect, [turn(call("get_shipment"))])[0]
    assert check.passed and check.name == "forbidden_tools"

    # A forbidden call fails the check even when the tool answered with an error.
    check = evaluate(expect, [turn(), turn(call("reschedule_delivery", is_error=True, tracking_no="KRV10000007"))])[0]
    assert not check.passed
    assert "call #0 reschedule_delivery" in check.detail


def test_reply_contains_is_case_insensitive_and_reads_the_final_reply_only():
    expect = Expectation(reply_contains=("Verification Code", "TCK-"))
    turns = [turn(reply="TCK-000001 opened"), turn(reply="Please send the verification code.")]
    checks = by_name(evaluate(expect, turns))
    assert checks["reply_contains 'Verification Code'"].passed
    assert not checks["reply_contains 'TCK-'"].passed
    assert "Please send" in checks["reply_contains 'TCK-'"].detail


def test_final_state():
    expect = Expectation(final_state="open")
    assert evaluate(expect, [turn(state="done"), turn(state="open")])[0].passed
    check = evaluate(expect, [turn(state="done")])[0]
    assert not check.passed and check.detail == "expected open, got done"


def test_no_expectations_no_checks():
    assert evaluate(Expectation(), [turn()]) == []
