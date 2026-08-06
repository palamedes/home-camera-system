# Calendar + Apple (iCloud) two-way sync — build plan

A local calendar on Sentry, shown with **FullCalendar (standard/MIT — free)**, that
syncs **both directions** with **Apple iCloud via CalDAV**. Google is explicitly
out of scope. This is a "play with it" home-dashboard feature, not a groupware
product — so we optimise for *shows my iCloud events on the wall + let me add one
from the couch*, and we keep the hard edges (recurring/timezone) honest rather
than perfect.

Nothing here is built yet. This is the to-do list to peruse before we start.

---

## Scope decisions (already made)

- **Provider:** Apple iCloud only, over **CalDAV**. It's the only API iCloud has,
  and one protocol gives us read + write. No Google, no REST.
- **Direction:** two-way. Sentry ↔ iCloud.
- **UI:** FullCalendar **standard** views (month / week / day / list). No Premium
  resource-timeline views, so **$0** and MIT-licensed. Vendored into `static/`
  (no CDN — the wall display may have no internet), cache-busted via `asset()`
  like every other static file.
- **Ethos fit:** outbound-only, credential-gated, cached server-side — the same
  shape as the weather card (`weather.py`). Consistent with "no cloud account
  required to *run* Sentry"; iCloud is opt-in and lives behind a settings toggle.

---

## Architecture (mirrors the weather feature)

New pieces, each modelled on an existing analogue so there's a reference to copy:

| New file | Modelled on | Role |
|---|---|---|
| `nvr/calendar.py` | `nvr/weather.py` | `CalendarService`: background sync loop, CalDAV client, snapshot cache |
| `nvr/caldav_client.py` | `nvr/reolink.py` | thin iCloud CalDAV wrapper (discovery, list, PUT, DELETE) — kept separate so it's unit-testable without the service |
| `nvr/templates/calendar.html` | `dashboard.html` | the FullCalendar page |
| `nvr/static/calendar.js` | `weather.js` / `schedules.js` | FullCalendar init + event CRUD against our API |
| `nvr/static/vendor/fullcalendar.*` | (new) | vendored MIT bundle (js + css) |

Touched existing files:

- `nvr/config.py` — add `CalendarConfig` dataclass (+ parse in `load()`).
- `nvr/appsettings.py` — register a `calendar` settings section (`CALENDAR_FIELDS`,
  add to `SECTIONS`); secret handling for the app-specific password.
- `nvr/db.py` — new tables + methods (below).
- `nvr/main.py` — instantiate `calendar = CalendarService(cfg, db)`, `calendar.start()`
  in `lifespan` (next to `weather.start()`), add routes + a `/calendar` page.
- `nvr/templates/base.html` — a **Calendar** nav link (between Clips and Settings).
- `nvr/templates/settings.html` + `nvr/static/settings.js` — a **Calendar** tab
  (account, sync toggle, refresh cadence, which calendars to show) with the same
  `?`-help affordance as the other tabs.
- `requirements.txt` — new deps (below).
- `config/config.example.yaml` — documented `calendar:` seed block.
- `nvr/templates/dashboard.html` — *optional* "today / next up" agenda strip.

---

## Dependencies (decision needed — see Open questions)

Recurring events (RRULE) and timezones are the whole ballgame; we should **not**
hand-roll them. Proposed additions to `requirements.txt`:

