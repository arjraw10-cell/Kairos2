import React from "react";
import type { ConnectionStatus, TokenInfo } from "../types";
import { toHomeRelative } from "../utils";

interface StatusBarProps {
  connectionStatus: ConnectionStatus;
  tokens: TokenInfo | null;
  workspace: string;
  streaming: boolean;
  homeDir: string;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export function StatusBar({
  connectionStatus,
  tokens,
  workspace,
  streaming,
  homeDir,
}: StatusBarProps) {
  const dotColor =
    { connected: "var(--success)", connecting: "var(--warning)", disconnected: "var(--error-text)" }[
      connectionStatus
    ];

  const label =
    { connected: "Connected", connecting: "Connecting\u2026", disconnected: "Disconnected" }[
      connectionStatus
    ];

  const wsLabel = workspace ? toHomeRelative(workspace, homeDir) : "";

  return (
    <div className="status-bar">
      <div className="status-bar-left">
        <span className="status-dot" style={{ background: dotColor }} />
        <span>{label}</span>
        {wsLabel && (
          <>
            <span className="status-separator">&middot;</span>
            <span>{wsLabel}</span>
          </>
        )}
        {streaming && (
          <>
            <span className="status-separator">&middot;</span>
            <span className="status-streaming">streaming</span>
          </>
        )}
      </div>
      <div className="status-bar-right">
        {tokens && (
          <>
            <span className="status-tokens">
              {"\u2191"} {formatTokens(tokens.sessionInput)}
            </span>
            <span className="status-tokens">
              {"\u2193"} {formatTokens(tokens.sessionOutput)}
            </span>
            <span className="status-separator">&middot;</span>
            <span className="status-tokens">{tokens.contextPct}%</span>
          </>
        )}
      </div>
    </div>
  );
}
