# Oktoberfest 2026 Reservation Monitor

Watches the 13 main Wiesn tents for reservation availability on **Fri 25.09.2026** and **Sat 26.09.2026** and pushes a tap-to-book Pushover alert to your iPhone the moment a slot opens or a cancellation re-releases a table.

## How it works

- GitHub Actions cron runs `python -m src.main` every 5 minutes (the platform minimum).
- Each tent has a YAML in `tents/` declaring how to detect availability for the target dates.
- State is committed back to `state/state.json` after every run — a JSON diff over time, free history.
- On `unavailable → available` (or `unknown → available`), Pushover fires priority 1 with the booking URL pre-filled to the date.
- A separate Pushover app fires priority 0 if any tent fails 3 runs in a row.

## One-time setup

### 1. Pushover

1. Buy and install [Pushover](https://pushover.net) on your iPhone (~$5 one-time). Log in.
2. Note your **User Key** from https://pushover.net.
3. Create two applications at https://pushover.net/apps/build:
   - `Wiesn Alerts` → API token used for availability alerts (**`PUSHOVER_TOKEN`**).
   - `Wiesn Errors` → API token used for monitor failures (**`PUSHOVER_TOKEN_ERROR`**).
4. On iPhone: **Settings → Notifications → Pushover → Critical Alerts: On**. Priority-1 alerts will then break Focus / Do Not Disturb.

### 2. GitHub repository

1. Create a **private** GitHub repo and push this code.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `PUSHOVER_USER` — your user key.
   - `PUSHOVER_TOKEN` — Wiesn Alerts app token.
   - `PUSHOVER_TOKEN_ERROR` — Wiesn Errors app token.
3. Repo **Settings → Actions → General → Workflow permissions: Read and write**. The workflow needs this to commit `state/state.json` back to the repo.

### 3. Tent endpoint discovery (the manual part — ~30 min)

Most tent reservation pages are JavaScript SPAs that fetch availability from a JSON API. We monitor the JSON API directly, not the rendered page. You discover each tent's API once via DevTools, paste it into the tent's YAML, and flip `enabled: true`.

For each tent in `tents/*.yaml`:

1. Open `booking_url` in Chrome or Safari with **DevTools → Network** open and the **Fetch/XHR** filter active.
2. Pick a date in late September 2026 (the closer to 25/26.09 the better).
3. Watch the Network tab — one of the XHR responses contains availability for that date. Look for JSON with fields like `available`, `slots`, `status`, `ausgebucht`.
4. Right-click that request → **Copy → Copy as cURL** to capture method, URL, headers, body.
5. Edit the tent's YAML:

   ```yaml
   mode: api
   enabled: true
   api:
     endpoint: <captured URL — replace the date with {date}>
     method: POST   # or GET
     headers:
       # only headers the server actually requires — usually none beyond the defaults
     payload_template: '{"date":"{date}", ...}'   # POST body, with {date} placeholder
     # OR for GET:
     # query_template:
     #   date: "{date}"
     unavailable_when: '$.status == "ausgebucht"'   # JSONPath that's truthy when sold out
     # OR:
     # available_when: '$.slots[*].available'
   ```

6. Re-run the monitor with `python -m src.main --dry-run` to verify the new config without firing notifications.

If a tent has no online booking flow (Käfer, partly Augustiner), leave it as `mode: manual, enabled: false`.

### 4. Local dry run

```bash
python -m pip install -e .
PUSHOVER_USER=... PUSHOVER_TOKEN=... PUSHOVER_TOKEN_ERROR=... python -m src.main --dry-run
```

Verify `state/state.json` updates and no notifications fire.

## Verification (end-to-end before you trust it)

1. Enable one tent only (e.g. `hofbraeu.yaml`) once you've configured its api block. Run the workflow via **Actions → Wiesn Monitor → Run workflow**.
2. Confirm `state/state.json` is committed and contains the current real status.
3. **Synthetic transition**: locally edit `state/state.json` so that tent's date status is `unavailable`, commit, push. The next scheduled run should detect the (real) `unavailable → available` transition and push one Pushover. Time the round-trip: cron tick → iPhone notification should land within ~60 s.
4. **Tap test**: tap the iPhone notification — Safari opens the booking URL with the date prefilled.
5. **Failure-mode test**: corrupt one tent's `endpoint`, let the workflow run 3 times — confirm a `Wiesn-Monitor: Fehler` Pushover arrives.
6. **Critical-alert test**: enable iPhone Focus, fire a manual priority-1 Pushover from https://pushover.net, confirm it breaks through.
7. Enable all configured tents, let it run unattended for 7 days, review `state/state.json` git history for noise.

## Tent YAML reference

```yaml
slug: hofbraeu                # filename stem, used as state key
name: Hofbräu-Festzelt        # human name in notifications
booking_url: https://...      # tap-to-open URL in the alert
mode: api | html | hash | manual
enabled: true | false
dates: ["2026-09-25", "2026-09-26"]
notes: |
  Free-form

# mode: api
api:
  endpoint: https://...
  method: GET | POST
  headers: { ... }
  payload_template: '{"date":"{date}"}'   # for POST
  query_template: { date: "{date}" }       # for GET
  unavailable_when: 'JSONPath expression'  # truthy → sold out
  available_when: 'JSONPath expression'    # truthy → free

# mode: html
html:
  url_template: https://...?date={date}
  selector: ".calendar-day[data-date='{date}']"   # optional
  unavailable_regex: "ausgebucht|leider belegt"
  available_regex: "verf[üu]gbar"                 # optional

# mode: hash — last resort, fires on any change to a normalized region
hash:
  url_template: https://...
  selector: "#availability"

# mode: manual — placeholder; never auto-checked
```

## Out of scope (deliberately)

- **Auto-booking**. Forms differ per tent, several use CAPTCHA, and a misfired booking is hard to undo. V1 is alert + tap-to-open.
- **Oide Wiesn** tents (Tradition, Herzkasperl, Museumszelt) — excluded by choice.
- **Medium tents** (Weinzelt, Kalbskuchl, etc.) — drop in additional YAMLs to add.

## Files

- [`src/main.py`](src/main.py) — orchestrator
- [`src/notify.py`](src/notify.py) — Pushover client
- [`src/fetchers/`](src/fetchers) — api / html / hash detection
- [`tents/`](tents) — 13 tent configs
- [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml) — cron + commit-back
- [`state/state.json`](state/state.json) — diff source of truth
