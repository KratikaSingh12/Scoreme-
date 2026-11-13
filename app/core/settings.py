"""Runtime settings, read from environment variables with safe defaults."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    db_path: str = str(PROJECT_ROOT / "data" / "decisions.db")
    workflow_dir: str = str(PROJECT_ROOT / "config" / "workflows")
    # Chance (0..1) that a simulated external call fails with a transient error.
    dependency_failure_rate: float = 0.0
    # Max stages one run may execute. Guards against bad configs looping.
    max_steps_per_run: int = 50
    # IN_PROGRESS with no saved progress for this long counts as stuck.
    stale_after_seconds: float = 60.0
    # Key required in the X-Admin-Key header for admin actions. None = open dev mode.
    admin_api_key: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        return cls(
            db_path=os.getenv("DECISION_DB_PATH", defaults.db_path),
            workflow_dir=os.getenv("DECISION_WORKFLOW_DIR", defaults.workflow_dir),
            dependency_failure_rate=float(
                os.getenv("DECISION_DEPENDENCY_FAILURE_RATE", defaults.dependency_failure_rate)
            ),
            max_steps_per_run=int(os.getenv("DECISION_MAX_STEPS", defaults.max_steps_per_run)),
            stale_after_seconds=float(os.getenv("DECISION_STALE_AFTER_SECONDS", defaults.stale_after_seconds)),
            admin_api_key=os.getenv("DECISION_ADMIN_API_KEY") or None,
        )
