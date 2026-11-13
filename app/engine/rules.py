"""Evaluates the rules of one stage and decides the stage verdict.

All rules are evaluated even after one fails. That costs a little time but
gives reviewers the complete picture (every failed rule, not just the first).
"""

from dataclasses import asdict, dataclass
from typing import Optional

from app.config.models import RuleConfig
from app.engine.conditions import evaluate

# Higher number wins when several rules fail in the same stage.
SEVERITY = {"REJECTED": 2, "MANUAL_REVIEW": 1, "FLAG": 0}


@dataclass
class RuleResult:
    rule_id: str
    description: str
    passed: bool
    on_fail: str
    checks: list[dict]

    @property
    def action_taken(self) -> str:
        return "NONE" if self.passed else self.on_fail

    def to_dict(self) -> dict:
        data = asdict(self)
        data["action_taken"] = self.action_taken
        return data


def evaluate_rules(rules: list[RuleConfig], data: dict) -> list[RuleResult]:
    results = []
    for rule in rules:
        outcome = evaluate(rule.condition, data)
        results.append(
            RuleResult(
                rule_id=rule.id,
                description=rule.description,
                passed=outcome.passed,
                on_fail=rule.on_fail,
                checks=outcome.checks,
            )
        )
    return results


def stage_verdict(results: list[RuleResult]) -> Optional[RuleResult]:
    """Return the failed rule that decides the stage, or None if the stage passes.

    FLAG failures are recorded but never stop the workflow.
    """
    blocking = [r for r in results if not r.passed and r.on_fail != "FLAG"]
    if not blocking:
        return None
    return max(blocking, key=lambda r: SEVERITY[r.on_fail])


def describe_checks(checks: list[dict]) -> str:
    """Short text like 'credit.score=550 (expected gte 600)'."""
    parts = []
    for c in checks:
        if c["op"] in ("exists", "not_exists"):
            parts.append(f"{c['field']}={c['actual']!r} (expected {c['expected']})")
        else:
            parts.append(f"{c['field']}={c['actual']!r} (expected {c['op']} {c['expected']!r})")
    return "; ".join(parts)
