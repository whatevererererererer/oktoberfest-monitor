# Oktoberfest 2026 Reservation Monitor

Read-only availability monitor for Friday, **25 September 2026**, and
Saturday, **26 September 2026**. It never books, submits a reservation form,
or bypasses a CAPTCHA.

The repository contains 19 tent configurations. Eleven portals with validated
date controls are enabled; eight unsupported, bot-protected, contact-only,
widget, or out-of-scope tents remain disabled. Per-run health still reflects
whether each enabled portal also permits a date-correlated shift update.

## Runtime model

An external cron-job.org job dispatches `.github/workflows/monitor.yml`
approximately every four minutes. A run has two durable phases:

1. Probe every enabled tent, update health/baselines, and enqueue stable alert
   events in `state/state.json`.
2. Commit and push that outbox before sending anything. Deliver at most one
   Pushover message part per process and Git-checkpoint its cursor immediately.

The workflow always checks out current `main`, has one non-interrupted state
writer, retains at most one pending run, fails closed on a state conflict, and
never hides a final push failure. New dispatches replace only the pending run;
they do not cancel a writer during a Git/Pushover checkpoint. A crash after
Pushover accepts a message but before the Git checkpoint can repeat that one
ambiguous part; Pushover offers no idempotency key, so stronger exactly-once
delivery is not possible. Already checkpointed parts of a burst are not
intentionally repeated.

The 210-second internal delivery budget starts before Python/dependency and
Playwright setup (immediately after checkout), leaving margin before the
six-minute hard job timeout for the final state checkpoint. Remaining outbox
parts resume in the next, current-`main` run.

## Probe and health invariants

For the active `festzelt_os` mode:

- `available`: the target date exists and native select interaction produced
  at least one shift with a confirmed, target-correlated DOM update.
- `unavailable`: one unambiguous, plausible Oktoberfest date control exists,
  but the target date is absent.
- `unknown` / degraded: the target exists, but its shift control, options, or
  update cannot be proved.
- `error`: missing or ambiguous base control, bot/login/error page, timeout,
  invalid structure, navigation failure, or technical probe failure.

Unknown/error observations are stored separately and do not overwrite the last
reliable availability and shift baseline. Diagnostics are intentionally small:
page type, control/option counts, target/update evidence, shift count, and an
error class. Page HTML, cookies, tokens, and form values are never stored.

Both targets are selected sequentially on one low-load page per tent with
Playwright's native `select_option`. Date and shift controls are reacquired
after every rerender, and unchanged options from the previous date are rejected
unless a concrete relevant mutation proves the wizard updated. Failed Livewire
update responses are classified separately from an honestly empty shift step.
The browser uses Playwright's native Chromium identity; no Safari spoofing,
stealth plugin, challenge cookie, or CAPTCHA workaround is used.
German long dates, numeric German dates, ISO dates, whitespace, and NBSP are
normalized before exact comparison.

## Notification policy

Normal availability and ordinary newly visible shifts create one Pushover
message. An empty or unreadable shift step creates none.

High-attention hits create two groups of four messages:

- four messages five seconds apart;
- 30 seconds before the second group;
- four more messages five seconds apart.

The resulting gap sequence is `5, 5, 5, 30, 5, 5, 5`.

- Saturday: every new shift except `Mittag` is high-attention.
- Friday: every new shift except `Mittag` and `Nachmittag` is high-attention.

Shift order, case, whitespace, time suffixes, and parenthetical details do not
create duplicates. A removed shift that later reappears and a true
unavailable-to-available re-release do create a new event. Message timestamps
use `Europe/Berlin` explicitly.

Pushover transport verifies HTTP 200 plus JSON `status=1`, records the request
ID and quota headers, does not blindly retry ordinary 4xx responses, defers 429
responses, and retries 5xx/network timeouts with bounded backoff. Tests never
send a real message.

## State schema

`state/state.json` is the source of truth and Git history. Schema v2 contains:

- last reliable status/shifts;
- latest observed status and health;
- privacy-preserving probe diagnostics and degraded/error counters;
- stable alert sequences;
- a resumable outbox with message-part cursor, attempts, next due time,
  Pushover request/quota metadata, and delivered/dead-letter status.

Legacy snapshots migrate in memory without discarding their timestamps,
failure counters, statuses, or shifts. Legacy successful-looking observations
start with `health=unknown` until a v2 probe supplies control evidence. State
writes use an atomic same-directory replacement.

## Local verification

Install the project and its pinned Playwright extra, then install the matching
Chromium build:

```bash
python -m pip install -e '.[headless]'
python -m playwright install chromium
```

Run a safe live simulation:

```bash
python -m src.main --probe --dry-run
```

Dry-run sends nothing and does not modify `state/state.json`.

Run all synthetic tests:

```bash
python -m unittest discover -s tests -v
```

Production delivery is intentionally separate and should normally be invoked
only by the workflow after its probe checkpoint:

```bash
python -m src.main --deliver-next --max-wait-seconds 35
```

Exit code 3 means idle/deferred; exit code 2 means a terminal delivery failure
was written to dead-letter and must be checkpointed before the workflow fails.
After the underlying credential, quota, or provider problem has been reviewed
and corrected, that exact event can be resumed at its unsent part without
creating a new availability event:

```bash
python -m src.main --requeue-event EVENT_ID
```

Commit the resulting state checkpoint before invoking delivery. Requeue is an
explicit operator action so ordinary 4xx responses and bounded retry exhaustion
cannot create an automatic retry loop.

## Secrets

Configure these private GitHub Actions secrets:

- `PUSHOVER_USER`
- `PUSHOVER_TOKEN`
- `PUSHOVER_TOKEN_ERROR`

The workflow requires `contents: write` to commit state. Do not put keys,
cookies, captured HTML, or personal data in YAML, state, fixtures, logs, or
external review artifacts.

## Tent modes

Active tents use `festzelt_os`. The code retains conservative `api`, `html`,
`headless`, `hash`, and `manual` support. Marker-based fetchers return `unknown`
when neither explicit marker matches instead of inferring the opposite; missing
or empty hash regions are errors. A non-shift mode cannot create an actionable
availability alert until it supplies date-correlated shift evidence.

Do not enable a disabled tent merely because a landing page or generic form is
reachable. Enablement requires repeatable, target-date-specific live evidence.

## Important files

- `src/main.py` — probe orchestration and CLI
- `src/fetchers/festzelt_os.py` — native, evidence-based wizard probe
- `src/probe.py` — structured probe contract
- `src/events.py` — transitions, shift identity, stable event creation
- `src/outbox.py` — one-part delivery and retry cursor
- `src/notify.py` — Pushover payload and transport
- `src/state.py` — schema migration and atomic persistence
- `tents/*.yaml` — tent inventory and live-evidence notes
- `scripts/checkpoint_state.sh` — fail-closed Git checkpoint
- `.github/workflows/monitor.yml` — bounded, serialized production flow
- `tests/` — synthetic fetcher, transition, delivery, and Git-race coverage
