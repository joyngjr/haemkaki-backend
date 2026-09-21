# HackitRx — API

Medication supply tracking for people with haemophilia.

Features are added on top of a thin FastAPI skeleton. There is no auth and no
seeded data — a profile is just a name someone picks on the device.

## What is here

```
app/
  config.py       Settings from env (.env) — DATABASE_URL, CORS_ORIGINS
  db.py           SQLModel engine, table creation, get_session dependency
  main.py         FastAPI app, CORS, /health
  models.py       SQLModel tables + the dose/stock/factor enums
  schemas.py      Request and response models — the only place states are validated
  tracker_schemas.py  Request and response models for the routine and entries
  routers/
    users.py      Profile CRUD
    tracker.py    A profile's routine and day-by-day entries
```

Interactive docs at `/docs`.

| Method   | Path          |                                        |
| -------- | ------------- | -------------------------------------- |
| `GET`    | `/health`     | liveness                               |
| `GET`    | `/users`      | every profile, oldest first            |
| `POST`   | `/users`      | create a profile                       |
| `GET`    | `/users/{id}` | one profile                            |
| `PATCH`  | `/users/{id}` | update any subset of a profile's fields |
| `DELETE` | `/users/{id}` | remove a profile                       |

Each profile also has tracker data. There is one usual routine, and entries are
filed by day (a dose, a refill, or a missed dose):

| Method  | Path                            |                                                       |
| ------- | ------------------------------- | ----------------------------------------------------- |
| `GET`   | `/users/{id}/routine`           | the usual routine, all-null if not set yet             |
| `PATCH` | `/users/{id}/routine`           | change any of `vials`, `frequency`, `start_date`       |
| `GET`   | `/users/{id}/entries`           | every entry, oldest day first; `?from=&to=` to limit   |
| `PUT`   | `/users/{id}/entries/{day}`     | make that day hold exactly the entries sent (`[]` clears it) |
| `GET`   | `/users/{id}/shift`             | the answers to "shift future doses?" (entry ids), all-null if none |
| `PUT`   | `/users/{id}/shift`             | save those answers                                    |
| `GET`   | `/users/{id}/plans`             | the Plan Ahead plans (date range + optional frequency and dosage) |
| `PUT`   | `/users/{id}/plans`             | make the profile hold exactly the plans sent           |
| `GET`   | `/users/{id}/inventory`         | the Inventory card, or `null` if it has never been changed |
| `PUT`   | `/users/{id}/inventory`         | save whether the card is shown and its supplies        |

`frequency` is `{"unit": "days", "days": 3}` or `{"unit": "week", "weekdays": [1, 4]}`
with weekdays 0 (Sunday) to 6 (Saturday). Entry ids are chosen by the frontend and
kept, because it refers to doses by id. Deleting a profile deletes its tracker data.

`dose_state` is one of `covered` / `low` / `veryLow`, `stock_state` one of
`wellStocked` / `moderate` / `low`, and `factor_type` one of `VIII` / `IX` /
`XI` / `acquired` / `unknown` — spelled exactly as the frontend's `DoseState`,
`StockState` and `FactorType` unions.

A profile also carries a nullable `clinical_profile` object holding what the
onboarding form collects: diagnosis and its severity, sex, weight, date of
birth, drug allergies, treatment approach, and a medication section for each of
prophylaxis / on-demand / other. Its fields mirror the frontend's
`ClinicalProfile` and `MedicationDetails` types in `src/lib/api.ts` one for one,
so renaming a field here is a breaking change.

It is stored as one JSON column and validated in `app/schemas.py` — the column
cannot police which fields belong to which diagnosis, so that module is the only
thing that does. Two things there are easy to break by accident:

- Medication `dose`, `frequency` and `buffer_days` are **prose** the frontend
  composes (`"3 times per week"`, `"7 days"`) and strips again when it reopens a
  profile for editing. Store them verbatim; reformatting breaks that round-trip.
- Fields that do not apply to the chosen diagnosis are **coerced to null, not
  rejected**. The form re-checks only its first step before submitting, so a 422
  would block a submit the UI considers valid.

## Run locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # works as-is for local dev
uvicorn app.main:app --reload
```

Falls back to a local SQLite file (`hackitrx.db`) when `DATABASE_URL` is unset.

Lint: `ruff check .`. This project does not have tests — verify by exercising
the endpoints in `/docs`.

## Adding a feature

1. Define SQLModel tables in a model module and import it in `app/db.py` so
   `create_db_and_tables()` sees them.
2. Define request/response models with Pydantic.
3. Add a router and register it in `app/main.py`.

There are no migrations — tables are created from the models at startup, so a
changed model needs the local `hackitrx.db` deleted (or the table dropped) to
take effect.

## Deploying

Railway builds from `Dockerfile` and deploys on every push to `main`.
Set `DATABASE_URL` (Railway injects it) and `CORS_ORIGINS`. Full runbook in
[SETUP.md](SETUP.md).
