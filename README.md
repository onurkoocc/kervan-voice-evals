# kervan-voice-evals

Scenario evaluations of **kervan-voice**, the Kervan customer agent (Kervan
SPEC §1, §8, §9). Each scenario plays a short customer conversation against
`POST /converse` and checks deterministically what the agent did: which
kervan-mcp tools it called, with which arguments, whether a call failed, what
it must never call, what its last reply says, and the conversation state.

An optional judge model scores the transcripts as well. That score is a
quality gate, never a test: it is reported and never changes the exit code.

All data is synthetic.

## Usage

```sh
python -m kervan_evals run --voice-url http://voice:7000 [--scenarios DIR] [--judge anthropic] [--out FILE]
python -m kervan_evals validate [--scenarios DIR]
```

| Option | Meaning |
|---|---|
| `--voice-url URL` | Base URL of kervan-voice (required for `run`) |
| `--scenarios DIR` | Scenario directory, default `./scenarios` |
| `--judge anthropic` | Also score each transcript with a judge model (see below) |
| `--out FILE` | Also write the report to `FILE` (it is always printed to stdout) |
| `--today YYYY-MM-DD` | Date that `{{today}}` templates expand to; default today in `Europe/Istanbul` |
| `--timeout SECONDS` | Per-request timeout, default 30 |

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Every deterministic check of every scenario passed (`validate`: the scenarios are valid) |
| 1 | At least one check failed, or kervan-voice could not be reached or broke the `/converse` contract |
| 2 | Usage error or invalid scenario files (every problem is printed to stderr) |

## Scenarios

One YAML file per scenario in `scenarios/`; the file name is the `id`.
[`scenarios/README.md`](scenarios/README.md) lists the seed data each one
assumes and the replies the scripted agent is expected to give.

```yaml
id: reschedule-happy-path
description: The customer gives everything in one message; the agent reschedules.
turns:                      # customer utterances, sent in order to one fresh conversation
  - Move KRV10000002 to {{today+3}}. My verification code is 4821.
expect:                     # at least one of the four below
  tool_calls:               # must appear in this order; other calls may sit between them
    - name: reschedule_delivery
      arguments:            # a subset: these keys must be present with these values
        tracking_no: KRV10000002
        new_date: "{{today+3}}"
        verification_code: "4821"
      expect_error: false   # default; true means the call must have is_error: true
  reply_contains: [rescheduled]   # substrings of the final reply, case-insensitive
  forbidden_tools: [open_ticket]  # never called, in any turn, even if it failed
  final_state: done               # state after the last turn: open | done
```

Loading is strict: unknown fields, duplicate keys, unknown tool names,
non-string argument values (quote `"4821"`), a tool both expected and
forbidden, and unknown templates are all refused. `{{today}}`,
`{{today+N}}` and `{{today-N}}` expand to ISO dates in turns, argument values
and `reply_contains`.

## Report

```json
{
  "format": "kervan/eval-report/0.1",
  "voice_url": "http://voice:7000",
  "today": "2026-10-09",
  "scenarios": [
    {
      "id": "status-lookup",
      "passed": true,
      "checks": [{"name": "tool_call[0] get_shipment", "passed": true, "detail": "call #0 get_shipment(...) is_error=false"}],
      "conversation_id": "eval-status-lookup-3f2a9c1b7d4e",
      "transcript": [{"utterance": "...", "reply": "...", "tool_calls": [...], "state": "done"}]
    }
  ],
  "passed": true,
  "model_eval": { "...": "only with --judge" }
}
```

Check names: `tool_call[i] <tool>`, `forbidden_tools`, `reply_contains '<text>'`,
`final_state`, and `converse turn[i]` when a request failed (the remaining
checks of that scenario are then skipped and it fails).

## Judge (`--judge anthropic`)

Uses the `anthropic` SDK with the model in `EVAL_JUDGE_MODEL` (default
`claude-sonnet-5-5`, effort `medium`, JSON-schema output). The key comes from
`ANTHROPIC_API_KEY`, else `CLAUDE_API_KEY`, else the SDK's own credential
chain. One request per scenario; `model_eval` holds:

| Field | Meaning |
|---|---|
| `model_requested` | The model id sent |
| `model` / `model_snapshots` | The snapshot the API answered with (`null` / several if they differed) |
| `affects_exit_code` | Always `false` |
| `mean_score` | Mean of the 1–5 scores that were obtained |
| `scenarios[]` | `id`, `score`, `rationale`, `model`, `prompt_sha256`, and `error` when no usable verdict came back |

`prompt_sha256` is the SHA-256 of the canonical JSON (sorted keys, no
whitespace) of the exact request parameters: model, max_tokens, system,
messages and output_config. API errors, refusals and malformed answers are
recorded per scenario; the run continues and the exit code is unchanged.

The judge costs money. Tests never call the real API (the client is faked).

## Development

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv -r requirements-dev.txt
.venv/Scripts/python -m pytest      # Windows; .venv/bin/python on Linux/macOS
```

`requirements.txt` pins every runtime dependency (resolved with
`uv pip compile --universal`); `pyproject.toml` carries the ranges.
`tests/data/recordings.json` holds recorded `/converse` answers; the tests
replay them through `httpx.MockTransport` and through a real local HTTP
server.

## Docker

```sh
docker build -t kervan-voice-evals .
# on the network kervan-voice is reachable from:
docker run --rm --network <compose network> kervan-voice-evals run --voice-url http://voice:7000
```

Per SPEC §11 the `deps` stage sees only the requirement files and downloads
wheels (`pip download --only-binary=:all:`). Installing, the test suite and
the scenario validation then run in `RUN --network=none` steps; the runtime
image is only produced when the tests pass. It runs as the non-root user
`evals`.
