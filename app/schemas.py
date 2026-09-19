"""Request and response models.

The `user` table stores the state columns as plain strings and the whole
clinical profile as one JSON column, so this module is the only thing that
constrains them to the values the frontend knows how to render. Validate here,
never in the router.

The shapes below mirror `ClinicalProfile` and `MedicationDetails` in the
frontend's `src/lib/api.ts` field for field. Changing a name or a type here is a
breaking contract change, not a refactor.
"""

import json
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import DoseState, FactorType, StockState

NAME_MAX = 60
VIALS_MAX = 99
DAYS_COVER_MAX = 365

# The allergy picker has no selection limit and the catalog holds 187 drugs, so
# the worst honest case is a few thousand characters of joined names.
ALLERGY_DETAILS_MAX = 4000
# Longest real combination is 56 characters; the headroom is for new options.
TREATMENT_APPROACH_MAX = 128
ITEMS_JSON_MAX = 8000


class DiagnosisType(str, Enum):
    haemophilia_a = "haemophilia_a"
    haemophilia_b = "haemophilia_b"
    factor_xi_deficiency = "factor_xi_deficiency"
    acquired_haemophilia = "acquired_haemophilia"
    symptomatic_carrier_a = "symptomatic_carrier_a"
    symptomatic_carrier_b = "symptomatic_carrier_b"
    other_or_unknown = "other_or_unknown"


#: Diagnoses someone is born with — the only ones with a severity band and an
#: inhibitor history.
CONGENITAL_DIAGNOSES = frozenset(
    {
        DiagnosisType.haemophilia_a,
        DiagnosisType.haemophilia_b,
        DiagnosisType.symptomatic_carrier_a,
        DiagnosisType.symptomatic_carrier_b,
    }
)
#: Factor IX diagnoses, where anaphylaxis to FIX is a real and specific risk.
FACTOR_IX_DIAGNOSES = frozenset({DiagnosisType.haemophilia_b, DiagnosisType.symptomatic_carrier_b})


class Sex(str, Enum):
    male = "male"
    female = "female"
    other = "other"
    # The 12 September form offered this and the 19 September one does not.
    # Kept so an older profile still reads back.
    prefer_not_to_say = "prefer_not_to_say"


class CongenitalSeverity(str, Enum):
    severe = "severe"
    moderate = "moderate"
    mild = "mild"
    not_known = "not_known"


class FactorXiDeficiencyLevel(str, Enum):
    severe_deficiency = "severe_deficiency"
    partial_deficiency = "partial_deficiency"
    not_known = "not_known"


class AcquiredBleedingSeverity(str, Enum):
    life_threatening_or_major = "life_threatening_or_major"
    moderate_or_non_life_threatening = "moderate_or_non_life_threatening"
    unknown = "unknown"


class InhibitorStatus(str, Enum):
    current = "current"
    previous = "previous"
    none_known = "none_known"
    unknown = "unknown"


class YesNoUnknown(str, Enum):
    yes = "yes"
    no = "no"
    unknown = "unknown"


class TreatmentApproach(str, Enum):
    factor_replacement = "factor_replacement"
    non_factor_therapy = "non_factor_therapy"
    bypassing_therapy = "bypassing_therapy"
    specialist_plan = "specialist_plan"
    none = "none"


class MedicationItem(BaseModel):
    """One medication as the form records it.

    `dose`, `frequency` and `buffer_days` are prose the frontend composes
    ("3 times per week", "7 days") and regex-strips again when it reopens a
    profile for editing. Store them verbatim — reformatting here breaks that
    round-trip. Everything defaults to "" rather than None because the frontend
    reads these keys unconditionally.
    """

    name: str = Field(max_length=120)
    dose: str = Field(default="", max_length=32)
    unit: str = Field(default="", max_length=32)
    frequency: str = Field(default="", max_length=64)
    administration: str = Field(default="", max_length=64)
    buffer_days: str = Field(default="", max_length=32)


