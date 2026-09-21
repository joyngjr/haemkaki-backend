"""Request and response models for the tracker: the routine and the day-by-day
entries.

Field names mirror the frontend's `TrackerEntry` and `Frequency` types in
`src/lib/tracker-entries.ts` and `src/lib/tracker-dates.ts`, in snake_case. Like
`app/schemas.py`, this module is the only thing that constrains what the tables
hold — the columns are plain strings and integers.
"""

from datetime import date
from enum import Enum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

DAYS_MAX = 99
VIALS_MAX = 999
ENTRIES_PER_DAY_MAX = 50


class EntryKind(str, Enum):
    refill = "refill"
    prophylaxis = "prophylaxis"
    on_demand = "on-demand"
    follow_up = "follow-up"
    makeup = "makeup"
    missed = "missed"


class MissedStatus(str, Enum):
    awaiting = "awaiting"
    skipped = "skipped"
    taken = "taken"


class AmountSource(str, Enum):
    pending = "pending"
    routine = "routine"
    custom = "custom"


class EntryWrite(BaseModel):
    """One entry on a day, as the frontend holds it."""

    id: int = Field(ge=0, description="Chosen by the frontend; kept so doses can be referred to.")
    kind: EntryKind
    vials: int | None = Field(default=None, ge=1, le=VIALS_MAX)
    missed_status: MissedStatus | None = None
    taken_date: date | None = None
    missed_date: date | None = None
    amount_source: AmountSource | None = None
    amount_vials: int | None = Field(default=None, ge=1, le=VIALS_MAX)

    @model_validator(mode="after")
    def _check_kind_fields(self) -> Self:
        needs_vials = {EntryKind.refill, EntryKind.on_demand, EntryKind.follow_up}
        if self.kind in needs_vials and self.vials is None:
            raise ValueError(f"{self.kind.value} entries need a vial count")
        if self.kind == EntryKind.makeup:
            if self.missed_date is None or self.amount_source is None:
                raise ValueError("makeup entries need missed_date and amount_source")
        if self.kind == EntryKind.missed and self.missed_status is None:
            raise ValueError("missed entries need missed_status")
        if self.amount_source == AmountSource.custom and self.amount_vials is None:
            raise ValueError("a custom amount needs amount_vials")
        return self


class EntryRead(EntryWrite):
    day: date


class DaysFrequency(BaseModel):
    unit: Literal["days"]
    days: int = Field(ge=1, le=DAYS_MAX)


class WeekFrequency(BaseModel):
    unit: Literal["week"]
    weekdays: list[int] = Field(min_length=1, max_length=7)

    @field_validator("weekdays")
    @classmethod
    def _weekdays(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("weekdays are 0 (Sunday) to 6 (Saturday)")
        return sorted(set(value))


Frequency = Annotated[DaysFrequency | WeekFrequency, Field(discriminator="unit")]


class RoutineRead(BaseModel):
    vials: int | None = None
    frequency: Frequency | None = None
    start_date: date | None = None


class RoutineUpdate(BaseModel):
    """PATCH body. A field left out is untouched; an explicit null clears it."""

    vials: int | None = Field(default=None, ge=1, le=VIALS_MAX)
    frequency: Frequency | None = None
    start_date: date | None = None


class ShiftState(BaseModel):
    """Entry ids the frontend uses to remember its answers; all null before any."""

    anchor_id: int | None = Field(default=None, ge=0)
    handled_id: int | None = Field(default=None, ge=0)
    weekday_offset: int = Field(default=0, ge=-1000, le=1000)


PLANS_MAX = 100


class PlanWrite(BaseModel):
    id: int = Field(ge=0, description="Chosen by the frontend, like entry ids.")
    start_date: date
    end_date: date
    frequency: Frequency | None = None
    vials: int | None = Field(default=None, ge=1, le=VIALS_MAX)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


ITEMS_MAX = 100


class InventoryItem(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=40)
    quantity: int = Field(ge=0, le=9999)

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class InventoryState(BaseModel):
    visible: bool = True
    items: list[InventoryItem] = Field(default_factory=list, max_length=ITEMS_MAX)
