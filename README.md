# HackitRx — API

Medication supply tracking for people with haemophilia.

Profiles, an event ledger for the tracker, dose schedules, the values folded
from those two, a small supplies list, and a way to bring history in from
another tracker — as a bulk import, and as an MCP server an assistant can
call. There is no auth and no seeded data — a profile is just a name someone
picks on the device.

## What is here

```
app/
  config.py       Settings from env (.env) — DATABASE_URL, CORS_ORIGINS
  db.py           SQLModel engine, table creation, get_session dependency
  main.py         FastAPI app, CORS, /health
  models.py       SQLModel tables + the dose/stock/factor/event enums
  schemas.py      Request and response models — the only place states are validated
  services.py     Everything derived from the ledger. Nothing here is stored.
  importing.py    Bulk import from another tracker: plan, apply, undo, and the rules
  mcp_server.py   HaemKakis as tools for an assistant — the MCP server at /mcp
  routers/
    users.py      Profile CRUD
    events.py     The tracker's event ledger
    status.py     The folded view: supply, schedule, next dose, order advice
    schedules.py  Dose schedules — recurring series and moved occurrences
    plans.py      Plan Ahead — temporary changes to the routine over a date range
    supplies.py   The tracker's Inventory card — a counted list, replaced whole
    imports.py    Import batches — dry run, write, list, undo
```

Interactive docs at `/docs`.

| Method   | Path                         |                                                   |
| -------- | ---------------------------- | ------------------------------------------------- |
| `GET`    | `/health`                    | liveness                                          |
| `GET`    | `/users`                     | every profile, oldest first                       |
| `POST`   | `/users`                     | create a profile                                  |
| `GET`    | `/users/{id}`                | one profile                                       |
| `PATCH`  | `/users/{id}`                | update any subset of a profile's fields           |
| `DELETE` | `/users/{id}`                | remove a profile, its events and its supplies     |
| `GET`    | `/users/{id}/events`         | the ledger, oldest first (`since`/`until`/paging) |
| `POST`   | `/users/{id}/events`         | log one event                                     |
| `PUT`    | `/users/{id}/events/{event}` | replace one event outright                        |
| `DELETE` | `/users/{id}/events/{event}` | remove one event                                  |
| `GET`    | `/users/{id}/status`         | the folded view Home and the tracker read (`as_of` optional) |
| `GET`    | `/users/{id}/schedules`      | every series, earliest start first                |
| `POST`   | `/users/{id}/schedules`      | create a series (`interval_days` or `weekdays`); `replace: true` deletes the rest first |
| `DELETE` | `/users/{id}/schedules/{s}`  | remove a series and its moved occurrences         |
| `GET`    | `/users/{id}/schedules/occurrences` | planned doses in `since..until`, moved ones on their new day, plans applied |
| `PUT`    | `/users/{id}/schedules/{s}/exceptions/{day}` | move the dose the cycle put on `day` (`moved_to`) |
| `DELETE` | `/users/{id}/schedules/{s}/exceptions/{day}` | put a moved dose back on its cycle day |
| `GET`    | `/users/{id}/plans`          | every plan, earliest first                        |
| `POST`   | `/users/{id}/plans`          | add a plan; 422 if its dates overlap another      |
| `PUT`    | `/users/{id}/plans/{p}`      | replace one plan outright                         |
| `DELETE` | `/users/{id}/plans/{p}`      | remove a plan; doses logged while it ran stay     |
| `GET`    | `/users/{id}/supplies`       | the Inventory card's list, in display order       |
| `PUT`    | `/users/{id}/supplies`       | replace that list wholesale (`[]` clears it)      |
| `POST`   | `/users/{id}/imports`        | import up to 500 rows; `dry_run` reports without writing; 422 with the report if any row is in error |
| `GET`    | `/users/{id}/imports`        | the import batches, newest first                  |
| `DELETE` | `/users/{id}/imports/{b}`    | undo a batch: remove the events it created        |
| `POST`   | `/mcp`                       | the MCP server (Streamable HTTP, stateless, JSON) — see below |

