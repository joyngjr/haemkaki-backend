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
    DoseState,
    EventKind,
    FactorType,
    MissedStatus,
    StockState,
    TrackingEvent,
    parse_weekdays,
)

NAME_MAX = 60
VIALS_MAX = 99
DAYS_COVER_MAX = 365

# The allergy picker has no selection limit and the catalog holds 187 drugs, so
# the worst honest case is a few thousand characters of joined names.
ALLERGY_DETAILS_MAX = 4000
# Longest real combination is 56 characters; the headroom is for new options.
TREATMENT_APPROACH_MAX = 128
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


#: Diagnoses someone is born with — the only ones with a severity band and an
#: inhibitor history.
CONGENITAL_DIAGNOSES = frozenset(
    {
        DiagnosisType.haemophilia_a,
        DiagnosisType.haemophilia_b,
        DiagnosisType.symptomatic_carrier_a,
        DiagnosisType.symptomatic_carrier_b,
    }
)
#: Factor IX diagnoses, where anaphylaxis to FIX is a real and specific risk.
FACTOR_IX_DIAGNOSES = frozenset({DiagnosisType.haemophilia_b, DiagnosisType.symptomatic_carrier_b})


class Sex(str, Enum):
    male = "male"
    female = "female"
    other = "other"
    # The 12 September form offered this and the 19 September one does not.
    # Kept so an older profile still reads back.
    prefer_not_to_say = "prefer_not_to_say"


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


class InhibitorStatus(str, Enum):
    current = "current"
    previous = "previous"
    none_known = "none_known"
    unknown = "unknown"


class YesNoUnknown(str, Enum):
    yes = "yes"
    no = "no"
    unknown = "unknown"


class TreatmentApproach(str, Enum):
    factor_replacement = "factor_replacement"
    non_factor_therapy = "non_factor_therapy"
    bypassing_therapy = "bypassing_therapy"
    specialist_plan = "specialist_plan"
    none = "none"


class MedicationItem(BaseModel):
    """One medication as the form records it.

    `dose`, `frequency` and `buffer_days` are prose the frontend composes
    ("3 times per week", "7 days") and regex-strips again when it reopens a
    profile for editing. Store them verbatim — reformatting here breaks that
    round-trip. Everything defaults to "" rather than None because the frontend
    reads these keys unconditionally.
    """

    name: str = Field(max_length=120)
    dose: str = Field(default="", max_length=32)
    unit: str = Field(default="", max_length=32)
    frequency: str = Field(default="", max_length=64)
    administration: str = Field(default="", max_length=64)
    buffer_days: str = Field(default="", max_length=32)


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
    """Diagnosis-specific onboarding data, recorded rather than prescribed."""

    diagnosis: DiagnosisType
    sex: Sex = Sex.prefer_not_to_say
    weight_kg: float | None = Field(default=None, gt=0, le=500)
    date_of_birth: date | None = None
    has_drug_allergies: bool = False
    drug_allergy_details: str | None = Field(default=None, max_length=ALLERGY_DETAILS_MAX)
    # This is intentionally the diagnostic/pre-prophylaxis result, not a
    # contemporaneous result that may be normalised by regular treatment.
    diagnosis_factor_activity_percent: float | None = Field(default=None, ge=0, le=150)
    diagnosis_test_date: date | None = None
    congenital_severity: CongenitalSeverity | None = None
    factor_xi_deficiency_level: FactorXiDeficiencyLevel | None = None
    acquired_inhibitor_titre_bu_ml: float | None = Field(default=None, ge=0, le=10000)
    acquired_bleeding_severity: AcquiredBleedingSeverity | None = None
    inhibitor_status: InhibitorStatus | None = None
    fix_allergy_or_anaphylaxis: YesNoUnknown | None = None
    treatment_approach: str | None = Field(default=None, max_length=TREATMENT_APPROACH_MAX)
    prophylactic_medication: MedicationDetails | None = None
    minimum_buffer: str | None = Field(default=None, max_length=64)
    on_demand_medication: MedicationDetails | None = None
    other_medication: MedicationDetails | None = None
    medication_reminders: bool = False
    #: Days of coverage to keep in reserve before ordering. The form collects it
    #: in days; the fold turns it into an order date from the schedule. Lives in
    #: the JSON column like everything else here, so adding it needed no ALTER.
    minimum_buffer_days: float | None = Field(default=None, ge=0, le=DAYS_COVER_MAX)
    # Medical ID. Optional, and in the JSON column like `routine`, so a row
    # written before they existed reads back as None and the card says
    # "Not recorded" instead of inventing a contact.
    blood_type: BloodType | None = None
    emergency_contact: EmergencyContact | None = None
    primary_doctor: CareTeamContact | None = None

    @field_validator("date_of_birth")
    @classmethod
    def _not_in_the_future(cls, v: date | None) -> date | None:
        if v is not None and v > date.today():
            raise ValueError("date_of_birth must not be in the future")
        return v

    @field_validator("treatment_approach")
    @classmethod
    def _known_treatments(cls, v: str | None) -> str | None:
        """A comma-joined multi-select. The single column cannot police itself."""
        if v is None or not v.strip():
            return None
        tokens = [token.strip() for token in v.split(",")]
        if any(not token for token in tokens):
            raise ValueError("treatment_approach must not contain blank entries")
        if len(set(tokens)) != len(tokens):
            raise ValueError("treatment_approach must not repeat an entry")
        allowed = {member.value for member in TreatmentApproach}
        unknown = [token for token in tokens if token not in allowed]
        if unknown:
            raise ValueError(f"unknown treatment_approach: {', '.join(unknown)}")
        # Mirrors the form's toggle: choosing "none" clears everything else.
        if TreatmentApproach.none.value in tokens and len(tokens) > 1:
            raise ValueError("treatment_approach 'none' cannot be combined with other options")
        return ",".join(tokens)

    @model_validator(mode="after")
    def _clear_inapplicable_fields(self) -> "ClinicalProfile":
        """Drop values that do not belong to the chosen diagnosis.

        The form already nulls these, so this only catches a stale value left
        behind when someone edits their diagnosis. It coerces rather than
        rejects on purpose: the form's submit button re-checks only step 0, so a
        422 here would block a submit the UI considers valid, and the frontend
        renders `detail` only when it is a string — a field-error list shows up
        as a bare "Request failed (422)".
        """
        is_congenital = self.diagnosis in CONGENITAL_DIAGNOSES
        if not is_congenital:
            self.congenital_severity = None
            self.inhibitor_status = None
        if self.diagnosis is not DiagnosisType.factor_xi_deficiency:
            self.factor_xi_deficiency_level = None
        if self.diagnosis is not DiagnosisType.acquired_haemophilia:
            self.acquired_inhibitor_titre_bu_ml = None
            self.acquired_bleeding_severity = None
        if self.diagnosis not in FACTOR_IX_DIAGNOSES:
            self.fix_allergy_or_anaphylaxis = None
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
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Tracking events
#
# `trackingevent` is one table with a nullable column per kind-specific field,
# so nothing in the database knows that a refill has no `status`. The
# discriminated union below is the only thing that does. Every field name and
# every literal is the frontend's `TrackerEntry` / `DoseAmount` in
# `src/lib/tracker-entries.ts`, so the wire needs no translation layer.
# ---------------------------------------------------------------------------


