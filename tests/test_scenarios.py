from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from kervan_evals.scenario import ScenarioError, expand_dates, load_scenario_file, load_scenarios

from conftest import SCENARIOS_DIR, TODAY

VALID = """\
id: sample
description: A sample.
turns:
  - Where is KRV10000001?
expect:
  tool_calls:
    - name: get_shipment
      arguments:
        tracking_no: KRV10000001
"""


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def errors_of(tmp_path: Path, text: str, name: str = "sample") -> list[str]:
    path = write(tmp_path, name, text)
    with pytest.raises(ScenarioError) as info:
        load_scenario_file(path, TODAY)
    return [e.split(": ", 1)[1] for e in info.value.errors]


def test_bundled_scenarios_are_valid():
    scenarios = load_scenarios(SCENARIOS_DIR, TODAY)
    assert 8 <= len(scenarios) <= 10
    ids = [s.id for s in scenarios]
    assert len(set(ids)) == len(ids)
    # Each seeded tracking number belongs to one scenario, so they can run in any order.
    owners: dict[str, str] = {}
    for s in scenarios:
        for number in set(re.findall(r"KRV\d{8}", " ".join(s.turns))):
            assert number not in owners, f"{number} used by {owners[number]} and {s.id}"
            owners[number] = s.id


def test_bundled_scenarios_cover_the_required_cases():
    scenarios = {s.id: s for s in load_scenarios(SCENARIOS_DIR, TODAY)}
    expected_tools = {c.name for s in scenarios.values() for c in s.expect.tool_calls}
    assert expected_tools == {"get_shipment", "reschedule_delivery", "open_ticket", "search_help"}
    categories = {c.arguments.get("category") for s in scenarios.values() for c in s.expect.tool_calls}
    assert {"DAMAGED", "LOST"} <= categories
    assert any(c.expect_error for s in scenarios.values() for c in s.expect.tool_calls)
    no_code = scenarios["no-reschedule-without-code"]
    assert "reschedule_delivery" in no_code.expect.forbidden_tools and len(no_code.turns) > 1


def test_bundled_scenario_readme_lists_every_tracking_number():
    readme = (SCENARIOS_DIR / "README.md").read_text(encoding="utf-8")
    for s in load_scenarios(SCENARIOS_DIR, TODAY):
        for number in re.findall(r"KRV\d{8}", " ".join(s.turns)):
            assert number in readme, number


def test_templates_expand_against_today():
    happy = next(s for s in load_scenarios(SCENARIOS_DIR, TODAY) if s.id == "reschedule-happy-path")
    assert "2026-10-12" in happy.turns[0]
    assert happy.expect.tool_calls[0].arguments["new_date"] == "2026-10-12"
    assert "2026-10-12" in happy.expect.reply_contains


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{{today}}", "2026-10-09"),
        ("on {{ today + 1 }}!", "on 2026-10-10!"),
        ("{{today-9}}", "2026-09-30"),
        ("{{today+30}} and {{today}}", "2026-11-08 and 2026-10-09"),
        ("no template", "no template"),
    ],
)
def test_expand_dates(text, expected):
    assert expand_dates(text, TODAY) == expected


def test_expand_dates_rejects_unknown_templates():
    with pytest.raises(ValueError, match="unknown template"):
        expand_dates("{{tomorrow}}", TODAY)


def test_valid_minimal_scenario(tmp_path):
    scenario = load_scenario_file(write(tmp_path, "sample", VALID), TODAY)
    assert scenario.id == "sample"
    assert scenario.turns == ("Where is KRV10000001?",)
    assert scenario.expect.tool_calls[0].arguments == {"tracking_no": "KRV10000001"}
    assert scenario.expect.tool_calls[0].expect_error is False


