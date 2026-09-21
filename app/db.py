"""Database engine and session plumbing.

SQLModel table classes must be imported before create_db_and_tables() runs so
they are registered on SQLModel.metadata. Import new model modules here as they
are added.
"""

import json
from collections.abc import Generator
from datetime import date

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import get_settings
from app.models import (  # noqa: F401  (registers the tables)
    DosePlan,
    DoseSchedule,
    ScheduleException,
    SupplyItem,
    TrackingEvent,
    User,
)

settings = get_settings()

_connect_args = {"check_same_thread": False} if settings.sqlalchemy_url.startswith("sqlite") else {}
engine = create_engine(settings.sqlalchemy_url, echo=False, connect_args=_connect_args)

# A one-off column add, not the start of a migration framework. create_all()
# never ALTERs an existing table, and the deployed Postgres `user` table
# predates this column — without this, the first deploy after merge 500s on
# every query. Any further schema change still means dropping the table.
_ADD_CLINICAL_PROFILE_COLUMN = (
    "ALTER TABLE \"user\" ADD COLUMN clinical_profile_json VARCHAR NOT NULL DEFAULT '{}'"
)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("user")}
    if "clinical_profile_json" not in columns:
        with engine.begin() as connection:
            connection.execute(text(_ADD_CLINICAL_PROFILE_COLUMN))
    migrate_legacy_routines()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


def migrate_legacy_routines() -> None:
    """One-off data migration, in the spirit of the column add above.

    The prophylaxis routine used to live inside `clinical_profile_json` as
    `routine: {vials, interval_days, start_on, minimum_buffer_days}`. The
    schedule is a table now (`doseschedule`) and the buffer is
    `minimum_buffer_days` at the top of the profile. A row still carrying a
    `routine` key is converted once and the key removed, so this cannot run
    twice for the same row — or resurrect a routine the user has since deleted.
    A routine missing its start, interval or vials was never a series and is
    dropped; the buffer is kept either way.
    """
    with Session(engine) as session:
        for user in session.exec(select(User)).all():
            try:
                stored = json.loads(user.clinical_profile_json or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(stored, dict) or "routine" not in stored:
                continue
            routine = stored.pop("routine")
            if isinstance(routine, dict):
                buffer_days = routine.get("minimum_buffer_days")
                if buffer_days is not None and stored.get("minimum_buffer_days") is None:
                    stored["minimum_buffer_days"] = buffer_days
                start_on, interval, vials = (
                    routine.get("start_on"),
                    routine.get("interval_days"),
                    routine.get("vials"),
                )
                if start_on and interval and vials:
                    session.add(
                        DoseSchedule(
                            user_id=user.id,
                            start_on=date.fromisoformat(start_on),
                            interval_days=int(interval),
                            vials=int(vials),
                        )
                    )
            user.clinical_profile_json = json.dumps(stored)
            session.add(user)
        session.commit()