class MedicationDetails(MedicationItem):
    """A medication section.

    The API has one slot per section but the form allows several medications in
    each, so a multi-medication section arrives as the first medication's fields
    plus the whole list in `items_json`.
    """

    items_json: str | None = Field(default=None, max_length=ITEMS_JSON_MAX)

    @field_validator("items_json")
    @classmethod
    def _valid_items_json(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            items = json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"items_json must be valid JSON: {exc.msg}") from exc
        if not isinstance(items, list) or not items:
            raise ValueError("items_json must be a non-empty JSON array of medications")
        # Validate against MedicationItem, not MedicationDetails: the nesting
        # stops at one level.
        parsed = [MedicationItem.model_validate(item) for item in items]
        # Re-serialise so what is stored is canonical rather than whatever
        # spacing the client happened to send.
        return json.dumps([item.model_dump() for item in parsed])


class ClinicalProfile(BaseModel):
    """Diagnosis-specific onboarding data, recorded rather than prescribed."""

    diagnosis: DiagnosisType
    sex: Sex = Sex.prefer_not_to_say
    weight_kg: float | None = Field(default=None, gt=0, le=500)
    date_of_birth: date | None = None
    has_drug_allergies: bool = False
    drug_allergy_details: str | None = Field(default=None, max_length=ALLERGY_DETAILS_MAX)
    # This is intentionally the diagnostic/pre-prophylaxis result, not a
    # contemporaneous result that may be normalised by regular treatment.
    diagnosis_factor_activity_percent: float | None = Field(default=None, ge=0, le=150)
    diagnosis_test_date: date | None = None
    congenital_severity: CongenitalSeverity | None = None
    factor_xi_deficiency_level: FactorXiDeficiencyLevel | None = None
    acquired_inhibitor_titre_bu_ml: float | None = Field(default=None, ge=0, le=10000)
    acquired_bleeding_severity: AcquiredBleedingSeverity | None = None
    inhibitor_status: InhibitorStatus | None = None
    fix_allergy_or_anaphylaxis: YesNoUnknown | None = None
    treatment_approach: str | None = Field(default=None, max_length=TREATMENT_APPROACH_MAX)
    prophylactic_medication: MedicationDetails | None = None
    minimum_buffer: str | None = Field(default=None, max_length=64)
    on_demand_medication: MedicationDetails | None = None
    other_medication: MedicationDetails | None = None
    group_chats: list[str] = []
    medication_reminders: bool = False

    @field_validator("date_of_birth")
    @classmethod
    def _not_in_the_future(cls, v: date | None) -> date | None:
        if v is not None and v > date.today():
            raise ValueError("date_of_birth must not be in the future")
        return v

    @field_validator("treatment_approach")
    @classmethod
    def _known_treatments(cls, v: str | None) -> str | None:
        """A comma-joined multi-select. The single column cannot police itself."""
        if v is None or not v.strip():
            return None
        tokens = [token.strip() for token in v.split(",")]
        if any(not token for token in tokens):
            raise ValueError("treatment_approach must not contain blank entries")
        if len(set(tokens)) != len(tokens):
            raise ValueError("treatment_approach must not repeat an entry")
        allowed = {member.value for member in TreatmentApproach}
        unknown = [token for token in tokens if token not in allowed]
        if unknown:
            raise ValueError(f"unknown treatment_approach: {', '.join(unknown)}")
        # Mirrors the form's toggle: choosing "none" clears everything else.
        if TreatmentApproach.none.value in tokens and len(tokens) > 1:
            raise ValueError("treatment_approach 'none' cannot be combined with other options")
        return ",".join(tokens)

    @model_validator(mode="after")
    def _clear_inapplicable_fields(self) -> "ClinicalProfile":
        """Drop values that do not belong to the chosen diagnosis.

        The form already nulls these, so this only catches a stale value left
        behind when someone edits their diagnosis. It coerces rather than
        rejects on purpose: the form's submit button re-checks only step 0, so a
        422 here would block a submit the UI considers valid, and the frontend
        renders `detail` only when it is a string — a field-error list shows up
        as a bare "Request failed (422)".
        """
        is_congenital = self.diagnosis in CONGENITAL_DIAGNOSES
        if not is_congenital:
            self.congenital_severity = None
            self.inhibitor_status = None
        if self.diagnosis is not DiagnosisType.factor_xi_deficiency:
            self.factor_xi_deficiency_level = None
        if self.diagnosis is not DiagnosisType.acquired_haemophilia:
            self.acquired_inhibitor_titre_bu_ml = None
            self.acquired_bleeding_severity = None
        if self.diagnosis not in FACTOR_IX_DIAGNOSES:
            self.fix_allergy_or_anaphylaxis = None
        return self


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
