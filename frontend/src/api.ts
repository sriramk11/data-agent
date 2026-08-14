import type { SessionInfo, StepEvent } from "./types";

export async function uploadDataset(file: File): Promise<SessionInfo> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch("/api/sessions", { method: "POST", body: form });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function deleteSession(sessionId: string): Promise<void> {
  await fetch(`/api/sessions/${sessionId}`, { method: "DELETE" });
}

export async function approveStep(
  sessionId: string,
  approved: boolean,
  editedCode?: string,
): Promise<void> {
  await fetch(`/api/sessions/${sessionId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved, edited_code: editedCode }),
  });
}

/**
 * POST /query returns text/event-stream, but the browser's native
 * EventSource can't send a POST body -- so this hand-rolls the SSE framing
 * (blank-line-delimited "event: ...\ndata: ...\n\n" blocks) over a plain
 * fetch() ReadableStream instead.
 */
export async function* streamQuery(
  sessionId: string,
  question: string,
  requireApproval: boolean,
): AsyncGenerator<StepEvent> {
  const res = await fetch(`/api/sessions/${sessionId}/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, require_approval: requireApproval }),
  });
  if (!res.ok || !res.body) throw new Error(await res.text());

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let seq = 0;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);

      let type = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) type = line.slice(7).trim();
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (!data) continue;
      seq += 1;
      yield { type: type as StepEvent["type"], data: JSON.parse(data), key: `${type}-${seq}` };
    }
  }
}
