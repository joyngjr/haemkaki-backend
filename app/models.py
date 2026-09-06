"""SQLModel tables.

One row per profile. There is no auth: a profile is just a name someone picks
on the device, so anyone holding the app can switch between everyone in a
household.
"""

from datetime import UTC, datetime
from enum import Enum

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Naive UTC. SQLite drops tzinfo on round-trip and Postgres keeps it, so
    storing aware values would only break in production."""
    return datetime.now(UTC).replace(tzinfo=None)


class FactorType(str, Enum):
    viii = "VIII"
    ix = "IX"


class DoseState(str, Enum):
    """How much factor is in the patient right now — drives the platelet."""

    covered = "covered"
    low = "low"
    very_low = "veryLow"


class StockState(str, Enum):
    """How many vials are at home — drives the shelf. Independent of dose."""

    well_stocked = "wellStocked"
    moderate = "moderate"
    low = "low"


class User(SQLModel, table=True):
    # Columns are plain strings, not SQLAlchemy Enums, which persist by member
    # name rather than value. The enums above are enforced in app/schemas.py.
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    factor_type: str
    dose_state: str
    stock_state: str
    vials_on_hand: int = 0
    days_cover: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