class DoseAmountIn(BaseModel):
    """How much factor a made-up dose used.

    `routine` defers to the profile's prophylaxis dosage and `pending` means
    the user has not answered yet, so neither carries a vial count — folding
    an amount the user never gave would invent supply.
    """

    model_config = ConfigDict(extra="forbid")

    source: AmountSource
    vials: int | None = Field(default=None, gt=0, le=VIALS_MAX)

    @model_validator(mode="after")
    def _vials_match_source(self) -> "DoseAmountIn":
        if self.source is AmountSource.custom and self.vials is None:
            raise ValueError("a custom amount needs vials")
        if self.source is not AmountSource.custom and self.vials is not None:
            raise ValueError(f"a {self.source.value} amount must not carry vials")
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
    """The planned preventative dose. Its size always comes from the routine."""

    kind: Literal[EventKind.prophylaxis]


class OnDemandCreate(EventCreateBase):
    kind: Literal[EventKind.on_demand]
    vials: int = Field(gt=0, le=VIALS_MAX)


class FollowUpCreate(EventCreateBase):
    kind: Literal[EventKind.follow_up]
    vials: int = Field(gt=0, le=VIALS_MAX)


class MakeupCreate(EventCreateBase):
    """A dose that made up for a missed one, filed on the day it was taken."""

    kind: Literal[EventKind.makeup]
    missed_on: date
    amount: DoseAmountIn

    @model_validator(mode="after")
    def _made_up_after_the_miss(self) -> "MakeupCreate":
        if self.occurred_on < self.missed_on:
            raise ValueError("a dose cannot make up for a miss that has not happened yet")
        return self


