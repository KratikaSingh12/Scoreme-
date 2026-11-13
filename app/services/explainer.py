"""Turns the raw audit log into a readable explanation of a decision.

Nothing here is stored separately. The explanation is rebuilt from the
audit log every time, so it can never disagree with what really happened.
"""

from app.engine.rules import describe_checks


def build_explanation(request: dict, audit: list[dict], history: list[dict]) -> dict:
    rules_passed, rules_failed, dependency_calls, narrative = [], [], [], []
    step = 0

    for event in audit:
        kind, stage, d = event["event_type"], event["stage"], event["details"]

        if kind == "STAGE_STARTED":
            step += 1
            narrative.append(f"{step}. Stage '{stage}' ({d['type']}): {d.get('description') or ''}".rstrip(": "))

        elif kind == "RULE_EVALUATED":
            entry = {
                "stage": stage,
                "rule_id": d["rule_id"],
                "description": d["description"],
                "result": "PASS" if d["passed"] else "FAIL",
                "action_taken": d["action_taken"],
                "data_used": [
                    {"field": c["field"], "value": c["actual"], "operator": c["op"], "expected": c["expected"]}
                    for c in d["checks"]
                ],
            }
            (rules_passed if d["passed"] else rules_failed).append(entry)
            mark = "PASS" if d["passed"] else f"FAIL -> {d['action_taken']}"
            narrative.append(f"   - {d['rule_id']} {mark}: {d['description']} [{describe_checks(d['checks'])}]")

        elif kind == "BRANCH_EVALUATED":
            narrative.append(f"   - branch decision: go to {d['chosen']}")

        elif kind == "DEPENDENCY_CALL_ATTEMPT":
            status = "ok" if d["success"] else f"failed ({d['error']})"
            narrative.append(f"   - call {d['service']} attempt {d['attempt']}: {status}")

        elif kind == "DEPENDENCY_CALL_SUCCEEDED":
            dependency_calls.append({"stage": stage, "service": d["service"], "attempts": d["attempts"],
                                     "response": d["response"], "outcome": "success"})

        elif kind == "DEPENDENCY_CALL_FAILED":
            dependency_calls.append({"stage": stage, "service": d["service"], "attempts": d.get("attempts", 1),
                                     "error": d["error"], "outcome": "failed"})

        elif kind == "DECISION_MADE":
            narrative.append(f"Decision: {d['decision']} because {d['reason']}")

        elif kind == "RETRY_SCHEDULED":
            narrative.append(f"Paused for retry: {d['reason']}")

        elif kind == "MANUAL_REVIEW_RESOLVED":
            narrative.append(f"Manual review by {d['reviewer']}: {d['decision']} ({d.get('comment') or 'no comment'})")

        elif kind == "PROCESSING_FAILED":
            narrative.append(f"Processing failed: {d['error']} (will resume from '{d['resume_from']}')")

    return {
        "request_id": request["id"],
        "workflow": request["workflow_name"],
        "workflow_version": request["workflow_version"],
        "config_fingerprint": request["config_fingerprint"],
        "input": request["payload"],
        "status": request["status"],
        "decision": request["decision"],
        "summary": request["decision_reason"],
        "rules_triggered": rules_failed,
        "rules_passed": rules_passed,
        "dependency_calls": dependency_calls,
        "lifecycle": [f"{h['from_status'] or 'NEW'} -> {h['to_status']}" for h in history],
        "narrative": narrative,
        "audit_reference": f"/requests/{request['id']}/audit",
    }
