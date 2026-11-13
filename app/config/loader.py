"""Loads workflow YAML files and keeps the active set in memory.

Reload is all-or-nothing: every file is parsed and validated first, and
only when all of them are valid does the active set get swapped. A typo in
one file can never take down the workflows that are already running.
"""

import hashlib
import json
import threading
from pathlib import Path
from typing import Callable, Optional

import yaml
from pydantic import ValidationError

from app.config.models import WorkflowConfig
from app.core.errors import ConfigError, WorkflowNotFound


def parse_workflow(raw: dict, source: str = "<inline>") -> WorkflowConfig:
    try:
        return WorkflowConfig.model_validate(raw)
    except ValidationError as exc:
        errors = [
            {"location": ".".join(str(p) for p in err["loc"]), "error": err["msg"]}
            for err in exc.errors()
        ]
        raise ConfigError(f"invalid workflow config in {source}", details=errors) from exc


def config_fingerprint(config: WorkflowConfig) -> str:
    """Short hash of the config content, stored with each request.

    Default values are left out, so adding a new optional setting to the
    platform does not change the fingerprint of workflows that do not use it.
    """
    body = json.dumps(config.model_dump(mode="json", exclude_defaults=True), sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:12]


# Extra check supplied by the caller (the service uses it to compare against
# versions already stored with requests). Raises ConfigError to reject.
VersionGuard = Callable[[WorkflowConfig, str], None]


class WorkflowRegistry:
    def __init__(self, workflow_dir: str | Path, known_dependencies: Optional[set[str]] = None):
        self._dir = Path(workflow_dir)
        self._known_dependencies = known_dependencies
        self._workflows: dict[str, WorkflowConfig] = {}
        self._lock = threading.Lock()

    def _check_dependencies(self, config: WorkflowConfig, source: str) -> None:
        """A workflow may only call services that actually exist."""
        if self._known_dependencies is None:
            return
        for stage_name, stage in config.stages.items():
            if stage.type == "dependency" and stage.dependency not in self._known_dependencies:
                raise ConfigError(
                    f"{source}: stage '{stage_name}' uses unknown dependency '{stage.dependency}'",
                    details={"known_dependencies": sorted(self._known_dependencies)},
                )

    def _check_version(self, config: WorkflowConfig, fingerprint: str, source: str) -> None:
        """Same name + same version must mean the same content, and versions only go up."""
        current = self._workflows.get(config.name)
        if current is None:
            return
        if config.version < current.version:
            raise ConfigError(
                f"{source}: version {config.version} is lower than the active version {current.version}. "
                "Versions only go up; publish a rollback as a new version."
            )
        if config.version == current.version and fingerprint != config_fingerprint(current):
            raise ConfigError(
                f"{source}: content of '{config.name}' changed but version is still {config.version}. "
                "Bump the version so requests and audits can tell the two apart."
            )

    def load(self, guard: Optional[VersionGuard] = None) -> list[str]:
        """(Re)load every *.yaml file. Returns the names now active."""
        if not self._dir.is_dir():
            raise ConfigError(f"workflow directory not found: {self._dir}")

        loaded: dict[str, WorkflowConfig] = {}
        for path in sorted(self._dir.glob("*.y*ml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise ConfigError(f"YAML syntax error in {path.name}", details=str(exc)) from exc
            if not isinstance(raw, dict):
                raise ConfigError(f"{path.name} must contain a mapping at the top level")
            config = parse_workflow(raw, path.name)
            self._check_dependencies(config, path.name)
            fingerprint = config_fingerprint(config)
            self._check_version(config, fingerprint, path.name)
            if guard:
                guard(config, fingerprint)
            if config.name in loaded:
                raise ConfigError(f"workflow name '{config.name}' is defined twice")
            loaded[config.name] = config

        with self._lock:
            self._workflows = loaded
        return sorted(loaded)

    def register(self, config: WorkflowConfig) -> None:
        """Add or replace one workflow in memory (used by tests and tooling)."""
        self._check_dependencies(config, config.name)
        self._check_version(config, config_fingerprint(config), config.name)
        with self._lock:
            self._workflows = {**self._workflows, config.name: config}

    def get(self, name: str) -> WorkflowConfig:
        workflow = self._workflows.get(name)
        if workflow is None:
            raise WorkflowNotFound(
                f"workflow '{name}' does not exist", details={"available": self.names()}
            )
        return workflow

    def names(self) -> list[str]:
        return sorted(self._workflows)

    def all(self) -> list[WorkflowConfig]:
        return [self._workflows[n] for n in self.names()]
