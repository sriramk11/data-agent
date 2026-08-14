export interface SessionSchemaColumn {
  name: string;
  dtype: string;
}

export interface SessionInfo {
  session_id: string;
  filename: string;
  row_count: number;
  columns: SessionSchemaColumn[];
  sample_rows: Record<string, unknown>[];
}

export type StepEventType =
  | "plan"
  | "tool_call"
  | "tool_result"
  | "pending_approval"
  | "cost"
  | "error"
  | "answer"
  | "done";

export interface StepEvent {
  type: StepEventType;
  data: Record<string, any>;
  key: string; // synthetic, for React lists
}