`dose_state` is one of `covered` / `low` / `veryLow`, `stock_state` one of
`wellStocked` / `moderate` / `low`, and `factor_type` one of `VIII` / `IX` /
`XI` / `acquired` / `unknown` — spelled exactly as the frontend's `DoseState`,
`StockState` and `FactorType` unions.

A profile also carries a nullable `clinical_profile` object holding what the
onboarding form collects: diagnosis and its severity, sex, weight, date of
birth, drug allergies, treatment approach, a medication section for each of
prophylaxis / on-demand / other, the ordering buffer in days
(`minimum_buffer_days`), and the Medical ID fields — `blood_type`,
`emergency_contact` and `primary_doctor`, every one optional. Its fields mirror
the frontend's `ClinicalProfile` and `MedicationDetails` types in
`src/lib/api.ts` one for one, so renaming a field here is a breaking change.

It is stored as one JSON column and validated in `app/schemas.py` — the column
cannot police which fields belong to which diagnosis, so that module is the only
thing that does. Two things there are easy to break by accident:

- Medication `dose`, `frequency` and `buffer_days` are **prose** the frontend
  composes (`"3 times per week"`, `"7 days"`) and strips again when it reopens a
  profile for editing. Store them verbatim; reformatting breaks that round-trip.
- Fields that do not apply to the chosen diagnosis are **coerced to null, not
  rejected**. The form re-checks only its first step before submitting, so a 422
  would block a submit the UI considers valid.

Because the Medical ID fields live in that JSON column too, adding them needed
no `ALTER`: `_profile_response` re-validates on read, so a row written before
they existed comes back with them as `null` and the card says "Not recorded".

## The event ledger

`POST /users/{id}/events` takes one of six kinds, spelled exactly as the
frontend's `TrackerEntry` in `src/lib/tracker-entries.ts` — `refill`,
`prophylaxis`, `on-demand`, `follow-up`, `makeup`, `missed` — with an
`occurred_on` day. The table is one row per event with a nullable column per
kind-specific field, so the database cannot know that a refill has no `status`.
The discriminated union in `app/schemas.py` is the only thing that does. **Build
rows with `to_event(...)`, never `TrackingEvent(...)`** — `extra="forbid"` plus a
per-kind `Literal` is the whole safety mechanism.

`occurred_on` is a `date`, not a datetime. The tracker keys every entry by a
Singapore-local `YYYY-MM-DD` and never asks what time a dose was taken; a UTC
instant would move anything logged after 08:00 SGT onto the previous day.

### Nothing derived is stored

`app/services.py` folds the ledger from scratch on every call. That is what
makes backdating safe — an event inserted between two others lands in its
correct slot on the next fold, where a running balance column would have applied
it at the end. **Do not add a balance column.**

Three behaviours there are easy to break:

- A shortfall clamps to zero **per event** and accumulates `unaccounted_vials`,
  so a gap that a later refill covers is still reported as a gap. It is a
  data-quality signal (an unlogged refill), never a negative cupboard.
- Malformed rows are logged and skipped, not coerced. `event.vials or 0` would
  make a bad row invisible instead of countable.
- Every event read carries `applied_vials`, what the fold charged for it. A
  prophylaxis dose is sized by the schedule in force on its day — or by its own
  `vials`, which an import sets when the source recorded the amount — so this
  is the only place the tracker learns how big one was.

`/status` also reports `last_bleed_on`: the most recent `on-demand` dose. There
is no bleed table — on-demand use is the app's own marker for a treated bleed,
the same thing the calendar draws the blood drop for — so Home's "recent bleed"
state is folded from the ledger rather than recorded separately.

`vials_on_hand`, `days_cover`, `dose_state` and `stock_state` still have columns
on `user`, left over from when the client set them, but every read now returns
the folded value instead. `dose_state` is a **schedule estimate** — time since
the last dose against the schedule's interval — not a measured factor level and
not pharmacokinetics.

## Dose schedules

