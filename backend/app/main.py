"""FastAPI app: upload a dataset, ask questions, stream the agent loop over SSE."""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from app import config
from app.agent.orchestrator import run_query
from app.data_sources import ingest_csv
from app.models import ApprovalRequest, QueryRequest, SessionInfo
from app.session import create_session, destroy_session, get_session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

app = FastAPI(title="Data Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/sessions", response_model=SessionInfo)
async def create_session_endpoint(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Only .csv uploads are supported in this scaffold.")

    # Create the session shell first (allocates data_dir/output_dir and boots
    # the sandbox), then ingest the upload directly into its data_dir so the
    # sandboxed container can read it at /work/data/<filename>.
    session = create_session(file.filename, columns=[], row_count=0)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / file.filename
        tmp_path.write_bytes(await file.read())
        try:
            filename, columns, sample_rows, row_count = ingest_csv(tmp_path, session.data_dir)
        except Exception as e:
            destroy_session(session.session_id)
            raise HTTPException(400, f"Could not parse CSV: {e}")

    session.filename = filename
    session.columns = columns
    session.row_count = row_count
    session.sample_rows = sample_rows

    return SessionInfo(
        session_id=session.session_id,
        filename=filename,
        row_count=row_count,
        columns=columns,
        sample_rows=sample_rows,
    )


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    if not get_session(session_id):
        raise HTTPException(404, "Session not found")
    destroy_session(session_id)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/query")
async def query_endpoint(session_id: str, req: QueryRequest):
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    async def event_stream():
        try:
            async for ev in run_query(session, req.question, req.require_approval):
                yield f"event: {ev['type']}\ndata: {json.dumps(ev['data'])}\n\n"
        except Exception as e:  # last-resort guard so a bug doesn't hang the SSE connection
            logger.exception("orchestrator crashed")
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
            yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/sessions/{session_id}/approve")
async def approve_endpoint(session_id: str, req: ApprovalRequest):
    """Stretch goal: let the user inspect/edit generated code before it runs.
    Only meaningful when the in-flight query was started with
    require_approval=true -- the orchestrator parks the pending tool call on
    session.pending_approval and awaits this endpoint's signal.
    """
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    pending = session.pending_approval
    if not pending:
        raise HTTPException(409, "No tool call is currently awaiting approval.")

    pending.approved = req.approved
    pending.edited_code = req.edited_code
    pending.ready.set()
    return {"ok": True}


@app.get("/api/sessions/{session_id}/charts/{filename}")
async def get_chart(session_id: str, filename: str):
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    # filename comes from the sandbox's own output listing (never directly
    # from client input), but validate anyway before touching the filesystem.
    safe_name = Path(filename).name
    path = session.output_dir / safe_name
    if not path.is_file():
        raise HTTPException(404, "Chart not found")
    return FileResponse(path)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "sandbox_mode": config.SANDBOX_MODE}
