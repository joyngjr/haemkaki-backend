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
    DosePlan,
    DoseSchedule,
    ImportBatch,
    ScheduleException,
    SupplyItem,
    TrackingEvent,
    User,
)

settings = get_settings()

_connect_args = {"check_same_thread": False} if settings.sqlalchemy_url.startswith("sqlite") else {}
engine = create_engine(settings.sqlalchemy_url, echo=False, connect_args=_connect_args)

# One-off column adds, not the start of a migration framework. create_all()
# never ALTERs an existing table, and the deployed Postgres tables predate
# these columns — without them, the first deploy after merge 500s on every
# read of the table. Only a nullable or defaulted column can be added under
# live rows this way; any other schema change still means dropping the table.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "user",
        "clinical_profile_json",
        "ALTER TABLE \"user\" ADD COLUMN clinical_profile_json VARCHAR NOT NULL DEFAULT '{}'",
    ),
    (
        "trackingevent",
        "bleed_nature",
        "ALTER TABLE trackingevent ADD COLUMN bleed_nature VARCHAR",
    ),
    (
        "doseplan",
        "dose_dates",
        "ALTER TABLE doseplan ADD COLUMN dose_dates VARCHAR",
    ),
)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    inspector = inspect(engine)
    for table, column, ddl in _ADDED_COLUMNS:
        if column not in {existing["name"] for existing in inspector.get_columns(table)}:
            with engine.begin() as connection:
                connection.execute(text(ddl))
    drop_legacy_missed_events()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


def drop_legacy_missed_events() -> None:
    """One-off data cleanup, in the spirit of the migration above.

    A missed dose used to be a row of its own (`kind='missed'`, with a
    `status` and a `taken_on`). It is derived now — a planned day with no
    factor use on it — so the rows are redundant: they charged nothing, and
    the fold reproduces exactly the same days from the routine. Left in place
    they would fail to validate against `EventKind`, so they go on the way
    past. The dose a missed one was made up by is a `makeup` row of its own
    and is untouched.
    """
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM trackingevent WHERE kind = 'missed'"))
