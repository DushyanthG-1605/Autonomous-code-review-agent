from datetime import date, datetime, timezone
from typing import Any, Dict, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LegacyEventType = Literal[
    "session.start",
    "session.end",
    "task.started",
    "task.completed",
    "task.blocked",
    "file.edited",
    "command.run",
    "agent.prompt_submitted",
    "git.commit",
    "git.push",
    "git.branch",
]

EventType = Literal[
    "task.started",
    "task.progress",
    "task.blocked",
    "task.completed",
    "git.commit",
    "git.pull_request.opened",
    "git.pull_request.updated",
    "git.pull_request.merged",
    "test.completed",
    "build.completed",
    "session.started",
    "session.ended",
]

EventSource = Literal["skill", "cli", "mcp", "github", "ci"]


class StrictMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EmptyMetadata(StrictMetadata):
    pass


class TaskProgressMetadata(StrictMetadata):
    progress: int = Field(ge=0, le=100, strict=True)
    note: Optional[str] = Field(default=None, max_length=160, strict=True)


class TaskBlockedMetadata(StrictMetadata):
    reason_code: Literal[
        "dependency",
        "permission",
        "test_failure",
        "build_failure",
        "external",
        "other",
    ] = "other"
    note: Optional[str] = Field(default=None, max_length=160, strict=True)


class TaskCompletedMetadata(StrictMetadata):
    outcome: Literal["done", "partial", "abandoned"]
    note: Optional[str] = Field(default=None, max_length=160, strict=True)


class GitCommitMetadata(StrictMetadata):
    commit_sha: str = Field(pattern=r"^(?:[0-9a-fA-F]{7,64})$")
    files_changed: Optional[int] = Field(default=None, ge=0)
    insertions: Optional[int] = Field(default=None, ge=0)
    deletions: Optional[int] = Field(default=None, ge=0)
    repository: Optional[str] = Field(
        default=None, pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
    )
    branch: Optional[str] = Field(
        default=None, max_length=120, pattern=r"^[A-Za-z0-9._/-]+$"
    )
    task_reference: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}-[1-9][0-9]{0,11}$",
    )


class PullRequestMetadata(StrictMetadata):
    pull_request_number: int = Field(gt=0)
    base_branch: str = Field(
        min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._/-]+$"
    )
    head_branch: str = Field(
        min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._/-]+$"
    )
    repository: Optional[str] = Field(
        default=None, pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
    )
    task_reference: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}-[1-9][0-9]{0,11}$",
    )


class PullRequestUpdatedMetadata(PullRequestMetadata):
    pass


class TestCompletedMetadata(StrictMetadata):
    result: Literal["passed", "failed", "cancelled"]
    duration_ms: Optional[int] = Field(default=None, ge=0)
    tests_run: Optional[int] = Field(default=None, ge=0)
    tests_failed: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def failed_tests_do_not_exceed_total(self):
        if (
            self.tests_run is not None
            and self.tests_failed is not None
            and self.tests_failed > self.tests_run
        ):
            raise ValueError("tests_failed cannot exceed tests_run")
        return self


class BuildCompletedMetadata(StrictMetadata):
    result: Literal["passed", "failed", "cancelled"]
    duration_ms: Optional[int] = Field(default=None, ge=0)
    repository: Optional[str] = Field(
        default=None, pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
    )
    branch: Optional[str] = Field(
        default=None, max_length=120, pattern=r"^[A-Za-z0-9._/-]+$"
    )
    workflow_name: Optional[str] = Field(
        default=None, max_length=120, pattern=r"^[A-Za-z0-9_. /-]+$"
    )
    workflow_id: Optional[int] = Field(default=None, gt=0)
    run_id: Optional[int] = Field(default=None, gt=0)
    check_name: Optional[str] = Field(
        default=None, max_length=120, pattern=r"^[A-Za-z0-9_. /-]+$"
    )
    check_run_id: Optional[int] = Field(default=None, gt=0)
    task_reference: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}-[1-9][0-9]{0,11}$",
    )


EVENT_METADATA_MODELS = {
    "task.started": EmptyMetadata,
    "task.progress": TaskProgressMetadata,
    "task.blocked": TaskBlockedMetadata,
    "task.completed": TaskCompletedMetadata,
    "git.commit": GitCommitMetadata,
    "git.pull_request.opened": PullRequestMetadata,
    "git.pull_request.updated": PullRequestUpdatedMetadata,
    "git.pull_request.merged": PullRequestMetadata,
    "test.completed": TestCompletedMetadata,
    "build.completed": BuildCompletedMetadata,
    "session.started": EmptyMetadata,
    "session.ended": EmptyMetadata,
}


