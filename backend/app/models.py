"""Pydantic models: API request/response shapes and the structured tool
payloads the model is forced to emit. Structured outputs are what make the
orchestration reliable -- we never regex-parse the model's prose to decide
what happened, we validate its tool_use JSON against these schemas.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# --- Tool argument schemas (validated after the model calls a tool) -------

class RunPythonArgs(BaseModel):
    code: str = Field(..., description="Python source to execute in the sandbox kernel.")
    purpose: str = Field(..., description="One-line reason this step is being run (shown to the user).")


class MakeChartArgs(BaseModel):
    code: str = Field(
        ...,
        description=(
            "Python source that builds a matplotlib figure and saves it with "
            "plt.savefig('/work/output/<name>.png'). Must not call plt.show()."
        ),
    )
    title: str = Field(..., description="Short chart title, shown to the user.")


class SubmitAnswerArgs(BaseModel):
    answer: str = Field(..., description="Direct answer to the user's question, in plain English.")
    key_findings: list[str] = Field(default_factory=list, description="Supporting bullet points.")
    chart_files: list[str] = Field(
        default_factory=list, description="Filenames (from /work/output) referenced in the answer."
    )
    confidence: Literal["low", "medium", "high"] = "medium"
    caveats: Optional[str] = Field(None, description="Data quality issues, budget limits, or assumptions.")


# --- Session / API models --------------------------------------------------

class SessionSchemaColumn(BaseModel):
    name: str
    dtype: str


class SessionInfo(BaseModel):
    session_id: str
    filename: str
    row_count: int
    columns: list[SessionSchemaColumn]
    sample_rows: list[dict[str, Any]]


class QueryRequest(BaseModel):
    question: str
    require_approval: bool = False  # stretch goal: pause before executing generated code


class ApprovalRequest(BaseModel):
    approved: bool
    edited_code: Optional[str] = None


# --- SSE step events --------------------------------------------------------
# Every event the orchestrator yields is one of these, serialized to JSON
# over text/event-stream. `type` is the discriminator the frontend switches on.

class StepEvent(BaseModel):
    type: Literal[
        "plan", "tool_call", "tool_result", "pending_approval",
        "cost", "error", "answer", "done",
    ]
    data: dict[str, Any]
