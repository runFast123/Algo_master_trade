"""NSE/BSE trading holidays.

``is_indian_market_hours`` previously covered weekday session times only, so the
app believed the market was open on Republic Day. Equity trading holidays are
published annually by the exchanges and do not follow a rule that can be
derived, so they are listed.

A year that has not been filled in is reported as unknown rather than treated as
having no holidays. Silently assuming a blank year is fully trading is how a
calendar becomes worse than none: it looks authoritative and is wrong.
"""

import datetime
from typing import Dict, List, Optional

# NSE/BSE equity segment trading holidays. Source: exchange holiday circulars.
# Muhurat trading sessions are not regular sessions and are not listed here.
HOLIDAYS: Dict[int, Dict[str, str]] = {
    2025: {
        "2025-02-26": "Mahashivratri",
        "2025-03-14": "Holi",
        "2025-03-31": "Id-Ul-Fitr (Ramzan Id)",
        "2025-04-10": "Shri Mahavir Jayanti",
        "2025-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
        "2025-04-18": "Good Friday",
        "2025-05-01": "Maharashtra Day",
        "2025-08-15": "Independence Day",
        "2025-08-27": "Shri Ganesh Chaturthi",
        "2025-10-02": "Mahatma Gandhi Jayanti / Dussehra",
        "2025-10-21": "Diwali Laxmi Pujan",
        "2025-10-22": "Balipratipada",
        "2025-11-05": "Prakash Gurpurb Sri Guru Nanak Dev",
        "2025-12-25": "Christmas",
    },
    2026: {
        "2026-01-26": "Republic Day",
        "2026-03-04": "Holi",
        "2026-03-21": "Id-Ul-Fitr (Ramzan Id)",
        "2026-03-31": "Shri Mahavir Jayanti",
        "2026-04-03": "Good Friday",
        "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
        "2026-05-01": "Maharashtra Day",
        "2026-08-15": "Independence Day",
        "2026-09-14": "Shri Ganesh Chaturthi",
        "2026-10-02": "Mahatma Gandhi Jayanti",
        "2026-10-20": "Dussehra",
        "2026-11-09": "Diwali Laxmi Pujan",
        "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
        "2026-12-25": "Christmas",
    },
}


def is_known(day: datetime.date) -> bool:
    """Whether the calendar covers this date's year at all."""
    return day.year in HOLIDAYS


def holiday_name(day: datetime.date) -> Optional[str]:
    """The holiday falling on this date, or None. None for an unknown year."""
    return HOLIDAYS.get(day.year, {}).get(day.isoformat())


def is_trading_day(day: datetime.date) -> bool:
    """Weekend and holiday check.

    An unknown year falls back to the weekday rule, which is the honest answer:
    weekends are certain, holidays are not known. Callers that need certainty
    should check :func:`is_known` first.
    """
    if day.weekday() >= 5:
        return False
    return holiday_name(day) is None


def next_trading_day(day: datetime.date) -> Optional[datetime.date]:
    """The next day the market trades, or None beyond the calendar."""
    candidate = day + datetime.timedelta(days=1)
    for _ in range(30):
        if not is_known(candidate):
            return None
        if is_trading_day(candidate):
            return candidate
        candidate += datetime.timedelta(days=1)
    return None


def upcoming(day: datetime.date, limit: int = 5) -> List[Dict[str, str]]:
    """The next few holidays from this date, for display."""
    found: List[Dict[str, str]] = []
    for year in sorted(y for y in HOLIDAYS if y >= day.year):
        for iso, name in sorted(HOLIDAYS[year].items()):
            if iso > day.isoformat():
                found.append({"date": iso, "name": name})
                if len(found) >= limit:
                    return found
    return found


def describe(day: datetime.date) -> Dict[str, object]:
    """Everything the interface needs to explain why the market is shut."""
    if not is_known(day):
        return {
            "known": False,
            "is_trading_day": day.weekday() < 5,
            "reason": "Weekend" if day.weekday() >= 5 else None,
            "note": f"No holiday calendar loaded for {day.year}; "
                    "weekends only. Add the year to market_calendar.HOLIDAYS.",
            "upcoming": [],
        }

    name = holiday_name(day)
    weekend = day.weekday() >= 5
    return {
        "known": True,
        "is_trading_day": not weekend and name is None,
        "reason": "Weekend" if weekend else name,
        "holiday": name,
        "next_trading_day": (
            nxt.isoformat() if (nxt := next_trading_day(day)) else None
        ),
        "upcoming": upcoming(day),
    }
