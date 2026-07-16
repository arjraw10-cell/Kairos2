import React, { useState, useCallback } from "react";
import { toHomeRelative } from "../utils";

const DEFAULT_WORKSPACE = "kairos-workspace";

interface WorkspacePickerProps {
  workspaces: string[];
  homeDir: string;
  onSelect: (workspace: string) => void;
  connectionStatus: string;
}

export function WorkspacePicker({
  workspaces,
  homeDir,
  onSelect,
  connectionStatus,
}: WorkspacePickerProps) {
  const [customPath, setCustomPath] = useState("");
  const [showCustom, setShowCustom] = useState(false);

  const displayWorkspaces = (() => {
    const def = workspaces.find((ws) => ws.endsWith(DEFAULT_WORKSPACE));
    const rest = workspaces.filter((ws) => !ws.endsWith(DEFAULT_WORKSPACE));
    return def ? [def, ...rest] : [DEFAULT_WORKSPACE, ...workspaces];
  })();

  const openCustom = useCallback(() => {
    setCustomPath(homeDir ? homeDir + "\\" : "");
    setShowCustom(true);
  }, [homeDir]);

  const submitCustom = useCallback(() => {
    const trimmed = customPath.trim();
    if (!trimmed) return;
    let resolved = trimmed;
    const looksRelative =
      !trimmed.match(/^[A-Za-z]:/) && !trimmed.startsWith("/");
    if (looksRelative && homeDir) {
      resolved = homeDir + "\\" + trimmed;
    }
    onSelect(resolved);
  }, [customPath, homeDir, onSelect]);

  const onCustomKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submitCustom();
      }
    },
    [submitCustom]
  );

  const placeholder = homeDir
    ? `${homeDir}\\project-name`
    : "C:\\Users\\you\\project";

  return (
    <div className="picker-overlay">
      <div className="picker-card">
        <div className="picker-title">Choose a workspace</div>
        <div className="picker-subtitle">
          Paths are relative to{" "}
          <span className="picker-home">{homeDir || "~"}</span>
        </div>

        <div className="picker-list">
          {displayWorkspaces.map((ws) => {
            const isDefault = ws.endsWith(DEFAULT_WORKSPACE);
            const label = toHomeRelative(ws, homeDir);
            return (
              <div key={ws} className="picker-item" onClick={() => onSelect(ws)}>
                <div className="picker-item-icon">
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                    <path
                      d="M2 4l6-2 6 2v8l-6 2-6-2V4z"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinejoin="round"
                    />
                  </svg>
                </div>
                <div className="picker-item-info">
                  <div className="picker-item-name">
                    {label}
                    {isDefault && (
                      <span className="picker-item-badge">default</span>
                    )}
                  </div>
                  <div className="picker-item-path">{ws}</div>
                </div>
                <svg
                  width="12"
                  height="12"
                  viewBox="0 0 16 16"
                  fill="none"
                  className="picker-item-arrow"
                >
                  <path
                    d="M6 3l5 5-5 5"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </div>
            );
          })}
        </div>

        {!showCustom ? (
          <button className="picker-custom-btn" onClick={openCustom}>
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
              <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
            Enter a different path
          </button>
        ) : (
          <div className="picker-custom-row">
            <input
              type="text"
              className="picker-custom-input"
              value={customPath}
              onChange={(e) => setCustomPath(e.target.value)}
              onKeyDown={onCustomKeyDown}
              placeholder={placeholder}
              autoFocus
            />
            <button
              className="picker-go-btn"
              onClick={submitCustom}
              disabled={!customPath.trim()}
            >
              Go
            </button>
          </div>
        )}

        {connectionStatus !== "connected" && (
          <div className="picker-status">
            <span
              className="picker-status-dot"
              style={{
                background:
                  connectionStatus === "connecting"
                    ? "var(--warning)"
                    : "var(--error-text)",
              }}
            />
            {connectionStatus === "connecting"
              ? "Connecting to gateway\u2026"
              : "Disconnected from gateway"}
          </div>
        )}
      </div>
    </div>
  );
}
