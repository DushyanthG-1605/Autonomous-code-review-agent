from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal[
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


class VersionedEvent(BaseModel):
    """Validated envelope for a versioned activity event."""

    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    schema_version: Literal[1]
    type: EventType
    ts: datetime
    session_id: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None
    source: Optional[str] = None
    tool: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class SessionActivityData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool_version: Optional[str] = None
    duration_s: Optional[float] = None


class TaskActivityData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: Optional[str] = Field(default=None, max_length=200)
    outcome: Optional[str] = None
    reason: Optional[str] = None


class FileEditedData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: Optional[str] = None
    language: Optional[str] = None
    lines_added: Optional[int] = None
    lines_removed: Optional[int] = None


class CommandRunData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Optional[Literal["test", "build", "lint", "other"]] = None
    exit_code: Optional[int] = None
    duration_s: Optional[float] = None


class AgentPromptSubmittedData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    count: Optional[int] = None


class GitCommitData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sha: Optional[str] = None
    files_changed: Optional[int] = None
    lines_added: Optional[int] = None
    lines_removed: Optional[int] = None


class GitPushData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    commits: Optional[int] = None
    remote_branch: Optional[str] = None


class GitBranchData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Optional[str] = None
    name: Optional[str] = None


class EventCreate(BaseModel):
    """Shape of an event when it's being submitted to the API."""
    type: str                      # e.g. "agent.review_completed", "pr.merged"
    source: str                    # e.g. "code-review-agent", "github"
    summary: str                   # e.g. "Reviewed PR #4 — found 2 critical issues"
    repo: Optional[str] = None
    ref: Optional[str] = None      # e.g. "PR #4", commit SHA
    actor: Optional[str] = None    # e.g. "Dushyanth Gadapala"
    execution_id: Optional[str] = None
    status: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Event(EventCreate):
    """Shape of an event when it's returned from the API (includes DB fields)."""
    id: int
    timestamp: str


def now_iso() -> str:
    """Current UTC time in ISO format, used as the default timestamp."""
    return datetime.now(timezone.utc).isoformat()