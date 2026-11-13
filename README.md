# Resilient Decision Platform

A configurable workflow decision engine. You send it a request (a loan
application, an insurance claim, a new joiner). It validates the input,
runs it through the stages defined in a YAML file, calls external services
with deadlines and retries, and returns a decision you can fully explain
later: `APPROVED`, `REJECTED`, `MANUAL_REVIEW` or `RETRY_PENDING`.

Workflows and rules live in `config/workflows/*.yaml`. Changing a threshold
or adding a whole new workflow needs no Python changes.

| Document | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | design, components, data flow, trade-offs, assumptions, scaling |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | full YAML reference, version rules, how to add or change workflows |
| [docs/DECISION_EXAMPLES.md](docs/DECISION_EXAMPLES.md) | input, rules triggered, output and audit reasoning for real runs |
| [CLAUDE_INSTRUCTIONS.md](CLAUDE_INSTRUCTIONS.md) | build roadmap and checklist |

---

## 1. Setup

Requires Python 3.11 or newer.

```bash
cd decision-platform
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

**Copying the project to another computer:** copy the whole
`decision-platform` folder but leave out `.venv` and `data` if they exist
(a virtual environment only works on the machine that created it, and `data`
holds your local database). On the new machine, run the setup steps above
once. Everything else, including the database, is created automatically.

## 2. Run the API

```bash
uvicorn app.main:create_app --factory --reload
```

Open **http://127.0.0.1:8000/docs** for interactive Swagger docs where you
can try every endpoint from the browser.

To protect the admin endpoints, set a key before starting:

```bash
# Windows PowerShell
$env:DECISION_ADMIN_API_KEY="choose-a-secret"
# macOS / Linux
export DECISION_ADMIN_API_KEY="choose-a-secret"
```

Without a key the app runs in open dev mode and logs a warning.

All settings (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `DECISION_ADMIN_API_KEY` | not set | key required in the `X-Admin-Key` header for reload, review and recover |
| `DECISION_DB_PATH` | `data/decisions.db` | SQLite file |
| `DECISION_WORKFLOW_DIR` | `config/workflows` | folder with workflow YAML files |
| `DECISION_STALE_AFTER_SECONDS` | `60` | an `IN_PROGRESS` request with no progress for this long counts as stuck |
| `DECISION_DEPENDENCY_FAILURE_RATE` | `0.0` | chance (0 to 1) that a simulated external call times out |
| `DECISION_MAX_STEPS` | `50` | safety cap on stages per run |

Docker alternative:

```bash
docker build -t decision-platform .
docker run -p 8000:8000 -e DECISION_ADMIN_API_KEY=choose-a-secret decision-platform
```

## 3. Run the tests

```bash
pytest
```

153 tests, about 8 seconds.

| Scenario | Test file |
|---|---|
| Happy path | `tests/test_happy_path.py` |
| Invalid input | `tests/test_invalid_input.py` |
| Duplicate requests | `tests/test_duplicates.py` |
| Dependency failure | `tests/test_dependency_failure.py` |
| Retry flow | `tests/test_retry_flow.py` |
| Rule change | `tests/test_rule_change.py` |
| Partial save failure, stuck request recovery, optimistic locking | `tests/test_partial_save.py` |
| Manual review | `tests/test_manual_review.py` |
| Review queue listing | `tests/test_review_queue.py` |
| Admin key protection | `tests/test_security.py` |
| Version rules (also across restarts) | `tests/test_versioning.py` |
| Call deadlines | `tests/test_timeouts.py` |
| Layer rules enforced automatically | `tests/test_architecture.py` |
| Rule language and config validation (unit) | `tests/test_conditions.py`, `tests/test_config_validation.py` |

Regenerate the decision examples doc from real runs:

```bash
python scripts/generate_examples.py
```

---

## 4. API

| Method | Path | Admin key | Purpose |
|---|---|---|---|
| GET | `/health` | | liveness, workflows, services, protection mode |
| GET | `/workflows` | | list workflows with version and fingerprint |
| GET | `/workflows/{name}` | | full workflow config |
| POST | `/workflows/reload` | yes | reload YAML without restart (all or nothing) |
| POST | `/workflows/{name}/requests` | | submit a request, optional `Idempotency-Key` header |
| GET | `/requests` | | list requests oldest first; `?status=MANUAL_REVIEW` is the review queue; `workflow`, `limit`, `offset` |
| GET | `/requests/{id}` | | current status, decision and data |
| GET | `/requests/{id}/history` | | every status change |
| GET | `/requests/{id}/audit` | | full audit log |
| GET | `/requests/{id}/explanation` | | readable reasoning behind the decision |
| POST | `/requests/{id}/retry` | | resume a `RETRY_PENDING`, `FAILED` or stuck `IN_PROGRESS` request |
| POST | `/requests/{id}/review` | yes | resolve a `MANUAL_REVIEW` request |
| POST | `/admin/recover` | yes | resume every stuck request (also runs on startup) |

Error responses always look like this:

```json
{"error": "invalid_input", "message": "request payload failed validation", "details": [...]}
```

| HTTP | When |
|---|---|
| 201 | new request processed |
| 200 | duplicate request, stored result returned (`Idempotent-Replayed: true`) |
| 400 | workflow config invalid or breaks version rules |
| 401 | admin endpoint called without the right `X-Admin-Key` |
| 404 | unknown workflow or request |
| 409 | idempotency key reused with different data, wrong state, or concurrent change |
| 422 | invalid input or query parameters |
| 503 | storage failure, request saved as `FAILED` at its last checkpoint |

---

## 5. Demo walkthrough

Easiest on Windows: use the Swagger page at `/docs`. For admin endpoints,
click a lock icon or add the `X-Admin-Key` header.

The simulated services have demo switches inside the ids:

| Switch | Effect |
|---|---|
| `CS780` in an applicant id | credit score 780 |
| `RISK75` in a claim id | fraud score 75 |
| `FLAKY` | times out twice, then works |
| `SLOW` | answers after 5 s, so the 2 s deadline cuts it off |
| `DOWN` | always unavailable |
| `INVALID` | permanent error, not retried |

**Approved loan, then a duplicate**

```bash
curl -X POST localhost:8000/workflows/loan_approval/requests \
  -H "Content-Type: application/json" -H "Idempotency-Key: demo-1" \
  -d '{"applicant_id":"APP-CS780-001","applicant_name":"Asha Verma","age":32,
       "monthly_income":90000,"loan_amount":300000,"employment_type":"salaried",
       "documents":{"id_proof":"aadhaar-123"}}'
