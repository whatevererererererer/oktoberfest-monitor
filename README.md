# Oktoberfest 2026 Reservation Monitor

Read-only availability monitor for Saturday, **26 September 2026**. Friday,
25 September, is no longer probed or notified. The monitor never books, submits
a reservation form, or bypasses a CAPTCHA.

The repository contains 19 current tent configurations. Eighteen portals with
validated, target-date-specific evidence are enabled; only Glöckle Wirt remains
manual because its official page offers email contact but no live capacity
signal. The former Münchner Stubn entry was replaced by its 2026 successor,
Bartls Flößerstadl. Per-run health reflects whether each selected portal still
provides date-correlated shift evidence.

## Runtime model

An external cron-job.org job dispatches `.github/workflows/monitor.yml`
approximately every five minutes. A run has two durable phases:

1. Probe the next deterministic group of at most three enabled tents, update
   only their health/baselines, and enqueue stable alert events in
   `state/state.json`.
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

The 270-second normal-operation deadline starts in the first job step, before
checkout, Python/dependency, and Playwright setup. Both the probe and each
delivery wait are derived from the remaining budget; 50 seconds are reserved
for the HTTP/state/Git checkpoint and roughly 30 seconds remain for Actions
post-steps. A timed-out probe is not checkpointed. The five-minute job timeout
is a final safety net, and remaining outbox parts resume in the next
current-`main` run. Checkout is shallow because the writer guard compares exact
SHAs and never rebases through the historical state commits.

## Probe and health invariants

Every active mode returns the same strict structured result:

- `available`: the target date exists and native select interaction produced
  at least one shift with a confirmed, target-correlated DOM update.
- `unavailable`: the validated portal proves either that the target date is
  absent or, for Käfer's explicit slot feed, that target capacity is zero.
- `unknown` / degraded: the target exists, but its shift control, options, or
  update cannot be proved.
- `error`: missing or ambiguous base control, bot/login/error page, timeout,
  invalid structure, navigation failure, or technical probe failure.

Unknown/error observations are stored separately and do not overwrite the last
verified availability and shift baseline. The baseline has its own verification
flag, timestamp, and privacy-safe evidence snapshot. A target-specific unreadable
shift step is latched so the same real shift is reported once when it becomes
readable again; an unrelated bot/navigation error does not create that duplicate.
Diagnostics are intentionally small: page type, control/option counts,
target/update evidence, shift count, and an error class. Page HTML, cookies,
tokens, and form values are never stored.

`festzelt_os` validates a native date selection and its causally paired shift
update. `reservierungsmanager` extracts the public widget token afresh and uses
only the official event-day GET; embedded event IDs and optional indoor-only
filters constrain the result. `kaefer` lets the official browser application
make its authenticated slot GET, then validates both Saturday rows and their
capacity fields. `floesserstadl` reads the two server-rendered Mittag/Abend
select option lists with one GET. None of these adapters submits a reservation,
and a selectable request slot is evidence that an inquiry can be made, not a
guaranteed confirmation.

The enabled tents are split into deterministic, non-wrapping groups of at most
three. The next group's first slug is stored as a durable schema-v3 rotation
cursor, so an interrupted or uncheckpointed run retries the same group. With
eighteen enabled tents a complete rotation takes six five-minute runs. Tents not
selected for a run retain their observations, health, and failure counters
unchanged. A completed final group advances the cursor back to the first enabled
tent; a removed or disabled cursor target safely restarts there as well.

Festzelt-OS tents in the selected group run sequentially in configuration order
with a randomized 1–3 second pause before each tent. One browser and one freshly
navigated page are used per tent; the Saturday target is checked on that page
with Playwright's native `select_option`. Date and shift controls are read atomically and
reacquired after every rerender. Only visible, enabled controls count as
evidence. Livewire responses are paired with a request whose update payload
contains the selected target/model, so traffic from the prior target is not
accepted as evidence; an identical shift list is accepted only after the paired
response completes and the browser receives an additional DOM turn.
Placeholder, loading, sold-out, and other non-offer options are never shifts.
Failed updates are classified separately from an honestly empty shift step.
The Chromium context sends the historical Safari 17.5 on macOS 14.5 user-agent,
uses the German locale and a 1280×1100 viewport. No stealth plugin, challenge
cookie, or CAPTCHA workaround is used.
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

