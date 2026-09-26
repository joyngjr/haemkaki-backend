"""Plans — temporary changes to the routine over a date range.

"Plan Ahead" on the tracker: a trip, an illness, a procedure. A plan carries
the exact days a dose is due, a different dose size, or both, for its dates
only; the fold in `app/services.py` applies it when it walks the calendar, so
the planned doses, the run-out date and the order advice all follow it without
anything being copied into the series.

Plans do not overlap — two changes for one day would have to be reconciled by
something, and nothing here is that something. A plan that only resizes doses
is allowed without a routine (it simply plans nothing until one exists).

No authentication, for the same reason as `users.py`.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app import services
from app.db import get_session
from app.models import DosePlan, format_dates, utcnow
from app.routers.users import _get_or_404
from app.schemas import PlanCreate, PlanRead, plan_read

router = APIRouter(prefix="/users/{user_id}/plans", tags=["plans"])


def _plan_or_404(session: Session, user_id: int, plan_id: int) -> DosePlan:
    plan = session.get(DosePlan, plan_id)
    if plan is None or plan.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    return plan


def _reject_overlap(
    session: Session, user_id: int, payload: PlanCreate, ignore_id: int | None = None
) -> None:
    for other in services.load_plans(session, user_id):
        if other.id == ignore_id:
            continue
        if other.start_on <= payload.end_on and payload.start_on <= other.end_on:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Those dates overlap the plan from {other.start_on} to {other.end_on}",
            )


def _apply(plan: DosePlan, payload: PlanCreate) -> DosePlan:
    plan.start_on = payload.start_on
    plan.end_on = payload.end_on
    plan.dose_dates = format_dates(payload.dose_dates)
    plan.vials = payload.vials
    return plan


@router.get("", response_model=list[PlanRead])
def list_plans(user_id: int, session: Session = Depends(get_session)) -> list[PlanRead]:
    """Every plan, earliest first — past ones included, so the card can say "Ended"."""
    _get_or_404(session, user_id)
    return [plan_read(plan) for plan in services.load_plans(session, user_id)]


@router.post("", response_model=PlanRead, status_code=status.HTTP_201_CREATED)
def create_plan(
    user_id: int, payload: PlanCreate, session: Session = Depends(get_session)
) -> PlanRead:
    _get_or_404(session, user_id)
    _reject_overlap(session, user_id, payload)
    plan = _apply(DosePlan(user_id=user_id), payload)
    session.add(plan)
    session.commit()
    session.refresh(plan)
    return plan_read(plan)


@router.put("/{plan_id}", response_model=PlanRead)
def replace_plan(
    user_id: int, plan_id: int, payload: PlanCreate, session: Session = Depends(get_session)
) -> PlanRead:
    """Whole-plan replacement, like an event: editing a plan re-answers all of it."""
    plan = _plan_or_404(session, user_id, plan_id)
    _reject_overlap(session, user_id, payload, ignore_id=plan_id)
    _apply(plan, payload)
    plan.updated_at = utcnow()
    session.add(plan)
    session.commit()
    session.refresh(plan)
    return plan_read(plan)


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(user_id: int, plan_id: int, session: Session = Depends(get_session)) -> None:
    """Doses logged while the plan ran are events and stay."""
    session.delete(_plan_or_404(session, user_id, plan_id))
    session.commit()
