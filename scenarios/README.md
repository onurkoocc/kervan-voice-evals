# Scenarios

One YAML file per scenario; the file name (without `.yaml`) is the scenario
`id`. They run against kervan-voice in `AGENT_MODE=scripted` on the `staging`
environment, whose seed (`kervan-system/seed/staging.sql`) must match the
table below.

## Seed data the scenarios assume

"today" is the current date in `Europe/Istanbul`, the core's clock zone. The
evals expand `{{today+N}}` against the same date (override with `--today`),
so the seed must write delivery dates relative to the database's current
date (for example `current_date + 2` with the session time zone set to
`Europe/Istanbul`), not as fixed dates.

Each tracking number belongs to exactly one scenario, so scenarios can run in
any order and against the same environment more than once (a rerun of
`reschedule-happy-path` reschedules the same parcel again, which core allows).

| Tracking number | Scenario | Shipment status | Recipient phone, last 4 digits | Delivery row | Notes |
|---|---|---|---|---|---|
| `KRV10000001` | `status-lookup` | `IN_TRANSIT` | any | `SCHEDULED`, today+2 | `estimatedDelivery` set; at least one shipment event |
| `KRV10000002` | `reschedule-happy-path` | `IN_TRANSIT` | `4821` | `SCHEDULED`, today+5 | New date today+3 must be accepted (rule: tomorrow ≤ date ≤ today+14) |
| `KRV10000003` | `reschedule-missing-code` | `IN_TRANSIT` | `7310` | `SCHEDULED`, today+2 | Never actually rescheduled; the agent must ask for the code |
| `KRV10000004` | `reschedule-delivered-refused` | `DELIVERED` | `5066` | `DELIVERED`, today−2, attempts 1 | Correct code, so the refusal is `409 not_reschedulable`, not `403` |
| `KRV10000005` | `damaged-parcel-ticket` | `DELIVERED` | any | `DELIVERED`, today−1, attempts 1 | A ticket with category `DAMAGED` is created on each run |
| `KRV10000006` | `lost-parcel-ticket` | `IN_TRANSIT` | any | `SCHEDULED`, today−5 (overdue) | `estimatedDelivery` in the past; a `LOST` ticket is created on each run |
| `KRV10000007` | `no-reschedule-without-code` | `IN_TRANSIT` | `2290` | `SCHEDULED`, today+3 | Must never be rescheduled by the evals |
| `KRV10000008` | `reschedule-wrong-code` | `IN_TRANSIT` | `6142` | `SCHEDULED`, today+4 | The customer says `1111`, so core answers `403 verification_failed` |
| `KRV99999999` | `unknown-tracking-number` | — | — | — | Must **not** exist; `get_shipment` answers `404 not_found` |

`help-question` uses no tracking number; it needs at least one knowledge base
article in kervan-search (any article: the scenario only checks that
`search_help` was called and no shipment tool was).

## Agent behaviour the scenarios assume

These follow the scripted English policy of kervan-voice (SPEC §8). Where the
SPEC leaves a choice open, the scenarios fix it as below; the scripted agent
should be written to match.

| Scenario | Expected tool calls (in order, others may sit between) | Final reply must contain (case-insensitive) | Final state |
|---|---|---|---|
| `status-lookup` | `get_shipment` | the tracking number; the status as words, `in transit` | any |
| `reschedule-happy-path` | `reschedule_delivery` with the code and date | `rescheduled`; the new date as `YYYY-MM-DD` | `done` |
| `reschedule-missing-code` | none (`reschedule_delivery` forbidden) | `verification code` | `open` |
| `reschedule-delivered-refused` | `reschedule_delivery`, answered with an error | `cannot be rescheduled` | any |
| `damaged-parcel-ticket` | `open_ticket` with `category: DAMAGED` | the ticket id prefix `TCK-` | `done` |
| `lost-parcel-ticket` | `open_ticket` with `category: LOST` | `TCK-` | `done` |
| `help-question` | `search_help` (no shipment tools) | — | any |
| `unknown-tracking-number` | `get_shipment`, answered with an error | the tracking number; `could not find` | any |
| `no-reschedule-without-code` | none over three turns (`reschedule_delivery` forbidden) | `verification code` | `open` |
| `reschedule-wrong-code` | `reschedule_delivery`, answered with an error | `verification code` | any |

Policy choices this table makes:

- When the customer gives tracking number, date and code, the agent calls
  `reschedule_delivery` directly and relays core's refusal; it does not
  pre-check the status with `get_shipment` and refuse on its own (an extra
  `get_shipment` before the call is still allowed).
- `state` is `open` while the agent is waiting for information from the
  customer, `done` once the request was carried out.
- "tomorrow" in an utterance means today+1 in `Europe/Istanbul`.

`tests/data/recordings.json` holds one answer sequence per scenario that
passes every check; it doubles as a worked example of the replies above.
