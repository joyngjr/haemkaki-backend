"""Request and response models.

The `user` table stores the state columns as plain strings, so this module is
the only thing that constrains them to the values the frontend knows how to
render. Validate here, never in the router.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import DoseState, FactorType, StockState

NAME_MAX = 60
VIALS_MAX = 99
DAYS_COVER_MAX = 365


def _clean_name(v: str | None) -> str | None:
    if v is None:
        return None
    stripped = v.strip()
    if not stripped:
        raise ValueError("name must not be blank")
    return stripped


class ProfileBase(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    factor_type: FactorType
    dose_state: DoseState = DoseState.covered
    stock_state: StockState = StockState.well_stocked
    vials_on_hand: int = Field(default=0, ge=0, le=VIALS_MAX)
    days_cover: int = Field(default=0, ge=0, le=DAYS_COVER_MAX)

    _clean_name = field_validator("name")(_clean_name)


class ProfileCreate(ProfileBase):
    pass


class ProfileUpdate(BaseModel):
    """Every field optional — PATCH only touches what it names."""

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)
    factor_type: FactorType | None = None
    dose_state: DoseState | None = None
    stock_state: StockState | None = None
    vials_on_hand: int | None = Field(default=None, ge=0, le=VIALS_MAX)
    days_cover: int | None = Field(default=None, ge=0, le=DAYS_COVER_MAX)

    _clean_name = field_validator("name")(_clean_name)


class ProfileRead(ProfileBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
