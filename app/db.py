"""Database engine and session plumbing.

SQLModel table classes must be imported before create_db_and_tables() runs so
they are registered on SQLModel.metadata. Import new model modules here as they
are added.
"""

from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.models import User  # noqa: F401  (registers the table)

settings = get_settings()

_connect_args = {"check_same_thread": False} if settings.sqlalchemy_url.startswith("sqlite") else {}
engine = create_engine(settings.sqlalchemy_url, echo=False, connect_args=_connect_args)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
