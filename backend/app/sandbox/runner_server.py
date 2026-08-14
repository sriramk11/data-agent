#!/usr/bin/env python3
"""Runs INSIDE the sandbox (container or, in local-dev mode, a restricted
subprocess). Speaks a tiny line-delimited JSON protocol over stdin/stdout so
the host-side manager can send code and get results back without needing a
network port (which is disabled anyway -- --network none).

Protocol
--------
request  (one line of JSON on stdin):
    {"id": "<uuid>", "code": "<python source>", "timeout": 10}
response (one line of JSON on stdout):
    {"id": "<uuid>", "ok": true, "stdout": "...", "stderr": "...",
     "elapsed_ms": 123, "new_files": ["chart_1.png"]}

Design notes (the "what happens when generated code is broken" story):
  - Code runs with exec() against a PERSISTENT globals dict, so variables
    and imports from earlier steps (e.g. a loaded DataFrame) survive into
    later steps, like a notebook kernel -- the agent doesn't need to redo
    work every turn.
  - Any exception, including ones raised deep in pandas/numpy, is caught,
    formatted as a normal traceback, and returned as `stderr` with ok=False.
    Nothing here re-raises into the host process. The orchestrator feeds
    that traceback back to the model as a tool_result so it can self-correct
    (fix a KeyError, cast a dtype, etc.) on the next turn.
  - A per-call wall-clock budget is enforced with SIGALRM so a runaway loop
    can't hang a turn forever; the host manager has its own, longer timeout
    as a second line of defense that kills+restarts the whole sandbox if
    this process stops responding entirely (e.g. a segfault in a C
    extension that swallows the alarm).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path

# Inside the Docker sandbox these are always /work/{data,output} (fixed by
# the bind mounts in manager.py). The local-dev fallback (no container) has
# no fixed mount points, so it points these at the real per-session dirs via
# env vars set by the bootstrap wrapper.
DATA_DIR = Path(os.environ.get("SANDBOX_DATA_DIR", "/work/data"))
OUTPUT_DIR = Path(os.environ.get("SANDBOX_OUTPUT_DIR", "/work/output"))


class StepTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise StepTimeout("execution exceeded the per-step time budget")


def _make_namespace() -> dict:
    ns: dict = {"__name__": "__sandbox__"}
    # Pre-import the data-analysis stack the agent is expected to use so it
    # doesn't burn a step importing, and so we don't have to allow arbitrary
    # imports of things like `os` or `socket` in generated code.
    import matplotlib
    matplotlib.use("Agg")  # headless, no display in the container
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd

    ns["pd"] = pd
    ns["np"] = np
    ns["plt"] = plt
    try:
        import duckdb
        ns["duckdb"] = duckdb
    except ImportError:
        pass
    return ns


def _run_one(ns: dict, code: str, timeout: int) -> dict:
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    before = {p.name for p in OUTPUT_DIR.glob("*")}
    ok = True
    start = time.monotonic()

    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(max(1, int(timeout)))
    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            exec(compile(code, "<agent_step>", "exec"), ns)
    except StepTimeout as e:
        ok = False
        stderr_buf.write(f"TimeoutError: {e}\n")
    except BaseException:
        ok = False
        stderr_buf.write(traceback.format_exc())
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    after = {p.name for p in OUTPUT_DIR.glob("*")}
    new_files = sorted(after - before)

    return {
        "ok": ok,
        "stdout": stdout_buf.getvalue()[-8000:],
        "stderr": stderr_buf.getvalue()[-8000:],
        "elapsed_ms": elapsed_ms,
        "new_files": new_files,
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ns = _make_namespace()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue  # malformed request from host; drop and wait for next

        result = _run_one(ns, req.get("code", ""), req.get("timeout", 10))
        result["id"] = req.get("id")
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
