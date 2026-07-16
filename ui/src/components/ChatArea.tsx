import React, { useRef, useEffect, useState, useCallback } from "react";
import type { UIMessage, ToolCallInfo, TurnStep } from "../types";
import { renderMarkdown } from "../markdown";

// ============================================================
// ChatArea
// ============================================================

interface ChatAreaProps {
  messages: UIMessage[];
  streaming: boolean;
  streamText: string;
  toolCalls: ToolCallInfo[];
  completedSteps: TurnStep[];
  workspace: string;
  hasSession: boolean;
  loadingSession: boolean;
}

export function ChatArea({
  messages,
  streaming,
  streamText,
  toolCalls,
  completedSteps,
  workspace,
  hasSession,
  loadingSession,
}: ChatAreaProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom on new messages/streaming
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, streamText, toolCalls, completedSteps]);

  // Loading state — show skeleton placeholders
  if (loadingSession && messages.length === 0) {
    return (
      <div className="chat-area">
        <div className="chat-messages">
          <SkeletonMessage align="user" width="35%" />
          <div className="msg-spacer" />
          <SkeletonMessage align="assistant" width="70%" lines={3} />
          <div className="msg-spacer" />
          <SkeletonMessage align="user" width="50%" />
          <div className="msg-spacer" />
          <SkeletonMessage align="assistant" width="60%" lines={2} />
          <div className="msg-spacer" />
          <SkeletonMessage align="user" width="25%" />
          <div className="msg-spacer" />
          <SkeletonMessage align="assistant" width="55%" lines={4} />
        </div>
      </div>
    );
  }

  // Empty state
  if (!hasSession || messages.length === 0) {
    return (
      <div className="chat-area">
        <div className="chat-empty">
          <svg width="28" height="28" viewBox="0 0 32 32" fill="none">
            <path d="M16 4L4 10v12l12 6 12-6V10L16 4z" stroke="#4a9eff" strokeWidth="1.5" fill="none" />
            <circle cx="16" cy="16" r="4" fill="#4a9eff" opacity="0.3" />
            <path d="M16 12v8M12 16h8" stroke="#4a9eff" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          <div className="chat-empty-title">Let's build</div>
          {workspace && (
            <div className="chat-empty-workspace">
              <svg width="11" height="11" viewBox="0 0 16 16" fill="none">
                <path d="M2 4l6-2 6 2v8l-6 2-6-2V4z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
              </svg>
              {workspace.split(/[/\\]/).filter(Boolean).pop()}
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="chat-area">
      <div ref={scrollRef} className="chat-messages">
        {messages.map((msg) => (
          <React.Fragment key={msg.id}>
            {msg.role === "user" && <UserMessage content={msg.content} imageUrls={msg.image_urls} />}
            {msg.role === "assistant" && (
              <AssistantMessage content={msg.content} turn={msg.turn} />
            )}
            {msg.role === "system" && <SystemMessage content={msg.content} />}
            <div className="msg-spacer" />
          </React.Fragment>
        ))}

        {(streaming || completedSteps.length > 0) && (
          <div className="msg-assistant">
            <div className="turn-container">
              {completedSteps.map((step, i) => (
                <ThinkingBlock key={i} step={step} />
              ))}

              {streaming && toolCalls.length > 0 && !streamText && (
                <div className="thinking-dots">thinking…</div>
              )}

              {streaming && toolCalls.length > 0 && streamText && (
                <div className="turn-thinking-body">
                  <div className="turn-thinking-item">
                    <span>{streamText}</span>
                  </div>
                </div>
              )}

              {streaming && toolCalls.length > 0 && (
                <div className="turn-tools">
                  {toolCalls.map((tc, i) => (
                    <ToolBadge key={i} tool={tc} />
                  ))}
                </div>
              )}

              {streaming && toolCalls.length === 0 && streamText && (
                <div className="turn-response">
                  {renderMarkdown(streamText)}
                  <span className="stream-cursor">▌</span>
                </div>
              )}
            </div>
          </div>
        )}

        <div className="msg-spacer" />
      </div>
    </div>
  );
}

// ============================================================
// Sub-components
// ============================================================

function UserMessage({ content, imageUrls }: { content: string; imageUrls?: string[] }) {
  return (
    <div className="msg-user">
      <div className="msg-user-bubble">
        {imageUrls && imageUrls.length > 0 && (
          <div className="msg-user-images">
            {imageUrls.map((url, i) => (
              <img key={i} src={url} alt={`Attached ${i + 1}`} className="msg-user-image" />
            ))}
          </div>
        )}
        {content && <div className="msg-user-text">{content}</div>}
      </div>
    </div>
  );
}

function AssistantMessage({
  content,
  turn,
}: {
  content: string;
  turn?: { steps: TurnStep[]; response: string | null };
}) {
  if (!turn) {
    return (
      <div className="msg-assistant">
        <div className="turn-container">
          <div className="turn-response">{renderMarkdown(content)}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="msg-assistant">
      <div className="turn-container">
        {turn.steps.map((step, i) => (
          <ThinkingBlock key={i} step={step} />
        ))}
        {turn.response && (
          <div className="turn-response">{renderMarkdown(turn.response)}</div>
        )}
      </div>
    </div>
  );
}

function SystemMessage({ content }: { content: string }) {
  return (
    <div className="msg-system">
      <div className="msg-system-text">{content}</div>
    </div>
  );
}

// ============================================================
// Thinking Block (collapsible)
// ============================================================

function ThinkingBlock({ step }: { step: TurnStep }) {
  const [expanded, setExpanded] = useState(false);

  const durationSec = (step.durationMs / 1000).toFixed(1);
  const hasThinking = step.thinking.trim().length > 0;
  const hasTools = step.toolCalls.length > 0;

  // Thinking only
  if (hasThinking && !hasTools) {
    return (
      <div>
        <button
          className={`turn-thinking-toggle${expanded ? " expanded" : ""}`}
          onClick={() => setExpanded(!expanded)}
        >
          <span className="turn-thinking-toggle-icon">▶</span>
          Thought for {durationSec}s
        </button>
        {expanded && (
          <div className="turn-thinking-body">
            <div className="turn-thinking-item">
              <span>{step.thinking}</span>
            </div>
            <div className="turn-thinking-done">
              <span className="turn-thinking-done-icon">✓</span>
              Done
            </div>
          </div>
        )}
      </div>
    );
  }

  // Has tools (with or without thinking) — always show toggle + tools
  if (hasTools) {
    return (
      <div>
        <button
          className={`turn-thinking-toggle${expanded ? " expanded" : ""}`}
          onClick={() => setExpanded(!expanded)}
        >
          <span className="turn-thinking-toggle-icon">▶</span>
          Thought for {durationSec}s
        </button>
        {expanded && hasThinking && (
          <div className="turn-thinking-body">
            <div className="turn-thinking-item">
              <span>{step.thinking}</span>
            </div>
            <div className="turn-thinking-done">
              <span className="turn-thinking-done-icon">✓</span>
              Done
            </div>
          </div>
        )}
        <div className="turn-tools">
          {step.toolCalls.map((tc, i) => (
            <ToolBadge key={i} tool={tc} />
          ))}
        </div>
      </div>
    );
  }

  return null;
}

// ============================================================
// Tool Badge
// ============================================================

function ToolBadge({ tool }: { tool: ToolCallInfo }) {
  return (
    <div className="turn-tool-badge">
      <span className="turn-tool-badge-icon">▸</span>
      <span className="turn-tool-badge-summary">{tool.summary}</span>
    </div>
  );
}

// ============================================================
// Skeleton Loading Placeholder
// ============================================================

function SkeletonMessage({ align, width, lines = 1 }: { align: "user" | "assistant"; width: string; lines?: number }) {
  return (
    <div className={`msg-${align}`}>
      {align === "user" ? (
        <div className="skeleton-bubble skeleton-user" style={{ width }}>
          {Array.from({ length: lines }).map((_, i) => (
            <div key={i} className="skeleton-line" style={{ width: i === lines - 1 ? "60%" : "100%" }} />
          ))}
        </div>
      ) : (
        <div className="skeleton-assistant" style={{ width }}>
          {Array.from({ length: lines }).map((_, i) => (
            <div key={i} className="skeleton-line" style={{ width: i === lines - 1 ? "65%" : "100%" }} />
          ))}
        </div>
      )}
    </div>
  );
}
