"""Orchestrates a request from intake to decision.

Responsibilities (and nothing else):
  * validate input and enforce idempotency
  * pin the workflow version a request started with
  * drive the executor stage by stage, saving a checkpoint after each stage
  * apply lifecycle transitions through core.states
  * recover cleanly from storage failures (mark FAILED, allow resume)
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config.loader import WorkflowRegistry, config_fingerprint
from app.config.models import WorkflowConfig
from app.core.errors import (
    ConcurrentModification,
    ConfigError,
    DuplicateRequestKey,
    IdempotencyConflict,
    InputValidationError,
    InvalidStateTransition,
    PersistenceError,
    PlatformError,
    RequestNotFound,
)
from app.core.states import RETRYABLE_STATUSES, TERMINAL_TARGETS, Status, ensure_transition
from app.engine.executor import StageOutcome, WorkflowExecutor
from app.engine.validator import validate_payload
from app.services.explainer import build_explanation
from app.storage.repository import RequestRepository

log = logging.getLogger(__name__)


@dataclass
class SubmitResult:
    request: dict
    duplicate: bool


def hash_payload(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _event(event_type: str, stage: Optional[str] = None, **details) -> dict:
    return {"event_type": event_type, "stage": stage, "details": details}


class DecisionService:
    def __init__(
        self,
        registry: WorkflowRegistry,
        repository: RequestRepository,
        executor: WorkflowExecutor,
        max_steps: int = 50,
        stale_after_seconds: float = 60.0,
    ):
        self.registry = registry
        self.repo = repository
        self.executor = executor
        self.max_steps = max_steps
        # An IN_PROGRESS request with no saved progress for this long is
        # treated as stuck (its worker crashed or storage failed twice).
        self.stale_after_seconds = stale_after_seconds

    # ================================================================ intake
    def submit(self, workflow_name: str, payload: dict, idempotency_key: Optional[str] = None) -> SubmitResult:
        workflow = self.registry.get(workflow_name)

        errors = validate_payload(payload, workflow.input_schema)
        if errors:
            raise InputValidationError("request payload failed validation", details=errors)

        payload_hash = hash_payload(payload)
        # No key from the client: identical payloads are treated as the same request.
        key = idempotency_key or f"auto-{payload_hash[:32]}"

        existing = self.repo.find_by_idempotency_key(workflow_name, key)
        if existing:
            return self._handle_duplicate(existing, payload_hash, key)

        request_id = str(uuid.uuid4())
        fingerprint = config_fingerprint(workflow)
        record = {
            "id": request_id,
            "workflow_name": workflow.name,
            "workflow_version": workflow.version,
            "config_fingerprint": fingerprint,
            "workflow_snapshot": workflow.model_dump(mode="json"),
            "idempotency_key": key,
            "payload_hash": payload_hash,
            "payload": payload,
            "context": payload,
            "status": Status.RECEIVED.value,
            "current_stage": workflow.start_stage,
            "decision": None,
            "decision_reason": None,
            "attempt_count": 0,
        }
        events = [
            _event("REQUEST_RECEIVED", None, idempotency_key=key, workflow=workflow.name,
                   workflow_version=workflow.version, config_fingerprint=fingerprint),
            _event("VALIDATION_PASSED", None, fields_checked=sorted(workflow.input_schema)),
        ]
        try:
            self.repo.create(record, events)
        except DuplicateRequestKey:
            # Two identical requests raced each other; the other one won the insert.
            existing = self.repo.find_by_idempotency_key(workflow_name, key)
            return self._handle_duplicate(existing, payload_hash, key)

        return SubmitResult(self._run(request_id, reason="initial processing"), duplicate=False)

    def _handle_duplicate(self, existing: dict, payload_hash: str, key: str) -> SubmitResult:
        if existing["payload_hash"] != payload_hash:
            raise IdempotencyConflict(
                "idempotency key was already used with a different payload",
                details={"idempotency_key": key, "existing_request_id": existing["id"]},
            )
        self.repo.append_audit(existing["id"], [_event("DUPLICATE_REQUEST", None, idempotency_key=key)])
        return SubmitResult(self.repo.get(existing["id"]), duplicate=True)

    # ========================================================== retry/review
    def retry(self, request_id: str) -> dict:
        request = self.get(request_id)
        status = Status(request["status"])
        if status == Status.IN_PROGRESS:
            # Stuck after a crash or storage outage? Recover it first, then retry.
            if not self._is_stale(request):
                raise InvalidStateTransition(
                    "request is still being processed, try again later",
                    details={"stale_after_seconds": self.stale_after_seconds},
                )
            if not self._mark_failed(request_id, request["current_stage"],
                                     RuntimeError("no progress within the lease time")):
                raise InvalidStateTransition("request changed while recovering it, reload and try again")
            request = self.get(request_id)
            status = Status(request["status"])
        if status not in RETRYABLE_STATUSES:
            raise InvalidStateTransition(
                f"only {sorted(s.value for s in RETRYABLE_STATUSES)} requests can be retried, "
                f"this one is {status.value}"
            )
        return self._run(request_id, reason="retry requested", extra_events=[
            _event("RETRY_REQUESTED", request["current_stage"], previous_status=status.value)
        ])

    def review(self, request_id: str, decision: str, reviewer: str, comment: str = "") -> dict:
        request = self.get(request_id)
        current = Status(request["status"])
        target = Status(decision)
        if current != Status.MANUAL_REVIEW:
            raise InvalidStateTransition(f"request is {current.value}, not waiting for manual review")
        ensure_transition(current, target)

        reason = f"manual review by {reviewer}: {comment or 'no comment'}"
        self.repo.commit_step(
            request_id,
            request["version"],
            updates={"status": target.value, "decision": target.value, "decision_reason": reason},
            events=[
                _event("MANUAL_REVIEW_RESOLVED", None, reviewer=reviewer, decision=target.value, comment=comment),
                _event("STATE_CHANGED", None, from_status=current.value, to_status=target.value),
            ],
            transition=(current.value, target.value, None, reason),
        )
        return self.get(request_id)

    # ============================================================== recovery
    def recover_stuck(self) -> list[dict]:
        """Find requests stuck IN_PROGRESS and resume them from their checkpoint.

        Runs at startup and on POST /admin/recover. Safe to run while other
        workers are active: a request that is really being processed keeps
        bumping its version, so the optimistic lock makes us skip it.
        """
        results = []
        for request_id in self.repo.find_stuck(self._stale_cutoff()):
            try:
                request = self.retry(request_id)
                results.append({"request_id": request_id, "status": request["status"]})
            except PlatformError as exc:
                results.append({"request_id": request_id, "status": "SKIPPED", "error": exc.message})
        return results

    def _stale_cutoff(self) -> str:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.stale_after_seconds)
        return cutoff.isoformat(timespec="milliseconds")

    def _is_stale(self, request: dict) -> bool:
        return request["updated_at"] <= self._stale_cutoff()

    # ================================================================= reads
    def get(self, request_id: str) -> dict:
        request = self.repo.get(request_id)
        if request is None:
            raise RequestNotFound(f"request '{request_id}' does not exist")
        return request

    def history(self, request_id: str) -> list[dict]:
        self.get(request_id)  # 404 if missing
        return self.repo.history(request_id)

    def audit(self, request_id: str) -> list[dict]:
        self.get(request_id)
        return self.repo.audit(request_id)

    def explain(self, request_id: str) -> dict:
        request = self.get(request_id)
        return build_explanation(request, self.repo.audit(request_id), self.repo.history(request_id))

    def list_requests(
        self, status: Optional[str] = None, workflow: Optional[str] = None, limit: int = 20, offset: int = 0
    ) -> dict:
        """Oldest first, so a review queue is worked first in, first out."""
        allowed = [s.value for s in Status]
        if status is not None and status not in allowed:
            raise InputValidationError(f"unknown status '{status}'", details={"allowed": allowed})
        items, total = self.repo.list_requests(status, workflow, limit, offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    # ======================================================== configuration
    def reload_workflows(self) -> list[WorkflowConfig]:
        """Reload YAML. Rejected as a whole if any file breaks a rule, including version rules."""
        self.registry.load(guard=self._check_version_history)
        return self.registry.all()

    def _check_version_history(self, config: WorkflowConfig, fingerprint: str) -> None:
        """A version number that stored requests already used must keep its exact content.

        Without this, editing a rule without bumping the version would make two
        different rule sets look identical in the audit trail.
        """
        used = self.repo.workflow_versions_used(config.name)  # {version: {fingerprints}}
        if not used:
            return
        fingerprints = used.get(config.version)
        if fingerprints and fingerprint not in fingerprints:
            raise ConfigError(
                f"'{config.name}' version {config.version} was already used by stored requests "
                f"with different content. Bump the version number.",
                details={"workflow": config.name, "version": config.version},
            )
        highest = max(used)
        if config.version < highest:
            raise ConfigError(
                f"'{config.name}' version {config.version} is lower than version {highest} already "
                f"used by stored requests. Versions only go up; publish a rollback as a new version.",
                details={"workflow": config.name, "version": config.version, "highest_used": highest},
            )

    # ============================================================ execution
    def _run(self, request_id: str, reason: str, extra_events: Optional[list[dict]] = None) -> dict:
        request = self.get(request_id)
        current = Status(request["status"])
        ensure_transition(current, Status.IN_PROGRESS)

        version = self.repo.commit_step(
            request_id,
            request["version"],
            updates={"status": Status.IN_PROGRESS.value, "attempt_count": request["attempt_count"] + 1},
            events=(extra_events or []) + [
                _event("STATE_CHANGED", request["current_stage"], from_status=current.value,
                       to_status=Status.IN_PROGRESS.value, reason=reason)
            ],
            transition=(current.value, Status.IN_PROGRESS.value, request["current_stage"], reason),
        )

        # Always run with the config the request started with, even if the
        # YAML has been changed since. New rules apply to new requests only.
        workflow = WorkflowConfig.model_validate(request["workflow_snapshot"])
        context = request["context"]
        stage = request["current_stage"]

        try:
            for _ in range(self.max_steps):
                outcome = self.executor.run_stage(workflow, stage, context, request_id)
                context = outcome.context
                if outcome.next_target in TERMINAL_TARGETS:
                    self._finish(request_id, version, outcome)
                    return self.get(request_id)
                # Checkpoint: if anything fails later, we resume from next_target.
                version = self.repo.commit_step(
                    request_id, version,
                    updates={"context": context, "current_stage": outcome.next_target},
                    events=outcome.events,
                )
                stage = outcome.next_target
            raise PlatformError(f"workflow exceeded {self.max_steps} steps, aborting")
        except ConcurrentModification:
            raise  # another worker owns this request now; leave it alone
        except PlatformError as exc:
            self._mark_failed(request_id, stage, exc)
            raise
        except Exception as exc:  # unexpected bug: never leave a request stuck IN_PROGRESS
            log.exception("unexpected error while processing %s", request_id)
            self._mark_failed(request_id, stage, exc)
            raise PlatformError("unexpected error while processing request", details=str(exc)) from exc

    def _finish(self, request_id: str, version: int, outcome: StageOutcome) -> None:
        target = Status(outcome.next_target)
        events = list(outcome.events)

        if target == Status.RETRY_PENDING:
            # Keep current_stage on the failed stage so a retry resumes right there.
            updates = {"status": target.value, "context": outcome.context, "current_stage": outcome.stage,
                       "decision_reason": outcome.reason}
            events.append(_event("RETRY_SCHEDULED", outcome.stage, reason=outcome.reason))
        else:
            updates = {"status": target.value, "context": outcome.context, "current_stage": None,
                       "decision": target.value, "decision_reason": outcome.reason}
            events.append(_event("DECISION_MADE", outcome.stage, decision=target.value, reason=outcome.reason))

        events.append(_event("STATE_CHANGED", outcome.stage, from_status=Status.IN_PROGRESS.value,
                             to_status=target.value))
        self.repo.commit_step(
            request_id, version, updates=updates, events=events,
            transition=(Status.IN_PROGRESS.value, target.value, outcome.stage, outcome.reason),
        )

    def _mark_failed(self, request_id: str, stage: str, error: Exception) -> bool:
        """Best effort: move the request to FAILED at its last saved checkpoint.

        The failed transaction was rolled back, so the stored stage is the
        last one that completed. A retry resumes from there.
        """
        try:
            request = self.repo.get(request_id)
            if request is None or request["status"] != Status.IN_PROGRESS.value:
                return False
            message = f"{type(error).__name__}: {getattr(error, 'message', str(error))}"
            self.repo.commit_step(
                request_id, request["version"],
                updates={"status": Status.FAILED.value, "decision_reason": message},
                events=[
                    _event("PROCESSING_FAILED", stage, error=message, resume_from=request["current_stage"]),
                    _event("STATE_CHANGED", stage, from_status=Status.IN_PROGRESS.value, to_status=Status.FAILED.value),
                ],
                transition=(Status.IN_PROGRESS.value, Status.FAILED.value, request["current_stage"], message),
            )
            return True
        except (PersistenceError, PlatformError):
            # Storage is still down. The request stays IN_PROGRESS at its last
            # checkpoint. recover_stuck() or a plain retry picks it up later.
            log.error("could not mark request %s as FAILED", request_id)
            return False
