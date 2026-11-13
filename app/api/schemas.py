"""Request and response shapes of the REST API (also drive the Swagger docs)."""

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class RequestView(BaseModel):
    request_id: str
    workflow: str
    workflow_version: int
    config_fingerprint: str
    status: str
    decision: Optional[str]
    decision_reason: Optional[str]
    current_stage: Optional[str]
    attempt_count: int
    idempotency_key: str
    data: dict[str, Any] = Field(description="input plus results from external services")
    created_at: str
    updated_at: str
    duplicate: bool = False

    @classmethod
    def from_record(cls, record: dict, duplicate: bool = False) -> "RequestView":
        return cls(
            request_id=record["id"],
            workflow=record["workflow_name"],
            workflow_version=record["workflow_version"],
            config_fingerprint=record["config_fingerprint"],
            status=record["status"],
            decision=record["decision"],
            decision_reason=record["decision_reason"],
            current_stage=record["current_stage"],
            attempt_count=record["attempt_count"],
            idempotency_key=record["idempotency_key"],
            data=record["context"],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
            duplicate=duplicate,
        )


class RequestSummary(BaseModel):
    request_id: str
    workflow: str
    workflow_version: int
    status: str
    decision: Optional[str]
    decision_reason: Optional[str]
    current_stage: Optional[str]
    attempt_count: int
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: dict) -> "RequestSummary":
        return cls(request_id=row["id"], workflow=row["workflow_name"], **{
            k: row[k] for k in ("workflow_version", "status", "decision", "decision_reason",
                                "current_stage", "attempt_count", "created_at", "updated_at")
        })


class RequestPage(BaseModel):
    items: list[RequestSummary]
    total: int
    limit: int
    offset: int


class ReviewBody(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    reviewer: str = Field(min_length=1)
    comment: str = ""


class WorkflowSummary(BaseModel):
    name: str
    version: int
    description: str
    stages: list[str]
    fingerprint: str


class ErrorBody(BaseModel):
    error: str
    message: str
    details: Any = None
