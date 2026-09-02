"""Small date helpers shared by main.py and admin.py."""

from datetime import date, timedelta


def upcoming_weekdays(days_ahead: int = 7) -> list[date]:
    today = date.today()
    return [d for i in range(days_ahead) if (d := today + timedelta(days=i)).weekday() < 5]


def resolve_target_day(day_param: str | None, days: list[date]) -> date:
    """Pick which day a day-picker view should show: the requested one if it's
    a valid ISO date within `days`, otherwise the first selectable day.
    Shared by the employee map view and the admin reassign view, which offer
    the same set of upcoming weekdays."""
    if day_param:
        try:
            requested = date.fromisoformat(day_param)
        except ValueError:
            pass
        else:
            if requested in days:
                return requested
    return days[0]
