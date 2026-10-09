"""Scenario files: strict loading, validation and date templates.

A scenario is a YAML file (SPEC §9):

    id: reschedule-happy-path
    description: ...
    turns: ["customer utterance", ...]
    expect:
      tool_calls: [{name, arguments (subset), expect_error}]
      reply_contains: [...]
      forbidden_tools: [...]
      final_state: open | done

Strings in `turns`, tool-call `arguments` and `reply_contains` may use the
templates `{{today}}` and `{{today+N}}` / `{{today-N}}`; they expand to ISO
dates relative to the run's "today" (Europe/Istanbul, the core's clock zone).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# The kervan-mcp tool names (SPEC §4).
TOOLS = ("get_shipment", "reschedule_delivery", "open_ticket", "search_help")
STATES = ("open", "done")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}")
_TODAY_RE = re.compile(r"^\s*today\s*(?:([+-])\s*(\d{1,3}))?\s*$")

_TOP_KEYS = {"id", "description", "turns", "expect"}
_EXPECT_KEYS = {"tool_calls", "reply_contains", "forbidden_tools", "final_state"}
_CALL_KEYS = {"name", "arguments", "expect_error"}


class ScenarioError(ValueError):
    """One or more scenario files are invalid. `errors` lists every problem."""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class ExpectedCall:
    name: str
    arguments: dict[str, Any]
    expect_error: bool = False


@dataclass(frozen=True)
class Expectation:
    tool_calls: tuple[ExpectedCall, ...] = ()
    reply_contains: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    final_state: str | None = None


@dataclass(frozen=True)
class Scenario:
    id: str
    description: str
    turns: tuple[str, ...]
    expect: Expectation


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys and keeps dates as strings."""


_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def expand_dates(text: str, today: dt.date) -> str:
    """Replace `{{today}}`, `{{today+N}}` and `{{today-N}}` with ISO dates."""

    def repl(match: re.Match[str]) -> str:
        inner = _TODAY_RE.match(match.group(1))
        if inner is None:
            raise ValueError(f"unknown template {match.group(0)!r}")
        sign, days = inner.groups()
        offset = int(days or 0) * (-1 if sign == "-" else 1)
        return (today + dt.timedelta(days=offset)).isoformat()

    return _TEMPLATE_RE.sub(repl, text)


