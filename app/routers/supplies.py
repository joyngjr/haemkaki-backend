"""Non-factor supplies — the tracker's Inventory card.

A checklist rather than a ledger. Gauze, syringes and saline are counted, not
folded, so the row is the balance, and the whole list is replaced on every
edit: at a handful of rows per profile that is simpler than per-item ids the
card would have to invent before the server answered.

No authentication, for the same reason as `users.py`.
"""

from typing import Annotated

from fastapi import APIRouter, Body, Depends
from sqlmodel import Session, select

from app.db import get_session
from app.models import SupplyItem
from app.routers.users import _get_or_404
from app.schemas import SUPPLIES_MAX, SupplyItemIn, SupplyItemRead

router = APIRouter(prefix="/users/{user_id}/supplies", tags=["supplies"])


def _supplies(session: Session, user_id: int) -> list[SupplyItem]:
    return list(
        session.exec(
            select(SupplyItem)
            .where(SupplyItem.user_id == user_id)
            .order_by(SupplyItem.position, SupplyItem.id)
        ).all()
    )


@router.get("", response_model=list[SupplyItemRead])
def list_supplies(user_id: int, session: Session = Depends(get_session)) -> list[SupplyItem]:
    """In the order the card shows them."""
    _get_or_404(session, user_id)
    return _supplies(session, user_id)


@router.put("", response_model=list[SupplyItemRead])
def replace_supplies(
    user_id: int,
    items: Annotated[list[SupplyItemIn], Body(max_length=SUPPLIES_MAX)],
    session: Session = Depends(get_session),
) -> list[SupplyItem]:
    """Replace the whole list, in the order given. An empty list clears it."""
    _get_or_404(session, user_id)
    for row in _supplies(session, user_id):
        session.delete(row)
    for position, item in enumerate(items):
        session.add(SupplyItem(user_id=user_id, position=position, **item.model_dump()))
    session.commit()
    return _supplies(session, user_id)
