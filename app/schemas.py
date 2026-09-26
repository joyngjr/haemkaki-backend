"""Request and response models.

The `user` table stores the state columns as plain strings and the whole
clinical profile as one JSON column, so this module is the only thing that
constrains them to the values the frontend knows how to render. Validate here,
never in the router.

The shapes below mirror `ClinicalProfile` and `MedicationDetails` in the
frontend's `src/lib/api.ts` field for field. Changing a name or a type here is a
breaking contract change, not a refactor.
"""

import json
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import (
    AmountSource,
    BleedNature,
    DoseState,
    EventKind,
    FactorType,
    StockState,
    TrackingEvent,
    parse_dates,
    parse_weekdays,
)

NAME_MAX = 60
# Amounts are whole vials, three digits: a quarter's worth of factor can arrive as one
# refill. The app never converts IU — whoever enters an IU figure does, at the vial size
# they know.
VIALS_MAX = 999
# A vial's labelled strength. Products come in 250 to 4000 IU; the cap only
# catches a typo, and nothing multiplies by it — it is printed on the Medical ID.
IU_PER_VIAL_MAX = 10_000
DAYS_COVER_MAX = 365

# The allergy picker has no selection limit and the catalog holds 187 drugs, so
# the worst honest case is a few thousand characters of joined names.
ALLERGY_DETAILS_MAX = 4000
ITEMS_JSON_MAX = 8000
# A prophylaxis interval longer than a quarter is not a routine.
INTERVAL_DAYS_MAX = 90
# A temporary change longer than a year is a new routine, not a plan.
PLAN_DAYS_MAX = 366
# The fold walks the schedule this far ahead before calling the cupboard stocked.
FORECAST_DAYS = 365


class DiagnosisType(str, Enum):
    haemophilia_a = "haemophilia_a"
    haemophilia_b = "haemophilia_b"
    factor_xi_deficiency = "factor_xi_deficiency"
    acquired_haemophilia = "acquired_haemophilia"
    symptomatic_carrier_a = "symptomatic_carrier_a"
    symptomatic_carrier_b = "symptomatic_carrier_b"
    other_or_unknown = "other_or_unknown"


#: Diagnoses someone is born with — the only ones with a severity band.
CONGENITAL_DIAGNOSES = frozenset(
    {
        DiagnosisType.haemophilia_a,
        DiagnosisType.haemophilia_b,
        DiagnosisType.symptomatic_carrier_a,
        DiagnosisType.symptomatic_carrier_b,
    }
)


class CongenitalSeverity(str, Enum):
    severe = "severe"
    moderate = "moderate"
    mild = "mild"
    not_known = "not_known"


class FactorXiDeficiencyLevel(str, Enum):
    severe_deficiency = "severe_deficiency"
    partial_deficiency = "partial_deficiency"
    not_known = "not_known"


class AcquiredBleedingSeverity(str, Enum):
    life_threatening_or_major = "life_threatening_or_major"
    moderate_or_non_life_threatening = "moderate_or_non_life_threatening"
    unknown = "unknown"


class MedicationItem(BaseModel):
    """One medication as the form records it.

    Just what a label says: a product, and how much of it. `dose` is stored as
    the prose the frontend typed rather than a number; the forms count vials —
    the only unit they offer — and the tracker reads the prophylactic dose back
    as the usual dose size when it parses as a whole number of them. Both
    default rather than None because the frontend reads these keys
    unconditionally.

    How often it is taken is not here: the tracker's routine owns the schedule,
    and the supply buffer is one number per profile (`minimum_buffer_vials`).

    `iu_per_vial` is the strength on the vial's label, recorded so a responder
    reading the Medical ID knows what "2 vials" means. It is shown, never
    converted: every amount the app counts stays in vials.
    """

    name: str = Field(max_length=120)
    dose: str = Field(default="", max_length=32)
    unit: str = Field(default="vials", max_length=32)
    iu_per_vial: int | None = Field(default=None, ge=1, le=IU_PER_VIAL_MAX)