Friday events are outside the active target-date policy and are quarantined
before delivery. This also prevents a previously queued Friday event from being
sent after the configuration change.

Shift order, case, whitespace, time suffixes, and parenthetical details do not
create duplicates. A removed shift that later reappears and a true
unavailable-to-available re-release do create a new event. Message timestamps
use `Europe/Berlin` explicitly.

Monitor-error messages explain the privacy-safe diagnostic class and include
the tent's configured official booking URL as `Seite manuell prüfen`. Raw page
content, exception text, cookies, and form values never enter the message. The
monitor never opens that link or performs a booking action; it is only a
shortcut for attended inspection by the recipient.

Pushover transport verifies HTTP 200 plus JSON `status=1`, records the request
ID and quota headers, does not blindly retry ordinary 4xx responses, defers 429
responses without consuming the ordinary retry budget, and retries 5xx/network
timeouts with bounded backoff. Quota gates are separate for availability and
monitor-error tokens. Malformed events are quarantined instead of blocking the
queue. Burst gaps are scheduled from provider-call completion, not request
start. Tests never send a real message.

## State schema

`state/state.json` is the source of truth and Git history. Schema v3 contains:

- last verified status/shifts and their provenance;
- latest observed status and health;
- privacy-preserving probe diagnostics and combined unhealthy/error counters;
- stable alert sequences;
- a resumable outbox with message-part cursor, attempts, next due time,
  channel-specific Pushover quota metadata, and delivered/dead-letter status;
- run start/end/duration and the exact checked-out producer revision;
- the durable next-tent cursor for three-tent probe rotation.

Legacy snapshots migrate in memory without discarding timestamps, counters, or
their previous values in migration diagnostics. A successful-looking legacy
baseline without correlation evidence becomes `unknown`, so an empty historical
`available` value cannot suppress the first real shift. A state from a newer
schema fails closed. State writes use an atomic same-directory replacement.

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

Active tents use `festzelt_os`, `reservierungsmanager`, `kaefer`, or
`floesserstadl`. The code retains conservative `api`, `html`, `headless`,
`hash`, and `manual` support. Marker-based fetchers return `unknown` when neither
explicit marker matches instead of inferring the opposite; missing or empty
hash regions are errors. A legacy/non-shift mode cannot create an actionable
availability alert until it supplies date-correlated shift evidence.

Do not enable a disabled tent merely because a landing page or generic form is
reachable. Enablement requires repeatable, target-date-specific live evidence.

## Deployment and rollback

The first deployment of schema v3 and the fail-closed writer guard requires a
quiet writer boundary: pause the external cron dispatch, let every running and
pending old workflow finish or cancel it, update from current `origin/main`, and
only then push the reviewed code commit. Resume dispatch only after confirming
that no old checkout can write schema v2 over schema v3.

Validate at least three consecutive runs by duration, outbox/health values and
producer ancestry. `producer_revision` is the exact checkout HEAD, so after the
first run it normally names a preceding state commit rather than remaining
textually equal to the deployment commit. The deployment commit must be an
ancestor of every observed producer revision, with no intervening code change.

After any schema-v3 checkpoint, do not roll back with a plain revert to the old
schema-v2 loader: it would accept v3 while silently dropping the new provenance,
quota, and outbox metadata on save. First pause cron and drain workflows. A safe
rollback must retain the v3 loader/state/outbox and fail-closed checkpoint as a
forward-compatible fix, or leave the monitor disabled while such a fix is
deployed. Never restore an older `state/state.json` over its Git history.

## Important files

- `src/main.py` — probe orchestration and CLI
- `src/fetchers/festzelt_os.py` — native, evidence-based wizard probe
- `src/fetchers/reservierungsmanager.py` — public widget event-day adapter
- `src/fetchers/kaefer.py` — browser-captured, capacity-validated slot feed
- `src/fetchers/floesserstadl.py` — structured server-form option reader
- `src/probe.py` — structured probe contract
- `src/events.py` — transitions, shift identity, stable event creation
- `src/outbox.py` — one-part delivery and retry cursor
- `src/notify.py` — Pushover payload and transport
- `src/state.py` — schema migration and atomic persistence
- `tents/*.yaml` — tent inventory and live-evidence notes
- `scripts/checkpoint_state.sh` — fail-closed Git checkpoint
- `.github/workflows/monitor.yml` — bounded, serialized production flow
- `tests/` — synthetic fetcher, transition, delivery, and Git-race coverage
