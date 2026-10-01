from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .models import Event, Run, RunState


class RunStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)"
            )

    def save(self, run: Run) -> None:
        payload = json.dumps(asdict(run))
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runs (id, created_at, state, payload) VALUES (?, ?, ?, ?)",
                (run.id, run.created_at, run.state.value, payload),
            )

    def get(self, run_id: str) -> Run | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._deserialize(row[0]) if row else None

    def recent(self, limit: int = 20) -> list[Run]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._deserialize(row[0]) for row in rows]

    @staticmethod
    def _deserialize(payload: str) -> Run:
        raw = json.loads(payload)
        events = [
            Event(state=RunState(item["state"]), message=item["message"], at=item["at"], detail=item.get("detail", False))
            for item in raw["events"]
        ]
        return Run(
            id=raw["id"], repository_url=raw["repository_url"], issue_number=raw["issue_number"],
            state=RunState(raw["state"]), plan=raw["plan"], review=raw.get("review", {}),
            events=events, created_at=raw["created_at"],
        )
