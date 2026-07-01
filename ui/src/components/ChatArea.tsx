import React, { useRef, useEffect } from "react";
import type { ChatMessage, ToolCallInfo } from "../types";
import { MessageBubble } from "./MessageBubble";

interface ChatAreaProps {
  messages: ChatMessage[];
  streaming: boolean;
  streamText: string;
  toolCalls: ToolCallInfo[];
  workspace: string;
  hasSession: boolean;
}

export function ChatArea({
  messages,
  streaming,
  streamText,
  toolCalls,
  workspace,
  hasSession,
}: ChatAreaProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, streamText, toolCalls]);

  // Empty state — show landing screen
  if (!hasSession || messages.length === 0) {
    return (
      <div style={styles.container}>
        <div style={styles.emptyState}>
          <div style={styles.logoIcon}>
            <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
              <path
                d="M16 4L4 10v12l12 6 12-6V10L16 4z"
                stroke="#4a9eff"
                strokeWidth="1.5"
                fill="none"
              />
              <circle cx="16" cy="16" r="4" fill="#4a9eff" opacity="0.3" />
              <path
                d="M16 12v8M12 16h8"
                stroke="#4a9eff"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
            </svg>
          </div>
          <div style={styles.emptyTitle}>Let's build</div>
          {workspace && (
            <div style={styles.emptyWorkspace}>
              <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                <path
                  d="M2 4l6-2 6 2v8l-6 2-6-2V4z"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinejoin="round"
                />
              </svg>
              {workspace.split(/[/\\]/).filter(Boolean).pop()}
            </div>
          )}
        </div>
      </div>
    );
  }

  // Messages view
  return (
    <div style={styles.container}>
      <div ref={scrollRef} style={styles.messages}>
        {messages.map((msg) => (
          <React.Fragment key={msg.id}>
            <MessageBubble role={msg.role} content={msg.content} />
            <div style={styles.messageSpacer} />
          </React.Fragment>
        ))}

        {/* Active tool calls during streaming */}
        {streaming && toolCalls.length > 0 && (
          <div style={styles.toolCalls}>
            {toolCalls.map((tc, i) => (
              <div key={i} style={styles.toolBadge}>
                <span style={styles.toolIcon}>▸</span>
                {tc.summary}
              </div>
            ))}
          </div>
        )}

        {/* Streaming text */}
        {streaming && streamText && (
          <MessageBubble role="assistant" content={streamText} isStreaming />
        )}
        {streaming && !streamText && toolCalls.length === 0 && (
          <div style={styles.thinking}>
            <span style={styles.thinkingDots}>thinking</span>
          </div>
        )}
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
  },
  emptyState: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    gap: 16,
    color: "#555",
  },
  logoIcon: {
    width: 64,
    height: 64,
    borderRadius: 20,
    background: "linear-gradient(135deg, #2a2a3a, #1a1a2a)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    border: "1px solid #333",
  },
  emptyTitle: {
    fontSize: 22,
    fontWeight: 600,
    color: "#888",
  },
  emptyWorkspace: {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    fontSize: 13,
    color: "#666",
    background: "#1e1e1e",
    padding: "6px 14px",
    borderRadius: 8,
    border: "1px solid #2a2a2a",
    marginTop: 4,
  },
  messages: {
    flex: 1,
    overflowY: "auto",
    padding: "16px 24px 8px",
    display: "flex",
    flexDirection: "column",
  },
  messageSpacer: {
    height: 12,
  },
  toolCalls: {
    padding: "4px 0 8px 48px",
    display: "flex",
    flexDirection: "column",
    gap: 4,
  },
  toolBadge: {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    fontSize: 12,
    color: "#666",
    fontFamily: "'SF Mono', 'Fira Code', Consolas, monospace",
    padding: "2px 0",
  },
  toolIcon: {
    color: "#4a9eff",
    fontSize: 10,
  },
  thinking: {
    padding: "8px 0",
    color: "#555",
    fontSize: 13,
    fontStyle: "italic",
  },
  thinkingDots: {
    opacity: 0.6,
  },
};
