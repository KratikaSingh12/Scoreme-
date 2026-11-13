"""Condition language used by rules and branches.

A condition is plain data (it comes from YAML). Two shapes exist:

Leaf check:
    {field: credit.score, op: gte, value: 600}
    {field: loan_amount, op: lte, value_field: monthly_income, factor: 20}

Combinators:
    {all: [cond, cond]}   every one must pass
    {any: [cond, cond]}   at least one must pass
    {not: cond}           inverts the result

Every leaf evaluation is recorded as a "check" with the actual value read
from the data. That record is what makes decisions explainable later.

The allowed shapes and operator names are defined in
app/config/condition_schema.py; this module only evaluates them.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config.condition_schema import COMPARISON_OPERATORS, PRESENCE_OPERATORS

MISSING = object()


def _compare(fn: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
    """Wrap a comparison so type mismatches count as a failed check, not a crash."""

    def wrapped(actual: Any, expected: Any) -> bool:
        try:
            return bool(fn(actual, expected))
        except TypeError:
            return False

    return wrapped


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": _compare(lambda a, b: a == b),
    "ne": _compare(lambda a, b: a != b),
    "gt": _compare(lambda a, b: a > b),
    "gte": _compare(lambda a, b: a >= b),
    "lt": _compare(lambda a, b: a < b),
    "lte": _compare(lambda a, b: a <= b),
    "in": _compare(lambda a, b: a in b),
    "not_in": _compare(lambda a, b: a not in b),
    "contains": _compare(lambda a, b: b in a),
    "between": _compare(lambda a, b: b[0] <= a <= b[1]),
    "matches": _compare(lambda a, b: re.fullmatch(b, str(a)) is not None),
}

# Every operator the config layer allows must have an implementation here.
if set(OPERATORS) != COMPARISON_OPERATORS:
    raise RuntimeError("operator implementations are out of sync with app/config/condition_schema.py")


@dataclass
class ConditionResult:
    passed: bool
    checks: list[dict] = field(default_factory=list)


def get_path(data: dict, path: str) -> Any:
    """Read a dotted path like 'credit.score'. Returns MISSING if absent."""
    current: Any = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return MISSING
    return current


def _present(value: Any) -> bool:
    return value is not MISSING and value is not None and value != ""


def evaluate(condition: dict, data: dict) -> ConditionResult:
    if "all" in condition:
        parts = [evaluate(c, data) for c in condition["all"]]
        return ConditionResult(all(p.passed for p in parts), _flatten(parts))
    if "any" in condition:
        parts = [evaluate(c, data) for c in condition["any"]]
        return ConditionResult(any(p.passed for p in parts), _flatten(parts))
    if "not" in condition:
        inner = evaluate(condition["not"], data)
        return ConditionResult(not inner.passed, inner.checks)
    return _evaluate_leaf(condition, data)


def _flatten(parts: list[ConditionResult]) -> list[dict]:
    return [check for part in parts for check in part.checks]


def _evaluate_leaf(cond: dict, data: dict) -> ConditionResult:
    path, op = cond["field"], cond["op"]
    actual = get_path(data, path)
    check: dict[str, Any] = {
        "field": path,
        "op": op,
        "actual": None if actual is MISSING else actual,
    }

    if op in PRESENCE_OPERATORS:
        present = _present(actual)
        passed = present if op == "exists" else not present
        check["expected"] = "present" if op == "exists" else "absent"
        check["passed"] = passed
        return ConditionResult(passed, [check])

    expected = _resolve_expected(cond, data)
    check["expected"] = expected

    if actual is MISSING or expected is MISSING:
        check["passed"] = False
        check["note"] = "field missing in data"
        return ConditionResult(False, [check])

    passed = OPERATORS[op](actual, expected)
    check["passed"] = passed
    return ConditionResult(passed, [check])


def _resolve_expected(cond: dict, data: dict) -> Any:
    """Expected value is either a literal or another field times a factor."""
    if "value_field" not in cond:
        return cond.get("value")
    other = get_path(data, cond["value_field"])
    if other is MISSING:
        return MISSING
    factor = cond.get("factor")
    if factor is None:
        return other
    try:
        return other * factor
    except TypeError:
        return MISSING
