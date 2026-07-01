// Protocol types matching kairos/gateway/protocol.py

// Server -> Client message types
export type ServerMessageType =
  | "connected"
  | "new_session_created"
  | "sessions_list"
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

// Client -> Server message types
export type ClientMessageType =
  | "connect"
  | "new_session"
  | "load_session"
  | "unload"
  | "list_sessions"
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
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  timestamp: number;
}

export interface ToolCallInfo {
  name: string;
  summary: string;
}

export interface TokenInfo {
  sessionInput: number;
  sessionOutput: number;
  contextPct: number;
  turnInput: number;
  turnOutput: number;
}

export type ConnectionStatus = "disconnected" | "connecting" | "connected";
