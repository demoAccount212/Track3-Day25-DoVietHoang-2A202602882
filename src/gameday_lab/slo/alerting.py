"""Part E — Multi-window, multi-burn-rate alerting (Google SRE Workbook, ch. 5).

Input: per-minute ``total`` and ``bad`` request counts for a 30-day window.

burn_rate(window) = error_ratio(window) / (1 - SLO)

A rule fires at minute i when BOTH its long and its short window (ending at i,
inclusive) burn at least ``burn_rate``. The long window says "this is
significant"; the short window says "it is still happening" (so the alert
resets quickly after recovery).

Tests: tests/test_alerting.py, then `make slo`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlertRule:
    name: str
    severity: str  # "page" or "ticket"
    long_min: int
    short_min: int
    burn_rate: float


# SRE Workbook defaults for a 30-day SLO window
DEFAULT_RULES: tuple[AlertRule, ...] = (
    AlertRule("fast_burn_2pct_1h", "page", 60, 5, 14.4),
    AlertRule("slow_burn_5pct_6h", "page", 360, 30, 6.0),
    AlertRule("budget_leak_10pct_3d", "ticket", 4320, 360, 1.0),
)


@dataclass(frozen=True)
class AlertEvent:
    rule: str
    severity: str
    start_min: int  # first minute the rule fired
    end_min: int  # first minute it stopped firing (exclusive)


class WindowSums:
    """O(1) sum of any window, via prefix sums. Build once, query many times.

    43,200 minutes x 6 windows: recomputing every window with sum() is far too slow.
    """

    def __init__(self, values: list[int]) -> None:
        """Build the prefix-sum list (prefix[0] = 0)."""
        self.prefix = [0]
        for v in values:
            self.prefix.append(self.prefix[-1] + v)

    def sum(self, end_inclusive: int, length: int) -> int:
        """Sum of values[end_inclusive - length + 1 .. end_inclusive],
        clipped at index 0 when the window starts before the data."""
        start = end_inclusive - length + 1
        if start < 0:
            start = 0
        return self.prefix[end_inclusive + 1] - self.prefix[start]


class BurnRateAlerter:
    def __init__(self, slo: float = 0.995, rules: tuple[AlertRule, ...] = DEFAULT_RULES) -> None:
        if not 0 < slo < 1:
            raise ValueError("slo must be in (0, 1)")
        self.slo = slo
        self.rules = rules

    def burn_rate(self, total: WindowSums, bad: WindowSums, end: int, length: int) -> float:
        """(bad / total over the window) / (1 - slo); 0.0 if total is 0."""
        t = total.sum(end, length)
        if t == 0:
            return 0.0
        b = bad.sum(end, length)
        error_ratio = b / t
        return error_ratio / (1 - self.slo)

    def evaluate(self, total: list[int], bad: list[int]) -> list[AlertEvent]:
        """One AlertEvent per contiguous firing episode of each rule.

        For each rule, scan the minutes; the rule fires at minute i when
        burn(long) >= rule.burn_rate AND burn(short) >= rule.burn_rate.
        An episode starts at the first firing minute and ends (exclusive) at the
        first minute it stops firing (or len(total)). Sort by start_min.
        """
        total_ws = WindowSums(total)
        bad_ws = WindowSums(bad)
        n = len(total)
        events = []

        for rule in self.rules:
            firing = [False] * n
            for i in range(n):
                long_burn = self.burn_rate(total_ws, bad_ws, i, rule.long_min)
                short_burn = self.burn_rate(total_ws, bad_ws, i, rule.short_min)
                if long_burn >= rule.burn_rate and short_burn >= rule.burn_rate:
                    firing[i] = True

            # Find contiguous episodes
            i = 0
            while i < n:
                if firing[i]:
                    start_min = i
                    while i < n and firing[i]:
                        i += 1
                    end_min = i  # exclusive
                    events.append(AlertEvent(
                        rule=rule.name,
                        severity=rule.severity,
                        start_min=start_min,
                        end_min=end_min
                    ))
                else:
                    i += 1

        events.sort(key=lambda e: e.start_min)
        return events


def error_budget_report(total: list[int], bad: list[int], slo: float = 0.995) -> dict[str, float | bool]:
    """How much of the 30-day error budget is spent?

    Return a dict with:
      requests          sum(total)
      allowed_bad       (1 - slo) * requests            (round to 1 decimal)
      bad               sum(bad)
      availability      1 - bad / requests              (round to 6 decimals)
      budget_consumed   bad / allowed_bad               (round to 4 decimals)
      budget_remaining  1 - budget_consumed             (round to 4 decimals)
      freeze_releases   True when nothing is left (budget_remaining <= 0)
    """
    requests = sum(total)
    allowed_bad = round((1 - slo) * requests, 1)
    bad_count = sum(bad)
    availability = round(1 - bad_count / requests, 6) if requests > 0 else 1.0
    budget_consumed = round(bad_count / allowed_bad, 4) if allowed_bad > 0 else 0.0
    budget_remaining = round(1 - budget_consumed, 4)
    freeze_releases = budget_remaining <= 0

    return {
        "requests": requests,
        "allowed_bad": allowed_bad,
        "bad": bad_count,
        "availability": availability,
        "budget_consumed": budget_consumed,
        "budget_remaining": budget_remaining,
        "freeze_releases": freeze_releases,
    }