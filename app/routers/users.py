"""Profile CRUD.

No authentication by design: this is a household device where anyone can switch
to anyone. Adding auth later means putting it in front of this router, not
reshaping it.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app import services
from app.db import get_session
from app.models import (
    DosePlan,
    DoseSchedule,
    ScheduleException,
    SupplyItem,
    TrackingEvent,
    User,
    utcnow,
)
from app.schemas import ClinicalProfile, ProfileCreate, ProfileRead, ProfileUpdate

router = APIRouter(prefix="/users", tags=["users"])

EMPTY_CLINICAL_PROFILE = "{}"


def _clinical_json(profile: ClinicalProfile | None) -> str:
    """Storage form of the clinical profile. Absent is stored as an empty object."""
    return profile.model_dump_json() if profile else EMPTY_CLINICAL_PROFILE


def _clinical_profile(user: User) -> ClinicalProfile | None:
    """Re-validate the stored JSON rather than passing it straight through.

    A row written before a field existed then comes back with that field's
    default instead of missing the key. The frontend reads medication keys
    unconditionally, so a missing one is a crash there, not a blank.
    """
    stored = json.loads(user.clinical_profile_json)
    return ClinicalProfile.model_validate(stored) if stored else None


def _profile_response(session: Session, user: User) -> dict:
    """Expose the JSON column as a typed API field, with the four state fields
    folded from the event ledger rather than read back from their columns.

    `vials_on_hand`, `days_cover`, `dose_state` and `stock_state` are derived
    values that happen to have columns, left over from when the client set
    them. Folding them here means Home's scene is right without the frontend
    changing a line. The columns stay writable so an older client does not
    break, but the ledger wins on read.

    This is one fold per profile per request. At household scale — a handful of
    profiles, hundreds of events — that is the price of not storing a balance.
    """
    clinical_profile = _clinical_profile(user)
    buffer_days = clinical_profile.minimum_buffer_days if clinical_profile else None
    supply = services.build_supply(session, user.id, buffer_days)
    return {
        "id": user.id,
        "name": user.name,
        "factor_type": user.factor_type,
        "dose_state": services.dose_state(supply),
        "stock_state": services.stock_state(supply.vials_on_hand),
        "vials_on_hand": supply.vials_on_hand,
        "days_cover": supply.days_cover,
        "clinical_profile": clinical_profile.model_dump(mode="json") if clinical_profile else None,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


def _get_or_404(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile not found")
    return user


@router.get("", response_model=list[ProfileRead])
def list_users(session: Session = Depends(get_session)) -> list[dict]:
    """Everyone on this device, oldest first, so the switcher order is stable."""
    users = session.exec(select(User).order_by(User.created_at, User.id)).all()
    return [_profile_response(session, user) for user in users]


@router.post("", response_model=ProfileRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: ProfileCreate, session: Session = Depends(get_session)) -> dict:
    data = payload.model_dump(mode="json", exclude={"clinical_profile"})
    data["clinical_profile_json"] = _clinical_json(payload.clinical_profile)
    user = User(**data)
    session.add(user)
    session.commit()
    session.refresh(user)
    return _profile_response(session, user)


@router.get("/{user_id}", response_model=ProfileRead)
def get_user(user_id: int, session: Session = Depends(get_session)) -> dict:
    return _profile_response(session, _get_or_404(session, user_id))


@router.patch("/{user_id}", response_model=ProfileRead)
def update_user(
    user_id: int, payload: ProfileUpdate, session: Session = Depends(get_session)
) -> dict:
    user = _get_or_404(session, user_id)
    changes = payload.model_dump(mode="json", exclude_unset=True, exclude={"clinical_profile"})
    for field, value in changes.items():
        setattr(user, field, value)
    # `exclude_unset` cannot tell "absent" from an explicit null, and only an
    # explicit null should clear the profile.
    if "clinical_profile" in payload.model_fields_set:
        user.clinical_profile_json = _clinical_json(payload.clinical_profile)
    user.updated_at = utcnow()
    session.add(user)
    session.commit()
    session.refresh(user)
    return _profile_response(session, user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, session: Session = Depends(get_session)) -> None:
    user = _get_or_404(session, user_id)
    # No ON DELETE CASCADE: SQLite ignores it unless foreign keys are switched
    # on per connection, so the children are removed here where it is certain.
    for table in (ScheduleException, DoseSchedule, DosePlan, TrackingEvent, SupplyItem):
        for row in session.exec(select(table).where(table.user_id == user_id)).all():
            session.delete(row)
    session.delete(user)
    session.commit()
