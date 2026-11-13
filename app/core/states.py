"""Request lifecycle: the single source of truth for statuses and allowed moves.

    RECEIVED -> IN_PROGRESS -> APPROVED
                            -> REJECTED
                            -> MANUAL_REVIEW -> APPROVED / REJECTED
                            -> RETRY_PENDING -> IN_PROGRESS (retry)
                            -> FAILED        -> IN_PROGRESS (retry)
"""

from enum import Enum

from app.core.errors import InvalidStateTransition


class Status(str, Enum):
    RECEIVED = "RECEIVED"
    IN_PROGRESS = "IN_PROGRESS"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    RETRY_PENDING = "RETRY_PENDING"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS: dict[Status, set[Status]] = {
    Status.RECEIVED: {Status.IN_PROGRESS},
    Status.IN_PROGRESS: {
        Status.APPROVED,
        Status.REJECTED,
        Status.MANUAL_REVIEW,
        Status.RETRY_PENDING,
        Status.FAILED,
    },
    Status.MANUAL_REVIEW: {Status.APPROVED, Status.REJECTED},
    Status.RETRY_PENDING: {Status.IN_PROGRESS},
    Status.FAILED: {Status.IN_PROGRESS},
    Status.APPROVED: set(),
    Status.REJECTED: set(),
}

# Final decisions. A request in one of these never changes again.
FINAL_STATUSES = {Status.APPROVED, Status.REJECTED}

# Targets a workflow stage may point to instead of another stage.
TERMINAL_TARGETS = {
    Status.APPROVED.value,
    Status.REJECTED.value,
    Status.MANUAL_REVIEW.value,
    Status.RETRY_PENDING.value,
}

RETRYABLE_STATUSES = {Status.RETRY_PENDING, Status.FAILED}


def ensure_transition(current: Status, target: Status) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransition(
            f"cannot move request from {current.value} to {target.value}",
            details={"from": current.value, "to": target.value},
        )
