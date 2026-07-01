import React from "react";
import type { ConnectionStatus, TokenInfo } from "../types";

interface StatusBarProps {
  connectionStatus: ConnectionStatus;
  tokens: TokenInfo | null;
  workspace: string;
  streaming: boolean;
}

function formatTokens(n: number): string {
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

export function StatusBar({ connectionStatus, tokens, workspace, streaming }: StatusBarProps) {
  const statusColor = {
    connected: "#4caf50",
    connecting: "#ff9800",
    disconnected: "#f44336",
  }[connectionStatus];

  const statusLabel = {
    connected: "Connected",
    connecting: "Connecting...",
    disconnected: "Disconnected",
  }[connectionStatus];

  const folderName = workspace.split(/[/\\]/).filter(Boolean).pop() || "workspace";

  return (
    <div style={styles.bar}>
      <div style={styles.left}>
        <span style={styles.statusDot(statusColor)} />
        <span style={styles.label}>{statusLabel}</span>
        <span style={styles.separator}>·</span>
        <span style={styles.label}>{folderName}</span>
        {streaming && (
          <>
            <span style={styles.separator}>·</span>
            <span style={styles.streamingLabel}>streaming</span>
          </>
        )}
      </div>
      <div style={styles.right}>
        {tokens && (
          <>
            <span style={styles.tokenStat}>
              ↑ {formatTokens(tokens.sessionInput)}
            </span>
            <span style={styles.tokenStat}>
              ↓ {formatTokens(tokens.sessionOutput)}
            </span>
            <span style={styles.separator}>·</span>
            <span style={styles.tokenStat}>
              {tokens.contextPct}%
            </span>
          </>
        )}
      </div>
    </div>
  );
}

const styles: Record<string, any> = {
  bar: {
    height: 28,
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "0 16px",
    background: "#111111",
    borderTop: "1px solid #2a2a2a",
    fontSize: 11,
    color: "#666",
    flexShrink: 0,
  },
  left: {
    display: "flex",
    alignItems: "center",
    gap: 6,
  },
  right: {
    display: "flex",
    alignItems: "center",
    gap: 8,
  },
  statusDot: (color: string) => ({
    width: 6,
    height: 6,
    borderRadius: "50%",
    background: color,
    flexShrink: 0,
  }),
  label: {
    color: "#666",
  },
  separator: {
    color: "#444",
  },
  tokenStat: {
    fontFamily: "'SF Mono', 'Fira Code', Consolas, monospace",
    color: "#555",
  },
  streamingLabel: {
    color: "#4a9eff",
    fontStyle: "italic",
  },
};
