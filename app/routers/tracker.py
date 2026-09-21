"""The tracker's saved data for one profile: the routine and the entries.

No authentication, like the rest of the API. Entries are written a whole day at
a time — the frontend already holds the day's list and replaces it — which keeps
the make-up and missed-dose links in one request instead of several that could
land half-applied.
"""

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, col, select

from app.db import get_session
from app.models import (
    TrackerEntry,
    TrackerInventory,
    TrackerPlan,
    TrackerRoutine,
    TrackerShift,
    User,
    utcnow,
)
from app.tracker_schemas import (
    ENTRIES_PER_DAY_MAX,
    PLANS_MAX,
    DaysFrequency,
    EntryRead,
    EntryWrite,
    Frequency,
    InventoryState,
    PlanWrite,
    RoutineRead,
    RoutineUpdate,
    ShiftState,
    WeekFrequency,
)

router = APIRouter(prefix="/users/{user_id}", tags=["tracker"])


def _require_user(session: Session, user_id: int) -> None:
    if session.get(User, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile not found")


def _entry_read(row: TrackerEntry) -> EntryRead:
    return EntryRead(
        id=row.entry_id,
        day=row.day,
        kind=row.kind,
        vials=row.vials,
        bleed_nature=row.bleed_nature,
        missed_status=row.missed_status,
        taken_date=row.taken_date,
        missed_date=row.missed_date,
        amount_source=row.amount_source,
        amount_vials=row.amount_vials,
    )


@router.get("/entries", response_model=list[EntryRead])
def list_entries(
    user_id: int,
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    session: Session = Depends(get_session),
) -> list[EntryRead]:
    """Entries oldest day first, optionally limited to a date range (inclusive)."""
    _require_user(session, user_id)
    query = select(TrackerEntry).where(TrackerEntry.user_id == user_id)
    if date_from is not None:
        query = query.where(TrackerEntry.day >= date_from)
    if date_to is not None:
        query = query.where(TrackerEntry.day <= date_to)
    rows = session.exec(query.order_by(TrackerEntry.day, col(TrackerEntry.id))).all()
    return [_entry_read(row) for row in rows]


@router.put("/entries/{day}", response_model=list[EntryRead])
def replace_day(
    user_id: int,
    day: date,
    entries: list[EntryWrite],
    session: Session = Depends(get_session),
) -> list[EntryRead]:
    """Make `day` hold exactly these entries. An empty list clears the day."""
    _require_user(session, user_id)
    if len(entries) > ENTRIES_PER_DAY_MAX:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Too many entries for one day")
    if len({entry.id for entry in entries}) != len(entries):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Entry ids must be unique")

    existing = session.exec(
        select(TrackerEntry).where(TrackerEntry.user_id == user_id, TrackerEntry.day == day)
    ).all()
    for row in existing:
        session.delete(row)
    saved = [
        TrackerEntry(
            user_id=user_id,
            entry_id=entry.id,
            day=day,
            kind=entry.kind.value,
            vials=entry.vials,
            bleed_nature=entry.bleed_nature.value if entry.bleed_nature else None,
            missed_status=entry.missed_status.value if entry.missed_status else None,
            taken_date=entry.taken_date,
            missed_date=entry.missed_date,
            amount_source=entry.amount_source.value if entry.amount_source else None,
            amount_vials=entry.amount_vials,
        )
        for entry in entries
    ]
    session.add_all(saved)
    session.commit()
    for row in saved:
        session.refresh(row)
    return [_entry_read(row) for row in saved]


def _frequency_from_columns(unit: str | None, days: int | None, weekdays: str) -> Frequency | None:
    if unit == "days" and days:
        return DaysFrequency(unit="days", days=days)
    if unit == "week" and weekdays:
        return WeekFrequency(unit="week", weekdays=[int(day) for day in weekdays.split(",")])
    return None


def _frequency_to_columns(frequency: Frequency | None) -> dict:
    return {
        "frequency_unit": frequency.unit if frequency else None,
        "frequency_days": frequency.days if isinstance(frequency, DaysFrequency) else None,
        "frequency_weekdays": (
            ",".join(str(day) for day in frequency.weekdays)
            if isinstance(frequency, WeekFrequency)
            else ""
        ),
    }


def _routine_read(row: TrackerRoutine | None) -> RoutineRead:
    if row is None:
        return RoutineRead()
    frequency = _frequency_from_columns(
        row.frequency_unit, row.frequency_days, row.frequency_weekdays
    )
    return RoutineRead(vials=row.vials, frequency=frequency, start_date=row.start_date)


@router.get("/routine", response_model=RoutineRead)
def get_routine(user_id: int, session: Session = Depends(get_session)) -> RoutineRead:
    """The usual routine, or all-null if none has been set yet."""
    _require_user(session, user_id)
    return _routine_read(session.get(TrackerRoutine, user_id))


@router.patch("/routine", response_model=RoutineRead)
def update_routine(
    user_id: int, payload: RoutineUpdate, session: Session = Depends(get_session)
) -> RoutineRead:
    """Change any subset of the routine, creating it on first use."""
    _require_user(session, user_id)
    row = session.get(TrackerRoutine, user_id) or TrackerRoutine(user_id=user_id)
    changed = payload.model_fields_set
    if "vials" in changed:
        row.vials = payload.vials
    if "start_date" in changed:
        row.start_date = payload.start_date
    if "frequency" in changed:
        for column, value in _frequency_to_columns(payload.frequency).items():
            setattr(row, column, value)
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return _routine_read(row)


@router.get("/shift", response_model=ShiftState)
def get_shift(user_id: int, session: Session = Depends(get_session)) -> ShiftState:
    """The saved answers to "shift future doses?", or all-null if there are none."""
    _require_user(session, user_id)
    row = session.get(TrackerShift, user_id)
    if row is None:
        return ShiftState()
    return ShiftState(
        anchor_id=row.anchor_id, handled_id=row.handled_id, weekday_offset=row.weekday_offset
    )


@router.put("/shift", response_model=ShiftState)
def put_shift(
    user_id: int, payload: ShiftState, session: Session = Depends(get_session)
) -> ShiftState:
    _require_user(session, user_id)
    row = session.get(TrackerShift, user_id) or TrackerShift(user_id=user_id)
    row.anchor_id = payload.anchor_id
    row.handled_id = payload.handled_id
    row.weekday_offset = payload.weekday_offset
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    return payload


def _plan_read(row: TrackerPlan) -> PlanWrite:
    return PlanWrite(
        id=row.plan_id,
        start_date=row.start_date,
        end_date=row.end_date,
        frequency=_frequency_from_columns(
            row.frequency_unit, row.frequency_days, row.frequency_weekdays
        ),
        vials=row.vials,
    )


@router.get("/plans", response_model=list[PlanWrite])
def list_plans(user_id: int, session: Session = Depends(get_session)) -> list[PlanWrite]:
    _require_user(session, user_id)
    rows = session.exec(
        select(TrackerPlan)
        .where(TrackerPlan.user_id == user_id)
        .order_by(TrackerPlan.start_date, col(TrackerPlan.id))
    ).all()
    return [_plan_read(row) for row in rows]


@router.put("/plans", response_model=list[PlanWrite])
def replace_plans(
    user_id: int, plans: list[PlanWrite], session: Session = Depends(get_session)
) -> list[PlanWrite]:
    """Make the profile hold exactly these plans. The list is short, so it is sent whole."""
    _require_user(session, user_id)
    if len(plans) > PLANS_MAX:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Too many plans")
    if len({plan.id for plan in plans}) != len(plans):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Plan ids must be unique")
    for row in session.exec(select(TrackerPlan).where(TrackerPlan.user_id == user_id)).all():
        session.delete(row)
    session.add_all(
        TrackerPlan(
            user_id=user_id,
            plan_id=plan.id,
            start_date=plan.start_date,
            end_date=plan.end_date,
            vials=plan.vials,
            **_frequency_to_columns(plan.frequency),
        )
        for plan in plans
    )
    session.commit()
    return plans


@router.get("/inventory", response_model=InventoryState | None)
def get_inventory(user_id: int, session: Session = Depends(get_session)) -> InventoryState | None:
    """The Inventory card's saved state, or null if it has never been changed."""
    _require_user(session, user_id)
    row = session.get(TrackerInventory, user_id)
    if row is None:
        return None
    return InventoryState(visible=row.visible, items=json.loads(row.items_json))


@router.put("/inventory", response_model=InventoryState)
def put_inventory(
    user_id: int, payload: InventoryState, session: Session = Depends(get_session)
) -> InventoryState:
    _require_user(session, user_id)
    if len({item.id for item in payload.items}) != len(payload.items):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Item ids must be unique")
    row = session.get(TrackerInventory, user_id) or TrackerInventory(user_id=user_id)
    row.visible = payload.visible
    row.items_json = json.dumps([item.model_dump() for item in payload.items])
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    return payload
