"""Everything derived from the event ledger and the dose schedules.

Nothing in here is stored. The ledger is folded from scratch on every call,
which is what makes backdating safe: an event inserted between two existing
ones simply lands in its correct chronological slot on the next fold. A running
balance column would have applied it at the end.

Three questions, answered in one pass because they share their inputs:

* how many vials are at home — refills minus doses, each prophylaxis dose
  sized by the schedule (or the plan) in force on its day;
* what is planned — the schedule's occurrences, with moved ones moved and a
  plan's dates following the plan instead, and from those the next dose that
  nothing has settled;
* how long the cupboard lasts — the planned doses walked forward against the
  stock, which gives the run-out date and, less the buffer, the order date.

The dose state is a *schedule estimate*, not a measured factor level and not
pharmacokinetics. It is the same claim the frontend already makes for its
cover figure (`CoverStatus.source: "scheduleEstimate"` in `src/lib/home-data.ts`).
"""

import logging
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from sqlmodel import Session, select

from app.models import (
    AmountSource,
    DosePlan,
    DoseSchedule,
    DoseState,
    EventKind,
    ScheduleException,
    StockState,
    TrackingEvent,
    parse_weekdays,
)
from app.schemas import FORECAST_DAYS

logger = logging.getLogger(__name__)

#: The app is Singapore-only, and `occurred_on` is a Singapore-local calendar
#: date (`getSingaporeToday` in `src/lib/tracker-dates.ts`). Comparing it to the
#: server's own `date.today()` would put Railway eight hours behind the user and
#: hide today's doses until 08:00.
SGT = timezone(timedelta(hours=8))


def today_sgt() -> date:
    return datetime.now(SGT).date()


#: Kinds that take factor out of the cupboard, and so count as a dose.
DOSE_KINDS = frozenset(
    {
        EventKind.prophylaxis.value,
        EventKind.on_demand.value,
        EventKind.follow_up.value,
        EventKind.makeup.value,
    }
)

#: Kinds that settle a planned dose on their day: any dose (the tracker keeps
#: one factor use per day, so an on-demand dose stands in for the planned one)
#: or a missed-dose record, whatever it was answered with.
RESOLVING_KINDS = DOSE_KINDS | {EventKind.missed.value}

#: Above this fraction of the interval elapsed, the last dose is still holding.
COVERED_FRACTION = 0.66

STOCK_WELL_STOCKED = 5
STOCK_MODERATE = 2

#: An order covers this many days of doses past the day stock runs out, plus
#: the buffer. A month is what a household order looks like.
ORDER_COVERS_DAYS = 30


@dataclass(frozen=True)
class Occurrence:
    """One planned dose. `on` is where it sits; `original_on` is where the
    cycle put it, and is what identifies it.

    A dose belongs to the series (`schedule_id`, and it can be moved) or to a
    plan that replaces the series' rhythm for its dates (`plan_id`).
    """

    on: date
    original_on: date
    vials: int
    schedule_id: int | None = None
    plan_id: int | None = None

    @property
    def moved(self) -> bool:
        return self.on != self.original_on


@dataclass(frozen=True)
class OrderAdvice:
    by_on: date
    vials: int
    due: bool
    #: The working behind `vials`, so the card can show it. See `schemas.OrderAdvice`.
    covers_until: date
    planned_doses: int
    planned_vials: int
    leftover_vials: int
    buffer_days: int


