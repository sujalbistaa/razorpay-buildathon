"""Tracks issuer downtime purely from the windows a policy would actually know about as of
`now` — World.downtime_windows_known_by(t) already filters to windows whose begin <= t, the
same boundary a real payment.downtime.started webhook would respect. Nothing here reads
ahead of that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vasool.domain.types import DowntimeWindow, Rail, Severity


@dataclass(frozen=True)
class DowntimeTracker:
    windows: tuple[DowntimeWindow, ...]

    def is_down(self, issuer: str, method: Rail, t: datetime) -> bool:
        return any(
            w.issuer == issuer and w.method == method and w.begin <= t and (w.end is None or t < w.end)
            and w.severity is Severity.HIGH
            for w in self.windows
        )

    def expected_resolution(self, issuer: str, method: Rail, t: datetime) -> datetime | None:
        # None among open_ends means "still ongoing, no known end" -- distinct from "no
        # matching window at all" (open_ends empty). Either way there's nothing to report a
        # concrete resolution time for.
        open_windows = [
            w for w in self.windows if w.issuer == issuer and w.method == method and w.begin <= t
            and (w.end is None or t < w.end)
        ]
        if not open_windows or any(w.end is None for w in open_windows):
            return None
        return max(w.end for w in open_windows if w.end is not None)
