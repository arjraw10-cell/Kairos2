import React, { useEffect } from "react";
import { useGateway } from "./hooks/useGateway";
import { Sidebar } from "./components/Sidebar";
import { ChatArea } from "./components/ChatArea";
import { ChatInput } from "./components/ChatInput";
import { StatusBar } from "./components/StatusBar";

export function App() {
  const gw = useGateway();

  // Global Escape key handler — interrupt streaming / stop agent
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && gw.streaming) {
        e.preventDefault();
        gw.interrupt();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [gw.streaming, gw.interrupt]);

  return (
    <div style={styles.app}>
      {/* Error toast */}
      {gw.error && (
        <div style={styles.toast}>
          <span style={styles.toastIcon}>⚠</span>
          {gw.error}
        </div>
      )}

      {/* Sidebar */}
      <Sidebar
        sessions={gw.sessions}
        currentSessionId={gw.currentSessionId}
        onSelectSession={gw.loadSession}
        onNewThread={() => gw.newSession()}
        workspace={gw.workspace}
      />

      {/* Main area */}
      <div style={styles.main}>
        {/* Header */}
        <div style={styles.header} className="titlebar-drag">
          <div style={styles.headerTitle}>
            {gw.currentSessionId
              ? gw.messages[0]?.content?.slice(0, 60) || "New thread"
              : "New thread"}
          </div>
        </div>

        {/* Chat messages */}
        <ChatArea
          messages={gw.messages}
          streaming={gw.streaming}
          streamText={gw.streamText}
          toolCalls={gw.toolCalls}
          workspace={gw.workspace}
          hasSession={!!gw.currentSessionId}
        />

        {/* Input */}
        <ChatInput
          onSend={gw.sendMessage}
          onInterrupt={gw.interrupt}
          streaming={gw.streaming}
          disabled={gw.status !== "connected"}
        />

        {/* Status bar */}
        <StatusBar
          connectionStatus={gw.status}
          tokens={gw.tokens}
          workspace={gw.workspace}
          streaming={gw.streaming}
        />
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  app: {
    display: "flex",
    height: "100vh",
    width: "100vw",
    overflow: "hidden",
    background: "#1a1a1a",
  },
  main: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
  },
  header: {
    height: 38,
    display: "flex",
    alignItems: "center",
    padding: "0 24px",
    flexShrink: 0,
  },
  headerTitle: {
    fontSize: 13,
    color: "#888",
    fontWeight: 500,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap" as const,
  },
  toast: {
    position: "fixed",
    top: 48,
    right: 24,
    zIndex: 1000,
    background: "#3a1a1a",
    border: "1px solid #5a2a2a",
    borderRadius: 8,
    padding: "10px 16px",
    fontSize: 13,
    color: "#ff8a8a",
    display: "flex",
    alignItems: "center",
    gap: 8,
    boxShadow: "0 4px 12px rgba(0,0,0,0.4)",
    animation: "fadeIn 0.2s ease",
  },
  toastIcon: {
    fontSize: 16,
  },
};
