"""The folded view of one profile: supply, schedule, next dose, order advice.

Everything here is derived in `app/services.py` on every call. The endpoint
exists so Home and the tracker can ask one question instead of fetching the
whole ledger and re-deriving what the fold already derives.
"""

from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app import services
from app.db import get_session
from app.models import User
from app.routers.users import _clinical_profile, _get_or_404
from app.schemas import OrderAdvice, StatusRead, event_read, occurrence_read, schedule_read

router = APIRouter(prefix="/users/{user_id}/status", tags=["status"])

RECENT_EVENTS = 5


def status_read(session: Session, user: User, as_of: date | None = None) -> StatusRead:
    """The fold for one profile as the API reports it.

    Shared with the importer and the MCP tools, which answer a write with the
    balance it produced rather than sending the client back for it.
    """
    clinical_profile = _clinical_profile(user)
    buffer_days = clinical_profile.minimum_buffer_days if clinical_profile else None
    supply = services.build_supply(session, user.id, buffer_days, as_of)
    return StatusRead(
        as_of=supply.as_of,
        vials_on_hand=supply.vials_on_hand,
        unaccounted_vials=supply.unaccounted_vials,
        days_cover=supply.days_cover,
        runs_out_on=supply.runs_out_on,
        last_dose_on=supply.last_dose_on,
        last_bleed_on=supply.last_bleed_on,
        schedule=schedule_read(supply.schedule) if supply.schedule else None,
        next_dose=occurrence_read(supply.next_dose) if supply.next_dose else None,
        # The two OrderAdvice types share their field names on purpose.
        order=OrderAdvice(**asdict(supply.order)) if supply.order else None,
        dose_state=services.dose_state(supply),
        stock_state=services.stock_state(supply.vials_on_hand),
        recent_events=[
            event_read(event, supply.applied.get(event.id or 0, 0))
            for event in reversed(supply.events[-RECENT_EVENTS:])
        ],
    )


@router.get("", response_model=StatusRead)
def read_status(
    user_id: int,
    as_of: date | None = Query(None, description="Defaults to today in Singapore."),
    session: Session = Depends(get_session),
) -> StatusRead:
    return status_read(session, _get_or_404(session, user_id), as_of)
