"""Dose schedules — the prophylaxis routine as a calendar's recurring event.

A series is created and deleted whole. Moving one dose is an exception keyed
by the date the cycle put it on; a permanent shift is a new series in place of
the old one (`replace`). Nothing here is a record of a dose taken — that is
the event ledger — so deleting a series leaves the history intact.

No authentication, for the same reason as `users.py`.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app import services
from app.db import get_session
from app.models import DoseSchedule, ScheduleException, format_weekdays, utcnow
from app.routers.users import _get_or_404
from app.schemas import (
    MoveOccurrence,
    OccurrenceRead,
    ScheduleCreate,
    ScheduleRead,
    occurrence_read,
    schedule_read,
)

router = APIRouter(prefix="/users/{user_id}/schedules", tags=["schedules"])

#: The calendar asks a month or two at a time; a year is plenty.
WINDOW_MAX_DAYS = 366


def _schedule_or_404(session: Session, user_id: int, schedule_id: int) -> DoseSchedule:
    schedule = session.get(DoseSchedule, schedule_id)
    if schedule is None or schedule.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    return schedule


def _delete_schedule(session: Session, schedule: DoseSchedule) -> None:
    for exception in session.exec(
        select(ScheduleException).where(ScheduleException.schedule_id == schedule.id)
    ).all():
        session.delete(exception)
    session.delete(schedule)


@router.get("", response_model=list[ScheduleRead])
def list_schedules(user_id: int, session: Session = Depends(get_session)) -> list[ScheduleRead]:
    """Every series, earliest start first. The tracker keeps one; the API allows more."""
    _get_or_404(session, user_id)
    return [schedule_read(schedule) for schedule in services.load_schedules(session, user_id)]


# Declared before the `/{schedule_id}` routes so "occurrences" is never read as an id.
@router.get("/occurrences", response_model=list[OccurrenceRead])
def list_occurrences(
    user_id: int,
    since: date = Query(..., description="First day of the window, inclusive."),
    until: date = Query(..., description="Last day of the window, inclusive."),
    session: Session = Depends(get_session),
) -> list[OccurrenceRead]:
    """The planned doses in a window, moved ones on their new day, a plan's
    dates following the plan."""
    _get_or_404(session, user_id)
    if until < since or (until - since).days > WINDOW_MAX_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"since..until must run forwards and span at most {WINDOW_MAX_DAYS} days",
        )
    found = services.occurrences(
        services.load_schedules(session, user_id),
        services.load_exceptions(session, user_id),
        services.load_plans(session, user_id),
        since,
        until,
    )
    return [occurrence_read(occurrence) for occurrence in found]


def replace_series(session: Session, user_id: int, payload: ScheduleCreate) -> DoseSchedule:
    """Create a series, first deleting every other one when `payload.replace`
    asks — in the same transaction, so there is no window with no routine.
    Shared with the MCP `set_routine` tool."""
    if payload.replace:
        for existing in services.load_schedules(session, user_id):
            _delete_schedule(session, existing)
    schedule = DoseSchedule(
        user_id=user_id,
        start_on=payload.start_on,
        interval_days=payload.interval_days,
        weekdays=format_weekdays(payload.weekdays),
        vials=payload.vials,
    )
    session.add(schedule)
    session.commit()
    session.refresh(schedule)
    return schedule


@router.post("", response_model=ScheduleRead, status_code=status.HTTP_201_CREATED)
def create_schedule(
    user_id: int, payload: ScheduleCreate, session: Session = Depends(get_session)
) -> ScheduleRead:
    _get_or_404(session, user_id)
    return schedule_read(replace_series(session, user_id, payload))


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(
    user_id: int, schedule_id: int, session: Session = Depends(get_session)
) -> None:
    """Removes the series and its moved occurrences. Logged doses are events and stay."""
    _delete_schedule(session, _schedule_or_404(session, user_id, schedule_id))
    session.commit()


@router.put("/{schedule_id}/exceptions/{original_on}", response_model=OccurrenceRead)
def move_occurrence(
    user_id: int,
    schedule_id: int,
    original_on: date,
    payload: MoveOccurrence,
    session: Session = Depends(get_session),
) -> OccurrenceRead:
    """Move the dose the cycle put on `original_on` to another day.

    Idempotent per occurrence: moving it again replaces the earlier move. The
    day it moves to must be free of other planned doses, or two would share a
    calendar cell and the tracker's one-use-per-day rule could not tell them
    apart.
    """
    schedule = _schedule_or_404(session, user_id, schedule_id)
    plans = services.load_plans(session, user_id)
    # A cycle day inside a plan with its own rhythm is not a planned dose: the
    # plan's doses stand in for it, and they follow the plan rather than moving.
    if not services.is_tick(schedule, original_on) or services.frequency_overridden(
        plans, original_on
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No dose is planned on that day")
    if payload.moved_to == original_on:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "That dose is already on that day"
        )
    schedules = services.load_schedules(session, user_id)
    exceptions = services.load_exceptions(session, user_id)
    for other in services.occurrences(
        schedules, exceptions, plans, payload.moved_to, payload.moved_to
    ):
        if (other.schedule_id, other.original_on) != (schedule_id, original_on):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "A dose is already planned on that day"
            )
    existing = next(
        (e for e in exceptions if e.schedule_id == schedule_id and e.original_on == original_on),
        None,
    )
    if existing is None:
        existing = ScheduleException(
            user_id=user_id,
            schedule_id=schedule_id,
            original_on=original_on,
            moved_to=payload.moved_to,
        )
    else:
        existing.moved_to = payload.moved_to
        existing.updated_at = utcnow()
    session.add(existing)
    session.commit()
    return OccurrenceRead(
        on=payload.moved_to,
        original_on=original_on,
        schedule_id=schedule_id,
        plan_id=None,
        vials=services.dose_vials_on(schedules, plans, payload.moved_to) or schedule.vials,
        moved=True,
    )


@router.delete("/{schedule_id}/exceptions/{original_on}", status_code=status.HTTP_204_NO_CONTENT)
def restore_occurrence(
    user_id: int, schedule_id: int, original_on: date, session: Session = Depends(get_session)
) -> None:
    """Put a moved dose back on its cycle day."""
    _schedule_or_404(session, user_id, schedule_id)
    exception = session.exec(
        select(ScheduleException).where(
            ScheduleException.schedule_id == schedule_id,
            ScheduleException.original_on == original_on,
        )
    ).first()
    if exception is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That dose has not been moved")
    session.delete(exception)
    session.commit()
