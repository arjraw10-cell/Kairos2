import React from "react";
import type { Session } from "../types";

interface SidebarProps {
  sessions: Session[];
  currentSessionId: string | null;
  onSelectSession: (id: string) => void;
  onNewThread: () => void;
  workspace: string;
}

export function Sidebar({
  sessions,
  currentSessionId,
  onSelectSession,
  onNewThread,
  workspace,
}: SidebarProps) {
  // Group sessions by workspace
  const grouped = sessions.reduce<Record<string, Session[]>>((acc, s) => {
    const ws = s.workspace || "Default";
    // Extract just the last folder name for display
    const folder = ws.split(/[/\\]/).filter(Boolean).pop() || ws;
    if (!acc[folder]) acc[folder] = [];
    acc[folder].push(s);
    return acc;
  }, {});

  return (
    <div style={styles.container}>
      {/* Title bar drag region */}
      <div style={styles.titlebar} className="titlebar-drag" />

      {/* New thread button */}
      <div style={styles.newThread} onClick={onNewThread}>
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" style={{ marginRight: 8 }}>
          <path d="M8 1v14M1 8h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
        New thread
      </div>

      {/* Session list */}
      <div style={styles.sessionList}>
        {Object.entries(grouped).map(([folder, folderSessions]) => (
          <div key={folder} style={styles.group}>
            <div style={styles.groupHeader}>
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" style={{ marginRight: 6, opacity: 0.5 }}>
                <path d="M2 4h12M2 8h12M2 12h8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
              {folder}
            </div>
            {folderSessions.map((s) => (
              <div
                key={s.id}
                style={{
                  ...styles.sessionItem,
                  ...(s.id === currentSessionId ? styles.sessionItemActive : {}),
                }}
                onClick={() => onSelectSession(s.id)}
              >
                {s.id === currentSessionId && <span style={styles.activeDot} />}
                <span style={styles.sessionPreview}>
                  {s.preview || s.id.replace("chat_", "")}
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>

      {/* Bottom workspace indicator */}
      <div style={styles.bottomBar}>
        <div style={styles.workspaceLabel}>
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" style={{ marginRight: 6, opacity: 0.5 }}>
            <path d="M2 4l6-2 6 2v8l-6 2-6-2V4z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
          </svg>
          {workspace.split(/[/\\]/).filter(Boolean).pop() || "workspace"}
        </div>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    width: 280,
    minWidth: 280,
    background: "#111111",
    borderRight: "1px solid #2a2a2a",
    display: "flex",
    flexDirection: "column",
    height: "100%",
    overflow: "hidden",
  },
  titlebar: {
    height: 38,
    flexShrink: 0,
  },
  newThread: {
    display: "flex",
    alignItems: "center",
    padding: "10px 16px",
    margin: "4px 12px 8px",
    borderRadius: 8,
    cursor: "pointer",
    fontSize: 14,
    fontWeight: 500,
    color: "#e0e0e0",
    background: "transparent",
    border: "1px solid #333",
    transition: "background 0.15s",
  },
  sessionList: {
    flex: 1,
    overflowY: "auto",
    padding: "0 12px",
  },
  group: {
    marginBottom: 8,
  },
  groupHeader: {
    display: "flex",
    alignItems: "center",
    padding: "6px 4px",
    fontSize: 12,
    fontWeight: 600,
    color: "#888",
    textTransform: "uppercase",
    letterSpacing: "0.05em",
  },
  sessionItem: {
    display: "flex",
    alignItems: "center",
    padding: "7px 8px",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 13,
    color: "#bbb",
    transition: "background 0.12s",
    gap: 6,
  },
  sessionItemActive: {
    background: "#1e1e1e",
    color: "#fff",
  },
  activeDot: {
    width: 6,
    height: 6,
    borderRadius: "50%",
    background: "#4a9eff",
    flexShrink: 0,
  },
  sessionPreview: {
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap" as const,
  },
  bottomBar: {
    padding: "10px 16px",
    borderTop: "1px solid #2a2a2a",
    flexShrink: 0,
  },
  workspaceLabel: {
    display: "flex",
    alignItems: "center",
    fontSize: 12,
    color: "#666",
  },
};