class VersionedEvent(BaseModel):
    """Compatibility envelope emitted by collector schema version 1."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    schema_version: Literal[1]
    type: LegacyEventType
    ts: datetime
    session_id: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None
    source: Optional[str] = None
    tool: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class CanonicalEvent(BaseModel):
    """Strict v2 contract; identity and project claims are server-derived."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    schema_version: Literal[2]
    event_type: EventType
    project_id: Optional[UUID] = None
    task_id: Optional[UUID] = None
    actor_id: Optional[UUID] = None
    source: EventSource
    occurred_at: datetime
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value, info):
        event_type = info.data.get("event_type")
        metadata_model = EVENT_METADATA_MODELS.get(event_type)
        if metadata_model is None:
            raise ValueError("Unsupported event type")
        return metadata_model.model_validate(value, strict=True).model_dump(
            exclude_none=True
        )

    @model_validator(mode="after")
    def task_events_require_task_id(self):
        if self.event_type.startswith("task.") and self.task_id is None:
            raise ValueError("task events must reference a task")
        return self


class DevLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_username: str = Field(min_length=1, max_length=120)
    project_id: UUID


class PairingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    scopes: list[Literal["events:read", "events:ingest"]] = Field(
        min_length=1, max_length=2
    )


class PairingClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=30, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    device_label: str = Field(min_length=1, max_length=100)


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    description: Optional[str] = Field(default=None, max_length=2000)
    project_key: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$",
    )


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=5000)
    external_key: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=80,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}-[1-9][0-9]{0,11}$",
    )
    assignee_id: Optional[UUID] = None
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    due_date: Optional[date] = None


class TaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=5000)
    external_key: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=80,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}-[1-9][0-9]{0,11}$",
    )
    assignee_id: Optional[UUID] = None
    status: Optional[
        Literal["todo", "in_progress", "blocked", "in_review", "done"]
    ] = None
    priority: Optional[Literal["low", "medium", "high", "urgent"]] = None
    progress_percent: Optional[int] = Field(
        default=None, ge=0, le=100, strict=True
    )
    due_date: Optional[date] = None
    note: Optional[str] = Field(default=None, max_length=2000)


class SessionActivityData(StrictMetadata):
    tool_version: Optional[str] = None
    duration_s: Optional[float] = None


class TaskActivityData(StrictMetadata):
    summary: Optional[str] = Field(default=None, max_length=200)
    outcome: Optional[Literal["done", "partial", "abandoned"]] = None
    reason: Optional[str] = Field(default=None, max_length=120)


class FileEditedData(StrictMetadata):
    path: Optional[str] = Field(default=None, max_length=240)
    language: Optional[str] = Field(default=None, max_length=40)
    lines_added: Optional[int] = Field(default=None, ge=0)
    lines_removed: Optional[int] = Field(default=None, ge=0)


class CommandRunData(StrictMetadata):
    kind: Optional[Literal["test", "build", "lint", "other"]] = None
    exit_code: Optional[int] = None
    duration_s: Optional[float] = Field(default=None, ge=0, le=86400)


class AgentPromptSubmittedData(StrictMetadata):
    count: Optional[int] = Field(default=None, ge=0)


class GitCommitData(StrictMetadata):
    sha: Optional[str] = Field(default=None, pattern=r"^(?:[0-9a-fA-F]{7,64})$")
    files_changed: Optional[int] = Field(default=None, ge=0)
    lines_added: Optional[int] = Field(default=None, ge=0)
    lines_removed: Optional[int] = Field(default=None, ge=0)


class GitPushData(StrictMetadata):
    commits: Optional[int] = Field(default=None, ge=0)
    remote_branch: Optional[str] = Field(
        default=None, max_length=120, pattern=r"^[A-Za-z0-9._/-]+$"
    )


class GitBranchData(StrictMetadata):
    action: Optional[Literal["created", "switched", "deleted", "updated"]] = None
    name: Optional[str] = Field(
        default=None, max_length=120, pattern=r"^[A-Za-z0-9._/-]+$"
    )


class EventCreate(BaseModel):
    """Shape of an event when it's being submitted to the API."""

    type: str = Field(min_length=1, max_length=80)
    source: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=500)
    repo: Optional[str] = Field(default=None, max_length=200)
    ref: Optional[str] = Field(default=None, max_length=200)
    actor: Optional[str] = Field(default=None, max_length=120)
    execution_id: Optional[str] = Field(default=None, max_length=200)
    status: Optional[str] = Field(default=None, max_length=80)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Event(EventCreate):
    """Shape of an event when it's returned from the API (includes DB fields)."""

    id: int
    timestamp: str


def now_iso() -> str:
    """Current UTC time in ISO format, used as the default timestamp."""
    return datetime.now(timezone.utc).isoformat()
