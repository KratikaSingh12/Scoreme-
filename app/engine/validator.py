"""Validates an incoming payload against the workflow's input_schema.

Returns a list of field errors instead of stopping at the first one, so
the client can fix everything in one go.
"""

import re
from typing import Any

from app.config.models import FieldSpec

TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    # bool is a subclass of int in Python, so exclude it explicitly
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def validate_payload(payload: Any, schema: dict[str, FieldSpec]) -> list[dict]:
    if not isinstance(payload, dict):
        return [{"field": "<body>", "error": "request body must be a JSON object"}]

    errors: list[dict] = []
    for name, spec in schema.items():
        if name not in payload or payload[name] is None:
            if spec.required:
                errors.append({"field": name, "error": "is required"})
            continue
        errors.extend(_check_field(name, payload[name], spec))
    # Unknown extra fields are allowed on purpose: clients can start sending
    # new fields before the config uses them (tolerates requirement changes).
    return errors


def _check_field(name: str, value: Any, spec: FieldSpec) -> list[dict]:
    if not TYPE_CHECKS[spec.type](value):
        return [{"field": name, "error": f"must be of type {spec.type}", "got": type(value).__name__}]

    errors = []
    if spec.type in ("number", "integer"):
        if spec.min is not None and value < spec.min:
            errors.append({"field": name, "error": f"must be >= {spec.min}", "got": value})
        if spec.max is not None and value > spec.max:
            errors.append({"field": name, "error": f"must be <= {spec.max}", "got": value})
    if spec.type == "string":
        if spec.min_length is not None and len(value) < spec.min_length:
            errors.append({"field": name, "error": f"must have at least {spec.min_length} characters"})
        if spec.max_length is not None and len(value) > spec.max_length:
            errors.append({"field": name, "error": f"must have at most {spec.max_length} characters"})
        if spec.pattern is not None and not re.fullmatch(spec.pattern, value):
            errors.append({"field": name, "error": f"must match pattern {spec.pattern}", "got": value})
    if spec.enum is not None and value not in spec.enum:
        errors.append({"field": name, "error": f"must be one of {spec.enum}", "got": value})
    return errors