class MedicationDetails(MedicationItem):
    """A medication section.

    The API has one slot per section but the form allows several medications in
    each, so a multi-medication section arrives as the first medication's fields
    plus the whole list in `items_json`.
    """

    items_json: str | None = Field(default=None, max_length=ITEMS_JSON_MAX)

    @field_validator("items_json")
    @classmethod
    def _valid_items_json(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            items = json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"items_json must be valid JSON: {exc.msg}") from exc
        if not isinstance(items, list) or not items:
            raise ValueError("items_json must be a non-empty JSON array of medications")
        # Validate against MedicationItem, not MedicationDetails: the nesting
        # stops at one level.
        parsed = [MedicationItem.model_validate(item) for item in items]
        # Re-serialise so what is stored is canonical rather than whatever
        # spacing the client happened to send.
        return json.dumps([item.model_dump() for item in parsed])


def _clean_name(v: str | None) -> str | None:
    if v is None:
        return None
    stripped = v.strip()
    if not stripped:
        raise ValueError("name must not be blank")
    return stripped


# ---------------------------------------------------------------------------
# Medical ID
#
# Who to call and what blood to give. These live inside the clinical profile's
# JSON column rather than on `user`, so adding them needed no ALTER, and every
# one is optional: a card that says "Not recorded" is honest, a card that shows
# a placeholder stranger's phone number is not.
# ---------------------------------------------------------------------------


class BloodType(str, Enum):
    a_positive = "A+"
    a_negative = "A-"
    b_positive = "B+"
    b_negative = "B-"
    ab_positive = "AB+"
    ab_negative = "AB-"
    o_positive = "O+"
    o_negative = "O-"
    unknown = "unknown"


CONTACT_NAME_MAX = 80
CONTACT_PHONE_MAX = 32


def _clean_phone(v: str) -> str:
    """Keep the number as typed, minus surrounding whitespace.

    `tel:` links strip the inner spaces themselves, and "+65 9123 4567" is how
    the number is written on the card. The only check is that there is
    something to dial.
    """
    stripped = v.strip()
    if sum(ch.isdigit() for ch in stripped) < 3:
        raise ValueError("phone must contain a number to dial")
    return stripped


