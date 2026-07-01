import React, { useState, useRef, useCallback } from "react";

interface ChatInputProps {
  onSend: (content: string) => void;
  onInterrupt: () => void;
  streaming: boolean;
  disabled?: boolean;
}

export function ChatInput({ onSend, onInterrupt, streaming, disabled }: ChatInputProps) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSubmit = useCallback(() => {
    const trimmed = text.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setText("");
    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  }, [text, onSend]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (streaming) {
          onInterrupt();
        } else {
          handleSubmit();
        }
      }
    },
    [handleSubmit, streaming, onInterrupt]
  );

  const handleInput = useCallback(() => {
    const ta = textareaRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
    }
  }, []);

  return (
    <div style={styles.container}>
      <div style={styles.inputCard}>
        <textarea
          ref={textareaRef}
          style={styles.textarea}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          onInput={handleInput}
          placeholder="Ask anything..."
          rows={1}
          disabled={disabled}
        />
        <div style={styles.bottomRow}>
          <div style={styles.leftActions}>
            {/* Attach button placeholder */}
            <button style={styles.iconBtn} title="Attach file">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </button>
          </div>
          <div style={styles.rightActions}>
            {streaming ? (
              <button style={styles.stopBtn} onClick={onInterrupt} title="Stop generation">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
                  <rect x="3" y="3" width="10" height="10" rx="2" />
                </svg>
              </button>
            ) : (
              <button
                style={{
                  ...styles.sendBtn,
                  opacity: text.trim() ? 1 : 0.4,
                }}
                onClick={handleSubmit}
                disabled={!text.trim() || disabled}
                title="Send message"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                  <path d="M12 19V5M5 12l7-7 7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    padding: "0 24px 16px",
    flexShrink: 0,
  },
  inputCard: {
    background: "#1e1e1e",
    border: "1px solid #333",
    borderRadius: 16,
    padding: "12px 16px 8px",
    maxWidth: 720,
    margin: "0 auto",
  },
  textarea: {
    width: "100%",
    background: "transparent",
    border: "none",
    outline: "none",
    color: "#e0e0e0",
    fontSize: 14,
    lineHeight: 1.5,
    fontFamily: "inherit",
    resize: "none",
    minHeight: 24,
    maxHeight: 200,
  },
  bottomRow: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    marginTop: 8,
  },
  leftActions: {
    display: "flex",
    gap: 4,
  },
  rightActions: {
    display: "flex",
    gap: 8,
    alignItems: "center",
  },
  iconBtn: {
    background: "transparent",
    border: "none",
    color: "#666",
    cursor: "pointer",
    padding: 4,
    borderRadius: 6,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    transition: "color 0.15s",
  },
  sendBtn: {
    width: 32,
    height: 32,
    borderRadius: "50%",
    background: "#4a9eff",
    border: "none",
    color: "#fff",
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    transition: "opacity 0.15s",
  },
  stopBtn: {
    width: 32,
    height: 32,
    borderRadius: "50%",
    background: "#ff4a4a",
    border: "none",
    color: "#fff",
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
  },
};
