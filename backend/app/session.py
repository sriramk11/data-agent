"""In-memory session registry.

One Session per uploaded dataset. Holds the sandbox handle, the running
conversation history (so follow-up questions can reuse context), the cost
tracker, and the plumbing for the "review code before running" approval
flow. A real deployment would move this to Redis/a DB; in-memory is fine for
a single-process demo and keeps the orchestration logic front and center.
"""
from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app import config
from app.agent.cost import CostTracker
from app.models import SessionSchemaColumn
from app.sandbox.manager import SandboxSession, make_sandbox


@dataclass
class PendingApproval:
    """State for one tool call awaiting human review (require_approval mode)."""

    tool_id: str
    tool_name: str
    code: str
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False
    edited_code: Optional[str] = None


@dataclass
class Session:
    session_id: str
    filename: str
    columns: list[SessionSchemaColumn]
    row_count: int
    data_dir: Path
    output_dir: Path
    sandbox: SandboxSession
    cost: CostTracker
    history: list[dict[str, Any]] = field(default_factory=list)
    pending_approval: Optional[PendingApproval] = None
    sample_rows: list[dict[str, Any]] = field(default_factory=list)

    def stop(self) -> None:
        self.sandbox.stop()
        shutil.rmtree(self.data_dir.parent, ignore_errors=True)


SESSIONS: dict[str, Session] = {}


def create_session(filename: str, columns: list[SessionSchemaColumn], row_count: int) -> Session:
    session_id = str(uuid.uuid4())
    base = config.SESSIONS_DIR / session_id
    data_dir = base / "data"
    output_dir = base / "output"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    sandbox = make_sandbox(session_id, data_dir, output_dir)
    sandbox.start()

    session = Session(
        session_id=session_id,
        filename=filename,
        columns=columns,
        row_count=row_count,
        data_dir=data_dir,
        output_dir=output_dir,
        sandbox=sandbox,
        cost=CostTracker(config.MODEL),
    )
    SESSIONS[session_id] = session
    return session


def get_session(session_id: str) -> Optional[Session]:
    return SESSIONS.get(session_id)


def destroy_session(session_id: str) -> None:
    session = SESSIONS.pop(session_id, None)
    if session:
        session.stop()
