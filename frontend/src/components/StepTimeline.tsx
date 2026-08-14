import type { StepEvent } from "../types";

function ToolCall({ ev }: { ev: StepEvent }) {
  return (
    <div className="step step-tool_call">
      <div className="step-head">
        <span className="badge">{ev.data.tool}</span>
        {ev.data.input?.purpose && <span className="muted"> — {ev.data.input.purpose}</span>}
      </div>
      {ev.data.input?.code && <pre className="code">{ev.data.input.code}</pre>}
    </div>
  );
}

function ToolResult({ ev }: { ev: StepEvent }) {
  const ok = ev.data.ok;
  return (
    <div className={`step step-tool_result ${ok ? "ok" : "fail"}`}>
      <div className="step-head">
        <span className="badge">{ok ? "✓" : "✗"} {ev.data.tool}</span>
        {typeof ev.data.elapsed_ms === "number" && (
          <span className="muted"> {ev.data.elapsed_ms}ms</span>
        )}
      </div>
      {ev.data.stdout && <pre className="code">{ev.data.stdout}</pre>}
      {ev.data.stderr && <pre className="code error-text">{ev.data.stderr}</pre>}
      {ev.data.chart_urls?.map((url: string) => (
        <img key={url} src={url} alt="chart" className="chart-img" />
      ))}
    </div>
  );
}

export default function StepTimeline({ events }: { events: StepEvent[] }) {
  return (
    <div className="timeline">
      {events.map((ev) => {
        switch (ev.type) {
          case "plan":
            return (
              <div key={ev.key} className="step step-plan">
                {ev.data.text}
              </div>
            );
          case "tool_call":
            return <ToolCall key={ev.key} ev={ev} />;
          case "tool_result":
            return <ToolResult key={ev.key} ev={ev} />;
          case "cost":
            return (
              <div key={ev.key} className="step step-cost muted">
                +${ev.data.turn_usd.toFixed(4)} this turn · ${ev.data.cumulative_usd.toFixed(4)} total ·{" "}
                {ev.data.input_tokens}in/{ev.data.output_tokens}out tok
              </div>
            );
          case "error":
            return (
              <div key={ev.key} className="step step-error">
                ⚠ {ev.data.message}
              </div>
            );
          case "pending_approval":
            return (
              <div key={ev.key} className="step step-pending">
                ⏸ Waiting for approval: <strong>{ev.data.tool}</strong>
                <pre className="code">{ev.data.code}</pre>
              </div>
            );
          case "answer":
            return (
              <div key={ev.key} className="step step-answer">
                <div className="answer-text">{ev.data.answer}</div>
                {ev.data.key_findings?.length > 0 && (
                  <ul>
                    {ev.data.key_findings.map((f: string, i: number) => (
                      <li key={i}>{f}</li>
                    ))}
                  </ul>
                )}
                {ev.data.chart_urls?.map((url: string) => (
                  <img key={url} src={url} alt="chart" className="chart-img" />
                ))}
                <div className="muted">
                  confidence: {ev.data.confidence}
                  {ev.data.caveats && ` — ${ev.data.caveats}`}
                </div>
              </div>
            );
          default:
            return null;
        }
      })}
    </div>
  );
}
