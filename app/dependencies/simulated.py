"""Simulated external services: credit bureau, fraud detection, background check.

They behave like real remote services: they can time out, be unavailable,
or reject a request. Failures can be triggered on purpose for demos:

    an input value containing "DOWN"    -> always unavailable (transient)
    an input value containing "FLAKY"   -> times out twice, then succeeds
    an input value containing "INVALID" -> permanent error, no retry helps
    an input value containing "SLOW"    -> answers after slow_seconds (beats the timeout)
    failure_rate > 0                    -> random transient failures

Responses are cached by idempotency key, so a repeated call for the same
request and stage returns the same answer, like an idempotent real API.
"""

import hashlib
import random
import re
import threading
import time
from collections import defaultdict
from typing import Callable, Optional

from app.core.errors import DependencyTimeout, DependencyUnavailable, PermanentDependencyError
from app.dependencies.base import ExternalService


def _stable_number(text: str, modulo: int) -> int:
    """Deterministic pseudo-random number so the same input gives the same result."""
    return int(hashlib.sha256(text.encode()).hexdigest(), 16) % modulo


class SimulatedService(ExternalService):
    def __init__(
        self,
        name: str,
        compute: Callable[[dict], dict],
        failure_rate: float = 0.0,
        flaky_failures: int = 2,
        slow_seconds: float = 5.0,
        rng: Optional[random.Random] = None,
    ):
        self.name = name
        self._compute = compute
        self._failure_rate = failure_rate
        self._flaky_failures = flaky_failures
        self._rng = rng or random.Random()
        self._responses: dict[str, dict] = {}
        self._attempts: dict[str, int] = defaultdict(int)
        self._slow_seconds = slow_seconds
        self._lock = threading.Lock()
        self.call_count = 0  # handy for tests and demos

    def call(self, params: dict, idempotency_key: str) -> dict:
        markers = " ".join(str(v) for v in params.values()).upper()
        if "SLOW" in markers:
            time.sleep(self._slow_seconds)  # outside the lock, so other calls are not blocked

        with self._lock:
            self.call_count += 1
            if idempotency_key in self._responses:
                return self._responses[idempotency_key]

            if "DOWN" in markers:
                raise DependencyUnavailable(f"{self.name} is unavailable (HTTP 503)")
            if "INVALID" in markers:
                raise PermanentDependencyError(f"{self.name} rejected the request (HTTP 400)")
            if "FLAKY" in markers:
                self._attempts[idempotency_key] += 1
                if self._attempts[idempotency_key] <= self._flaky_failures:
                    raise DependencyTimeout(f"{self.name} timed out")
            if self._failure_rate and self._rng.random() < self._failure_rate:
                raise DependencyTimeout(f"{self.name} timed out (random)")

            response = self._compute(params)
            self._responses[idempotency_key] = response
            return response


# --------------------------------------------------------------------------
# Business logic of each fake service
# --------------------------------------------------------------------------


def credit_report(params: dict) -> dict:
    applicant = str(params["applicant_id"])
    # Demo knob: an id like "APP-CS780-01" forces a credit score of 780.
    forced = re.search(r"CS(\d{3})", applicant.upper())
    score = int(forced.group(1)) if forced else 300 + _stable_number(applicant, 601)
    return {
        "score": score,
        "active_loans": _stable_number(applicant + "loans", 3),
        "recent_defaults": 1 if score < 650 else 0,
        "bureau": "SimBureau",
    }


def fraud_score(params: dict) -> dict:
    claim = str(params["claim_id"])
    # Demo knob: "CLM-RISK75-1" forces a risk score of 75.
    forced = re.search(r"RISK(\d{1,2})", claim.upper())
    risk = int(forced.group(1)) if forced else _stable_number(claim, 100)
    return {"risk_score": risk, "model": "fraud-v3"}


def background_report(params: dict) -> dict:
    candidate = str(params["candidate_id"])
    # Demo knob: an id containing "HIT" returns a flagged record.
    status = "flagged" if "HIT" in candidate.upper() else "clear"
    return {"status": status, "checked_sources": ["criminal", "employment", "education"]}


def build_default_services(failure_rate: float = 0.0) -> list[SimulatedService]:
    return [
        SimulatedService("credit_bureau", credit_report, failure_rate),
        SimulatedService("fraud_detection", fraud_score, failure_rate),
        SimulatedService("background_check", background_report, failure_rate),
    ]