```

Send it again: HTTP 200, same `request_id`, `"duplicate": true`. Nothing is processed twice.

**Why was it decided this way?**

```bash
curl localhost:8000/requests/<request_id>/explanation
```

**Dependency down, then retry**

Submit with `"applicant_id":"APP-DOWN-01"`. The result is `RETRY_PENDING`
at stage `credit_check`. Then:

```bash
curl -X POST localhost:8000/requests/<request_id>/retry
```

**Slow dependency cut off by the deadline**

Submit with `"applicant_id":"APP-SLOW-01"`. Each attempt is stopped after
2 s and the request ends in `RETRY_PENDING`. The audit shows
`gave no answer within 2.0s` for every attempt.

**Review queue**

Submit without `id_proof` in `documents`, then:

```bash
curl "localhost:8000/requests?status=MANUAL_REVIEW"

curl -X POST localhost:8000/requests/<request_id>/review \
  -H "Content-Type: application/json" -H "X-Admin-Key: choose-a-secret" \
  -d '{"decision":"APPROVED","reviewer":"ops.lead","comment":"verified offline"}'
```

**Change a rule live**

1. In `config/workflows/loan_approval.yaml` set `RISK-001` value to `800` **and** `version: 2`.
2. `curl -X POST localhost:8000/workflows/reload -H "X-Admin-Key: choose-a-secret"`
3. New requests use v2. Requests already in progress keep v1.

If you change the value but forget the version bump, reload refuses with
400 and the running rules stay as they were.

---

## 6. Project structure

```
decision-platform/
├── app/
│   ├── main.py             FastAPI app factory, error handler, startup recovery
│   ├── container.py        wires all components together
│   ├── api/                REST routes, response schemas, admin key check
│   ├── services/           decision service (orchestration) and explainer
│   ├── engine/             validator, condition evaluator, rules, stage executor
│   ├── config/             workflow config models, condition schema, YAML loader
│   ├── storage/            SQLite schema and repository
│   ├── dependencies/       external service interface and simulators
│   └── core/               errors, lifecycle states, retry policy, settings
├── config/workflows/       loan_approval, claim_processing, employee_onboarding
├── tests/                  153 tests
├── scripts/                generate_examples.py
└── docs/                   architecture, configuration, decision examples
```

Dependencies point one way: `api -> services -> (engine | storage | config | dependencies) -> core`.
`tests/test_architecture.py` fails the build if any module breaks this.
