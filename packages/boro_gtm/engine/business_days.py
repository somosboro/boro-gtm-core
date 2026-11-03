"""Business days, business hours and follow-up dates, in Chile (CE-ADR-006).

Every date here is computed, never inferred: a follow-up is due because the
calendar says so, not because a model thought it was time.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from boro_gtm.engine.strategy import Strategy


class BusinessCalendar:
    def __init__(self, strategy: Strategy) -> None:
        self.tz = ZoneInfo(strategy.timezone)
        self.holidays = frozenset(date.fromisoformat(d) for d in strategy.holidays)
        start, end = strategy.business_hours
        self.start = time.fromisoformat(start)
        self.end = time.fromisoformat(end)

    def local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.tz)

    def is_business_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def business_day_of(self, moment: datetime) -> date:
        return self.local(moment).date()

    def add_business_days(self, day: date, n: int) -> date:
        current = day
        added = 0
        while added < n:
            current += timedelta(days=1)
            if self.is_business_day(current):
                added += 1
        return current

    def next_business_day(self, day: date) -> date:
        current = day
        while not self.is_business_day(current):
            current += timedelta(days=1)
        return current

    def opening(self, day: date) -> datetime:
        return datetime.combine(day, self.start, self.tz)

    def closing(self, day: date) -> datetime:
        return datetime.combine(day, self.end, self.tz)

    def within_hours(self, moment: datetime) -> bool:
        local = self.local(moment)
        return (self.is_business_day(local.date())
                and self.start <= local.time() <= self.end)

    def next_open_moment(self, moment: datetime) -> datetime:
        """`moment` if it is inside business hours, else the next opening."""
        local = self.local(moment)
        day = local.date()
        if self.is_business_day(day) and local.time() < self.start:
            return self.opening(day)
        if self.within_hours(local):
            return local
        return self.opening(self.next_business_day(day + timedelta(days=1)))
