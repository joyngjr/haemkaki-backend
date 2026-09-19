"""Profile CRUD.

No authentication by design: this is a household device where anyone can switch
to anyone. Adding auth later means putting it in front of this router, not
reshaping it.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.db import get_session
from app.models import User, utcnow
from app.schemas import ClinicalProfile, ProfileCreate, ProfileRead, ProfileUpdate

router = APIRouter(prefix="/users", tags=["users"])

EMPTY_CLINICAL_PROFILE = "{}"


def _clinical_json(profile: ClinicalProfile | None) -> str:
    """Storage form of the clinical profile. Absent is stored as an empty object."""
    return profile.model_dump_json() if profile else EMPTY_CLINICAL_PROFILE


def _profile_response(user: User) -> dict:
    """Expose the JSON column as a typed API field without leaking storage.

    The stored JSON is re-validated rather than passed straight through, so a
    row written before a field existed comes back with that field's default
    instead of missing the key. The frontend reads medication keys
    unconditionally, so a missing one is a crash there, not a blank.
    """
    stored = json.loads(user.clinical_profile_json)
    clinical_profile = (
        ClinicalProfile.model_validate(stored).model_dump(mode="json") if stored else None
    )
    return {
        "id": user.id,
        "name": user.name,
        "factor_type": user.factor_type,
        "dose_state": user.dose_state,
        "stock_state": user.stock_state,
        "vials_on_hand": user.vials_on_hand,
        "days_cover": user.days_cover,
        "clinical_profile": clinical_profile,
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
    return [_profile_response(user) for user in users]


@router.post("", response_model=ProfileRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: ProfileCreate, session: Session = Depends(get_session)) -> dict:
    data = payload.model_dump(mode="json", exclude={"clinical_profile"})
    data["clinical_profile_json"] = _clinical_json(payload.clinical_profile)
    user = User(**data)
    session.add(user)
    session.commit()
    session.refresh(user)
    return _profile_response(user)


@router.get("/{user_id}", response_model=ProfileRead)
def get_user(user_id: int, session: Session = Depends(get_session)) -> dict:
    return _profile_response(_get_or_404(session, user_id))


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
    return _profile_response(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, session: Session = Depends(get_session)) -> None:
    session.delete(_get_or_404(session, user_id))
    session.commit()