class EmergencyContact(BaseModel):
    """Who a responder should call first."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=CONTACT_NAME_MAX)
    relationship: str = Field(default="", max_length=40)
    phone: str = Field(min_length=1, max_length=CONTACT_PHONE_MAX)

    _clean_name = field_validator("name")(_clean_name)
    _clean_phone = field_validator("phone")(_clean_phone)


class CareTeamContact(BaseModel):
    """The treating clinician or centre.

    The phone is optional: a hospital is still worth naming without a direct
    line, and the card only offers a call button when there is one.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=CONTACT_NAME_MAX)
    organisation: str = Field(default="", max_length=120)
    phone: str | None = Field(default=None, max_length=CONTACT_PHONE_MAX)

    _clean_name = field_validator("name")(_clean_name)

    @field_validator("phone")
    @classmethod
    def _optional_phone(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return _clean_phone(v)


class ClinicalProfile(BaseModel):
    """What the app records about a person, recorded rather than prescribed.

    Only `diagnosis` is required, and onboarding asks for nothing else: the
    severity and the Medical ID block are filled in from the Medical ID page
    when the person wants a card, and `minimum_buffer_vials` is set beside the
    routine on the tracker, which is the only screen where it means anything.
    Every field therefore has to read back as "not recorded" on its own.
    """

    diagnosis: DiagnosisType
    #: Severity as recorded at diagnosis, before prophylaxis — one field per
    #: diagnosis family, and `_clear_inapplicable_fields` nulls the rest.
    congenital_severity: CongenitalSeverity | None = None
    factor_xi_deficiency_level: FactorXiDeficiencyLevel | None = None
    acquired_bleeding_severity: AcquiredBleedingSeverity | None = None
    #: What is taken. The prophylaxis product names Home's status card; both
    #: appear on the Medical ID's "current medication" line.
    prophylactic_medication: MedicationDetails | None = None
    on_demand_medication: MedicationDetails | None = None
    #: Vials to keep at home on top of the routine — what the person expects
    #: to need if they bleed in the coming month. Collected beside the routine;
    #: every order adds it, and the fold says to order early once the stock is
    #: forecast to fall below it. Lives in the JSON column like everything else.
    minimum_buffer_vials: int | None = Field(default=None, ge=0, le=VIALS_MAX)
    #: The day of the month the person orders next month's supply, asked beside
    #: the buffer — early enough for delivery before the 1st. The fold advises
    #: ordering on it; a month too short for it uses its last day.
    order_day_of_month: int | None = Field(default=None, ge=1, le=31)
    # Medical ID. Optional, and in the JSON column, so a row written before
    # these existed reads back as None and the card says "Not recorded"
    # instead of inventing a contact.
    date_of_birth: date | None = None
    has_drug_allergies: bool = False
    drug_allergy_details: str | None = Field(default=None, max_length=ALLERGY_DETAILS_MAX)
    blood_type: BloodType | None = None
    emergency_contact: EmergencyContact | None = None
    primary_doctor: CareTeamContact | None = None

    @field_validator("date_of_birth")
    @classmethod
    def _not_in_the_future(cls, v: date | None) -> date | None:
        if v is not None and v > date.today():
            raise ValueError("date_of_birth must not be in the future")
        return v

    @model_validator(mode="after")
    def _clear_inapplicable_fields(self) -> "ClinicalProfile":
        """Drop the severity values that do not belong to the chosen diagnosis.

        The forms already null these, so this only catches a severity left
        behind when someone changes their diagnosis afterwards. It coerces
        rather than rejects on purpose: the two are recorded on different
        screens, so the new diagnosis can arrive while the old severity is
        still stored, and a 422 there would block a save the UI considers
        valid — the frontend renders `detail` only when it is a string, so a
        field-error list shows up as a bare "Request failed (422)".
        """
        if self.diagnosis not in CONGENITAL_DIAGNOSES:
            self.congenital_severity = None
        if self.diagnosis is not DiagnosisType.factor_xi_deficiency:
            self.factor_xi_deficiency_level = None
        if self.diagnosis is not DiagnosisType.acquired_haemophilia:
            self.acquired_bleeding_severity = None
        return self


class ProfileBase(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    factor_type: FactorType
    dose_state: DoseState = DoseState.covered
    stock_state: StockState = StockState.well_stocked
    vials_on_hand: int = Field(default=0, ge=0, le=VIALS_MAX)
    days_cover: int = Field(default=0, ge=0, le=DAYS_COVER_MAX)
    clinical_profile: ClinicalProfile | None = None

    _clean_name = field_validator("name")(_clean_name)


class ProfileCreate(ProfileBase):
    pass


class ProfileUpdate(BaseModel):
    """Every field optional — PATCH only touches what it names."""

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)
    factor_type: FactorType | None = None
    dose_state: DoseState | None = None
    stock_state: StockState | None = None
    vials_on_hand: int | None = Field(default=None, ge=0, le=VIALS_MAX)
    days_cover: int | None = Field(default=None, ge=0, le=DAYS_COVER_MAX)
    clinical_profile: ClinicalProfile | None = None

    _clean_name = field_validator("name")(_clean_name)


class ProfileRead(ProfileBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    #: Folded from the ledger on every read, so unlike the writable column it
    #: is not capped: a few refills on the shelf can add up past VIALS_MAX.
    vials_on_hand: int = 0
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Tracking events
#
# `trackingevent` is one table with a nullable column per kind-specific field,
# so nothing in the database knows that a refill has no `missed_on`. The
# discriminated union below is the only thing that does. Every field name and
# every literal is the frontend's `TrackerEntry` / `DoseAmount` in
# `src/lib/tracker-entries.ts`, so the wire needs no translation layer.
# ---------------------------------------------------------------------------


class DoseAmountIn(BaseModel):
    """How much factor a made-up dose used.

    `routine` defers to the routine's dose size and `pending` means the user
    has not answered yet, so neither carries an amount — folding an amount the
    user never gave would invent supply.
    """

    model_config = ConfigDict(extra="forbid")

    source: AmountSource
    vials: int | None = Field(default=None, gt=0, le=VIALS_MAX)

    @model_validator(mode="after")
    def _vials_match_source(self) -> "DoseAmountIn":
        if self.source is AmountSource.custom and self.vials is None:
            raise ValueError("a custom amount needs a vial count")
        if self.source is not AmountSource.custom and self.vials is not None:
            raise ValueError(f"a {self.source.value} amount must not carry a vial count")
        return self


class EventCreateBase(BaseModel):
    """`extra="forbid"` is the enforcement point.

    Each subclass carries exactly the fields of its own kind, so dumping one
    can only populate that kind's columns and every other stays null.
    """

    model_config = ConfigDict(extra="forbid")

    occurred_on: date


class RefillCreate(EventCreateBase):
    kind: Literal[EventKind.refill]
    vials: int = Field(gt=0, le=VIALS_MAX)


class ProphylaxisCreate(EventCreateBase):
    """The planned preventative dose.

    Sized by the routine in force on its day unless `vials` says otherwise.
    History imported from another tracker usually predates any routine here,
    and a routine-sized dose with no routine charges nothing.
    """

    kind: Literal[EventKind.prophylaxis]
    vials: int | None = Field(default=None, gt=0, le=VIALS_MAX)


class OnDemandCreate(EventCreateBase):
    """A dose given for a bleed — the app's own marker for one.

    `bleed_nature` is optional on the wire so history imported from another
    tracker, which rarely records it, still loads; the tracker's own flow
    always asks.
    """

    kind: Literal[EventKind.on_demand]
    vials: int = Field(gt=0, le=VIALS_MAX)
    bleed_nature: BleedNature | None = None


class FollowUpCreate(EventCreateBase):
    kind: Literal[EventKind.follow_up]
    vials: int = Field(gt=0, le=VIALS_MAX)


class MakeupCreate(EventCreateBase):
    """A planned dose taken late, filed on the day it was actually taken.

    One event, not two: the miss itself is derived (a planned day with no
    factor use on it), so `missed_on` is the only record that this dose was
    the one owed for that day.
    """

    kind: Literal[EventKind.makeup]
    missed_on: date
    amount: DoseAmountIn

    @model_validator(mode="after")
    def _made_up_after_the_miss(self) -> "MakeupCreate":
        if self.occurred_on < self.missed_on:
            raise ValueError("a dose cannot make up for a miss that has not happened yet")
        return self


class CountCreate(EventCreateBase):
    """The vials actually at home, counted — how a wrong entry is corrected.

    Not a delta: the fold takes the count over whatever the entries before it
    add up to, so editing or backdating an earlier entry cannot move a figure
    the user checked against the shelf. Same-day entries are placed by the
    order they were logged in, so a dose logged after the count still comes off it.
    Zero is a real count.
    """

    kind: Literal[EventKind.count]
    vials: int = Field(ge=0, le=VIALS_MAX)


EventCreate = Annotated[
    RefillCreate | ProphylaxisCreate | OnDemandCreate | FollowUpCreate | MakeupCreate | CountCreate,
    Field(discriminator="kind"),
]


def to_event(payload: EventCreate, user_id: int) -> TrackingEvent:
    """The only sanctioned way to build a TrackingEvent.

    Because each Create model carries exactly its own fields and forbids
    extras, dumping it can only populate columns belonging to that kind.
    """
    # Not mode="json": the date columns want `date` objects, and `kind` is
    # unwrapped explicitly so a plain VARCHAR column stores the value rather
    # than whatever str() an enum member happens to produce.
    data = payload.model_dump(exclude={"amount"})
    data["kind"] = payload.kind.value
    if isinstance(data.get("bleed_nature"), BleedNature):
        data["bleed_nature"] = data["bleed_nature"].value
    amount = getattr(payload, "amount", None)
    if amount is not None:
        data["amount_source"] = amount.source.value
        data["amount_vials"] = amount.vials
    return TrackingEvent(user_id=user_id, **data)


class TrackingEventRead(BaseModel):
    """Flat, not a discriminated union — deliberately.

    The calendar is a heterogeneous list, and a response union would force the
    frontend to narrow before it could render a row. Flat costs a few nulls and
    buys `events.map(...)`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: EventKind
    occurred_on: date
    vials: int | None = None
    bleed_nature: BleedNature | None = None
    missed_on: date | None = None
    amount_source: AmountSource | None = None
    amount_vials: int | None = None
    #: The vials the fold charged the cupboard for this event: positive for a
    #: refill, negative for a dose, zero when the amount is not known, and for
    #: a count the correction it made to the running total. Derived
    #: on every read — a prophylaxis dose is sized by the schedule in force on
    #: its day, or by its own `vials` when it was logged with one — so the
    #: tracker shows the figure the supply total actually used.
    applied_vials: int = 0


def event_read(event: TrackingEvent, applied_vials: int) -> TrackingEventRead:
    return TrackingEventRead.model_validate({**event.model_dump(), "applied_vials": applied_vials})


# ---------------------------------------------------------------------------
# Dose schedules
#
# A series is a calendar's recurring event: created and deleted whole, never
# edited in place. Moving one occurrence is an exception keyed by the date the
# cycle put it on. A permanent shift is a new series replacing the old.
#
# A plan is a temporary change to the routine over a date range: the exact days
# a dose is due, a dose size, or both. Its days are picked on the calendar
# rather than following a rhythm.
# ---------------------------------------------------------------------------


def _clean_weekdays(v: list[int] | None) -> list[int] | None:
    if v is None:
        return None
    days = sorted(set(v))
    if not days:
        raise ValueError("pick at least one weekday")
    if any(day < 0 or day > 6 for day in days):
        raise ValueError("weekdays run 0 (Sunday) to 6 (Saturday)")
    return days


class RecurrenceIn(BaseModel):
    """How often a dose is due: every `interval_days`, or on fixed `weekdays`.

    Weekdays are numbered as the frontend's `Date.getDay()` — 0 = Sunday to
    6 = Saturday — so the calendar never translates.
    """

    model_config = ConfigDict(extra="forbid")

    interval_days: int | None = Field(default=None, gt=0, le=INTERVAL_DAYS_MAX)
    weekdays: list[int] | None = Field(default=None, max_length=7)

    _clean_weekdays = field_validator("weekdays")(_clean_weekdays)

    @property
    def has_frequency(self) -> bool:
        return self.interval_days is not None or self.weekdays is not None

    @model_validator(mode="after")
    def _one_kind_of_frequency(self) -> "RecurrenceIn":
        if self.interval_days is not None and self.weekdays is not None:
            raise ValueError("a dose is every N days or on fixed weekdays, not both")
        return self


class ScheduleCreate(RecurrenceIn):
    start_on: date
    vials: int = Field(gt=0, le=VIALS_MAX)
    #: Delete every other series first, in the same transaction. The tracker
    #: keeps one routine at a time, and two calls would leave a window with none.
    replace: bool = False

    @model_validator(mode="after")
    def _needs_a_frequency(self) -> "ScheduleCreate":
        if not self.has_frequency:
            raise ValueError("a series needs interval_days or weekdays")
        return self


class ScheduleRead(BaseModel):
    id: int
    start_on: date
    interval_days: int | None
    weekdays: list[int] | None
    vials: int


def schedule_read(schedule) -> ScheduleRead:
    """From a `models.DoseSchedule`, whose weekdays are one comma-separated column."""
    return ScheduleRead(
        id=schedule.id,
        start_on=schedule.start_on,
        interval_days=schedule.interval_days,
        weekdays=parse_weekdays(schedule.weekdays),
        vials=schedule.vials,
    )


class PlanCreate(BaseModel):
    """A temporary change to the routine between two dates, inclusive: the
    days a dose is due, a different dose, or both. Whatever is left unset
    stays as the routine has it. Plans may not overlap; the router checks.

    `dose_dates` are picked one by one and must fall inside the range; they
    replace the routine's doses there, so a day left out has no dose."""

    model_config = ConfigDict(extra="forbid")

    start_on: date
    end_on: date
    dose_dates: list[date] | None = Field(default=None, min_length=1, max_length=PLAN_DAYS_MAX)
    vials: int | None = Field(default=None, gt=0, le=VIALS_MAX)

    @field_validator("dose_dates")
    @classmethod
    def _sorted_dates(cls, v: list[date] | None) -> list[date] | None:
        return sorted(set(v)) if v is not None else None

    @model_validator(mode="after")
    def _a_change_over_a_range(self) -> "PlanCreate":
        if self.end_on < self.start_on:
            raise ValueError("a plan cannot end before it starts")
        if (self.end_on - self.start_on).days >= PLAN_DAYS_MAX:
            raise ValueError(f"a plan runs at most {PLAN_DAYS_MAX} days; longer is a new routine")
        if self.dose_dates and not (
            self.start_on <= self.dose_dates[0] and self.dose_dates[-1] <= self.end_on
        ):
            raise ValueError("every dose date must fall inside the plan's dates")
        if self.dose_dates is None and self.vials is None:
            raise ValueError("a plan changes the dose days, the dose, or both")
        return self


class PlanRead(BaseModel):
    id: int
    start_on: date
    end_on: date
    dose_dates: list[date] | None
    vials: int | None


def plan_read(plan) -> PlanRead:
    """From a `models.DosePlan`, whose dose dates are one comma-separated column."""
    return PlanRead(
        id=plan.id,
        start_on=plan.start_on,
        end_on=plan.end_on,
        dose_dates=parse_dates(plan.dose_dates),
        vials=plan.vials,
    )


class OccurrenceRead(BaseModel):
    """One planned dose. `on` is where it sits on the calendar; `original_on`
    is where the cycle put it, and identifies it for moving.

    A dose belongs to the series (`schedule_id`) or to a plan that replaces
    the series' dose days for its dates (`plan_id`). Only a series' dose can
    be moved; a plan's doses follow the plan.
    """

    on: date
    original_on: date
    schedule_id: int | None
    plan_id: int | None
    vials: int
    moved: bool


def occurrence_read(occurrence) -> OccurrenceRead:
    """From a `services.Occurrence`, duck-typed so schemas never imports services."""
    return OccurrenceRead(
        on=occurrence.on,
        original_on=occurrence.original_on,
        schedule_id=occurrence.schedule_id,
        plan_id=occurrence.plan_id,
        vials=occurrence.vials,
        moved=occurrence.moved,
    )


class MoveOccurrence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    moved_to: date


class OrderAdvice(BaseModel):
    """When to order and how much, from stock, the schedule, the buffer and the
    monthly order day.

    `by_on` is the profile's next monthly order day (`on_order_day`), or an
    earlier day when the stock is forecast to fall below the buffer or run out
    before it — today, if it already has. Without an order day only the stock
    sets it. `due` says it has arrived.

    An order on the order day is next month's supply: `covers_from` is the 1st
    and `covers_until` the last day, and the delivery has until the 1st to
    arrive. An early order runs until the next regular order's month begins.
    `vials` covers the planned doses in that window, the bridge doses between
    `by_on` and `covers_from` that the stock must still supply, and the buffer
    on top, less what will still be there after `by_on`'s dose. Only logged
    doses have left the cupboard: a missed dose is never counted as used. The
    rest is the working, so the card can show how the number was reached.
    """

    by_on: date
    vials: int
    due: bool
    on_order_day: bool
    #: The window the order is sized for: a calendar month with an order day
    #: set (or the rest of one, for an early order), otherwise the day after
    #: `by_on` through `ORDER_COVERS_DAYS` after it.
    covers_from: date
    covers_until: date
    #: The doses planned from `covers_from` through `covers_until`, and their vials.
    planned_doses: int
    planned_vials: int
    #: The doses planned after `by_on` and before `covers_from`, and their vials.
    bridge_doses: int
    bridge_vials: int
    #: What will still be in the cupboard after `by_on`'s dose.
    leftover_vials: int
    #: The vials the profile keeps at home; zero when none is set.
    buffer_vials: int


class StatusRead(BaseModel):
    """What Home and the tracker need that a profile row cannot say.

    Facts only. The cover wording and the activity-safety heuristics stay in
    `Home.tsx` where they are already isolated and labelled as prototype logic.
    """

    as_of: date
    vials_on_hand: int
    #: Factor used that the ledger could not supply. Non-zero means an unlogged
    #: refill, not a negative cupboard.
    unaccounted_vials: int
    #: Calendar days until the cupboard cannot supply a planned dose, capped at
    #: FORECAST_DAYS. Zero without a schedule.
    days_cover: int
    #: The first planned dose the cupboard cannot supply. None when stocked for
    #: the whole forecast, or when there is no schedule.
    runs_out_on: date | None
    last_dose_on: date | None
    #: The most recent on-demand dose. On-demand use is the app's marker for a
    #: treated bleed — it is what the calendar draws the blood drop for — so
    #: Home can say "recent bleed" without a bleed table.
    last_bleed_on: date | None
    #: The series in force today.
    schedule: ScheduleRead | None
    #: The first planned dose after the last logged one with no factor use on
    #: it. In the past means overdue, and it is also the first entry of
    #: `missed_doses`.
    next_dose: OccurrenceRead | None
    order: OrderAdvice | None
    dose_state: DoseState
    stock_state: StockState
    #: Planned days over the last MISSED_LOOKBACK_DAYS, before today, with no
    #: factor use logged on them — newest first. Derived, never stored: this is
    #: what "missed dose" means, and logging a dose on one of these days (or a
    #: makeup naming it) takes it off the list.
    missed_doses: list[date]
    #: Newest first — the "Recent activity" list.
    recent_events: list[TrackingEventRead]


# ---------------------------------------------------------------------------
# Supplies — the tracker's Inventory card
# ---------------------------------------------------------------------------

#: The card's own input limit.
SUPPLY_NAME_MAX = 40
SUPPLY_QUANTITY_MAX = 9999
#: More than this is not a household's cupboard.
SUPPLIES_MAX = 50


class SupplyItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=SUPPLY_NAME_MAX)
    quantity: int = Field(default=0, ge=0, le=SUPPLY_QUANTITY_MAX)

    _clean_name = field_validator("name")(_clean_name)


class SupplyItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    quantity: int


# ---------------------------------------------------------------------------
# Imports from another tracker
#
# Rows arrive already in the ledger vocabulary (`EventCreate`); the service in
# `app/importing.py` decides, row by row, what an import would do, and the
# report below is that decision — the same shape whether it was a dry run or
# the real thing. `POST /users/{id}/imports` and the MCP tool both return it.
# ---------------------------------------------------------------------------

IMPORT_ROWS_MAX = 500
IMPORT_SOURCE_MAX = 120

OnConflict = Literal["skip", "replace"]


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Where the rows came from — a file name, "Claude.ai" — kept on the batch.
    source: str = Field(default="import", min_length=1, max_length=IMPORT_SOURCE_MAX)
    #: A row that collides with one already in the ledger: keep the ledger's
    #: row, or delete it and write this one.
    on_conflict: OnConflict = "skip"
    #: Report what would happen and write nothing.
    dry_run: bool = False
    events: list[EventCreate] = Field(min_length=1, max_length=IMPORT_ROWS_MAX)


class ImportAction(str, Enum):
    create = "create"
    replace = "replace"
    skip = "skip"
    error = "error"


class ImportReason(str, Enum):
    #: An identical row is already in the ledger, or earlier in this batch.
    already_present = "already-present"
    #: Collides with a ledger row: skipped, or replaced it under `on_conflict`.
    conflict = "conflict"
    #: Collides with an earlier row of the same batch. Never written.
    in_batch_conflict = "in-batch-conflict"
    #: Dated after today in Singapore. The tracker cannot log the future.
    future_date = "future-date"
    #: A makeup dose whose `missed_on` day already holds a factor use, so the
    #: dose it claims to make up was not missed. Counting both would charge the
    #: cupboard twice for one dose.
    not_missed = "not-missed"


class ImportRowResult(BaseModel):
    index: int
    kind: EventKind
    occurred_on: date
    action: ImportAction
    reason: ImportReason | None = None
    #: Ledger ids this row collides with (skip) or removes (replace).
    existing_ids: list[int] = Field(default_factory=list)
    #: For a collision inside the batch, the earlier row's index.
    conflicts_with_index: int | None = None
    #: The id it was written under; None on a dry run or when nothing was written.
    event_id: int | None = None
    message: str


class ImportReport(BaseModel):
    dry_run: bool
    #: False on a dry run, and on a real import refused because a row was in
    #: error — nothing was written either way.
    written: bool
    batch_id: int | None = None
    source: str
    on_conflict: OnConflict
    rows_received: int
    rows_created: int
    rows_replaced: int
    rows_skipped: int
    rows_errored: int
    rows: list[ImportRowResult]
    #: The fold after a real import, so the caller can say what changed.
    status: StatusRead | None = None


class ImportBatchRead(BaseModel):
    id: int
    source: str
    created_at: datetime
    rows_received: int
    rows_created: int
    rows_replaced: int
    rows_skipped: int
    event_ids: list[int]
    #: How many of those events still exist — what an undo would remove.
    events_remaining: int
