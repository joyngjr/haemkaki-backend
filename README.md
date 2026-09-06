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
  routers/
    users.py      Profile CRUD
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

`dose_state` is one of `covered` / `low` / `veryLow`, `stock_state` one of
`wellStocked` / `moderate` / `low`, and `factor_type` one of `VIII` / `IX` —
spelled exactly as the frontend's `DoseState` and `StockState` unions.

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
