from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4


class RunState(StrEnum):
    CREATED = "created"
    UNDERSTANDING = "understanding"
    EXPLORING = "exploring"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    CREATING_DRAFT_PR = "creating_draft_pr"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class Event:
    state: RunState
    message: str
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Secondary lines — which model was asked, how it answered — shown quietly beneath the
    # state they belong to rather than as a transition of their own.
    detail: bool = False


@dataclass
class Run:
    repository_url: str
    issue_number: int
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    state: RunState = RunState.CREATED
    plan: list[str] = field(default_factory=list)
    review: dict = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def transition(self, state: RunState, message: str, detail: bool = False) -> Event:
        self.state = state
        event = Event(state=state, message=message, detail=detail)
        self.events.append(event)
        return event
