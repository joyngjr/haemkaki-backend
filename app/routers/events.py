"""The tracker's event ledger.

One row per logged action, scoped to a profile. No authentication, for the
same reason as `users.py`: the profile id in the path is the whole identity.

Everything derived from these rows — vials on hand, the last dose, the supply
history — is folded in `app/services.py` on every read. Nothing here stores a
balance, because users backdate and edit constantly and a running total would
apply a backdated event at the end instead of in its place.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app import services
from app.db import get_session
from app.models import TrackingEvent, utcnow
from app.routers.users import _get_or_404
from app.schemas import EventCreate, TrackingEventRead, event_read, to_event

router = APIRouter(prefix="/users/{user_id}/events", tags=["events"])

LIMIT_MAX = 500


def _event_or_404(session: Session, user_id: int, event_id: int) -> TrackingEvent:
    """Scope the lookup to the profile in the path.

    A 404 rather than a 403 for someone else's event: without auth there is no
    "forbidden", and confirming the id exists elsewhere would say more than it
    should.
    """
    event = session.get(TrackingEvent, event_id)
    if event is None or event.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return event


def _read_one(session: Session, event: TrackingEvent) -> TrackingEventRead:
    schedules = services.load_schedules(session, event.user_id)
    plans = services.load_plans(session, event.user_id)
    return event_read(
        event,
        services.event_vials(event, services.dose_vials_on(schedules, plans, event.occurred_on)),
    )


@router.get("", response_model=list[TrackingEventRead])
def list_events(
    user_id: int,
    since: date | None = Query(None, description="Earliest occurred_on to include."),
    until: date | None = Query(None, description="Latest occurred_on to include."),
    limit: int = Query(LIMIT_MAX, ge=1, le=LIMIT_MAX),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> list[TrackingEventRead]:
    """Oldest first, each with the vials the fold charged for it.

    The id tiebreak keeps same-day events in a stable order; without it two
    entries logged on one date can swap places between requests and the
    calendar reshuffles under the user.
    """
    _get_or_404(session, user_id)
    statement = select(TrackingEvent).where(TrackingEvent.user_id == user_id)
    if since is not None:
        statement = statement.where(TrackingEvent.occurred_on >= since)
    if until is not None:
        statement = statement.where(TrackingEvent.occurred_on <= until)
    statement = statement.order_by(TrackingEvent.occurred_on, TrackingEvent.id)
    events = list(session.exec(statement.offset(offset).limit(limit)).all())
    applied = services.applied_vials(
        events, services.load_schedules(session, user_id), services.load_plans(session, user_id)
    )
    return [event_read(event, applied.get(event.id or 0, 0)) for event in events]


@router.post("", response_model=TrackingEventRead, status_code=status.HTTP_201_CREATED)
def create_event(
    user_id: int, payload: EventCreate, session: Session = Depends(get_session)
) -> TrackingEventRead:
    """All writes go through `to_event` — it is the only thing stopping a
    refill from carrying a missed-dose status."""
    _get_or_404(session, user_id)
    event = to_event(payload, user_id)
    session.add(event)
    session.commit()
    session.refresh(event)
    return _read_one(session, event)


@router.put("/{event_id}", response_model=TrackingEventRead)
def replace_event(
    user_id: int, event_id: int, payload: EventCreate, session: Session = Depends(get_session)
) -> TrackingEventRead:
    """Whole-event replacement, not a PATCH.

    A partial update over a discriminated union either drops the discriminator
    or needs a second all-optional model per kind, and the tracker never wants
    one: editing a day replaces its entry outright.
    """
    existing = _event_or_404(session, user_id, event_id)
    replacement = to_event(payload, user_id)
    for field in TrackingEvent.model_fields:
        if field in {"id", "user_id", "created_at", "updated_at"}:
            continue
        setattr(existing, field, getattr(replacement, field))
    existing.updated_at = utcnow()
    session.add(existing)
    session.commit()
    session.refresh(existing)
    return _read_one(session, existing)


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(user_id: int, event_id: int, session: Session = Depends(get_session)) -> None:
    session.delete(_event_or_404(session, user_id, event_id))
    session.commit()
