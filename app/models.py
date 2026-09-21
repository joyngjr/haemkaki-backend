"""SQLModel tables.

One row per profile. There is no auth: a profile is just a name someone picks
on the device, so anyone holding the app can switch between everyone in a
household.
"""

from datetime import UTC, date, datetime
from enum import Enum

from sqlalchemy import BigInteger, Column
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Naive UTC. SQLite drops tzinfo on round-trip and Postgres keeps it, so
    storing aware values would only break in production."""
    return datetime.now(UTC).replace(tzinfo=None)


class FactorType(str, Enum):
    viii = "VIII"
    ix = "IX"
    xi = "XI"
    acquired = "acquired"
    unknown = "unknown"


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
    # The profile flow gathers conditional clinical information that differs by
    # diagnosis. Keeping it as validated JSON avoids pretending that FXI or
    # acquired haemophilia can be represented by an FVIII/FIX-only column.
    clinical_profile_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TrackerRoutine(SQLModel, table=True):
    """A profile's usual prophylaxis routine. At most one row per profile.

    The frequency is split across columns rather than stored as JSON because it
    is small and closed: either every `frequency_days` days, or on the weekdays
    in `frequency_weekdays` (comma-separated, 0 = Sunday .. 6 = Saturday).
    """

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    vials: int | None = None
    frequency_unit: str | None = None
    frequency_days: int | None = None
    frequency_weekdays: str = ""
    start_date: date | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class TrackerEntry(SQLModel, table=True):
    """One logged event on one day: a dose, a refill, or a missed dose.

    `entry_id` is the id the frontend gave the entry. It is kept because the
    frontend refers to doses by id (for the "shift future doses?" answers), so it
    has to survive a round-trip. The table's own `id` is only a surrogate key.
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    entry_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    day: date = Field(index=True)
    kind: str
    vials: int | None = None
    # On-demand dose: whether the bleed was "spontaneous" or "traumatic".
    bleed_nature: str | None = None
    # Missed dose: what the user answered, and when they took it instead.
    missed_status: str | None = None
    taken_date: date | None = None
    # Make-up dose: which day it made up for, and how much it used.
    missed_date: date | None = None
    amount_source: str | None = None
    amount_vials: int | None = None


class TrackerShift(SQLModel, table=True):
    """The user's answers to "shift future doses?". At most one row per profile.

    `anchor_id` and `handled_id` are frontend entry ids (see `TrackerEntry`):
    the dose the schedule was shifted to, and the latest dose already asked
    about. Without them the question would come back on every reload.
    """

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    anchor_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    handled_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    weekday_offset: int = 0
    updated_at: datetime = Field(default_factory=utcnow)


class TrackerPlan(SQLModel, table=True):
    """A temporary change to the routine over a date range (a "Plan Ahead" plan)."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    plan_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    start_date: date
    end_date: date
    # Same split as `TrackerRoutine`; all null/empty means "frequency as usual".
    frequency_unit: str | None = None
    frequency_days: int | None = None
    frequency_weekdays: str = ""
    vials: int | None = None


class TrackerInventory(SQLModel, table=True):
    """The Inventory card: supplies other than factor. One row per profile.

    A row exists only once the user has changed something, so "never saved"
    (show the starter list) is distinguishable from "removed everything".
    """

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    visible: bool = True
    items_json: str = "[]"
    updated_at: datetime = Field(default_factory=utcnow)
