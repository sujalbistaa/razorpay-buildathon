"""Scheduling helpers shared by more than one policy — promoted out of heuristic.py once
learned.py needed the same "push a message past quiet hours" logic for its dunning step.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from vasool.compliance.constants import CONTACT_QUIET_HOURS_IST
from vasool.domain.timezones import at_hour_ist, to_ist


def avoid_quiet_hours(t: datetime) -> datetime:
    quiet_start, quiet_end = CONTACT_QUIET_HOURS_IST
    ist_t = to_ist(t)
    if quiet_end <= ist_t.hour < quiet_start:
        return t
    target_date = ist_t.date() if ist_t.hour < quiet_end else ist_t.date() + timedelta(days=1)
    return at_hour_ist(target_date, quiet_end)