class _Validator:
    def __init__(self, source: str, today: dt.date):
        self.source = source
        self.today = today
        self.errors: list[str] = []

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{self.source}: {where}: {message}" if where else f"{self.source}: {message}")

    def keys(self, value: dict, allowed: set[str], required: set[str], where: str) -> None:
        for key in value:
            if key not in allowed:
                self.error(where, f"unknown field {key!r}")
        for key in sorted(required - value.keys()):
            self.error(where, f"missing required field {key!r}")

    def text(self, value: Any, where: str) -> str | None:
        if not isinstance(value, str) or not value.strip():
            self.error(where, "must be a non-empty string")
            return None
        try:
            return expand_dates(value, self.today)
        except ValueError as exc:
            self.error(where, str(exc))
            return None

    def text_list(self, value: Any, where: str, *, allow_empty: bool) -> tuple[str, ...]:
        if not isinstance(value, list) or (not value and not allow_empty):
            self.error(where, "must be a non-empty list" if not allow_empty else "must be a list")
            return ()
        items = [self.text(item, f"{where}[{i}]") for i, item in enumerate(value)]
        return tuple(item for item in items if item is not None)

    def tool_name(self, value: Any, where: str) -> str | None:
        if value not in TOOLS:
            self.error(where, f"unknown tool {value!r} (expected one of {', '.join(TOOLS)})")
            return None
        return value

    def arguments(self, value: Any, where: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.error(where, "must be a mapping")
            return {}
        result: dict[str, Any] = {}
        for key, item in value.items():
            # Every kervan-mcp tool argument is a string; an unquoted 4821 would
            # never match "4821", so refuse it here instead of failing at run time.
            if not isinstance(key, str) or not isinstance(item, str):
                self.error(f"{where}.{key}", "argument values must be strings (quote them)")
                continue
            expanded = self.text(item, f"{where}.{key}")
            if expanded is not None:
                result[key] = expanded
        return result

    def tool_call(self, value: Any, where: str) -> ExpectedCall | None:
        if not isinstance(value, dict):
            self.error(where, "must be a mapping")
            return None
        self.keys(value, _CALL_KEYS, {"name"}, where)
        name = self.tool_name(value.get("name"), f"{where}.name")
        arguments = self.arguments(value.get("arguments", {}), f"{where}.arguments")
        expect_error = value.get("expect_error", False)
        if not isinstance(expect_error, bool):
            self.error(f"{where}.expect_error", "must be true or false")
            expect_error = False
        if name is None:
            return None
        return ExpectedCall(name=name, arguments=arguments, expect_error=expect_error)

    def expectation(self, value: Any) -> Expectation:
        if not isinstance(value, dict):
            self.error("expect", "must be a mapping")
            return Expectation()
        self.keys(value, _EXPECT_KEYS, set(), "expect")
        if not value.keys() & _EXPECT_KEYS:
            self.error("expect", "must state at least one expectation")

        calls: list[ExpectedCall] = []
        raw_calls = value.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            self.error("expect.tool_calls", "must be a list")
        else:
            for i, item in enumerate(raw_calls):
                call = self.tool_call(item, f"expect.tool_calls[{i}]")
                if call is not None:
                    calls.append(call)

        reply_contains = self.text_list(value.get("reply_contains", []), "expect.reply_contains", allow_empty=True)

        forbidden: list[str] = []
        raw_forbidden = value.get("forbidden_tools", [])
        if not isinstance(raw_forbidden, list):
            self.error("expect.forbidden_tools", "must be a list")
        else:
            for i, item in enumerate(raw_forbidden):
                name = self.tool_name(item, f"expect.forbidden_tools[{i}]")
                if name is not None:
                    forbidden.append(name)
        for name in sorted({c.name for c in calls} & set(forbidden)):
            self.error("expect", f"tool {name!r} is both expected and forbidden")

        final_state = value.get("final_state")
        if final_state is not None and final_state not in STATES:
            self.error("expect.final_state", f"must be one of {', '.join(STATES)}")
            final_state = None

        return Expectation(
            tool_calls=tuple(calls),
            reply_contains=reply_contains,
            forbidden_tools=tuple(forbidden),
            final_state=final_state,
        )


def parse_scenario(data: Any, source: str, today: dt.date, *, expected_id: str | None = None) -> Scenario:
    """Validate one parsed YAML document. Raises ScenarioError listing every problem."""
    v = _Validator(source, today)
    if not isinstance(data, dict):
        raise ScenarioError([f"{source}: the document must be a mapping"])
    v.keys(data, _TOP_KEYS, _TOP_KEYS, "")

    scenario_id = data.get("id")
    id_ok = isinstance(scenario_id, str) and _ID_RE.match(scenario_id) is not None
    if "id" in data and not id_ok:
        v.error("id", "must be lower-case letters, digits and dashes")
    elif id_ok and expected_id is not None and scenario_id != expected_id:
        v.error("id", f"{scenario_id!r} does not match the file name {expected_id!r}")

    description = data.get("description")
    if "description" in data and (not isinstance(description, str) or not description.strip()):
        v.error("description", "must be a non-empty string")

    turns = v.text_list(data["turns"], "turns", allow_empty=False) if "turns" in data else ()
    expect = v.expectation(data["expect"]) if "expect" in data else Expectation()
    if v.errors:
        raise ScenarioError(v.errors)
    return Scenario(id=scenario_id, description=description.strip(), turns=turns, expect=expect)


def load_scenario_file(path: Path, today: dt.date) -> Scenario:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ScenarioError([f"{path}: {exc}"]) from exc
    return parse_scenario(data, str(path), today, expected_id=path.stem)


def load_scenarios(directory: Path, today: dt.date) -> list[Scenario]:
    """Load every `*.yaml` / `*.yml` file in `directory`, sorted by file name."""
    if not directory.is_dir():
        raise ScenarioError([f"{directory}: not a directory"])
    paths = sorted(p for p in directory.iterdir() if p.suffix in (".yaml", ".yml") and p.is_file())
    if not paths:
        raise ScenarioError([f"{directory}: no scenario files (*.yaml)"])

    errors: list[str] = []
    scenarios: list[Scenario] = []
    seen: dict[str, Path] = {}
    for path in paths:
        try:
            scenario = load_scenario_file(path, today)
        except ScenarioError as exc:
            errors.extend(exc.errors)
            continue
        if scenario.id in seen:
            errors.append(f"{path}: id: {scenario.id!r} is also used by {seen[scenario.id]}")
            continue
        seen[scenario.id] = path
        scenarios.append(scenario)
    if errors:
        raise ScenarioError(errors)
    return scenarios
