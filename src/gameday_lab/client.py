"""Part C — ResilientClient: compose classification, retry, breaker and fallback.

The simulator calls ``start`` for every logical LLM call, then ``on_success``
or ``on_error`` for every attempt. You answer with ``Send`` (try a provider,
maybe after a delay) or ``GiveUp`` (user gets a static degraded answer).

Tests: tests/test_client.py, then `make compare` (6 incident tapes).

Per-call scratch space lives in ``call.state`` (a dict). Suggested keys:
    "ladder": list[str]  providers this call may use, in order
    "idx":    int        position of the provider we are currently on
    "tries":  int        attempts already made on that provider

Decision table for ``on_error`` (implement it!):

| ErrorKind        | breaker              | then                                                    |
|------------------|----------------------|---------------------------------------------------------|
| CLIENT_ERROR     | release (not health) | GiveUp("client_error") — never retry, never fail over   |
| QUOTA_EXHAUSTED  | release              | cooldown provider for quota_cooldown_s; fail over now   |
| RATE_LIMITED     | release              | cooldown provider until now + retry_after; fail over now|
| OVERLOADED /     | on_failure           | retry SAME provider after backoff.delay(tries) if       |
| SERVER_ERROR /   |                      |   tries < max_attempts_per_provider AND budget allows   |
| TIMEOUT          |                      |   AND it fits the deadline; otherwise fail over         |

Cooldowns are per provider and shared by all calls (other calls must avoid a
rate-limited provider too). When every provider in the ladder is unusable,
you may wait for a *short* (<= 5 s) rate-limit cooldown if it fits the deadline.
"""

from __future__ import annotations

import random
from typing import Any

from gameday_lab.breaker import BreakerState, Permit, SlidingWindowBreaker  # noqa: F401
from gameday_lab.errors import ErrorKind, classify  # noqa: F401
from gameday_lab.retry import FullJitterBackoff, RetryBudget
from gameday_lab.sim.contracts import (  # noqa: F401
    Action,
    Attempt,
    BaseClient,
    CallContext,
    GiveUp,
    ProviderError,
    ProviderInfo,
    Send,
)

RATE_LIMIT_WAIT_MAX_S = 5.0  # only wait for short (rate-limit) cooldowns, never for quota


