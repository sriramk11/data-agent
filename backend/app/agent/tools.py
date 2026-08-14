"""Tool schemas (the model's entire action space) plus defense-in-depth
static checks on generated code before it ever reaches the sandbox.

Scoping the tool set is itself a guardrail: the model has exactly three
verbs -- run_python, make_chart, submit_answer. There is no shell tool, no
file-write-anywhere tool, no network tool, so "wandering off task" has a
small blast radius even before the sandbox gets involved.
"""
from __future__ import annotations

import re

from pydantic import ValidationError

from app.models import MakeChartArgs, RunPythonArgs, SubmitAnswerArgs

TOOLS = [
    {
        "name": "run_python",
        "description": (
            "Execute Python against the loaded dataset to explore or compute something "
            "(e.g. pandas groupby/aggregate, duckdb SQL over a DataFrame, statistics). "
            "Runs in a persistent kernel -- variables and imports from earlier calls are "
            "still available. pandas as pd, numpy as np, and duckdb are pre-imported. "
            "Use print() to surface anything you want to see in the result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute."},
                "purpose": {"type": "string", "description": "One-line reason for this step."},
            },
            "required": ["code", "purpose"],
        },
    },
    {
        "name": "make_chart",
        "description": (
            "Execute Python that builds a matplotlib figure and saves it with plt.savefig() "
            "into the output directory named in the system prompt. Use this only once you "
            "know what you want to plot -- explore with run_python first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source that ends in plt.savefig(...)."},
                "title": {"type": "string", "description": "Short chart title."},
            },
            "required": ["code", "title"],
        },
    },
    {
        "name": "submit_answer",
        "description": (
            "Finish the task and deliver the final answer. This is the ONLY way to end the "
            "conversation -- call it exactly once, when you have enough evidence to answer "
            "confidently (or to honestly report that you could not)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "key_findings": {"type": "array", "items": {"type": "string"}},
                "chart_files": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "caveats": {"type": "string"},
            },
            "required": ["answer"],
        },
    },
]

ARG_MODELS = {
    "run_python": RunPythonArgs,
    "make_chart": MakeChartArgs,
    "submit_answer": SubmitAnswerArgs,
}


def validate_tool_args(name: str, raw_input: dict) -> tuple[object | None, str | None]:
    """Validate a tool_use block's input against its schema.

    Returns (parsed_args, None) on success or (None, error_message) on
    failure. The error message is designed to be fed straight back to the
    model as a tool_result so it can correct its own call shape.
    """
    model = ARG_MODELS.get(name)
    if model is None:
        return None, f"Unknown tool '{name}'."
    try:
        return model.model_validate(raw_input), None
    except ValidationError as e:
        return None, f"Invalid arguments for {name}: {e.errors()}"


# --- Defense-in-depth static scan -----------------------------------------
# The Docker sandbox (no network, read-only fs except /work/output, dropped
# capabilities, non-root, resource limits) is the actual security boundary.
# This scan is a *cheap second layer*: it catches obviously-adversarial or
# accidental sandbox-escape attempts before we even bother spending a
# container exec on them, and returns a clear, actionable error so a model
# that innocently tried `import os` for a path join gets steered toward the
# allowed alternative instead of just failing silently.
FORBIDDEN_PATTERNS = [
    (r"\bimport\s+subprocess\b", "importing subprocess is not allowed"),
    (r"\bimport\s+socket\b", "importing socket is not allowed (network is disabled anyway)"),
    (r"\bimport\s+ctypes\b", "importing ctypes is not allowed"),
    (r"\bos\.system\s*\(", "os.system is not allowed"),
    (r"\bos\.popen\s*\(", "os.popen is not allowed"),
    (r"\b__import__\s*\(", "dynamic __import__ is not allowed"),
    (r"\bshutil\.rmtree\s*\(", "shutil.rmtree is not allowed"),
    (r"\.\./", "path traversal ('../') is not allowed"),
    # Blocklist rather than a /work/-prefix allowlist: the exact sandbox
    # mount path differs between SANDBOX_MODE=docker (fixed /work/...) and
    # SANDBOX_MODE=local (the real host path, since there's no chroot to
    # remap it -- see manager.py SandboxSession.data_path). A blocklist of
    # sensitive system directories works the same way in both modes.
    (
        r"\bopen\s*\([^)]*['\"]\s*/(etc|root|proc|sys|private/etc|System|Library|usr|bin|sbin)(/|['\"])",
        "opening files under system directories is not allowed",
    ),
]


def scan_for_forbidden_patterns(code: str) -> list[str]:
    violations = []
    for pattern, message in FORBIDDEN_PATTERNS:
        if re.search(pattern, code):
            violations.append(message)
    return violations
