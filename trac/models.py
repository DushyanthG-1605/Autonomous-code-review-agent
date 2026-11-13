from pydantic import BaseModel
from typing import Any, Dict, Optional
from datetime import datetime, timezone
from pydantic import Field


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