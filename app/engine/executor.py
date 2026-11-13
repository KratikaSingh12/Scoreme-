"""Runs ONE workflow stage and reports where the request should go next.

The executor is pure logic. It never reads or writes the database. The
service layer calls it stage by stage and saves a checkpoint after each
stage, which is what lets a failed request resume from where it stopped.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable

from app.config.models import StageConfig, WorkflowConfig
from app.core.errors import DependencyTimeout, PermanentDependencyError, RetriesExhausted
from app.core.retry import RetryPolicy, call_with_retry
from app.dependencies.base import ExternalService
from app.dependencies.registry import DependencyRegistry
from app.engine.conditions import MISSING, evaluate, get_path
from app.engine.rules import describe_checks, evaluate_rules, stage_verdict


@dataclass
class StageOutcome:
    stage: str
    next_target: str
    context: dict
    reason: str
    events: list[dict] = field(default_factory=list)


def _event(event_type: str, stage: str, **details) -> dict:
    return {"event_type": event_type, "stage": stage, "details": details}


class WorkflowExecutor:
    def __init__(
        self,
        dependencies: DependencyRegistry,
        sleep: Callable[[float], None] = time.sleep,
        max_parallel_calls: int = 16,
    ):
        self._dependencies = dependencies
        self._sleep = sleep
        # External calls run on this pool so we can stop waiting after a deadline.
        # Its fixed size also acts as a bulkhead: hung services cannot use up
        # every thread of the API.
        self._call_pool = ThreadPoolExecutor(max_workers=max_parallel_calls, thread_name_prefix="dependency")

    def _call_with_deadline(self, service: ExternalService, params: dict, key: str, timeout: float) -> dict:
        """Wait at most `timeout` seconds; a late answer counts as a transient timeout.

        Python cannot kill a running thread, so a call that overruns keeps going
        in the background and its result is thrown away. Real HTTP adapters
        should also set their own socket timeout.
        """
        future = self._call_pool.submit(service.call, params, key)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            future.cancel()
            raise DependencyTimeout(f"{service.name} gave no answer within {timeout}s") from None

    def run_stage(
        self, workflow: WorkflowConfig, stage_name: str, context: dict, request_id: str
    ) -> StageOutcome:
        stage = workflow.stages[stage_name]
        handler = {
            "rules": self._run_rules,
            "branch": self._run_branch,
            "dependency": self._run_dependency,
        }[stage.type]

        started = _event("STAGE_STARTED", stage_name, type=stage.type, description=stage.description)
        outcome = handler(stage_name, stage, context, request_id)
        outcome.events.insert(0, started)
        outcome.events.append(
            _event("STAGE_COMPLETED", stage_name, next=outcome.next_target, reason=outcome.reason)
        )
        return outcome

    # ------------------------------------------------------------------ rules
    def _run_rules(self, name: str, stage: StageConfig, context: dict, _rid: str) -> StageOutcome:
        results = evaluate_rules(stage.rules, context)
        events = [_event("RULE_EVALUATED", name, **r.to_dict()) for r in results]

        deciding = stage_verdict(results)
        if deciding is None:
            flagged = [r.rule_id for r in results if not r.passed]
            reason = f"all blocking rules in '{name}' passed"
            if flagged:
                reason += f" (flagged: {', '.join(flagged)})"
            return StageOutcome(name, stage.on_pass, context, reason, events)

        reason = (
            f"rule {deciding.rule_id} failed: {deciding.description}. "
            f"Data: {describe_checks(deciding.checks)}"
        )
        return StageOutcome(name, deciding.on_fail, context, reason, events)

    # ----------------------------------------------------------------- branch
    def _run_branch(self, name: str, stage: StageConfig, context: dict, _rid: str) -> StageOutcome:
        evaluated = []
        for index, branch in enumerate(stage.branches):
            result = evaluate(branch.when, context)
            evaluated.append(
                {"branch": index, "matched": result.passed, "next": branch.next, "checks": result.checks}
            )
            if result.passed:
                reason = f"branch {index} matched in '{name}': {describe_checks(result.checks)}"
                event = _event("BRANCH_EVALUATED", name, branches=evaluated, chosen=branch.next)
                return StageOutcome(name, branch.next, context, reason, [event])

        reason = f"no branch matched in '{name}', took default"
        event = _event("BRANCH_EVALUATED", name, branches=evaluated, chosen=stage.default)
        return StageOutcome(name, stage.default, context, reason, [event])

    # ------------------------------------------------------------- dependency
    def _run_dependency(self, name: str, stage: StageConfig, context: dict, request_id: str) -> StageOutcome:
        service_name = stage.dependency
        events: list[dict] = []

        params = {param: get_path(context, path) for param, path in stage.input.items()}
        missing = [p for p, v in params.items() if v is MISSING]
        if missing:
            reason = f"cannot call {service_name}: missing input {missing}"
            events.append(_event("DEPENDENCY_CALL_FAILED", name, service=service_name, error=reason, transient=False))
            return StageOutcome(name, stage.on_permanent_failure, context, reason, events)

        # Same key on every attempt and every resume, so the remote side can dedupe.
        idempotency_key = f"{request_id}:{name}"
        policy = RetryPolicy(
            max_attempts=stage.retry.max_attempts,
            base_delay=stage.retry.base_delay,
            max_delay=stage.retry.max_delay,
        )

        def record_attempt(attempt: int, error: Exception | None) -> None:
            events.append(
                _event(
                    "DEPENDENCY_CALL_ATTEMPT",
                    name,
                    service=service_name,
                    attempt=attempt,
                    success=error is None,
                    error=None if error is None else f"{type(error).__name__}: {error}",
                )
            )

        try:
            service = self._dependencies.get(service_name)
            response, attempts = call_with_retry(
                lambda: self._call_with_deadline(service, params, idempotency_key, stage.timeout_seconds),
                policy, record_attempt, self._sleep,
            )
        except RetriesExhausted as exc:
            reason = f"{service_name} still failing after {exc.attempts} attempts: {exc.last_error}"
            events.append(
                _event("DEPENDENCY_CALL_FAILED", name, service=service_name, error=str(exc.last_error),
                       attempts=exc.attempts, transient=True)
            )
            return StageOutcome(name, stage.on_failure, context, reason, events)
        except PermanentDependencyError as exc:
            reason = f"{service_name} returned a permanent error: {exc}"
            events.append(
                _event("DEPENDENCY_CALL_FAILED", name, service=service_name, error=str(exc), transient=False)
            )
            return StageOutcome(name, stage.on_permanent_failure, context, reason, events)

        events.append(
            _event("DEPENDENCY_CALL_SUCCEEDED", name, service=service_name, attempts=attempts,
                   request=params, response=response, stored_as=stage.output_key)
        )
        new_context = {**context, stage.output_key: response}
        reason = f"{service_name} responded after {attempts} attempt(s)"
        return StageOutcome(name, stage.on_success, new_context, reason, events)
