# Configuration Model

Every workflow is one YAML file in `config/workflows/`. The platform reads
all of them at startup and again on `POST /workflows/reload`.

## 1. File layout

```yaml
name: loan_approval          # unique id, used in the URL
version: 1                   # bump on every change; stored with each request
description: Personal loan application approval

input_schema:                # what a valid request looks like
  age: {type: integer, min: 0, max: 120}
  ...

start_stage: eligibility     # first stage to run

stages:                      # named stages, each of one type
  eligibility: {type: rules, ...}
  credit_check: {type: dependency, ...}
  amount_routing: {type: branch, ...}
```

## 2. `input_schema`

| Key | Applies to | Meaning |
|---|---|---|
| `type` | all | `string`, `number`, `integer`, `boolean`, `object`, `array` |
| `required` | all | default `true` |
| `min`, `max` | number, integer | inclusive bounds |
| `min_length`, `max_length` | string | length bounds |
| `enum` | all | allowed values |
| `pattern` | string | full-match regular expression |
| `description` | all | documentation only |

All errors are reported together. Fields not listed in the schema are accepted.

## 3. Stage types

### `rules`: checks that must hold

```yaml
eligibility:
  type: rules
  description: Mandatory eligibility checks
  rules:
    - id: ELIG-001                       # unique across the workflow
      description: Applicant must be at least 21 years old
      condition: {field: age, op: gte, value: 21}
      on_fail: REJECTED                  # REJECTED | MANUAL_REVIEW | FLAG
  on_pass: credit_check                  # where to go if no blocking rule failed
```

Every rule is evaluated, even after one fails. The stage verdict is the
strongest failed action: `REJECTED` beats `MANUAL_REVIEW`. `FLAG` is recorded
in the audit trail and decision reason but does not stop the workflow.

### `branch`: conditional routing

```yaml
amount_routing:
  type: branch
  branches:                              # checked in order, first match wins
    - when: {field: loan_amount, op: gt, value: 1000000}
      next: MANUAL_REVIEW
  default: standard_checks               # when nothing matches
```

### `dependency`: call an external service

```yaml
credit_check:
  type: dependency
  dependency: credit_bureau              # name registered in app/container.py
  input: {applicant_id: applicant_id}    # service param -> field path in request data
  output_key: credit                     # response is stored as data.credit
  retry: {max_attempts: 3, base_delay: 0.2, max_delay: 1.0}
  timeout_seconds: 2.0                   # deadline per attempt (default 2, max 60)
  on_success: risk_assessment
  on_failure: RETRY_PENDING              # transient errors after all retries (default)
  on_permanent_failure: MANUAL_REVIEW    # errors retrying cannot fix (default)
```

Later stages can read the response, for example `credit.score`.

An attempt that misses its deadline counts as a transient timeout and is
retried like any other timeout.

### Targets

Any `on_pass`, `next`, `default`, `on_success`, `on_failure` or
`on_permanent_failure` is either another stage name or one of:
`APPROVED`, `REJECTED`, `MANUAL_REVIEW`, `RETRY_PENDING`.

## 4. Condition language

Leaf check:

```yaml
{field: credit.score, op: gte, value: 600}                         # literal
{field: loan_amount, op: lte, value_field: monthly_income, factor: 20}  # other field x factor
{field: documents.id_proof, op: exists}                            # presence
```

| Operator | Meaning | `value` |
|---|---|---|
| `eq`, `ne` | equal, not equal | any |
| `gt`, `gte`, `lt`, `lte` | comparisons | number or string |
| `in`, `not_in` | membership | list |
| `contains` | field (list or string) contains value | any |
| `between` | `low <= field <= high` | `[low, high]` |
| `matches` | regular expression full match | pattern |
| `exists`, `not_exists` | field present and not empty | none |

Combinators can be nested to any depth:

```yaml
condition:
  all:
    - {field: age, op: gte, value: 21}
    - any:
        - {field: credit.score, op: gte, value: 750}
        - {field: employment_type, op: eq, value: salaried}
    - not: {field: country, op: in, value: [XX, YY]}
```

A missing field or a type mismatch makes a check fail (it never crashes),
and the audit entry notes why.

## 5. Version rules

* **Same version, same content.** If you change anything in a workflow,
  bump `version`. Reload refuses an edited file whose version did not change.
* **Versions only go up.** To roll back, copy the old rules and give them a
  new, higher version number.
* Both rules are checked against the running config and against versions
  already stored with requests, so they also hold after a restart.
* Writing a default value explicitly (for example `timeout_seconds: 2.0`)
  does not count as a change.

Every request records the version and a content fingerprint of the rules it
ran under, visible in `GET /requests/{id}` and `GET /workflows`.

## 6. What is checked on load

A config is rejected, and the running one kept, when it has:

* unknown keys (for example `on_sucess`)
* an unknown operator or a malformed condition
* a target that is neither a stage nor a terminal outcome
* a `start_stage` that does not exist
* duplicate rule ids
* a cycle between stages
* a dependency name no service is registered under
* a stage missing the fields its type needs
* a `timeout_seconds` that is zero, negative or above 60
* a version that breaks the version rules above

## 7. Common tasks

**Change a threshold.** Edit the value, bump `version`, then
`POST /workflows/reload` (with the `X-Admin-Key` header if a key is set). New requests use it right away. Requests already
in progress finish under the version they started with.

**Add a new workflow.** Create `config/workflows/<name>.yaml`, reload, and
submit to `POST /workflows/<name>/requests`. No code needed. Example:

```yaml
name: vendor_approval
version: 1
input_schema:
  vendor_id: {type: string}
  gst_registered: {type: boolean}
  annual_turnover: {type: number, min: 0}
start_stage: compliance
stages:
  compliance:
    type: rules
    rules:
      - id: VEN-001
        description: Vendor must be GST registered
        condition: {field: gst_registered, op: eq, value: true}
        on_fail: REJECTED
    on_pass: size_check
  size_check:
    type: branch
    branches:
      - when: {field: annual_turnover, op: gte, value: 10000000}
        next: APPROVED
    default: MANUAL_REVIEW
```

**Add a new external service.** Implement `ExternalService.call(params,
idempotency_key)` in `app/dependencies/`, raising `TransientDependencyError`
or `PermanentDependencyError` on failure, and register it in
`app/container.py`. Workflows can then refer to it by name.

**Add a new operator.** Add its name to `app/config/condition_schema.py`
and its implementation to `OPERATORS` in `app/engine/conditions.py`. The
app refuses to start if the two lists do not match.
