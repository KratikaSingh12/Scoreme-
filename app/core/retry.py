"""Retry with exponential backoff and jitter.

Only transient dependency errors are retried. Permanent errors are raised
straight away because repeating them only adds load and delay.
"""

import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

from app.core.errors import RetriesExhausted, TransientDependencyError

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.2  # seconds before the 2nd attempt
    max_delay: float = 2.0  # cap so backoff never grows without limit
    jitter: float = 0.1  # +/- fraction, spreads out retry storms

    def delay_for(self, attempt: int) -> float:
        """Delay to wait after the given failed attempt (1-based)."""
        delay = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        if self.jitter:
            delay *= 1 + random.uniform(-self.jitter, self.jitter)
        return max(0.0, delay)


AttemptHook = Callable[[int, Optional[Exception]], None]


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    on_attempt: Optional[AttemptHook] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[T, int]:
    """Run fn until it succeeds or attempts run out.

    Returns (result, attempts_used). on_attempt(attempt, error_or_None) is
    called after every attempt so callers can write audit events.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            result = fn()
        except TransientDependencyError as exc:
            last_error = exc
            if on_attempt:
                on_attempt(attempt, exc)
            if attempt < policy.max_attempts:
                sleep(policy.delay_for(attempt))
            continue
        if on_attempt:
            on_attempt(attempt, None)
        return result, attempt
    raise RetriesExhausted(last_error, policy.max_attempts)
