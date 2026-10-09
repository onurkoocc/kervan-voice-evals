from __future__ import annotations

import copy
import datetime as dt
import json
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = ROOT / "scenarios"
RECORDINGS = ROOT / "tests" / "data" / "recordings.json"
TODAY = dt.date(2026, 10, 9)  # the date the recordings were made for


def load_recordings() -> dict[str, list[dict[str, Any]]]:
    data = json.loads(RECORDINGS.read_text(encoding="utf-8"))
    data.pop("_comment")
    return data


class FakeVoice:
    """Replays recorded /converse answers.

    The scenario is read from the conversation id (`eval-<scenario id>-<hex>`),
    the answer from how many turns that conversation has had so far.
    """

    def __init__(self, recordings: dict[str, list[dict[str, Any]]]):
        self.recordings = recordings
        self.turns: dict[str, int] = defaultdict(int)
        self.requests: list[dict[str, Any]] = []

    def answer(self, body: dict[str, Any]) -> tuple[int, Any]:
        self.requests.append(body)
        conversation_id = body["conversation_id"]
        scenario_id = conversation_id.removeprefix("eval-").rsplit("-", 1)[0]
        script = self.recordings.get(scenario_id)
        n = self.turns[conversation_id]
        self.turns[conversation_id] += 1
        if script is None or n >= len(script):
            return 404, {"error": "no recording"}
        return 200, script[n]

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/converse"
        status, body = self.answer(json.loads(request.content))
        return httpx.Response(status, json=body)


@pytest.fixture(autouse=True)
def no_real_judge(monkeypatch):
    """Tests never reach the Anthropic API: the real client factory is disabled."""

    def forbidden():
        raise AssertionError("a test tried to create a real Anthropic client")

    monkeypatch.setattr("kervan_evals.judge.default_client", forbidden)
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def recordings() -> dict[str, list[dict[str, Any]]]:
    return copy.deepcopy(load_recordings())


@pytest.fixture
def fake_voice(recordings) -> FakeVoice:
    return FakeVoice(recordings)


@pytest.fixture
def voice_server(fake_voice) -> Iterator[str]:
    """A real HTTP server on localhost serving `fake_voice`; yields its base URL."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server naming)
            if self.path != "/converse":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            status, body = fake_voice.answer(json.loads(self.rfile.read(length)))
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