- `caldav>=1.3` — CalDAV client with iCloud discovery already handled. Pulls in
  `requests`, `lxml`, `vobject` as transitive deps (heavier than our current
  httpx-only footprint — that's the tradeoff).
- `icalendar>=5.0` — parse/build individual VEVENTs.
- `recurring-ical-events>=2.1` — expand RRULE → concrete instances for a date
  window so FullCalendar just gets a flat list.

Alternative (leaner, more work): hand-roll CalDAV over our existing `httpx`
(PROPFIND/REPORT/PUT XML) + `icalendar` only. Saves 3 transitive deps, costs a
few days and some iCloud-quirk debugging. **Recommendation: use `caldav`** — the
point of this feature is to play with it, not to reimplement RFC 4791.

---

## Data model (`nvr/db.py`)

Two tables. Local-authored events and mirrored iCloud events coexist; a `source`
column tells them apart.

```
calendar_events
  id            INTEGER PK
  uid           TEXT UNIQUE   -- iCalendar UID (we generate for local events)
  source        TEXT          -- 'local' | 'icloud'
  calendar_id   TEXT          -- which iCloud calendar (collection) it belongs to
  title         TEXT
  description   TEXT
  location      TEXT
  start_utc     REAL          -- epoch; all-day flagged separately
  end_utc       REAL
  all_day       INTEGER
  rrule         TEXT          -- raw RRULE string, NULL if single
  tzid          TEXT          -- original timezone id (for write-back fidelity)
  etag          TEXT          -- CalDAV ETag for conflict detection
  href          TEXT          -- CalDAV resource URL
  updated_utc   REAL
  deleted       INTEGER       -- soft-delete tombstone for sync
  dirty         INTEGER       -- local change not yet pushed to iCloud

calendar_sync_state
  calendar_id   TEXT PK
  sync_token    TEXT          -- CalDAV sync-collection token for incremental pulls
  display_name  TEXT
  color         TEXT
  enabled       INTEGER       -- show this calendar in the UI?
  last_sync_utc REAL
```

Methods to add (names follow existing `db.py` style): `upsert_calendar_event`,
`get_calendar_events(start, end)`, `mark_event_dirty`, `tombstone_event`,
`get_dirty_events`, `set_sync_token`, `list_calendars`, `set_calendar_enabled`.

---

## Settings section (`nvr/appsettings.py`)

Register `"calendar"` in `SECTIONS` with a `CALENDAR_FIELDS` schema, same pattern
as `WEATHER_FIELDS`:

- `enabled` (bool) — master on/off; service loop self-guards when false (copy
  weather's "always run loop, `refresh()` early-returns" idiom).
- `apple_id` (str) — the iCloud account email.
- `app_password` (secret str) — **app-specific password** from appleid.apple.com,
  never the real password. Stored in `data/nvr.db` (git-ignored) exactly like
  camera creds; **redacted** in any GET that returns settings (return `"••••"` /
  a `has_password` bool, never the value).
- `refresh_seconds` (int, min 60) — pull cadence; default 300.
- `default_calendar_id` (str) — where Sentry-created events get written.
- `write_enabled` (bool) — safety valve: allow two-way, or run read-only. Default
  read-only until the user explicitly flips it (so a first-run bug can't scribble
  on their real calendar).

---

## CalDAV client (`nvr/caldav_client.py`)

Thin, testable wrapper around the `caldav` lib, iCloud-specialised:

- `connect()` — `caldav.DAVClient(url="https://caldav.icloud.com", username=apple_id,
  password=app_password)`, discover principal → calendar home set → calendars.
- `list_calendars()` → `[{id, display_name, color}]`.
- `pull(calendar_id, sync_token)` → changed events since token + new token
  (incremental via `sync-collection`; fall back to full REPORT on first run or
  token expiry).
- `push(event)` → PUT a VEVENT (create/update); returns new ETag.
- `delete(href, etag)` → DELETE with `If-Match` for safe concurrent delete.

iCloud specifics to bake in: the `caldav.icloud.com` endpoint, app-specific
password auth, principal-discovery dance, and iCloud's habit of 30x-redirecting
to a sharded host (`pXX-caldav.icloud.com`) — the `caldav` lib follows these but
we log them.

---

## Sync loop (`nvr/calendar.py` — `CalendarService`)

Copy `WeatherService`'s skeleton: `start()` spawns a daemon thread, `_loop()`
sleeps `refresh_seconds`, `refresh()` does one cycle and is also callable directly
(for a manual "Sync now" button, like the weather refresh thread in `main.py`).

Each `refresh()`:

1. **Pull:** for each enabled calendar, `pull(sync_token)`; upsert changes into
   `calendar_events`; honour tombstones.
2. **Push:** find `dirty` local events; `push()` them; clear `dirty`, store ETag.
3. **Conflict rule (keep it simple):** compare ETags. If both sides changed since
   last sync → **iCloud wins** (it's the source of truth for the phone in your
   pocket), and we log the clobber. Good enough for a home dashboard; note it in
   the UI so it's not surprising.
4. Update `snapshot()` cache + `last_sync_utc`.

Recurring events: store the master VEVENT (with `rrule`) once; **expand to
concrete instances only at read time** for the requested window using
`recurring-ical-events`, so the DB stays small and edits target the master.
(Editing a single instance of a series is a known CalDAV foot-gun — see Milestone
4; v1 can edit/delete the whole series and defer per-instance edits.)

---

## API endpoints (`nvr/main.py`)

All auth-gated like the rest; writes are admin-or-owner per your role model.

- `GET  /calendar` — the FullCalendar page (auth required).
- `GET  /api/calendar/events?start=&end=` — flattened events (incl. expanded
  recurrences) for the visible window → feeds FullCalendar's `events` feed.
- `POST /api/calendar/events` — create a local event (→ marked dirty → pushed).
- `PATCH /api/calendar/events/{id}` — edit (drag/resize in FullCalendar hits this).
- `DELETE /api/calendar/events/{id}` — delete/tombstone.
- `GET  /api/calendar/calendars` — list iCloud calendars + enabled flags.
- `PATCH /api/calendar/calendars/{id}` — toggle visibility / color.
- `POST /api/calendar/sync` — manual "Sync now" (spawns a one-shot refresh thread,
  mirrors the weather-refresh pattern at `main.py:1222`).
- `POST /api/settings/calendar` — save the settings section (via `appsettings`).
- `POST /api/settings/calendar/test` — verify credentials + list calendars
  without saving (mirrors `/api/alerts/test`).

---

## Frontend

- **`calendar.html`** — a FullCalendar container + a "new event" modal; theme-aware
  (respects the existing light/dark toggle — pass CSS vars into FullCalendar so it
  isn't a bright rectangle in dark mode).
- **`calendar.js`** — init FullCalendar with month/week/day/list toolbar; `events`
  points at `/api/calendar/events`; wire `dateClick`/`select` → create modal,
  `eventClick` → edit, `eventDrop`/`eventResize` → PATCH. Show a "last synced Xm
  ago" line + a Sync-now button.
- **`static/vendor/fullcalendar.min.js` + `.css`** — vendored MIT bundle (the
  `index.global.min.js` single-file build; no bundler needed, matches our
  no-build-step approach).
- **Nav** — add `Calendar` to `base.html` between Clips and Settings.
- **Optional dashboard strip** — a compact "Today / Next up" agenda on
  `dashboard.html`, next to the weather card, reading the same `snapshot()`.

---

## Security & privacy notes

- App-specific password is a **secret**: DB only, never `config.yaml`, never
  committed (already covered by `.gitignore` on `data/`), **redacted** on read.
- Outbound to `caldav.icloud.com` only; on an air-gapped box, leave `enabled:
  false` (same guidance as weather).
- `write_enabled` defaults **false** — read-only until you trust it, so we can't
  corrupt your real calendar during first-run debugging.
- Calendar contents can be personal; the `/calendar` page and API stay behind
  login like everything else. Consider whether *viewers* (non-admin role) should
  see it — decision below.

---

## Testing (`tests/`, following existing style)

- `test_calendar.py` — service loop with a **mocked CalDAV client** (no network):
  pull upserts, push clears dirty, tombstones delete, ETag-conflict → iCloud wins,
  recurrence expansion returns the right instances for a window, `refresh()`
  early-returns when disabled.
- `test_calendar_settings.py` — section validation, **password redaction on read**,
  `test` endpoint behaviour, `write_enabled` gate blocks pushes.
- Page-render smoke test for `/calendar` (admin + viewer per the visibility
  decision).
- `conftest.py` — stub `calendar.start()` and wipe `calendar_events` /
  `calendar_sync_state` between tests (as done for `weather`/`events`).

---

## Milestones (suggested build order — each independently shippable)

- [ ] **M1 — Local-only calendar.** DB tables, CRUD API, FullCalendar page + nav,
      create/edit/delete local events. **No iCloud yet.** Fully useful on its own
      (trash day, camera maintenance, reminders). *~1 day.*
- [ ] **M2 — Read-only iCloud pull.** `caldav_client` + service loop, settings tab
      with credentials + "Sync now", calendar visibility toggles. iCloud events
      appear read-only in FullCalendar. *~1–2 days.*
- [ ] **M3 — Write-back.** Push local creates/edits/deletes to iCloud behind
      `write_enabled`; ETag conflict handling (iCloud wins). Full two-way for
      **non-recurring** events + whole-series edits. *~1–2 days.*
- [ ] **M4 — Recurrence polish (optional).** Per-instance edits/exceptions
      (EXDATE / RECURRENCE-ID), timezone-correct all-day vs timed. The fiddly
      tail; defer until M1–M3 feel good. *~1–2 days.*
- [ ] **M5 — Dashboard agenda strip (optional).** "Today / Next up" on the home
      dashboard next to weather.

Realistic first-playable: **M1 + M2 in a weekend** (see your calendar on the
wall). Trustworthy two-way: **through M3**.

---

## Open questions for you

1. **Dependency call:** OK to add `caldav` + `icalendar` + `recurring-ical-events`
   (heavier, fast), or prefer the lean hand-rolled-over-httpx route (slower)?
   *Recommend: the libraries.*
2. **Viewer visibility:** should non-admin *viewer* accounts see the calendar, or
   is it admin-only? (Calendars are more personal than camera feeds.)
3. **One iCloud account or several?** v1 assumes a single household iCloud login.
   Multiple Apple IDs (yours + spouse's) is doable but changes the settings shape
   — worth it now, or later?
4. **Write default:** ship with `write_enabled` **off** (read-only until you flip
   it) — agree? *Recommend: yes.*
5. **Dashboard strip:** want the "Today / Next up" agenda on the home dashboard,
   or keep the calendar to its own page for now?
