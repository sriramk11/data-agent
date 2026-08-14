"""Central configuration and tunable guardrails.

Every "how do we stop this thing from running away" knob lives here so the
failure-handling story (step budgets, cost budgets, error budgets, sandbox
limits) is auditable in one place instead of scattered through the code.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Anthropic ---------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5")
# Generous headroom: Sonnet 5 runs adaptive thinking by default, and thinking
# shares this same budget with the visible response -- too tight a cap here
# truncates mid-turn rather than mid-thought.
MAX_TOKENS_PER_TURN = 4096

# --- Agent loop guardrails ----------------------------------------------
# Hard cap on plan->act->observe turns per query. This is the backstop
# against a model that never converges on submit_answer.
MAX_STEPS = 12
# If the sandbox returns an error this many turns in a row, stop retrying
# blindly and surface the failure instead of burning the whole step budget
# on the same mistake.
MAX_CONSECUTIVE_ERRORS = 3
# Dollar ceiling per query (rough, computed from token usage). Independent
# of MAX_STEPS because a model can burn budget with a few very large calls.
MAX_COST_USD = 0.50

# --- Sandbox --------------------------------------------------------------
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "data-agent-sandbox:latest")
SANDBOX_EXEC_TIMEOUT_S = 10          # wall-clock budget per run_python/make_chart call
SANDBOX_STARTUP_TIMEOUT_S = 15
SANDBOX_MEMORY = "512m"
SANDBOX_CPUS = "1"
SANDBOX_PIDS_LIMIT = "128"
# Use the real Docker sandbox by default. SANDBOX_MODE=local runs the same
# runner script as a plain subprocess with rlimits instead of a container --
# useful for local dev on a machine without Docker, but it is NOT the
# security boundary the design relies on. Never use it for untrusted data.
SANDBOX_MODE = os.environ.get("SANDBOX_MODE", "docker")

# --- Storage --------------------------------------------------------------
RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR", Path(__file__).resolve().parents[2] / "runtime"))
SESSIONS_DIR = RUNTIME_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# --- CORS -------------------------------------------------------------
FRONTEND_ORIGINS = os.environ.get("FRONTEND_ORIGINS", "http://localhost:5173").split(",")
