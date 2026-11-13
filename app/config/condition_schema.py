"""Shape of the condition language: which operators exist and how a condition looks.

This is part of the config contract, so it lives in the config layer. The
engine (app/engine/conditions.py) implements each operator listed here and
checks at import time that the two lists match.

    leaf:        {field: credit.score, op: gte, value: 600}
                 {field: loan_amount, op: lte, value_field: monthly_income, factor: 20}
    combinators: {all: [...]}, {any: [...]}, {not: {...}}
"""

from typing import Any

COMPARISON_OPERATORS = frozenset(
    {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "between", "matches"}
)
# Operators that look only at whether the field is present.
PRESENCE_OPERATORS = frozenset({"exists", "not_exists"})
ALL_OPERATORS = COMPARISON_OPERATORS | PRESENCE_OPERATORS


def validate_condition(condition: Any, where: str = "condition") -> None:
    """Check the shape of a condition at config load time.

    Catching a typo here means a broken rule is rejected on reload instead
    of silently failing every request at runtime.
    """
    if not isinstance(condition, dict) or not condition:
        raise ValueError(f"{where}: must be a non-empty mapping")

    for key in ("all", "any"):
        if key in condition:
            items = condition[key]
            if not isinstance(items, list) or not items:
                raise ValueError(f"{where}.{key}: must be a non-empty list")
            for i, item in enumerate(items):
                validate_condition(item, f"{where}.{key}[{i}]")
            return
    if "not" in condition:
        validate_condition(condition["not"], f"{where}.not")
        return

    if "field" not in condition or "op" not in condition:
        raise ValueError(f"{where}: leaf condition needs 'field' and 'op'")
    op = condition["op"]
    if op not in ALL_OPERATORS:
        raise ValueError(f"{where}: unknown operator '{op}'. Allowed: {sorted(ALL_OPERATORS)}")
    if op not in PRESENCE_OPERATORS and "value" not in condition and "value_field" not in condition:
        raise ValueError(f"{where}: operator '{op}' needs 'value' or 'value_field'")
    if op == "between":
        value = condition.get("value")
        if not (isinstance(value, list) and len(value) == 2):
            raise ValueError(f"{where}: 'between' needs value: [low, high]")
    if op in ("in", "not_in") and "value" in condition and not isinstance(condition["value"], list):
        raise ValueError(f"{where}: '{op}' needs a list value")
