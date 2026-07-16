import { useState, useRef, useCallback, useEffect } from "react";
import type {
  ServerMessage,
  Session,
  SessionView,
  ToolCallInfo,
  TokenInfo,
  ConnectionStatus,
  TurnStep,
  UIMessage,
} from "../types";

const WS_URL = "ws://127.0.0.1:8765/ws";

let _nextId = 0;
function uid(): string {
  return `${++_nextId}_${Date.now()}`;
}

interface SessionData {
  workspace: string;
  messages: UIMessage[];
  streamText: string;
  toolCalls: ToolCallInfo[];
  completedSteps: TurnStep[];
  tokens: TokenInfo | null;
  streaming: boolean;
  loading: boolean;
}

function emptySessionData(workspace = ""): SessionData {
  return {
    workspace,
    messages: [],
    streamText: "",
    toolCalls: [],
    completedSteps: [],
    tokens: null,
    streaming: false,
    loading: false,
  };
}

function historyToUiMessages(history: any[] | undefined): UIMessage[] {
  if (!Array.isArray(history)) return [];

  return history
    .filter((m) => m?.role === "user" || m?.role === "assistant")
    .map((m) => {
      const raw = m.content;
      let text = "";
      const images: string[] = [];

      if (typeof raw === "string") {
        text = raw;
      } else if (Array.isArray(raw)) {
        for (const block of raw) {
          if (block?.type === "text") {
            text += (text ? "\n" : "") + (block.text || "");
          } else if (block?.type === "image_url") {
            const url = block.image_url?.url;
            if (url) images.push(url);
          }
        }
      }

      return {
        id: uid(),
        role: m.role as UIMessage["role"],
        content: text,
        image_urls: images.length > 0 ? images : undefined,
      };
    });
}

// ============================================================
// Gateway Hook
// ============================================================