class ResilientClient(BaseClient):
    name = "resilient"

    def __init__(
        self,
        providers: dict[str, ProviderInfo],
        fallback_order: list[str] | tuple[str, ...],
        settings: dict[str, Any],
        seed: int = 0,
    ) -> None:
        """Reads the `client:` block of configs/gameday.yaml."""
        self.providers = providers
        self.order = list(fallback_order)
        self.timeout_s = float(settings.get("attempt_timeout_s", 8.0))
        self.max_attempts_per_provider = int(settings.get("max_attempts_per_provider", 2))
        self.min_time_left_s = float(settings.get("min_time_left_s", 1.0))
        self.quota_cooldown_s = float(settings.get("quota_cooldown_s", 300.0))
        self.backoff = FullJitterBackoff(
            float(settings.get("backoff_base_s", 0.25)),
            float(settings.get("backoff_cap_s", 4.0)),
            random.Random(seed),
        )
        self.budget = RetryBudget(
            float(settings.get("retry_budget_ratio", 0.1)),
            float(settings.get("retry_budget_min_per_s", 0.5)),
            float(settings.get("retry_budget_max_tokens", 20.0)),
        )
        breaker_cfg = dict(settings.get("breaker", {}))
        self.breakers = {name: SlidingWindowBreaker(name, **breaker_cfg) for name in providers}
        self.cooldown_until: dict[str, float] = {name: 0.0 for name in providers}

    # ------------------------------------------------------------------ provided helper
    def _send(self, call: CallContext, provider: str, now: float, delay: float) -> Action | None:
        """Build a Send for ``provider`` (after ``delay``), or None if its breaker refuses.

        Takes a breaker permit (carried in ``tag``), and caps the attempt timeout
        so it never runs past the call's deadline. Returns GiveUp("deadline") if
        there is not enough time left for a meaningful attempt.
        """
        permit = self.breakers[provider].allow(now)
        if permit is None:
            return None
        timeout = min(self.timeout_s, call.deadline - (now + delay))
        if timeout < self.min_time_left_s:
            self.breakers[provider].release(permit, now)
            return GiveUp("deadline")
        return Send(provider, timeout_s=timeout, delay_s=delay, tag=permit)

    # ------------------------------------------------------------------ TODO
    def ladder(self, task: str) -> list[str]:
        """Filter ``self.order`` by capability — a "tool" call may only use
        providers with ``supports_tools``."""
        if task == "tool":
            return [name for name in self.order if self.providers[name].supports_tools]
        # "chat" uses full order
        return list(self.order)

    def _find_next_provider(self, call: CallContext, now: float, delay: float = 0.0, skip_current: bool = False) -> tuple[Action | None, int, int]:
        """Find the next available provider in the ladder.
        
        Returns (action, new_idx, new_tries) or (None, idx, tries) if none found.
        
        Note: call.state["idx"] points to the next provider to try.
        When skip_current=True, we start searching from the current idx (not idx+1).
        """
        ladder = call.state.get("ladder", self.ladder(call.task))
        idx = call.state.get("idx", 0)
        tries = call.state.get("tries", 0)

        # Walk through the ladder starting from idx
        # When skip_current=True, we want to skip the provider we just failed on
        # and start from the current idx (which is already the next one)
        start_idx = idx if skip_current else idx
        n = len(ladder)
        
        for i in range(n):
            probe_idx = (start_idx + i) % n
            provider = ladder[probe_idx]
            # Check if provider is in cooldown
            if now < self.cooldown_until[provider]:
                # Skip this provider
                continue
            # Try to send to this provider
            result = self._send(call, provider, now, delay)
            if result is not None:
                new_idx = (probe_idx + 1) % n if i < n - 1 else probe_idx
                return result, new_idx, 0
            # Send failed (breaker refused), move to next provider

        return None, idx, tries

    def _route(self, call: CallContext, now: float, delay: float = 0.0) -> Action:
        """Pick the provider for the next attempt.

        Walk the ladder from call.state["idx"]:
          * skip a provider still in cooldown at send time (now + delay)
          * otherwise try self._send(...); a non-None result is your answer
          * when skipping, move idx forward, reset tries, and set delay = 0
            (a failover goes out immediately)
        If the ladder is exhausted: optionally wait for the earliest *short*
        rate-limit cooldown (<= RATE_LIMIT_WAIT_MAX_S, breaker not OPEN, fits the
        deadline), else GiveUp("no_provider_available").
        """
        # Try to find available provider normally
        result, new_idx, new_tries = self._find_next_provider(call, now, delay)
        if result is not None:
            call.state["idx"] = new_idx
            call.state["tries"] = new_tries
            return result

        # Ladder exhausted - try failover with rate limit cooldowns
        # Find the earliest short cooldown that fits the deadline
        ladder = call.state.get("ladder", self.ladder(call.task))
        earliest_cooldown = None
        earliest_provider = None
        for provider in ladder:
            if now < self.cooldown_until[provider]:
                if self.cooldown_until[provider] - now < RATE_LIMIT_WAIT_MAX_S:
                    if earliest_cooldown is None or self.cooldown_until[provider] < earliest_cooldown:
                        earliest_cooldown = self.cooldown_until[provider]
                        earliest_provider = provider

        if earliest_provider is not None and earliest_cooldown is not None:
            wait_until = earliest_cooldown
            time_to_wait = wait_until - now
            # Check if we have enough time left in the deadline
            if 0 < time_to_wait <= (call.deadline - now) and time_to_wait <= RATE_LIMIT_WAIT_MAX_S:
                # Wait for the cooldown to expire
                new_idx = 0
                call.state["idx"] = new_idx
                call.state["tries"] = 0
                return self._send(call, earliest_provider, now, time_to_wait)
            else:
                # Can't wait, fail over
                new_idx = 0
                call.state["idx"] = new_idx
                call.state["tries"] = 0
                return GiveUp("no_provider_available")

        # No provider available
        new_idx = 0
        call.state["idx"] = new_idx
        call.state["tries"] = 0
        return GiveUp("no_provider_available")

    def start(self, call: CallContext, now: float) -> Action:
        """Record the request in the retry budget, initialise call.state
        (ladder, idx=0, tries=0) and route."""
        # Record request in retry budget
        self.budget.record_request(now)
        # Initialise call.state
        call.state = {
            "ladder": self.ladder(call.task),
            "idx": 0,
            "tries": 0,
        }
        # Route to first provider
        return self._route(call, now, delay=0.0)

    def on_success(self, attempt: Attempt, now: float, latency_s: float) -> None:
        """Report success + latency to the provider's breaker (attempt.tag is the Permit)."""
        provider = attempt.provider
        if provider in self.breakers:
            self.breakers[provider].on_success(attempt.tag, now, latency_s)

    def on_error(self, attempt: Attempt, err: ProviderError, now: float) -> Action:
        """Implement the decision table in the module docstring.

        | ErrorKind        | breaker              | then                                                    |
        |------------------|----------------------|---------------------------------------------------------|
        | CLIENT_ERROR     | release (not health) | GiveUp("client_error") — never retry, never fail over   |
        | QUOTA_EXHAUSTED  | release              | cooldown provider for quota_cooldown_s; fail over now   |
        | RATE_LIMITED     | release              | cooldown provider until now + retry_after; fail over now|
        | OVERLOADED /     | on_failure           | retry SAME provider after backoff.delay(tries) if       |
        | SERVER_ERROR /   |                      |   tries < max_attempts_per_provider AND budget allows   |
        | TIMEOUT          |                      |   AND it fits the deadline; otherwise fail over         |
        """
        provider = attempt.provider
        error_kind = classify(err)
        permit = attempt.tag
        call = attempt.call

        if error_kind == ErrorKind.CLIENT_ERROR:
            # Release the permit (not health), give up
            if provider in self.breakers and permit is not None:
                self.breakers[provider].release(permit, now)
            return GiveUp("client_error")

        if error_kind == ErrorKind.QUOTA_EXHAUSTED:
            # Release the permit, cooldown provider for quota_cooldown_s; fail over now
            if provider in self.breakers and permit is not None:
                self.breakers[provider].release(permit, now)
            self.cooldown_until[provider] = now + self.quota_cooldown_s
            # Fail over - try next provider (skip the current one)
            result, new_idx, new_tries = self._find_next_provider(call, now, delay=0.0, skip_current=True)
            if result is not None:
                call.state["idx"] = new_idx
                call.state["tries"] = new_tries
            return result or GiveUp("no_provider_available")

        if error_kind == ErrorKind.RATE_LIMITED:
            # Release the permit, cooldown provider until now + retry_after; fail over now
            if provider in self.breakers and permit is not None:
                self.breakers[provider].release(permit, now)
            # Get retry_after from the error
            retry_after = getattr(err, 'retry_after', 1.0)
            self.cooldown_until[provider] = now + retry_after
            # Fail over - try next provider (skip the current one)
            result, new_idx, new_tries = self._find_next_provider(call, now, delay=0.0, skip_current=True)
            if result is not None:
                call.state["idx"] = new_idx
                call.state["tries"] = new_tries
            return result or GiveUp("no_provider_available")

        # OVERLOADED, SERVER_ERROR, TIMEOUT - use on_failure logic
        # Record failure in breaker (these errors count toward breaker)
        if provider in self.breakers and permit is not None:
            self.breakers[provider].on_failure(permit, now)

        # Retry same provider after backoff if under max attempts and budget allows
        tries = attempt.number  # attempt.number is 1-based index
        if provider in self.breakers:
            # Check if we should retry same provider
            if tries < self.max_attempts_per_provider and self.budget.try_acquire_retry(now):
                # Calculate backoff delay
                delay = self.backoff.delay(tries)
                # Update state: stay on same provider, increment tries
                call.state["tries"] = tries
                # Send with backoff delay
                result = self._send(call, provider, now, delay)
                if result is not None:
                    call.state["tries"] = tries
                    return result
                # Send failed (might be budget exhausted), fall through to fail over

        # Fail over to next provider
        result, new_idx, new_tries = self._find_next_provider(call, now, delay=0.0, skip_current=True)
        if result is not None:
            call.state["idx"] = new_idx
            call.state["tries"] = new_tries
        return result or GiveUp("no_provider_available")

    def on_cancel(self, call: CallContext, tag: Any, now: float) -> None:
        """A delayed attempt was dropped (deadline) — release its permit."""
        # The tag is the Permit, which has the breaker name
        if tag is not None and hasattr(tag, 'breaker'):
            provider = tag.breaker
            if provider in self.breakers:
                self.breakers[provider].release(tag, now)