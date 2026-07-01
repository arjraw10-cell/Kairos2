import { useState, useRef, useCallback, useEffect } from "react";
import type {
  ServerMessage,
  ChatMessage,
  Session,
  ToolCallInfo,
  TokenInfo,
  ConnectionStatus,
} from "../types";

const WS_URL = "ws://127.0.0.1:8765/ws";

let msgIdCounter = 0;
function nextId() {
  return `msg_${++msgIdCounter}_${Date.now()}`;
}

export function useGateway() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef<Map<string, (msg: ServerMessage) => void>>(new Map());

  // State
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const [sessions, setSessions] = useState<Session[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<string>("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [toolCalls, setToolCalls] = useState<ToolCallInfo[]>([]);
  const [tokens, setTokens] = useState<TokenInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [systemMessages, setSystemMessages] = useState<ChatMessage[]>([]);

  // Refs for values used in callbacks (avoid stale closures)
  const currentSessionIdRef = useRef(currentSessionId);
  currentSessionIdRef.current = currentSessionId;

  const send = useCallback((msg: any) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  }, []);

  const handleServerMessage = useCallback((msg: ServerMessage) => {
    switch (msg.type) {
      case "connected": {
        setCurrentSessionId(msg.session_id);
        setWorkspace(msg.workspace || "");
        if (msg.history && Array.isArray(msg.history)) {
          // Convert server history to chat messages (skip system prompt)
          const history: ChatMessage[] = msg.history
            .filter((m: any) => m.role !== "system")
            .map((m: any) => ({
              id: nextId(),
              role: m.role as ChatMessage["role"],
              content: typeof m.content === "string" ? m.content : "",
              timestamp: Date.now(),
            }));
          setMessages(history);
        }
        break;
      }

      case "new_session_created": {
        setCurrentSessionId(msg.session_id);
        setWorkspace(msg.workspace || "");
        setMessages([]);
        setToolCalls([]);
        setStreamText("");
        // Refresh session list
        send({ type: "list_sessions" });
        break;
      }

      case "sessions_list": {
        if (Array.isArray(msg.sessions)) {
          setSessions(msg.sessions);
        }
        break;
      }

      case "stream_start": {
        setStreaming(true);
        setStreamText("");
        setToolCalls([]);
        break;
      }

      case "stream_token": {
        setStreamText((prev) => prev + (msg.content || ""));
        break;
      }

      case "tool_call": {
        setToolCalls((prev) => [
          ...prev,
          { name: msg.name, summary: msg.summary || msg.name },
        ]);
        break;
      }

      case "stream_end": {
        // If no tool calls, this is a final assistant response
        if (!msg.has_tool_calls && msg.content) {
          setMessages((prev) => [
            ...prev,
            {
              id: nextId(),
              role: "assistant",
              content: msg.content,
              timestamp: Date.now(),
            },
          ]);
        }
        setStreaming(false);
        setStreamText("");
        setToolCalls([]);
        break;
      }

      case "done": {
        if (msg.response) {
          setMessages((prev) => {
            // Avoid duplicate: only add if last message isn't the same
            const last = prev[prev.length - 1];
            if (last?.role === "assistant" && last.content === msg.response) {
              return prev;
            }
            return [
              ...prev,
              {
                id: nextId(),
                role: "assistant",
                content: msg.response,
                timestamp: Date.now(),
              },
            ];
          });
        }
        setStreaming(false);
        setStreamText("");
        setToolCalls([]);
        break;
      }

      case "token_update": {
        setTokens({
          sessionInput: msg.session_input || 0,
          sessionOutput: msg.session_output || 0,
          contextPct: msg.context_pct || 0,
          turnInput: msg.turn_input || 0,
          turnOutput: msg.turn_output || 0,
        });
        break;
      }

      case "compacted": {
        setSystemMessages((prev) => [
          ...prev,
          {
            id: nextId(),
            role: "system",
            content: msg.message || "Context compacted",
            timestamp: Date.now(),
          },
        ]);
        break;
      }

      case "error": {
        setError(msg.message || "Unknown error");
        setStreaming(false);
        setTimeout(() => setError(null), 5000);
        break;
      }

      case "pong":
        break;

      case "exit":
        break;
    }
  }, [send]);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    setStatus("connecting");
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus("connected");
      setError(null);
      // Request session list immediately
      ws.send(JSON.stringify({ type: "list_sessions" }));
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data) as ServerMessage;
        handleServerMessage(msg);
      } catch {
        // ignore parse errors
      }
    };

    ws.onclose = () => {
      setStatus("disconnected");
      wsRef.current = null;
      // Reconnect after 2s
      reconnectTimerRef.current = setTimeout(connect, 2000);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [handleServerMessage]);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  // Actions
  const sendMessage = useCallback(
    (content: string, imageUrl?: string) => {
      if (!content.trim()) return;

      // Add user message to local state immediately
      setMessages((prev) => [
        ...prev,
        {
          id: nextId(),
          role: "user",
          content,
          timestamp: Date.now(),
        },
      ]);

      send({
        type: "message",
        content,
        image_url: imageUrl,
      });
    },
    [send]
  );

  const newSession = useCallback(
    (ws?: string) => {
      send({ type: "new_session", workspace: ws });
    },
    [send]
  );

  const loadSession = useCallback(
    (sessionId: string) => {
      send({ type: "load_session", session_id: sessionId });
    },
    [send]
  );

  const interrupt = useCallback(() => {
    send({ type: "interrupt" });
  }, [send]);

  const compact = useCallback(() => {
    send({ type: "compact" });
  }, [send]);

  const refreshSessions = useCallback(() => {
    send({ type: "list_sessions" });
  }, [send]);

  return {
    // State
    status,
    sessions,
    currentSessionId,
    workspace,
    messages,
    streaming,
    streamText,
    toolCalls,
    tokens,
    error,
    systemMessages,
    // Actions
    sendMessage,
    newSession,
    loadSession,
    interrupt,
    compact,
    refreshSessions,
  };
}