class MissedCreate(EventCreateBase):
    """A dose that was due but not taken. Filed on the day it was due.

    `taken_on` and `amount` stay optional even once the status is `taken`:
    the frontend's flow records the status first, then the day, then the
    amount, and each step has to be savable on its own.
    """

    kind: Literal[EventKind.missed]
    status: MissedStatus = MissedStatus.awaiting
    taken_on: date | None = None
    amount: DoseAmountIn | None = None

    @model_validator(mode="after")
    def _only_a_taken_dose_has_details(self) -> "MissedCreate":
        if self.status is not MissedStatus.taken:
            if self.taken_on is not None:
                raise ValueError(f"a {self.status.value} dose has no taken_on")
            if self.amount is not None:
                raise ValueError(f"a {self.status.value} dose has no amount")
        return self


EventCreate = Annotated[
    RefillCreate
    | ProphylaxisCreate
    | OnDemandCreate
    | FollowUpCreate
    | MakeupCreate
    | MissedCreate,
    Field(discriminator="kind"),
]


def to_event(payload: EventCreate, user_id: int) -> TrackingEvent:
    """The only sanctioned way to build a TrackingEvent.

    Because each Create model carries exactly its own fields and forbids
    extras, dumping it can only populate columns belonging to that kind.
    """
    # Not mode="json": the date columns want `date` objects, and the enums are
    # unwrapped explicitly so a plain VARCHAR column stores the value rather
    # than whatever str() an enum member happens to produce.
    data = payload.model_dump(exclude={"amount"})
    data["kind"] = payload.kind.value
    if data.get("status") is not None:
        data["status"] = data["status"].value
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
    missed_on: date | None = None
    amount_source: AmountSource | None = None
    amount_vials: int | None = None
    status: MissedStatus | None = None
    taken_on: date | None = None
    #: What the fold charged the cupboard for this event: positive for a
    #: refill, negative for a dose, zero when the amount is not known. Derived
    #: on every read — a prophylaxis dose is sized by the schedule in force on
    #: its day — so the tracker shows the figure the supply total actually used.
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
# A plan is a temporary change to the routine over a date range. It carries the
# same two frequency fields, both optional, plus an optional dose size.
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
    6 = Saturday — so the calendar never translates. Both may be left unset
    only where the subclass allows it (a plan that changes just the dose).
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


class PlanCreate(RecurrenceIn):
    """A temporary change to the routine between two dates, inclusive: a
    different frequency, a different dose, or both. Whatever is left unset
    stays as the routine has it. Plans may not overlap; the router checks."""

    start_on: date
    end_on: date
    vials: int | None = Field(default=None, gt=0, le=VIALS_MAX)

    @model_validator(mode="after")
    def _a_change_over_a_range(self) -> "PlanCreate":
        if self.end_on < self.start_on:
            raise ValueError("a plan cannot end before it starts")
        if (self.end_on - self.start_on).days >= PLAN_DAYS_MAX:
            raise ValueError(f"a plan runs at most {PLAN_DAYS_MAX} days; longer is a new routine")
        if not self.has_frequency and self.vials is None:
            raise ValueError("a plan changes the frequency, the dose, or both")
        return self


class PlanRead(BaseModel):
    id: int
    start_on: date
    end_on: date
    interval_days: int | None
    weekdays: list[int] | None
    vials: int | None


def plan_read(plan) -> PlanRead:
    """From a `models.DosePlan`, same column encoding as a series."""
    return PlanRead(
        id=plan.id,
        start_on=plan.start_on,
        end_on=plan.end_on,
        interval_days=plan.interval_days,
        weekdays=parse_weekdays(plan.weekdays),
        vials=plan.vials,
    )


class OccurrenceRead(BaseModel):
    """One planned dose. `on` is where it sits on the calendar; `original_on`
    is where the cycle put it, and identifies it for moving.

    A dose belongs to the series (`schedule_id`) or to a plan that replaces
    the series' frequency for its dates (`plan_id`). Only a series' dose can
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
    """When to order and how much, from stock and the schedule alone.

    `by_on` is the run-out date less the profile's buffer days; `due` says it
    has already arrived. `vials` covers the doses from the run-out date through
    the next month plus the buffer, less whatever is left in the cupboard. The
    rest is the working, so the card can show how the number was reached.
    """

    by_on: date
    vials: int
    due: bool
    #: The last day the order is sized to cover: the run-out date plus a month
    #: plus the buffer.
    covers_until: date
    #: The doses planned from the run-out date through `covers_until`, and the
    #: vials they use.
    planned_doses: int
    planned_vials: int
    #: What will still be in the cupboard on the run-out date — too little for
    #: that day's dose, but it counts towards the order.
    leftover_vials: int
    buffer_days: int


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
    #: The first planned dose after the last logged one that nothing has
    #: settled — logged, or recorded as missed. In the past means overdue.
    next_dose: OccurrenceRead | None
    order: OrderAdvice | None
    dose_state: DoseState
    stock_state: StockState
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
