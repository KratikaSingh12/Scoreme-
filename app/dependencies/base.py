"""Contract every external service adapter must follow.

The executor only knows this interface. Swapping the simulator for a real
HTTP client means writing one new class, nothing else changes.
"""

from abc import ABC, abstractmethod


class ExternalService(ABC):
    name: str

    @abstractmethod
    def call(self, params: dict, idempotency_key: str) -> dict:
        """Call the service.

        Must raise TransientDependencyError subclasses for retryable failures
        and PermanentDependencyError for failures that retrying cannot fix.
        The idempotency_key is the same for every retry of the same stage,
        so a real service can avoid doing the work twice.
        """
