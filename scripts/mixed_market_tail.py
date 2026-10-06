"""Date-only evidence for mixed China/Hong Kong/US constituent tails.

No quote synthesis or breadth eligibility changes. Unknown venues and calendar
errors never provide an exemption. Shenzhen shares Shanghai's date calendar,
as in export_holdings_prices; this mapping is not a source of prices.
"""
from functools import lru_cache
from datetime import timedelta

from venue_calendars import get_calendar


def venue_for(ticker):
    if ticker.endswith(('.SS', '.SZ')):
        return 'XSHG'
    if ticker.endswith('.HK'):
        return 'HKEX'
    if ticker.isalpha():
        return 'NYSE'
    return None


@lru_cache(maxsize=2048)
def venue_sessions(venue, first, last):
    try:
        return frozenset(get_calendar(venue).schedule(
            start_date=first, end_date=last).index.date)
    except Exception:
        return None


def is_closed(ticker, day):
    venue = venue_for(ticker)
    sessions = venue_sessions(venue, day, day) if venue else None
    return sessions is not None and day not in sessions


def is_open(ticker, day):
    venue = venue_for(ticker)
    sessions = venue_sessions(venue, day, day) if venue else None
    return sessions is not None and day in sessions


def closed_since(ticker, last_bar, expected):
    """Proof of NO missed venue session, not a relaxed staleness budget."""
    if last_bar >= expected:
        return False
    venue = venue_for(ticker)
    sessions = venue_sessions(venue, last_bar + timedelta(days=1), expected) if venue else None
    return sessions is not None and not sessions


def is_mixed_china_roster(tickers):
    venues = {venue_for(t) for t in tickers}
    return 'XSHG' in venues and bool(venues & {'HKEX', 'NYSE'})
