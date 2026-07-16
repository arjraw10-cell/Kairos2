// === Protocol Types (matching kairos/gateway/protocol.py) ===

export type ServerMessageType =
  | "connected"
  | "new_session_created"
  | "sessions_list"
  | "workspaces_list"
  | "stream_start"
  | "stream_token"
  | "stream_end"
  | "tool_call"
  | "done"
  | "token_update"
  | "compacted"
  | "unloaded"
  | "error"
  | "pong"
  | "exit";

export type ClientMessageType =
  | "connect"
  | "new_session"
  | "load_session"
  | "unload"
  | "list_sessions"
  | "list_workspaces"
  | "message"
  | "interrupt"
  | "stop"
  | "compact"
  | "ping";

export interface ServerMessage {
  type: ServerMessageType;
  [key: string]: any;
}

export interface Session {
  id: string;
  timestamp: string;
  workspace: string;
  preview: string;
  active?: boolean;
}

export interface TokenInfo {
  sessionInput: number;
  sessionOutput: number;
  contextPct: number;
  turnInput: number;
  turnOutput: number;
}

export type ConnectionStatus = "disconnected" | "connecting" | "connected";

// === UI Types ===

export interface ToolCallInfo {
  name: string;
  summary: string;
}

export interface TurnStep {
  thinking: string;
  toolCalls: ToolCallInfo[];
  durationMs: number;
}

export interface AssistantTurn {
  steps: TurnStep[];
  response: string | null;
}

export interface UIMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  image_urls?: string[];
  turn?: AssistantTurn;
}

/** The complete renderable state for one loaded conversation. */
export interface SessionView {
  id: string;
  workspace: string;
  preview: string;
  messages: UIMessage[];
  streamText: string;
  toolCalls: ToolCallInfo[];
  completedSteps: TurnStep[];
  tokens: TokenInfo | null;
  streaming: boolean;
  loading: boolean;
}
