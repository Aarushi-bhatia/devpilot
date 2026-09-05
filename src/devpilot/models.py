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


@dataclass
class Run:
    repository_url: str
    issue_number: int
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    state: RunState = RunState.CREATED
    plan: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def transition(self, state: RunState, message: str) -> Event:
        self.state = state
        event = Event(state=state, message=message)
        self.events.append(event)
        return event
