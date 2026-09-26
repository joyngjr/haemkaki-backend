from datetime import date, timedelta

from app.models import EventKind, TrackingEvent
from app.services import DOSE_KINDS, event_vials, forecast_bleed_vials


AS_OF = date(2026, 9, 26)


def event(kind: EventKind, vials: int, days_ago: int = 0) -> TrackingEvent:
    return TrackingEvent(
        user_id=1,
        kind=kind.value,
        occurred_on=AS_OF - timedelta(days=days_ago),
        vials=vials,
    )


def test_removal_subtracts_stock_without_being_a_dose() -> None:
    assert event_vials(event(EventKind.removal, 3), dose_vials=2) == -3
    assert EventKind.removal.value not in DOSE_KINDS


def test_bleed_forecast_weights_recent_use_more_heavily() -> None:
    recent = forecast_bleed_vials(
        [event(EventKind.on_demand, 4)], schedules=[], plans=[], as_of=AS_OF
    )
    older = forecast_bleed_vials(
        [event(EventKind.on_demand, 4, days_ago=31)], schedules=[], plans=[], as_of=AS_OF
    )

    assert recent == 2
    assert older == 1


def test_bleed_forecast_includes_follow_up_doses_but_not_prophylaxis() -> None:
    predicted = forecast_bleed_vials(
        [
            event(EventKind.on_demand, 2),
            event(EventKind.follow_up, 2),
            event(EventKind.prophylaxis, 20),
        ],
        schedules=[],
        plans=[],
        as_of=AS_OF,
    )

    assert predicted == 2