@dataclass
class SupplySnapshot:
    as_of: date
    vials_on_hand: int = 0
    #: Factor used that the ledger could not supply — a data-quality signal
    #: (an unlogged refill), never a negative stock level.
    unaccounted_vials: int = 0
    last_dose_on: date | None = None
    #: The most recent on-demand dose — a treated bleed, in the app's vocabulary.
    last_bleed_on: date | None = None
    #: The series in force on `as_of`.
    schedule: DoseSchedule | None = None
    next_dose: Occurrence | None = None
    runs_out_on: date | None = None
    days_cover: int = 0
    order: OrderAdvice | None = None
    events: list[TrackingEvent] = field(default_factory=list)
    #: Event id -> the vials the fold charged for it.
    applied: dict[int, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Recurrence — what a series and a plan have in common
# ---------------------------------------------------------------------------


class Recurrence(Protocol):
    """A first day and a rhythm: every `interval_days`, or on fixed `weekdays`
    (the comma-separated column, 0 = Sunday … 6 = Saturday)."""

    start_on: date
    interval_days: int | None
    weekdays: str | None


def js_weekday(day: date) -> int:
    """0 = Sunday … 6 = Saturday, as the frontend's `Date.getDay()` numbers them."""
    return (day.weekday() + 1) % 7


def has_frequency(rule: Recurrence) -> bool:
    return bool(rule.interval_days or rule.weekdays)


def cycle_days(rule: Recurrence) -> int:
    """The longest gap the rhythm can leave between two doses."""
    return rule.interval_days or 7


def expected_gap_days(rule: Recurrence) -> float:
    """The typical gap between doses — what the dose state measures elapsed
    time against. Three fixed weekdays are "about every 2⅓ days"."""
    if rule.interval_days:
        return float(rule.interval_days)
    days = parse_weekdays(rule.weekdays) or []
    return 7 / len(days) if days else 7.0


def is_tick(rule: Recurrence, day: date) -> bool:
    """Whether the rhythm itself lands on `day`, before any exception or plan."""
    if day < rule.start_on:
        return False
    if rule.weekdays:
        return js_weekday(day) in (parse_weekdays(rule.weekdays) or [])
    if not rule.interval_days:
        return False
    return (day - rule.start_on).days % rule.interval_days == 0


def _days(since: date, until: date) -> Iterator[date]:
    day = since
    while day <= until:
        yield day
        day += timedelta(days=1)


def _ticks(rule: Recurrence, since: date, until: date) -> list[date]:
    """The rhythm's own dates in [since, until]."""
    since = max(since, rule.start_on)
    if until < since:
        return []
    if rule.weekdays:
        wanted = set(parse_weekdays(rule.weekdays) or [])
        return [day for day in _days(since, until) if js_weekday(day) in wanted]
    if not rule.interval_days:
        return []
    first = -(-(since - rule.start_on).days // rule.interval_days)  # ceil
    ticks = []
    day = rule.start_on + timedelta(days=first * rule.interval_days)
    while day <= until:
        ticks.append(day)
        day += timedelta(days=rule.interval_days)
    return ticks


# ---------------------------------------------------------------------------
# Schedules and plans
# ---------------------------------------------------------------------------


def load_schedules(session: Session, user_id: int) -> list[DoseSchedule]:
    return list(
        session.exec(
            select(DoseSchedule)
            .where(DoseSchedule.user_id == user_id)
            .order_by(DoseSchedule.start_on, DoseSchedule.id)
        ).all()
    )


def load_exceptions(session: Session, user_id: int) -> list[ScheduleException]:
    return list(
        session.exec(select(ScheduleException).where(ScheduleException.user_id == user_id)).all()
    )


def load_plans(session: Session, user_id: int) -> list[DosePlan]:
    return list(
        session.exec(
            select(DosePlan)
            .where(DosePlan.user_id == user_id)
            .order_by(DosePlan.start_on, DosePlan.id)
        ).all()
    )


def plan_on(plans: Sequence[DosePlan], day: date) -> DosePlan | None:
    """The plan covering `day`, if any. Plans do not overlap."""
    return next((plan for plan in plans if plan.start_on <= day <= plan.end_on), None)


def frequency_overridden(plans: Sequence[DosePlan], day: date) -> bool:
    """Whether a plan replaces the routine's rhythm on `day`."""
    plan = plan_on(plans, day)
    return plan is not None and has_frequency(plan)


def active_schedule(schedules: list[DoseSchedule], day: date) -> DoseSchedule | None:
    """The series in force on `day`: the latest one already started, else the
    earliest one still to start."""
    started = [s for s in schedules if s.start_on <= day]
    if started:
        return max(started, key=lambda s: (s.start_on, s.id or 0))
    return min(schedules, key=lambda s: (s.start_on, s.id or 0)) if schedules else None


def dose_vials_on(schedules: list[DoseSchedule], plans: Sequence[DosePlan], day: date) -> int:
    """How big a prophylaxis dose is on `day`: a plan's size if one covering
    the day sets it, else the series in force. Zero with no schedule at all —
    the dose is then logged but cannot be sized, and counting nothing is the
    honest reading rather than inventing an amount."""
    plan = plan_on(plans, day)
    if plan is not None and plan.vials:
        return plan.vials
    schedule = active_schedule(schedules, day)
    return schedule.vials if schedule else 0


def occurrences(
    schedules: list[DoseSchedule],
    exceptions: list[ScheduleException],
    plans: Sequence[DosePlan],
    since: date,
    until: date,
) -> list[Occurrence]:
    """Every planned dose sitting in [since, until], sorted by date.

    A moved dose shows on its new day and not on its cycle day, so a dose moved
    into the window from outside it is included and one moved out is not.

    A plan with its own rhythm replaces the series' doses for its dates — a
    cycle day inside it is not planned, moved or not — and puts its own there
    instead, counted from the plan's first day. A plan with only a dose size
    leaves the days alone and resizes them.
    """
    overrides = [plan for plan in plans if has_frequency(plan)]

    def replaced(day: date) -> bool:
        return any(plan.start_on <= day <= plan.end_on for plan in overrides)

    def sized(schedule: DoseSchedule, on: date) -> int:
        plan = plan_on(plans, on)
        return plan.vials if plan is not None and plan.vials else schedule.vials

    moves = {(e.schedule_id, e.original_on): e.moved_to for e in exceptions}
    by_id = {s.id: s for s in schedules}
    seen: set[tuple[int, date]] = set()
    found: list[Occurrence] = []
    for schedule in schedules:
        assert schedule.id is not None
        for tick in _ticks(schedule, since, until):
            seen.add((schedule.id, tick))
            if replaced(tick):
                continue
            on = moves.get((schedule.id, tick), tick)
            if since <= on <= until:
                found.append(
                    Occurrence(
                        on=on,
                        original_on=tick,
                        vials=sized(schedule, on),
                        schedule_id=schedule.id,
                    )
                )
    for exception in exceptions:
        schedule = by_id.get(exception.schedule_id)
        if schedule is None or (schedule.id, exception.original_on) in seen:
            continue
        if replaced(exception.original_on) or not is_tick(schedule, exception.original_on):
            continue
        if since <= exception.moved_to <= until:
            found.append(
                Occurrence(
                    on=exception.moved_to,
                    original_on=exception.original_on,
                    vials=sized(schedule, exception.moved_to),
                    schedule_id=schedule.id,
                )
            )
    for plan in overrides:
        assert plan.id is not None
        for tick in _ticks(plan, since, min(until, plan.end_on)):
            found.append(
                Occurrence(
                    on=tick,
                    original_on=tick,
                    vials=plan.vials or dose_vials_on(schedules, (), tick),
                    plan_id=plan.id,
                )
            )
    found.sort(key=lambda o: (o.on, o.schedule_id or 0, o.plan_id or 0, o.original_on))
    return found


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def ledger(session: Session, user_id: int, as_of: date) -> list[TrackingEvent]:
    """Stored events up to `as_of`, oldest first. The id tiebreak keeps the fold
    deterministic for events on the same day."""
    return list(
        session.exec(
            select(TrackingEvent)
            .where(TrackingEvent.user_id == user_id, TrackingEvent.occurred_on <= as_of)
            .order_by(TrackingEvent.occurred_on, TrackingEvent.id)
        ).all()
    )


def event_vials(event: TrackingEvent, dose_vials: int) -> int:
    """Vials this event moves in or out of the cupboard.

    A dose whose amount is still unknown counts as zero rather than guessing,
    and a missed dose moves nothing — the dose itself is a separate `makeup`
    event on the day it was actually taken.
    """
    if event.kind == EventKind.refill.value:
        if not event.vials:
            # Not `event.vials or 0`: coercion would make a malformed row
            # invisible instead of countable.
            logger.warning("refill event %s has no vials; skipped", event.id)
            return 0
        return event.vials
    if event.kind == EventKind.prophylaxis.value:
        return -dose_vials
    if event.kind in {EventKind.on_demand.value, EventKind.follow_up.value}:
        if not event.vials:
            logger.warning("%s event %s has no vials; skipped", event.kind, event.id)
            return 0
        return -event.vials
    if event.kind == EventKind.makeup.value:
        if event.amount_source == AmountSource.custom.value:
            return -(event.amount_vials or 0)
        if event.amount_source == AmountSource.routine.value:
            return -dose_vials
        return 0
    return 0


def applied_vials(
    events: list[TrackingEvent], schedules: list[DoseSchedule], plans: Sequence[DosePlan]
) -> dict[int, int]:
    """What the fold charges for each event, keyed by id — for the read models."""
    return {
        event.id: event_vials(event, dose_vials_on(schedules, plans, event.occurred_on))
        for event in events
        if event.id is not None
    }


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------


def build_supply(
    session: Session,
    user_id: int,
    buffer_days: float | None = None,
    as_of: date | None = None,
) -> SupplySnapshot:
    """Fold the ledger into a vial count, then walk the schedule forward from it."""
    as_of = as_of or today_sgt()
    events = ledger(session, user_id, as_of)
    schedules = load_schedules(session, user_id)
    exceptions = load_exceptions(session, user_id)
    plans = load_plans(session, user_id)
    snapshot = SupplySnapshot(
        as_of=as_of, events=events, schedule=active_schedule(schedules, as_of)
    )

    total = 0
    for event in events:
        moved = event_vials(event, dose_vials_on(schedules, plans, event.occurred_on))
        if event.id is not None:
            snapshot.applied[event.id] = moved
        total += moved
        if total < 0:
            # Applied per event rather than once at the end, so a shortfall a
            # later refill covers is still recorded as a shortfall.
            snapshot.unaccounted_vials += -total
            total = 0
        if event.kind in DOSE_KINDS:
            snapshot.last_dose_on = event.occurred_on
        if event.kind == EventKind.on_demand.value:
            snapshot.last_bleed_on = event.occurred_on
    snapshot.vials_on_hand = total

    # Anything that puts doses on the calendar: the series, and any plan with
    # a rhythm of its own. A plan that only resizes doses plans nothing alone.
    rules: list[Recurrence] = [*schedules, *(plan for plan in plans if has_frequency(plan))]
    if not rules:
        return snapshot

    settled = {event.occurred_on for event in events if event.kind in RESOLVING_KINDS}

    # The next dose: the first planned one after the last logged dose that
    # nothing has settled. Searching from the last dose rather than from the
    # series start means someone who began logging late is not nagged about
    # every dose before that; searching past `as_of` means an unlogged dose
    # stays "next" — and reads as overdue — until it is logged, moved or
    # recorded as missed.
    since = (
        snapshot.last_dose_on + timedelta(days=1)
        if snapshot.last_dose_on
        else min(rule.start_on for rule in rules)
    )
    reach = max(as_of, since) + timedelta(days=2 * max(cycle_days(rule) for rule in rules))
    for occurrence in occurrences(schedules, exceptions, plans, since, reach):
        if occurrence.on not in settled:
            snapshot.next_dose = occurrence
            break

    # The forecast: planned doses from today against what is in the cupboard.
    # Today's dose, if already logged, was charged by the fold above.
    stock = total
    for occurrence in occurrences(
        schedules, exceptions, plans, as_of, as_of + timedelta(days=FORECAST_DAYS)
    ):
        if occurrence.on in settled:
            continue
        if stock < occurrence.vials:
            snapshot.runs_out_on = occurrence.on
            break
        stock -= occurrence.vials
    if snapshot.runs_out_on is None:
        snapshot.days_cover = FORECAST_DAYS
        return snapshot
    snapshot.days_cover = (snapshot.runs_out_on - as_of).days

    buffer = math.ceil(buffer_days or 0)
    by_on = snapshot.runs_out_on - timedelta(days=buffer)
    covered_until = snapshot.runs_out_on + timedelta(days=ORDER_COVERS_DAYS + buffer)
    planned = [
        occurrence
        for occurrence in occurrences(
            schedules, exceptions, plans, snapshot.runs_out_on, covered_until
        )
        if occurrence.on not in settled
    ]
    needed = sum(occurrence.vials for occurrence in planned)
    snapshot.order = OrderAdvice(
        by_on=by_on,
        vials=max(0, needed - stock),
        due=by_on <= as_of,
        covers_until=covered_until,
        planned_doses=len(planned),
        planned_vials=needed,
        leftover_vials=stock,
        buffer_days=buffer,
    )
    return snapshot


def stock_state(vials: int) -> StockState:
    """How full the shelf looks. Thresholds, not a forecast — the forecast is
    `days_cover`, and it needs a schedule to exist."""
    if vials >= STOCK_WELL_STOCKED:
        return StockState.well_stocked
    if vials >= STOCK_MODERATE:
        return StockState.moderate
    return StockState.low


def dose_state(snapshot: SupplySnapshot) -> DoseState:
    """How covered the patient is, from the schedule alone.

    With no schedule or no dose on record we cannot say, and the honest-looking
    answer would be `veryLow`. But `veryLow` is an alarm, and alarming someone
    about data they never gave us is noise — so an unknown state reads `low`.
    """
    if snapshot.schedule is None or snapshot.last_dose_on is None:
        return DoseState.low
    elapsed = (snapshot.as_of - snapshot.last_dose_on).days
    fraction = elapsed / expected_gap_days(snapshot.schedule)
    if fraction <= COVERED_FRACTION:
        return DoseState.covered
    if fraction <= 1.0:
        return DoseState.low
    return DoseState.very_low
