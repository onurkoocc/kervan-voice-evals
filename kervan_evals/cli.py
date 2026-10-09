"""Command line: `python -m kervan_evals run|validate`.

Exit codes: 0 every deterministic check passed (or the scenarios are valid),
1 a check failed, 2 usage error or invalid scenario files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from . import __version__, judge, runner
from .scenario import ScenarioError, load_scenarios

# The core's clock zone (SPEC §5); `{{today+N}}` templates expand against it.
CLOCK_ZONE = ZoneInfo("Europe/Istanbul")


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m kervan_evals", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--scenarios", type=Path, default=Path("scenarios"), metavar="DIR",
                         help="directory of *.yaml scenarios (default: ./scenarios)")
        sub.add_argument("--today", type=_date, metavar="YYYY-MM-DD",
                         help="date that {{today}} templates expand to (default: today in Europe/Istanbul)")

    run = commands.add_parser("run", help="run the scenarios against kervan-voice and print the report")
    run.add_argument("--voice-url", required=True, metavar="URL", help="base URL of kervan-voice, e.g. http://voice:7000")
    common(run)
    run.add_argument("--judge", choices=["anthropic"], help="also score transcripts with a judge model (quality gate only)")
    run.add_argument("--out", type=Path, metavar="FILE", help="also write the report to FILE")
    run.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS", help="per-request timeout (default 30)")

    validate = commands.add_parser("validate", help="load and validate the scenarios, then exit")
    common(validate)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    judge_client_factory: Callable[[], judge.MessagesClient] | None = None,
) -> int:
    """Entry point. `transport` and `judge_client_factory` exist for tests."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse exits 2 on usage errors, 0 on --help
        return int(exc.code or 0)

    today = args.today or dt.datetime.now(CLOCK_ZONE).date()
    try:
        scenarios = load_scenarios(args.scenarios, today)
    except ScenarioError as exc:
        for line in exc.errors:
            print(line, file=sys.stderr)
        return 2

    if args.command == "validate":
        print(f"{len(scenarios)} scenarios valid", file=sys.stderr)
        return 0

    voice_url = args.voice_url.rstrip("/")
    with httpx.Client(base_url=voice_url, timeout=args.timeout, transport=transport) as client:
        report: dict[str, Any] = runner.run(scenarios, client, voice_url, today)

    if args.judge == "anthropic":
        descriptions = {s.id: s.description for s in scenarios}
        model = judge.judge_model()
        try:
            client = (judge_client_factory or judge.default_client)()
        except Exception as exc:  # the judge must never change the outcome
            report["model_eval"] = {"judge": "anthropic", "model_requested": model, "affects_exit_code": False,
                                    "error": f"cannot create the client: {type(exc).__name__}: {exc}"}
        else:
            report["model_eval"] = judge.judge(report, descriptions, client, model)

    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")

    passed = sum(1 for s in report["scenarios"] if s["passed"])
    print(f"{passed}/{len(report['scenarios'])} scenarios passed", file=sys.stderr)
    return runner.exit_code(report)
