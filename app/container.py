"""Builds every component once and wires them together.

This is the only place that knows the concrete classes. Tests build their
own container with fake services, a temp database and no retry sleeps.
"""

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from app.config.loader import WorkflowRegistry
from app.core.settings import Settings
from app.dependencies.base import ExternalService
from app.dependencies.registry import DependencyRegistry
from app.dependencies.simulated import build_default_services
from app.engine.executor import WorkflowExecutor
from app.services.decision_service import DecisionService
from app.storage.db import Database
from app.storage.repository import RequestRepository


@dataclass
class Container:
    settings: Settings
    dependencies: DependencyRegistry
    registry: WorkflowRegistry
    repository: RequestRepository
    service: DecisionService

    @classmethod
    def build(
        cls,
        settings: Optional[Settings] = None,
        services: Optional[Iterable[ExternalService]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> "Container":
        settings = settings or Settings.from_env()

        dependencies = DependencyRegistry(
            services if services is not None else build_default_services(settings.dependency_failure_rate)
        )
        registry = WorkflowRegistry(settings.workflow_dir, known_dependencies=dependencies.names())
        repository = RequestRepository(Database(settings.db_path))
        executor = WorkflowExecutor(dependencies, sleep=sleep)
        service = DecisionService(
            registry, repository, executor,
            max_steps=settings.max_steps_per_run,
            stale_after_seconds=settings.stale_after_seconds,
        )
        # Load workflows through the service so version rules are checked
        # against requests already stored. A bad config stops startup loudly.
        service.reload_workflows()
        return cls(settings, dependencies, registry, repository, service)
