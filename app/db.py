"""Database engine and session plumbing.

SQLModel table classes must be imported before create_db_and_tables() runs so
they are registered on SQLModel.metadata. Import new model modules here as they
are added.
"""

from collections.abc import Generator

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.models import (  # noqa: F401  (registers the tables)
    TrackerEntry,
    TrackerInventory,
    TrackerPlan,
    TrackerRoutine,
    TrackerShift,
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


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