export function useGateway() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // -- Server state --
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const [sessions, setSessions] = useState<Session[]>([]);
  // This is the focused session. Multiple sessions can stream independently.
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState("");
  const [workspaces, setWorkspaces] = useState<string[]>([]);
  const [homeDir, setHomeDir] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notifications, setNotifications] = useState<{ id: string; message: string; exiting?: boolean }[]>([]);

  // -- Per-session display data (required for concurrent split panes) --
  const [sessionsData, setSessionsData] = useState<Record<string, SessionData>>({});
  const [tabOrder, setTabOrder] = useState<string[]>([]);

  // -- Refs for synchronous access inside WebSocket handlers --
  const activeSessionIdRef = useRef(activeSessionId);
  activeSessionIdRef.current = activeSessionId;
  const sessionsDataRef = useRef(sessionsData);
  sessionsDataRef.current = sessionsData;
  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;
  const tabOrderRef = useRef(tabOrder);
  tabOrderRef.current = tabOrder;
  const workspaceRef = useRef(workspace);
  workspaceRef.current = workspace;

  // Streaming bookkeeping must also be per session. A single global pending
  // step would mix tool-call traces when two agents stream at once.
  const pendingStepsRef = useRef<Record<string, { thinking: string; durationMs: number }>>({});
  const stepStartRef = useRef<Record<string, number>>({});

  const pushTab = useCallback((sid: string) => {
    setTabOrder((prev) => (prev.includes(sid) ? prev : [...prev, sid]));
  }, []);

  const removeTab = useCallback((sid: string) => {
    setTabOrder((prev) => prev.filter((id) => id !== sid));
  }, []);

  const notify = useCallback((message: string) => {
    const id = `notif_${++_nextId}_${Date.now()}`;
    setNotifications((prev) => [...prev, { id, message }]);
    setTimeout(() => {
      setNotifications((prev) => prev.map((n) => (n.id === id ? { ...n, exiting: true } : n)));
      setTimeout(() => {
        setNotifications((prev) => prev.filter((n) => n.id !== id));
      }, 300);
    }, 3500);
  }, []);

  // Build view models for loaded sessions. Disk-only sessions remain in
  // `sessions` until the user loads them and do not need display buffers yet.
  const sessionViews: Record<string, SessionView> = {};
  for (const [id, data] of Object.entries(sessionsData)) {
    const fromServer = sessions.find((s) => s.id === id);
    const firstUser = data.messages.find((m) => m.role === "user");
    sessionViews[id] = {
      id,
      workspace: data.workspace || fromServer?.workspace || "",
      preview: firstUser?.content?.slice(0, 50) || fromServer?.preview || "New thread",
      messages: data.messages,
      streamText: data.streamText,
      toolCalls: data.toolCalls,
      completedSteps: data.completedSteps,
      tokens: data.tokens,
      streaming: data.streaming,
      loading: data.loading,
    };
  }

  const tabSessions: Session[] = tabOrder
    .filter((id) => !!sessionViews[id])
    .map((id) => {
      const view = sessionViews[id];
      return {
        id,
        timestamp: sessions.find((s) => s.id === id)?.timestamp || "",
        workspace: view.workspace,
        preview: view.preview,
        active: view.streaming,
      };
    });

  const activeData = activeSessionId ? sessionsData[activeSessionId] : null;

  const send = useCallback((msg: any) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  }, []);

  // -- Server message handler ------------------------------------
  const handleMsg = useCallback(
    (msg: ServerMessage) => {
      switch (msg.type) {
        case "connected": {
          const sid = msg.session_id;
          if (sid) {
            setActiveSessionId(sid);
            setWorkspace(msg.workspace || "");
            pushTab(sid);
            const uiMsgs = historyToUiMessages(msg.history);
            setSessionsData((prev) => {
              const existing = prev[sid];
              // A focus switch can return a connected event for a session that
              // is already streaming. Never replace that live buffer with the
              // persisted snapshot.
              if (existing && !existing.loading) {
                return {
                  ...prev,
                  [sid]: { ...existing, workspace: msg.workspace || existing.workspace, loading: false },
                };
              }
              return {
                ...prev,
                [sid]: {
                  ...emptySessionData(msg.workspace || ""),
                  messages: uiMsgs,
                },
              };
            });
          }
          if (Array.isArray(msg.workspaces)) setWorkspaces(msg.workspaces);
          if (msg.home_dir) setHomeDir(msg.home_dir);
          break;
        }

        case "new_session_created": {
          const sid = msg.session_id;
          setActiveSessionId(sid);
          setWorkspace(msg.workspace || "");
          pushTab(sid);
          setSessionsData((prev) => ({
            ...prev,
            [sid]: {
              ...(prev[sid] || emptySessionData(msg.workspace || "")),
              workspace: msg.workspace || prev[sid]?.workspace || "",
              loading: false,
            },
          }));
          send({ type: "list_sessions" });
          break;
        }

        case "sessions_list": {
          if (Array.isArray(msg.sessions)) setSessions(msg.sessions);
          break;
        }

        case "workspaces_list": {
          if (Array.isArray(msg.workspaces)) setWorkspaces(msg.workspaces);
          break;
        }

        // -- Streaming ---------------------------------------------
        case "stream_start": {
          const sid = msg.session_id as string;
          if (!sid) break;

          const pending = pendingStepsRef.current[sid];
          if (pending) {
            setSessionsData((prev) => {
              const sd = prev[sid] || emptySessionData();
              return {
                ...prev,
                [sid]: {
                  ...sd,
                  completedSteps: [
                    ...sd.completedSteps,
                    { thinking: pending.thinking, toolCalls: [...sd.toolCalls], durationMs: pending.durationMs },
                  ],
                },
              };
            });
            delete pendingStepsRef.current[sid];
          }

          setSessionsData((prev) => ({
            ...prev,
            [sid]: {
              ...(prev[sid] || emptySessionData()),
              streaming: true,
              streamText: "",
              toolCalls: [],
              loading: false,
            },
          }));
          stepStartRef.current[sid] = Date.now();
          break;
        }

        case "stream_token": {
          const sid = msg.session_id as string;
          if (!sid || !msg.content) break;
          setSessionsData((prev) => {
            const sd = prev[sid] || emptySessionData();
            return { ...prev, [sid]: { ...sd, streamText: sd.streamText + msg.content } };
          });
          break;
        }

        case "tool_call": {
          const sid = msg.session_id as string;
          if (!sid) break;
          const newTool = { name: msg.name, summary: msg.summary || msg.name };
          setSessionsData((prev) => {
            const sd = prev[sid] || emptySessionData();
            return { ...prev, [sid]: { ...sd, toolCalls: [...sd.toolCalls, newTool] } };
          });
          break;
        }

        case "stream_end": {
          const sid = msg.session_id as string;
          if (!sid) break;
          const started = stepStartRef.current[sid] || Date.now();
          const durationMs = Date.now() - started;
          delete stepStartRef.current[sid];

          setSessionsData((prev) => {
            const sd = prev[sid] || emptySessionData();
            if (msg.has_tool_calls) {
              pendingStepsRef.current[sid] = { thinking: sd.streamText, durationMs };
              return {
                ...prev,
                [sid]: { ...sd, streamText: "", toolCalls: [] },
              };
            }

            let allSteps = [...sd.completedSteps];
            const pending = pendingStepsRef.current[sid];
            if (pending) {
              allSteps = [
                ...allSteps,
                { thinking: pending.thinking, toolCalls: [...sd.toolCalls], durationMs: pending.durationMs },
              ];
              delete pendingStepsRef.current[sid];
            }

            const messages = msg.content
              ? [
                  ...sd.messages,
                  allSteps.length > 0
                    ? { id: uid(), role: "assistant" as const, content: msg.content, turn: { steps: allSteps, response: msg.content } }
                    : { id: uid(), role: "assistant" as const, content: msg.content },
                ]
              : sd.messages;

            return {
              ...prev,
              [sid]: {
                ...sd,
                messages,
                streaming: false,
                streamText: "",
                toolCalls: [],
                completedSteps: [],
              },
            };
          });
          break;
        }

        case "done": {
          const sid = msg.session_id as string;
          if (!sid) break;
          if (sid !== activeSessionIdRef.current) {
            const fromServer = sessionsRef.current.find((s) => s.id === sid);
            const preview = fromServer?.preview || sessionViews[sid]?.preview || "Session";
            notify(`"${preview.slice(0, 30)}" finished`);
          }

          if (msg.response) {
            setSessionsData((prev) => {
              const sd = prev[sid] || emptySessionData();
              const last = sd.messages[sd.messages.length - 1];
              if (last?.role === "assistant" && last.content === msg.response) {
                return { ...prev, [sid]: { ...sd, streaming: false, streamText: "", toolCalls: [], completedSteps: [] } };
              }

              let allSteps = [...sd.completedSteps];
              const pending = pendingStepsRef.current[sid];
              if (pending) {
                allSteps = [
                  ...allSteps,
                  { thinking: pending.thinking, toolCalls: [...sd.toolCalls], durationMs: pending.durationMs },
                ];
                delete pendingStepsRef.current[sid];
              }
              return {
                ...prev,
                [sid]: {
                  ...sd,
                  messages: [
                    ...sd.messages,
                    allSteps.length > 0
                      ? { id: uid(), role: "assistant" as const, content: msg.response, turn: { steps: allSteps, response: msg.response } }
                      : { id: uid(), role: "assistant" as const, content: msg.response },
                  ],
                  streaming: false,
                  streamText: "",
                  toolCalls: [],
                  completedSteps: [],
                },
              };
            });
          } else {
            setSessionsData((prev) => {
              const sd = prev[sid] || emptySessionData();
              return {
                ...prev,
                [sid]: { ...sd, streaming: false, streamText: "", toolCalls: [], completedSteps: [] },
              };
            });
            delete pendingStepsRef.current[sid];
          }
          send({ type: "list_sessions" });
          break;
        }

        case "token_update": {
          const sid = msg.session_id as string;
          if (!sid) break;
          setSessionsData((prev) => {
            const sd = prev[sid] || emptySessionData();
            return {
              ...prev,
              [sid]: {
                ...sd,
                tokens: {
                  sessionInput: msg.session_input || 0,
                  sessionOutput: msg.session_output || 0,
                  contextPct: msg.context_pct || 0,
                  turnInput: msg.turn_input || 0,
                  turnOutput: msg.turn_output || 0,
                },
              },
            };
          });
          break;
        }

        case "compacted": {
          const sid = msg.session_id as string;
          if (!sid) break;
          setSessionsData((prev) => {
            const sd = prev[sid] || emptySessionData();
            return {
              ...prev,
              [sid]: {
                ...sd,
                messages: [...sd.messages, { id: uid(), role: "system", content: msg.message || "Context compacted" }],
              },
            };
          });
          break;
        }

        case "error": {
          setError(msg.message || "Unknown error");
          const sid = (msg.session_id as string) || activeSessionIdRef.current;
          if (sid) {
            setSessionsData((prev) => {
              const sd = prev[sid] || emptySessionData();
              return {
                ...prev,
                [sid]: { ...sd, loading: false, streaming: false, streamText: "", toolCalls: [], completedSteps: [] },
              };
            });
            delete pendingStepsRef.current[sid];
            delete stepStartRef.current[sid];
          }
          setTimeout(() => setError(null), 6000);
          break;
        }

        case "unloaded": {
          const sid = msg.session_id as string;
          if (!sid) break;
          removeTab(sid);
          delete pendingStepsRef.current[sid];
          delete stepStartRef.current[sid];
          setSessionsData((prev) => {
            const next = { ...prev };
            delete next[sid];
            return next;
          });
          if (activeSessionIdRef.current === sid) {
            setActiveSessionId(null);
            setWorkspace("");
          }
          break;
        }

        case "pong":
        case "exit":
          break;
      }
    },
    [notify, pushTab, removeTab, send]
  );

  // -- Connect ---------------------------------------------------
  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    setStatus("connecting");
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus("connected");
      setError(null);
      ws.send(JSON.stringify({ type: "connect" }));
      ws.send(JSON.stringify({ type: "list_sessions" }));
    };

    ws.onmessage = (event) => {
      try {
        handleMsg(JSON.parse(event.data) as ServerMessage);
      } catch {
        // Ignore malformed protocol messages.
      }
    };

    ws.onclose = () => {
      setStatus("disconnected");
      wsRef.current = null;
      reconnectRef.current = setTimeout(connect, 2000);
    };

    ws.onerror = () => ws.close();
  }, [handleMsg]);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  // -- Actions ---------------------------------------------------
  const sendMessage = useCallback(
    (sessionId: string, content: string, imageUrls?: string[]) => {
      if (!content.trim() && (!imageUrls || imageUrls.length === 0)) return;
      if (!sessionId) return;

      const sessionWorkspace =
        sessionsDataRef.current[sessionId]?.workspace ||
        sessionsRef.current.find((s) => s.id === sessionId)?.workspace ||
        workspaceRef.current;

      setSessionsData((prev) => {
        const sd = prev[sessionId] || emptySessionData(sessionWorkspace);
        return {
          ...prev,
          [sessionId]: {
            ...sd,
            workspace: sd.workspace || sessionWorkspace,
            messages: [
              ...sd.messages,
              {
                id: uid(),
                role: "user" as const,
                content,
                image_urls: imageUrls && imageUrls.length > 0 ? imageUrls : undefined,
              },
            ],
          },
        };
      });

      send({
        type: "message",
        session_id: sessionId,
        content,
        image_urls: imageUrls && imageUrls.length > 0 ? imageUrls : undefined,
        workspace: sessionWorkspace || undefined,
      });
    },
    [send]
  );

  const newSession = useCallback(
    (ws?: string) => {
      // Creating a thread must not interrupt or unload other loaded sessions;
      // the gateway keeps every session addressable by its session_id.
      send({ type: "new_session", workspace: ws });
    },
    [send]
  );

  const loadSession = useCallback(
    (id: string) => {
      const existing = sessionsDataRef.current[id];
      const serverSession = sessionsRef.current.find((s) => s.id === id);
      const knownWorkspace = existing?.workspace || serverSession?.workspace || "";

      setActiveSessionId(id);
      setWorkspace(knownWorkspace);

      if (existing) {
        // Loaded sessions are already live in this WebSocket. Switching focus
        // is local and must never interrupt their background stream.
        if (!existing.loading) return;
        return;
      }

      setSessionsData((prev) => ({
        ...prev,
        [id]: { ...emptySessionData(knownWorkspace), loading: true },
      }));
      pushTab(id);
      send({ type: "load_session", session_id: id });
    },
    [pushTab, send]
  );

  const interrupt = useCallback(
    (sessionId?: string) => {
      const sid = sessionId || activeSessionIdRef.current;
      if (sid) send({ type: "interrupt", session_id: sid });
    },
    [send]
  );

  const openNewTab = useCallback(() => {
    setActiveSessionId(null);
    setWorkspace("");
  }, []);

  const closeTab = useCallback(
    (id: string) => {
      send({ type: "interrupt", session_id: id });
      send({ type: "unload", session_id: id });
      removeTab(id);
      if (activeSessionIdRef.current === id) {
        setActiveSessionId(null);
        setWorkspace("");
      }
    },
    [removeTab, send]
  );

  const unloadSession = closeTab;

  // Kept as a compatibility action for callers that used the old name. It
  // now opens an empty tab instead of destroying every loaded session.
  const newThread = openNewTab;

  return {
    status,
    sessions,
    activeSessionId,
    workspace,
    workspaces,
    homeDir,
    error,
    notifications,
    tabSessions,
    sessionViews,
    messages: activeData?.messages || [],
    streaming: activeData?.streaming || false,
    streamText: activeData?.streamText || "",
    toolCalls: activeData?.toolCalls || [],
    completedSteps: activeData?.completedSteps || [],
    tokens: activeData?.tokens || null,
    loadingSession: activeData?.loading || false,
    sendMessage,
    newThread,
    newSession,
    loadSession,
    unloadSession,
    interrupt,
    openNewTab,
    closeTab,
  };
}
