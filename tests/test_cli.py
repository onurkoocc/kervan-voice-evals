from __future__ import annotations

import json
import subprocess
import sys

import httpx

from kervan_evals.cli import main

from conftest import ROOT, SCENARIOS_DIR


def run_cli(*args: str) -> int:
    return main(["run", "--scenarios", str(SCENARIOS_DIR), "--today", "2026-10-09", *args])


def test_run_against_a_live_fake_voice(voice_server, tmp_path, capsys):
    out = tmp_path / "reports" / "eval.json"
    assert run_cli("--voice-url", voice_server + "/", "--out", str(out)) == 0

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report == json.loads(out.read_text(encoding="utf-8"))
    assert report["format"] == "kervan/eval-report/0.1"
    assert report["voice_url"] == voice_server
    assert report["passed"] is True
    assert "model_eval" not in report
    assert "10/10 scenarios passed" in captured.err


def test_run_exits_1_when_a_check_fails(voice_server, fake_voice, capsys):
    fake_voice.recordings["status-lookup"][0]["reply"] = "It is somewhere."
    assert run_cli("--voice-url", voice_server) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert [s["id"] for s in report["scenarios"] if not s["passed"]] == ["status-lookup"]


def test_unreachable_voice_exits_1(capsys):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    code = main(["run", "--voice-url", "http://voice:7000", "--scenarios", str(SCENARIOS_DIR)],
                transport=httpx.MockTransport(refuse))
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert all(s["checks"][0]["name"] == "converse turn[0]" for s in report["scenarios"])


def test_invalid_scenarios_exit_2(tmp_path, capsys):
    (tmp_path / "bad.yaml").write_text("id: bad\n", encoding="utf-8")
    assert main(["run", "--voice-url", "http://voice:7000", "--scenarios", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bad.yaml: missing required field 'turns'" in captured.err


def test_usage_errors_exit_2(capsys):
    assert main(["run"]) == 2  # --voice-url is required
    assert main(["run", "--voice-url", "x", "--today", "10/09/2026"]) == 2
    assert main(["run", "--voice-url", "x", "--judge", "openai"]) == 2
    assert main([]) == 2
    capsys.readouterr()


def test_validate(capsys):
    assert main(["validate", "--scenarios", str(SCENARIOS_DIR)]) == 0
    assert "10 scenarios valid" in capsys.readouterr().err


def test_module_entry_point():
    result = subprocess.run(
        [sys.executable, "-m", "kervan_evals", "validate", "--scenarios", str(SCENARIOS_DIR)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "scenarios valid" in result.stderr
