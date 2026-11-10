"""Settings intentionally fail closed without a strong local service token."""

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    token: str
    state_dir: Path
    model: str = "deepseek-v4-flash"
    provider: str = "deepseek-official"
    profile: str = "sdk-minimal"
    task_timeout_seconds: int = 900
    max_queued_tasks: int = 32

    def __post_init__(self) -> None:
        if len(self.token) < 32 or self.token.startswith("replace-"):
            raise ValueError("HARNESS_API_TOKEN must be a random secret of at least 32 characters")
        if not 30 <= self.task_timeout_seconds <= 86400:
            raise ValueError("HARNESS_TASK_TIMEOUT_SECONDS must be between 30 and 86400")
        if type(self.max_queued_tasks) is not int or not 1 <= self.max_queued_tasks <= 1000:
            raise ValueError("HARNESS_MAX_QUEUED_TASKS must be between 1 and 1000")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            token=os.environ.get("HARNESS_API_TOKEN", ""),
            state_dir=Path(os.environ.get("HARNESS_STATE_DIR", ".local/state")).resolve(),
            model=os.environ.get("HARNESS_MODEL", "deepseek-v4-flash"),
            provider=os.environ.get("HARNESS_PROVIDER", "deepseek-official"),
            profile=os.environ.get("HARNESS_PROFILE", "sdk-minimal"),
            task_timeout_seconds=int(os.environ.get("HARNESS_TASK_TIMEOUT_SECONDS", "900")),
            max_queued_tasks=int(os.environ.get("HARNESS_MAX_QUEUED_TASKS", "32")),
        )
