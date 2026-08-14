import { useState } from "react";
import type { SessionInfo, StepEvent } from "./types";
import { approveStep, streamQuery, uploadDataset } from "./api";
import StepTimeline from "./components/StepTimeline";

export default function App() {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [uploading, setUploading] = useState(false);
  const [question, setQuestion] = useState("");
  const [requireApproval, setRequireApproval] = useState(false);
  const [events, setEvents] = useState<StepEvent[]>([]);
  const [running, setRunning] = useState(false);
  const [pending, setPending] = useState<{ code: string; tool: string } | null>(null);
  const [editedCode, setEditedCode] = useState("");

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const info = await uploadDataset(file);
      setSession(info);
      setEvents([]);
    } catch (err) {
      alert(`Upload failed: ${err}`);
    } finally {
      setUploading(false);
    }
  }

  async function onAsk() {
    if (!session || !question.trim() || running) return;
    setRunning(true);
    setEvents([]);
    try {
      for await (const ev of streamQuery(session.session_id, question, requireApproval)) {
        setEvents((prev) => [...prev, ev]);
        if (ev.type === "pending_approval") {
          setPending({ code: ev.data.code, tool: ev.data.tool });
          setEditedCode(ev.data.code);
        } else {
          setPending(null);
        }
        if (ev.type === "done") break;
      }
    } catch (err) {
      setEvents((prev) => [...prev, { type: "error", data: { message: String(err) }, key: `err-${prev.length}` }]);
    } finally {
      setRunning(false);
      setPending(null);
    }
  }

  async function respond(approved: boolean) {
    if (!session) return;
    await approveStep(session.session_id, approved, approved ? editedCode : undefined);
    setPending(null);
  }

  return (
    <div className="app">
      <header>
        <h1>Data Agent</h1>
        <p className="muted">Upload a CSV, ask a question in plain English, watch the agent plan → act → observe.</p>
      </header>

      {!session ? (
        <div className="upload-box">
          <input type="file" accept=".csv" onChange={onUpload} disabled={uploading} />
          {uploading && <p>Uploading & introspecting schema…</p>}
        </div>
      ) : (
        <>
          <div className="schema-box">
            <strong>{session.filename}</strong> · {session.row_count} rows
            <div className="columns">
              {session.columns.map((c) => (
                <span key={c.name} className="col-chip">
                  {c.name}: {c.dtype}
                </span>
              ))}
            </div>
          </div>

          <div className="ask-box">
            <textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="e.g. Which region grew fastest last quarter, and why?"
              rows={2}
              disabled={running}
            />
            <div className="ask-controls">
              <label className="muted">
                <input
                  type="checkbox"
                  checked={requireApproval}
                  onChange={(e) => setRequireApproval(e.target.checked)}
                  disabled={running}
                />{" "}
                Review code before it runs
              </label>
              <button onClick={onAsk} disabled={running || !question.trim()}>
                {running ? "Working…" : "Ask"}
              </button>
            </div>
          </div>

          {pending && (
            <div className="approval-modal">
              <h3>Review before running: {pending.tool}</h3>
              <textarea
                className="code-editor"
                value={editedCode}
                onChange={(e) => setEditedCode(e.target.value)}
                rows={10}
              />
              <div className="ask-controls">
                <button onClick={() => respond(false)} className="secondary">
                  Reject
                </button>
                <button onClick={() => respond(true)}>Run</button>
              </div>
            </div>
          )}

          <StepTimeline events={events} />
        </>
      )}
    </div>
  );
}
