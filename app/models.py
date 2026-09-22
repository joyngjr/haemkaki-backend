"""SQLModel tables.

One row per profile. There is no auth: a profile is just a name someone picks
on the device, so anyone holding the app can switch between everyone in a
household.
"""

from datetime import UTC, date, datetime
from enum import Enum

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


class EventKind(str, Enum):
    """The tracker's event ledger.

    The values are the frontend's `TrackerEntry["kind"]` strings verbatim,
    hyphens and all, so nothing has to map between the wire and the calendar.
    """

    refill = "refill"
    prophylaxis = "prophylaxis"
    on_demand = "on-demand"
    follow_up = "follow-up"
    makeup = "makeup"
    missed = "missed"


class AmountSource(str, Enum):
    """Where a made-up dose's size came from. Mirrors the frontend's `DoseAmount`."""

    pending = "pending"
    routine = "routine"
    custom = "custom"


class MissedStatus(str, Enum):
    awaiting = "awaiting"
    skipped = "skipped"
    taken = "taken"


class TrackingEvent(SQLModel, table=True):
    """One logged action on one day.

    Single table, one nullable column per kind-specific field: the database
    cannot know that a refill has no `status`, so `app/schemas.py`'s
    discriminated union is the only thing enforcing it. Build rows with
    `to_event(...)`, never `TrackingEvent(...)`.

    `occurred_on` is a date rather than a datetime on purpose. The frontend
    keys every entry by a Singapore-local `YYYY-MM-DD` (`toKey` in
    `src/lib/tracker-dates.ts`); storing UTC instants would move anything
    logged after 08:00 SGT onto the previous day. `created_at`/`updated_at`
    stay naive UTC like every other timestamp here.
    """

    # Columns are plain strings for the same reason User's are: SQLAlchemy
    # Enums persist by member name, and "on-demand" is not a member name.
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    kind: str
    occurred_on: date = Field(index=True)
    #: refill / on-demand / follow-up, and a prophylaxis dose that was imported
    #: with its own count. A prophylaxis dose without one defers to the routine.
    vials: int | None = None
    #: makeup -> the day whose dose this made up for.
    missed_on: date | None = None
    amount_source: str | None = None
    amount_vials: int | None = None
    #: missed only.
    status: str | None = None
    #: missed -> the day it was actually taken; points at a makeup event.
    taken_on: date | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SupplyItem(SQLModel, table=True):
    """One non-factor supply on the tracker's Inventory card — gauze, syringes,
    saline — as a name and a count.

    A checklist, not a ledger: nothing is derived from these rows, so the row
    *is* the balance and the whole list is replaced on every edit. `position`
    keeps the card's order stable across reloads.
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    name: str
    quantity: int = 0
    position: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


def parse_weekdays(text: str | None) -> list[int] | None:
    """The `weekdays` column as a list — "1,3,5" -> [1, 3, 5]; None stays None."""
    if not text:
        return None
    return [int(day) for day in text.split(",")]


def format_weekdays(days: list[int] | None) -> str | None:
    """A weekday list as the column stores it, sorted so two equal series compare equal."""
    if not days:
        return None
    return ",".join(str(day) for day in sorted(set(days)))


class DoseSchedule(SQLModel, table=True):
    """A recurring prophylaxis series: from `start_on`, either every
    `interval_days` or on the fixed `weekdays`, `vials` per dose.

    A plan, not a record — taking the dose is a separate `prophylaxis` event
    on the day. Like a calendar's recurring event it is created and deleted
    whole; one occurrence is moved with a `ScheduleException`, and a permanent
    shift is a new series in place of the old one.

    Exactly one of `interval_days` / `weekdays` is set; `app/schemas.py`
    enforces it, the columns cannot.
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    start_on: date
    #: Every N days from `start_on`. None for a weekday series.
    interval_days: int | None = None
    #: Fixed days of the week as "1,3,5" — 0 = Sunday … 6 = Saturday, the
    #: frontend's `Date.getDay()` — sorted. None for an interval series.
    weekdays: str | None = None
    vials: int
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class DosePlan(SQLModel, table=True):
    """A temporary change to the routine between two dates, inclusive — a
    trip, an illness, a procedure. "Plan Ahead" on the tracker.

    A plan with a frequency replaces the routine's doses for its dates and
    counts from its first day; a plan with only `vials` keeps the routine's
    days and changes their size. Whatever is None stays as the routine has it.
    Plans do not overlap; the router enforces it.
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    start_on: date
    end_on: date
    #: Same encoding as `DoseSchedule`; both None means "frequency as usual".
    interval_days: int | None = None
    weekdays: str | None = None
    #: Vials per dose while the plan runs. None means "dosage as usual".
    vials: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ScheduleException(SQLModel, table=True):
    """One occurrence of a series moved to another day.

    `original_on` is the date the cycle put it on and is what identifies the
    occurrence; `moved_to` is where it now sits on the calendar.
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    schedule_id: int = Field(foreign_key="doseschedule.id", index=True)
    original_on: date
    moved_to: date
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ImportBatch(SQLModel, table=True):
    """One import from another tracker, kept so it can be undone.

    The rows themselves are ordinary `trackingevent`s: the fold cannot tell they
    were imported. This is the receipt, which ids the import created, so
    `DELETE /users/{id}/imports/{batch}` takes exactly those back out. Rows an
    import replaced are gone; only what it added is reversible.
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    #: Free text from the client: the file name, "Claude.ai", "Excel export".
    source: str = ""
    #: JSON array of the trackingevent ids this import created.
    event_ids_json: str = "[]"
    rows_received: int = 0
    rows_created: int = 0
    rows_replaced: int = 0
    rows_skipped: int = 0
    created_at: datetime = Field(default_factory=utcnow)
