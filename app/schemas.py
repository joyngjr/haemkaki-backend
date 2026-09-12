"""Request and response models.

The `user` table stores the state columns as plain strings, so this module is
the only thing that constrains them to the values the frontend knows how to
render. Validate here, never in the router.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import DoseState, FactorType, StockState

NAME_MAX = 60
VIALS_MAX = 99
DAYS_COVER_MAX = 365


class DiagnosisType(str, Enum):
    haemophilia_a = "haemophilia_a"
    haemophilia_b = "haemophilia_b"
    factor_xi_deficiency = "factor_xi_deficiency"
    acquired_haemophilia = "acquired_haemophilia"
    symptomatic_carrier_a = "symptomatic_carrier_a"
    symptomatic_carrier_b = "symptomatic_carrier_b"
    other_or_unknown = "other_or_unknown"


class ClinicalProfile(BaseModel):
    """Diagnosis-specific onboarding data, recorded rather than prescribed."""

    diagnosis: DiagnosisType
    sex: str = "prefer_not_to_say"
    age: int | None = Field(default=None, ge=0, le=130)
    has_drug_allergies: bool = False
    drug_allergy_details: str | None = Field(default=None, max_length=1000)
    # This is intentionally the diagnostic/pre-prophylaxis result, not a
    # contemporaneous result that may be normalised by regular treatment.
    diagnosis_factor_activity_percent: float | None = Field(default=None, ge=0, le=150)
    diagnosis_test_date: str | None = Field(default=None, max_length=32)
    congenital_severity: str | None = Field(default=None, max_length=32)
    factor_xi_deficiency_level: str | None = Field(default=None, max_length=32)
    acquired_inhibitor_titre_bu_ml: float | None = Field(default=None, ge=0, le=10000)
    acquired_bleeding_severity: str | None = Field(default=None, max_length=32)
    inhibitor_status: str | None = Field(default=None, max_length=32)
    fix_allergy_or_anaphylaxis: str | None = Field(default=None, max_length=32)
    treatment_approach: str | None = Field(default=None, max_length=64)
    prophylactic_medication: dict[str, str] | None = None
    minimum_buffer: str | None = Field(default=None, max_length=64)
    on_demand_medication: dict[str, str] | None = None
    other_medication: dict[str, str] | None = None
    group_chats: list[str] = []
    medication_reminders: bool = False


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
    clinical_profile: ClinicalProfile | None = None

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
    clinical_profile: ClinicalProfile | None = None

    _clean_name = field_validator("name")(_clean_name)


class ProfileRead(ProfileBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