def test_unquoted_dates_stay_strings(tmp_path):
    text = VALID.replace("tracking_no: KRV10000001", "tracking_no: KRV10000001\n        new_date: 2026-10-12")
    scenario = load_scenario_file(write(tmp_path, "sample", text), TODAY)
    assert scenario.expect.tool_calls[0].arguments["new_date"] == "2026-10-12"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("description: A sample.\n", "description: A sample.\nnotes: x\n", "unknown field 'notes'"),
        ("description: A sample.\n", "", "missing required field 'description'"),
        ("id: sample", "id: Sample_1", "id: must be lower-case letters, digits and dashes"),
        ("id: sample", "id: other", "id: 'other' does not match the file name 'sample'"),
        ("  - Where is KRV10000001?\n", "", "turns: must be a non-empty list"),
        ("  - Where is KRV10000001?", "  - ''", "turns[0]: must be a non-empty string"),
        ("  - Where is KRV10000001?", "  - On {{yesterday}}", "turns[0]: unknown template '{{yesterday}}'"),
        ("name: get_shipment", "name: cancel_shipment", "expect.tool_calls[0].name: unknown tool 'cancel_shipment'"),
        ("tracking_no: KRV10000001", "tracking_no: 10000001", "expect.tool_calls[0].arguments.tracking_no: argument values must be strings"),
        ("      arguments:", "      expect_error: maybe\n      arguments:", "expect.tool_calls[0].expect_error: must be true or false"),
        ("      arguments:", "      args: {}\n      arguments:", "expect.tool_calls[0]: unknown field 'args'"),
        ("expect:\n", "expect:\n  final_state: closed\n", "expect.final_state: must be one of open, done"),
        ("expect:\n", "expect:\n  forbidden_tools: [get_shipment]\n", "expect: tool 'get_shipment' is both expected and forbidden"),
        ("expect:\n", "expect:\n  reply_contains: hello\n", "expect.reply_contains: must be a list"),
        ("expect:\n", "expect:\n  checks: []\n", "expect: unknown field 'checks'"),
    ],
)
def test_validation_errors(tmp_path, old, new, message):
    assert old in VALID
    errors = errors_of(tmp_path, VALID.replace(old, new, 1))
    assert any(e.startswith(message) for e in errors), errors


def test_empty_expect_is_refused(tmp_path):
    text = VALID.split("expect:")[0] + "expect: {}\n"
    assert errors_of(tmp_path, text) == ["expect: must state at least one expectation"]


def test_duplicate_keys_are_refused(tmp_path):
    errors = errors_of(tmp_path, VALID.replace("id: sample", "id: sample\nid: sample"))
    assert any("duplicate key 'id'" in e for e in errors)


def test_document_must_be_a_mapping(tmp_path):
    path = write(tmp_path, "sample", "- just a list\n")
    with pytest.raises(ScenarioError, match="must be a mapping"):
        load_scenario_file(path, TODAY)


def test_all_errors_are_reported_together(tmp_path):
    text = VALID.replace("name: get_shipment", "name: nope").replace("id: sample", "id: other")
    errors = errors_of(tmp_path, text)
    assert len(errors) == 2


def test_directory_errors(tmp_path):
    with pytest.raises(ScenarioError, match="not a directory"):
        load_scenarios(tmp_path / "missing", TODAY)
    with pytest.raises(ScenarioError, match="no scenario files"):
        load_scenarios(tmp_path, TODAY)


def test_errors_across_files_are_collected_and_ids_unique(tmp_path):
    write(tmp_path, "sample", VALID)
    (tmp_path / "sample.yml").write_text(VALID, encoding="utf-8")
    write(tmp_path, "broken", "id: broken\n")
    with pytest.raises(ScenarioError) as info:
        load_scenarios(tmp_path, TODAY)
    text = "\n".join(info.value.errors)
    assert "broken.yaml" in text and "missing required field 'turns'" in text
    assert "'sample' is also used by" in text


def test_load_order_is_by_file_name(tmp_path):
    for name in ("b-two", "a-one"):
        write(tmp_path, name, VALID.replace("id: sample", f"id: {name}"))
    assert [s.id for s in load_scenarios(tmp_path, dt.date(2026, 1, 1))] == ["a-one", "b-two"]
