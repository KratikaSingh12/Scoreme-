"""Name -> service lookup. Workflows refer to services by name only."""

from typing import Iterable

from app.core.errors import PermanentDependencyError
from app.dependencies.base import ExternalService


class DependencyRegistry:
    def __init__(self, services: Iterable[ExternalService] = ()):
        self._services: dict[str, ExternalService] = {}
        for service in services:
            self.register(service)

    def register(self, service: ExternalService) -> None:
        self._services[service.name] = service

    def get(self, name: str) -> ExternalService:
        service = self._services.get(name)
        if service is None:
            raise PermanentDependencyError(f"no external service registered as '{name}'")
        return service

    def names(self) -> set[str]:
        return set(self._services)
