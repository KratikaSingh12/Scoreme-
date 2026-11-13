"""REST endpoints. Thin on purpose: parse HTTP, call the service, shape the reply.

Routes only talk to the DecisionService (plus the registry and dependency
list for read-only system info). They never touch storage directly.
"""

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response, status

from app.api.schemas import RequestPage, RequestSummary, RequestView, ReviewBody, WorkflowSummary
from app.api.security import require_admin
from app.config.loader import config_fingerprint
from app.container import Container

router = APIRouter()
admin_only = [Depends(require_admin)]


def get_container(request: Request) -> Container:
    return request.app.state.container


# ------------------------------------------------------------------ health
@router.get("/health", tags=["system"])
def health(c: Container = Depends(get_container)) -> dict:
    return {
        "status": "ok",
        "workflows": c.registry.names(),
        "dependencies": sorted(c.dependencies.names()),
        "admin_protection": c.settings.admin_api_key is not None,
    }


# --------------------------------------------------------------- workflows
@router.get("/workflows", response_model=list[WorkflowSummary], tags=["workflows"])
def list_workflows(c: Container = Depends(get_container)):
    return [
        WorkflowSummary(name=w.name, version=w.version, description=w.description,
                        stages=list(w.stages), fingerprint=config_fingerprint(w))
        for w in c.registry.all()
    ]


@router.get("/workflows/{name}", tags=["workflows"])
def get_workflow(name: str, c: Container = Depends(get_container)) -> dict:
    workflow = c.registry.get(name)
    return {**workflow.model_dump(mode="json"), "fingerprint": config_fingerprint(workflow)}


@router.post("/workflows/reload", tags=["admin"], dependencies=admin_only)
def reload_workflows(c: Container = Depends(get_container)) -> dict:
    """Re-read YAML files. If any file is invalid, nothing changes and 400 is returned."""
    return {
        "reloaded": [
            {"name": w.name, "version": w.version, "fingerprint": config_fingerprint(w)}
            for w in c.service.reload_workflows()
        ]
    }


# ---------------------------------------------------------------- requests
@router.post(
    "/workflows/{name}/requests",
    response_model=RequestView,
    status_code=status.HTTP_201_CREATED,
    tags=["requests"],
)
def submit_request(
    name: str,
    response: Response,
    payload: Any = Body(..., examples=[{
        "applicant_id": "APP-CS780-001", "applicant_name": "Asha Verma", "age": 30,
        "monthly_income": 90000, "loan_amount": 300000, "employment_type": "salaried",
        "documents": {"id_proof": "doc-123"},
    }]),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    c: Container = Depends(get_container),
):
    result = c.service.submit(name, payload, idempotency_key)
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    return RequestView.from_record(result.request, duplicate=result.duplicate)


@router.get("/requests", response_model=RequestPage, tags=["requests"])
def list_requests(
    status_filter: Optional[str] = Query(None, alias="status", description="e.g. MANUAL_REVIEW for the review queue"),
    workflow: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    c: Container = Depends(get_container),
):
    """List requests, oldest first. `?status=MANUAL_REVIEW` is the reviewer's queue."""
    page = c.service.list_requests(status_filter, workflow, limit, offset)
    return RequestPage(**{**page, "items": [RequestSummary.from_row(row) for row in page["items"]]})


@router.get("/requests/{request_id}", response_model=RequestView, tags=["requests"])
def get_request(request_id: str, c: Container = Depends(get_container)):
    return RequestView.from_record(c.service.get(request_id))


@router.get("/requests/{request_id}/history", tags=["requests"])
def get_history(request_id: str, c: Container = Depends(get_container)) -> list[dict]:
    return c.service.history(request_id)


@router.get("/requests/{request_id}/audit", tags=["requests"])
def get_audit(request_id: str, c: Container = Depends(get_container)) -> list[dict]:
    return c.service.audit(request_id)


@router.get("/requests/{request_id}/explanation", tags=["requests"])
def get_explanation(request_id: str, c: Container = Depends(get_container)) -> dict:
    return c.service.explain(request_id)


@router.post("/requests/{request_id}/retry", response_model=RequestView, tags=["requests"])
def retry_request(request_id: str, c: Container = Depends(get_container)):
    return RequestView.from_record(c.service.retry(request_id))


@router.post("/requests/{request_id}/review", response_model=RequestView, tags=["admin"], dependencies=admin_only)
def review_request(request_id: str, body: ReviewBody, c: Container = Depends(get_container)):
    return RequestView.from_record(c.service.review(request_id, body.decision, body.reviewer, body.comment))


# ------------------------------------------------------------------- admin
@router.post("/admin/recover", tags=["admin"], dependencies=admin_only)
def recover_stuck_requests(c: Container = Depends(get_container)) -> dict:
    """Resume every request stuck IN_PROGRESS longer than the lease time."""
    return {"recovered": c.service.recover_stuck()}