The prophylaxis routine is a calendar's recurring event. A row in
`doseschedule` is a series — from `start_on`, either every `interval_days` or
on fixed `weekdays` (stored as `"1,3,5"`, 0 = Sunday … 6 = Saturday, the
frontend's `Date.getDay()`), `vials` per dose — and it is created and deleted
whole, never edited in place. Exactly one of the two frequency fields is set;
`app/schemas.py` enforces it. A permanent shift is a new series in place of
the old one (`POST` with `replace: true` does the delete and the create in one
transaction). Moving one dose is a `scheduleexception` keyed by the date the
cycle put it on (`original_on`), and `PUT` on that date replaces any earlier
move; the day it moves to must not already hold a planned dose.

A series is a plan, not a record. Taking the dose is still a `prophylaxis`
event on the day, so deleting a series leaves the history intact, and the
calendar shows a planned dose until its day is **settled** — by a dose of any
kind (the tracker keeps one factor use per day) or a missed-dose record.

From the series and the ledger, `/status` folds:

- `next_dose` — the first planned dose after the last logged dose that nothing
  has settled. It stays put, and reads as overdue, until it is logged, moved or
  recorded as missed; searching from the last dose rather than from the start
  means someone who began logging late is not nagged about everything before.
- `runs_out_on` and `days_cover` — the planned doses walked forward against the
  vials on hand; the first one the cupboard cannot supply is the run-out date.
  Stock that lasts the whole `FORECAST_DAYS` (a year) reads as 365 days and no
  run-out date.
- `order` — `by_on` is the run-out date less the profile's `minimum_buffer_days`
  (`due` once that day has arrived), and `vials` covers the doses from the
  run-out date through the next `ORDER_COVERS_DAYS` (30) plus the buffer, less
  what is left. The working comes with it — `covers_until`, `planned_doses`,
  `planned_vials`, `leftover_vials`, `buffer_days` — so the card can show how
  the number was reached. No schedule, no order advice: there is no usage to
  forecast.

### Plans

"Plan Ahead" is a `doseplan` row: a date range plus any of a rhythm
(`interval_days` or `weekdays`, encoded as on a series) and a `vials` size.
Inside its dates a plan with a rhythm replaces the series' doses — a cycle day
that falls inside it is not planned, moved or not — and puts its own there,
counted from the plan's first day; a plan with only `vials` keeps the series'
days and resizes them. A prophylaxis dose logged inside a plan is charged the
plan's size. Plans do not overlap (422), and a plan's dose cannot be moved: it
follows the plan. Nothing is copied into the series — the fold applies plans
when it walks the calendar, so the next dose, the run-out date and the order
advice follow them through the same code path. A plan with a rhythm plans its
doses even without a routine.

The routine used to live in `clinical_profile.routine` inside the JSON column.
`migrate_legacy_routines()` in `app/db.py` converts any row still carrying that
key at startup — a complete routine becomes a series, the buffer moves to
`minimum_buffer_days`, the key is removed so it cannot run twice — in the same
one-off spirit as the column add above it.

## Supplies

The `supplyitem` table is the tracker's Inventory card — gauze, syringes,
saline — as a name and a count per row. It is a checklist, not a ledger:
nothing is derived from it, the row *is* the balance, and `PUT` replaces the
whole list in the order given. At a handful of rows per profile that is simpler
than per-item routes and ids the card would have to invent before the server
answered. Ids therefore change on every write; the frontend never relies on
them.

## Importing from another tracker

Two ways in, both through `app/importing.py`, so the rules are enforced once.

`POST /users/{id}/imports` takes up to 500 rows in the ledger vocabulary and
answers with a verdict per row — `create`, `replace`, `skip` or `error` — and
the counts. `dry_run: true` reports without writing. A real import with any
row in error writes nothing and answers 422 with the same report as `detail`;
skipped rows are not errors. What was written is recorded in `importbatch`, so
`GET` lists the batches and `DELETE /users/{id}/imports/{b}` removes exactly the
events a batch created (rows it replaced are gone for good).

The rules are the tracker's own per-day invariants, which the API never
enforced before: one refill and one factor use (prophylaxis, on-demand,
follow-up, makeup) per day; a prophylaxis or makeup dose supersedes a `missed`
record on the same day, an on-demand or follow-up dose does not; no day after
today in Singapore; a row identical to one already logged is skipped as
`already-present`, so re-running the same sheet is idempotent. `on_conflict`
(`skip`, the default, or `replace`) only governs collisions with rows already
in the ledger — two rows in one batch that collide are an error whatever it
says, because two uses on one day have to be merged by whoever read the sheet.

### The MCP server

`app/mcp_server.py` exposes the same operations as tools for an assistant, over
the Model Context Protocol at `POST /mcp` (Streamable HTTP, stateless, JSON
responses). Connect Claude.ai or Claude Desktop with **Customize → Connectors →
Add custom connector** and the public URL (`https://<railway-host>/mcp`, no
sign-in), or Claude Code with

```bash
claude mcp add --transport http haemkakis https://<railway-host>/mcp
```

then hand the assistant the spreadsheet. Nothing on the server parses a file:
the assistant maps the rows, asks the user how many IU a vial holds when the
sheet is in IU, calls `import_events` with `dry_run=true`, shows the verdicts,
and only after the user confirms calls it again for real. The tools are
`list_profiles`, `get_profile`, `list_events`, `set_routine`, `import_events`,
`list_imports` and `undo_import`, plus an `import_tracker` prompt with the
procedure. Their descriptions are the assistant's whole user interface; keep
them exact.

There is no authentication, exactly like the REST API: anyone with the URL can
read and change every profile on the server.

Three things in `main.py` are deliberate. The transport is registered as an
exact route with `app.add_route("/mcp", ...)` rather than mounted: a `Mount`
only matches `/mcp/...` and answers a bare `POST /mcp` with a 307 that not every
MCP client follows. DNS-rebinding protection is switched off explicitly: the
SDK's default allow-list is localhost-only, which passes in Docker and rejects
Railway's `Host` header with 421. And `sse-starlette` is pinned to 3.0.0, the
last release without a hard `starlette>=0.49.1` floor, because
`fastapi==0.115.6` holds starlette below 0.42; bumping FastAPI lifts the pin.

To poke the server by hand, every request needs `Content-Type: application/json`
and `Accept: application/json, text/event-stream`:

```bash
curl -s -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Run locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # works as-is for local dev
uvicorn app.main:app --reload
```

Falls back to a local SQLite file (`hackitrx.db`) when `DATABASE_URL` is unset.
Nothing is seeded, so create a profile in `/docs` first — every
`/users/{id}/...` route 404s until one exists. The MCP server is then at
`http://localhost:8000/mcp`, which Claude Code can use directly
(`claude mcp add --transport http haemkakis http://localhost:8000/mcp`);
Claude.ai needs the deployed address.

Lint: `ruff check .`. This project does not have tests — verify by exercising
the endpoints in `/docs`.

## Adding a feature

1. Define SQLModel tables in `app/models.py` and import them in `app/db.py` so
   `create_db_and_tables()` sees them.
2. Define request/response models with Pydantic in `app/schemas.py`.
3. Add a router under `app/routers/` and register it in `app/main.py`.

There are no migrations — tables are created from the models at startup, and
`create_all()` never `ALTER`s an existing one. A new table appears on the next
start; a changed table needs the local `hackitrx.db` deleted (or the table
dropped in Railway's Postgres console) to take effect. Anything that can live in
`clinical_profile_json` should, for exactly that reason.

## Deploying

Railway builds from `Dockerfile` and deploys on every push to `main`. Set
`DATABASE_URL` (Railway injects it when a Postgres service is attached) and
`CORS_ORIGINS` (the Vercel production domain; preview URLs are matched by
regex). The Dockerfile copies only `app/`, so everything that ships must live
there. Once deployed, the MCP server is at `https://<railway-host>/mcp`.
