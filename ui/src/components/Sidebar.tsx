import React from "react";
import type { Session } from "../types";
import { toHomeRelative } from "../utils";

interface SidebarProps {
  sessions: Session[];
  activeSessionId: string | null;
  streaming: boolean;
  onSelectSession: (id: string) => void;
  onCloseSession: (id: string) => void;
  onNewThread: () => void;
  homeDir: string;
  workspace: string;
}

export function Sidebar({
  sessions,
  activeSessionId,
  streaming,
  onSelectSession,
  onCloseSession,
  onNewThread,
  homeDir,
  workspace,
}: SidebarProps) {
  // Group by workspace
  const grouped = sessions.reduce<Record<string, Session[]>>((acc, s) => {
    const ws = s.workspace || "default";
    if (!acc[ws]) acc[ws] = [];
    acc[ws].push(s);
    return acc;
  }, {});

  return (
    <div className="sidebar">
      <div className="sidebar-titlebar titlebar-drag" />

      <button className="sidebar-new-btn" onClick={onNewThread}>
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
          <path d="M8 1v14M1 8h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
        New thread
      </button>

      <div className="sidebar-sessions">
        {Object.entries(grouped).map(([wsPath, wsSessions]) => (
          <div key={wsPath} className="sidebar-group">
            <div className="sidebar-group-header">
              <svg width="12" height="12" viewBox="0 0 16 16" fill="none" style={{ opacity: 0.5 }}>
                <path d="M2 4l6-2 6 2v8l-6 2-6-2V4z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
              </svg>
              {toHomeRelative(wsPath, homeDir)}
            </div>
            {wsSessions.map((s) => {
              const isActive = s.id === activeSessionId;
              // Show streaming indicator on active session or any session marked running on server
              const isStreamingThis = isActive ? streaming : !!s.active;
              return (
                <div
                  key={s.id}
                  className={`sidebar-session${isActive ? " active" : ""}`}
                  onClick={() => onSelectSession(s.id)}
                >
                  {isStreamingThis ? (
                    <span className="sidebar-session-dot streaming" />
                  ) : isActive ? (
                    <span className="sidebar-session-dot" />
                  ) : null}
                  <span className="sidebar-session-label">
                    {s.preview || s.id.replace("chat_", "")}
                  </span>
                  {isActive && (
                    <button
                      className="sidebar-session-close"
                      onClick={(e) => {
                        e.stopPropagation();
                        onCloseSession(s.id);
                      }}
                      title="Close session"
                    >
                      <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor">
                        <path d="M4.646 4.646a.5.5 0 0 1 .708 0L8 7.293l2.646-2.647a.5.5 0 0 1 .708.708L8.707 8l2.647 2.646a.5.5 0 0 1-.708.708L8 8.707l-2.646 2.647a.5.5 0 0 1-.708-.708L7.293 8 4.646 5.354a.5.5 0 0 1 0-.708z" />
                      </svg>
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>

      {workspace && (
        <div className="sidebar-footer">
          <div className="sidebar-workspace-badge">
            <svg width="11" height="11" viewBox="0 0 16 16" fill="none" style={{ opacity: 0.5 }}>
              <path d="M2 4l6-2 6 2v8l-6 2-6-2V4z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
            </svg>
            {toHomeRelative(workspace, homeDir)}
          </div>
        </div>
      )}
    </div>
  );
}
