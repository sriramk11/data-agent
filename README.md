# Data Agent

Upload a CSV, ask a question in plain English — "which regions grew fastest
last quarter, and why?" — and an agent plans, writes Python, runs it in a
sandboxed container, inspects the results, and returns an answer with the
chart and the code it ran. You watch every step live.

The point of this project isn't the chat UI. It's the agent loop and the
failure handling underneath it — that's where most agent projects fall
apart, and it's what this README focuses on.

## Architecture

```
Browser (React)                FastAPI backend                  Docker sandbox
┌───────────────┐   SSE        ┌──────────────────┐  stdin/stdout ┌────────────────┐
│ upload CSV     │ ───────────▶│ orchestrator.py    │◀─────────────▶│ runner_server.py│
│ ask question   │              │  plan→act→observe  │   JSON lines  │ persistent      │
│ step timeline  │              │  →retry loop        │               │ python kernel   │
│ approve code   │              │                     │               │ --network none  │
└───────────────┘              │ Claude (tool use)   │               │ read-only rootfs│
                                └──────────────────────┘              └────────────────┘
```

- **Backend**: FastAPI. One SSE endpoint (`POST /api/sessions/{id}/query`) streams
  every step of the agent loop to the client as it happens.
- **Agent loop**: [`backend/app/agent/orchestrator.py`](backend/app/agent/orchestrator.py) — a plain `while True` loop
  around the Anthropic Messages API with three tools: `run_python`,
  `make_chart`, `submit_answer`. No agent framework; the loop is ~150 lines
  and every guardrail in it is visible.
- **Sandbox**: [`backend/app/sandbox/`](backend/app/sandbox) — each session gets its own Docker
  container running a small persistent "kernel" ([`runner_server.py`](backend/app/sandbox/runner_server.py)) that
  the backend talks to over stdin/stdout (no exposed port needed, since the
  container has no network anyway).
- **Frontend**: React + Vite, hand-rolled SSE client (native `EventSource`
  can't send a POST body, so it's a `fetch()` + `ReadableStream` parser —
  see [`frontend/src/api.ts`](frontend/src/api.ts)).

## Why a real sandbox, not just `exec()`

The model writes and runs its own Python. That's the whole point of the
product, and it's also the whole attack surface: a model can be tricked (via
prompt injection in the dataset itself, or just by being wrong) into writing
code that reads files it shouldn't, phones home, or eats all the RAM on the
box.

The security boundary is a **Docker container per session**, not a
try/except around `exec()`:

- `--network none` — no exfiltration path, full stop.
- `--read-only` root filesystem + a small `tmpfs` for `/tmp` — nothing
  persists, nothing outside the two mounted dirs is writable.
- Dataset mounted **read-only** at `/work/data`; a scratch dir mounted
  **read-write** at `/work/output` for charts only.
- `--memory`, `--cpus`, `--pids-limit` — a fork bomb or a runaway allocation
  hits a wall instead of taking down the host.
- `--cap-drop ALL`, `--security-opt no-new-privileges`, non-root user.

On top of that, [`app/agent/tools.py`](backend/app/agent/tools.py) does a cheap static regex scan
for obviously adversarial patterns (`import subprocess`, `os.system`, path
traversal) **before** a container exec is even spent on it — not the real
boundary, just a second, much cheaper layer that also gives the model a
clear error to self-correct from instead of a silent container kill.

`SANDBOX_MODE=local` exists for developing on a machine without Docker (this
one, currently — no Docker Desktop installed here). It runs the same
`runner_server.py` as a bare subprocess with `rlimit`s instead of a
container. It is explicitly **not** the security boundary and logs a loud
warning on startup; don't point it at untrusted data.

## The agent loop, and the parts that actually matter

**Structured outputs, not prose parsing.** The model's only way to act is
one of three tools, each with a Pydantic-validated schema:
`run_python(code, purpose)`, `make_chart(code, title)`,
`submit_answer(answer, key_findings, chart_files, confidence, caveats)`.
`submit_answer` is the *only* way the loop ends — there's no "if the model
sounds done, stop" heuristic. This is also what makes the SSE stream
reliable: every event the frontend renders comes from a typed tool call or
tool result, never from regexing the model's text.

**What happens when the model writes broken Python.** The sandbox never
raises into the backend process — it catches everything (including deep
pandas/numpy exceptions), formats a normal traceback, and returns it as a
`tool_result` with `is_error: true`. That goes back to the model on the next
turn exactly like a real error would in a REPL, and the model fixes its own
`KeyError` or dtype mismatch. No special-casing needed on the backend side —
this is just... tool use working as designed.

**How infinite loops are stopped.** Three independent budgets, checked every
turn ([`config.py`](backend/app/config.py)):
- `MAX_STEPS` (12) — hard cap on tool-using turns.
- `MAX_COST_USD` ($0.50) — computed from real token usage every turn,
  independent of step count (a model can burn budget in a few huge calls).
- `MAX_CONSECUTIVE_ERRORS` (3) — if the sandbox keeps failing, stop
  retrying blindly *before* the step budget is exhausted repeating the same
  mistake.

When any budget trips, the orchestrator doesn't just cut the connection — it
makes one more model call with `tool_choice` **forced** to `submit_answer`,
so the user gets a structured (if `confidence: "low"`) answer instead of a
session that silently dies mid-thought.

**How it's kept from wandering off task.** The tool surface is the
guardrail: there is no shell tool, no arbitrary-file-write tool, no network
tool. `run_python` only reaches the sandbox, which itself can't reach
anything but `/work/data` (read-only) and `/work/output` (write-only,
charts). "Wandering off task" has a small blast radius by construction, not
by asking nicely in the system prompt.

## Stretch goals implemented

- **Live cost counter**: every model turn's token usage is priced
  ([`agent/cost.py`](backend/app/agent/cost.py)) and streamed as a `cost` event; the frontend shows a
  running `$` total per query.
- **Inspect/edit generated code before it runs**: pass
  `require_approval: true` on a query and the orchestrator pauses before
  every `run_python`/`make_chart` call, emits a `pending_approval` event with
  the exact code, and `await`s an `asyncio.Event` that only
  `POST /api/sessions/{id}/approve` can set — with an optional edited version
  of the code to run instead.

## Running it

```bash
cp .env.example .env   # add your ANTHROPIC_API_KEY

# Sandbox image (build once)
docker build -t data-agent-sandbox:latest -f backend/sandbox.Dockerfile backend

# Backend + frontend
docker compose up --build     # backend on :8000
cd frontend && npm install && npm run dev   # frontend on :5173
```

No Docker on your machine? Set `SANDBOX_MODE=local` in `.env` and run the
backend directly from `backend/`:

```bash
pip install -r requirements.txt -r requirements-local-sandbox.txt
uvicorn app.main:app --reload
```

(the second requirements file is only needed in local mode — it's the
pandas/numpy/matplotlib/duckdb stack that `sandbox.Dockerfile` normally
bakes into the container image instead). See the security note above before
pointing local mode at anything but the bundled `sample_data/sales.csv`.

## What I'd add next

- Token-level model streaming (currently each turn is one non-streaming
  call; the *step* events stream, not individual tokens within a turn).
- A real DB-backed session store instead of the in-memory dict — sessions
  currently die with the process.
- SQL/Postgres `DataSource` (the `DataSource` abstraction in
  [`data_sources.py`](backend/app/data_sources.py) is CSV-only right now; a DB source needs a read-only
  connection and the same schema-introspection contract).
- A container pool instead of cold-starting a sandbox per session — the
  ~1-2s Docker startup is the biggest latency cost in the loop today.
