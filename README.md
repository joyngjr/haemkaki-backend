# HackitRx — API

Medication supply tracking for people with haemophilia.

This is a bare FastAPI skeleton. Features are added on top of it as they get
built — there is no domain model, no auth, and no seeded data yet.

## What is here

```
app/
  config.py   Settings from env (.env) — DATABASE_URL, CORS_ORIGINS
  db.py       SQLModel engine, table creation, get_session dependency
  main.py     FastAPI app, CORS, /health
```

`GET /health` is the only endpoint. Interactive docs at `/docs`.

## Run locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # works as-is for local dev
uvicorn app.main:app --reload
```

Falls back to a local SQLite file (`hackitrx.db`) when `DATABASE_URL` is unset.

Tests: `pytest -q`. Lint: `ruff check .`

## Adding a feature

1. Define SQLModel tables in a model module and import it in `app/db.py` so
   `create_db_and_tables()` sees them.
2. Define request/response models with Pydantic.
3. Add a router and register it in `app/main.py`.
4. Add tests under `tests/`.

There are no migrations — tables are created from the models at startup, so a
changed model needs the local `hackitrx.db` deleted (or the table dropped) to
take effect.

## Deploying

Railway builds from `Dockerfile` and deploys on every push to `main`.
Set `DATABASE_URL` (Railway injects it) and `CORS_ORIGINS`. Full runbook in
[SETUP.md](SETUP.md).
