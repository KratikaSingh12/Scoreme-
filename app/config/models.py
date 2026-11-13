"""Typed model of a workflow configuration file.

YAML is parsed into these models, so a bad config fails loudly at load
time with a clear message instead of failing requests at runtime.
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.states import TERMINAL_TARGETS
from app.config.condition_schema import validate_condition

FieldType = Literal["string", "number", "integer", "boolean", "object", "array"]
RuleAction = Literal["REJECTED", "MANUAL_REVIEW", "FLAG"]


class StrictModel(BaseModel):
    # Unknown keys in YAML are an error. Catches typos like 'on_sucess'.
    model_config = ConfigDict(extra="forbid")


class FieldSpec(StrictModel):
    type: FieldType
    required: bool = True
    min: Optional[float] = None
    max: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    enum: Optional[list[Any]] = None
    pattern: Optional[str] = None
    description: str = ""


class RetryConfig(StrictModel):
    max_attempts: int = Field(3, ge=1, le=10)
    base_delay: float = Field(0.2, ge=0)
    max_delay: float = Field(2.0, ge=0)


class RuleConfig(StrictModel):
    id: str
    description: str
    condition: dict[str, Any]
    # What happens when the condition is NOT met.
    on_fail: RuleAction = "REJECTED"

    @field_validator("condition")
    @classmethod
    def _check_condition(cls, value: dict) -> dict:
        validate_condition(value)
        return value


class BranchConfig(StrictModel):
    when: dict[str, Any]
    next: str

    @field_validator("when")
    @classmethod
    def _check_when(cls, value: dict) -> dict:
        validate_condition(value, "when")
        return value


class StageConfig(StrictModel):
    type: Literal["rules", "branch", "dependency"]
    description: str = ""

    # type: rules
    rules: list[RuleConfig] = Field(default_factory=list)
    on_pass: Optional[str] = None

    # type: branch
    branches: list[BranchConfig] = Field(default_factory=list)
    default: Optional[str] = None

    # type: dependency
    dependency: Optional[str] = None
    input: dict[str, str] = Field(default_factory=dict)  # service param -> data path
    output_key: Optional[str] = None
    retry: RetryConfig = Field(default_factory=RetryConfig)
    timeout_seconds: float = Field(2.0, gt=0, le=60)  # per attempt
    on_success: Optional[str] = None
    on_failure: str = "RETRY_PENDING"  # transient errors, retries used up
    on_permanent_failure: str = "MANUAL_REVIEW"  # errors that retrying cannot fix

    @model_validator(mode="after")
    def _check_fields_for_type(self) -> "StageConfig":
        required = {
            "rules": ["on_pass"],
            "branch": ["default"],
            "dependency": ["dependency", "output_key", "on_success"],
        }[self.type]
        missing = [name for name in required if not getattr(self, name)]
        if missing:
            raise ValueError(f"stage of type '{self.type}' needs: {', '.join(missing)}")
        if self.type == "rules" and not self.rules:
            raise ValueError("stage of type 'rules' needs at least one rule")
        if self.type == "branch" and not self.branches:
            raise ValueError("stage of type 'branch' needs at least one branch")
        return self

    def targets(self) -> list[str]:
        """Every place this stage can send the request next."""
        if self.type == "rules":
            return [self.on_pass]
        if self.type == "branch":
            return [b.next for b in self.branches] + [self.default]
        return [self.on_success, self.on_failure, self.on_permanent_failure]


class WorkflowConfig(StrictModel):
    name: str
    version: int = Field(ge=1)
    description: str = ""
    input_schema: dict[str, FieldSpec]
    start_stage: str
    stages: dict[str, StageConfig]

    @model_validator(mode="after")
    def _check_graph(self) -> "WorkflowConfig":
        if self.start_stage not in self.stages:
            raise ValueError(f"start_stage '{self.start_stage}' is not a defined stage")

        for name, stage in self.stages.items():
            for target in stage.targets():
                if target not in self.stages and target not in TERMINAL_TARGETS:
                    raise ValueError(
                        f"stage '{name}' points to unknown target '{target}'. "
                        f"Use a stage name or one of {sorted(TERMINAL_TARGETS)}"
                    )

        rule_ids = [r.id for s in self.stages.values() for r in s.rules]
        duplicates = {rid for rid in rule_ids if rule_ids.count(rid) > 1}
        if duplicates:
            raise ValueError(f"duplicate rule ids: {sorted(duplicates)}")

        self._check_no_cycles()
        return self

    def _check_no_cycles(self) -> None:
        """A loop between stages would run forever, so reject it on load."""
        visiting, done = set(), set()

        def visit(stage_name: str, path: list[str]) -> None:
            if stage_name in TERMINAL_TARGETS or stage_name in done:
                return
            if stage_name in visiting:
                cycle = " -> ".join(path + [stage_name])
                raise ValueError(f"workflow has a cycle: {cycle}")
            visiting.add(stage_name)
            for target in self.stages[stage_name].targets():
                visit(target, path + [stage_name])
            visiting.discard(stage_name)
            done.add(stage_name)

        visit(self.start_stage, [])
