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
from app.schemas import ProfileCreate, ProfileRead, ProfileUpdate

router = APIRouter(prefix="/users", tags=["users"])


def _profile_response(user: User) -> dict:
    """Expose the JSON column as a typed API field without leaking storage."""
    response = user.model_dump()
    clinical_profile = json.loads(user.clinical_profile_json)
    response["clinical_profile"] = clinical_profile or None
    return response


def _get_or_404(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Profile not found")
    return user


@router.get("", response_model=list[ProfileRead])
def list_users(session: Session = Depends(get_session)) -> list[dict]:
    """Everyone on this device, oldest first, so the switcher order is stable."""
    return [_profile_response(user) for user in session.exec(select(User).order_by(User.created_at, User.id)).all()]


@router.post("", response_model=ProfileRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: ProfileCreate, session: Session = Depends(get_session)) -> dict:
    data = payload.model_dump(mode="json", exclude={"clinical_profile"})
    data["clinical_profile_json"] = payload.clinical_profile.model_dump_json() if payload.clinical_profile else "{}"
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
    if "clinical_profile" in payload.model_fields_set:
        user.clinical_profile_json = (
            payload.clinical_profile.model_dump_json() if payload.clinical_profile else "{}"
        )
    user.updated_at = utcnow()
    session.add(user)
    session.commit()
    session.refresh(user)
    return _profile_response(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, session: Session = Depends(get_session)) -> None:
    session.delete(_get_or_404(session, user_id))
    session.commit()
