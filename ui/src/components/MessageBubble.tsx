import React, { useMemo } from "react";

interface MessageBubbleProps {
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  isStreaming?: boolean;
}

// Simple markdown-like renderer (no deps)
function renderContent(text: string): React.ReactNode[] {
  const lines = text.split("\n");
  const elements: React.ReactNode[] = [];
  let inCodeBlock = false;
  let codeLines: string[] = [];
  let codeLang = "";

  lines.forEach((line, i) => {
    // Code blocks
    if (line.startsWith("```")) {
      if (inCodeBlock) {
        elements.push(
          <pre key={`code-${i}`} style={styles.codeBlock}>
            <code>{codeLines.join("\n")}</code>
          </pre>
        );
        codeLines = [];
        inCodeBlock = false;
      } else {
        inCodeBlock = true;
        codeLang = line.slice(3).trim();
      }
      return;
    }

    if (inCodeBlock) {
      codeLines.push(line);
      return;
    }

    // Empty lines
    if (!line.trim()) {
      elements.push(<div key={`br-${i}`} style={{ height: 8 }} />);
      return;
    }

    // Headers
    if (line.startsWith("## ")) {
      elements.push(
        <h3 key={`h-${i}`} style={styles.h3}>
          {line.slice(3)}
        </h3>
      );
      return;
    }
    if (line.startsWith("### ")) {
      elements.push(
        <h4 key={`h-${i}`} style={styles.h4}>
          {line.slice(4)}
        </h4>
      );
      return;
    }

    // Bullet points
    if (line.match(/^[\s]*[-*]\s/)) {
      const text = line.replace(/^[\s]*[-*]\s/, "");
      elements.push(
        <div key={`li-${i}`} style={styles.bullet}>
          <span style={styles.bulletDot}>•</span>
          <span>{renderInline(text)}</span>
        </div>
      );
      return;
    }

    // Numbered lists
    if (line.match(/^[\s]*\d+\.\s/)) {
      const match = line.match(/^[\s]*(\d+)\.\s(.*)/);
      if (match) {
        elements.push(
          <div key={`ol-${i}`} style={styles.bullet}>
            <span style={styles.bulletNum}>{match[1]}.</span>
            <span>{renderInline(match[2])}</span>
          </div>
        );
      }
      return;
    }

    // Regular paragraph
    elements.push(
      <p key={`p-${i}`} style={styles.paragraph}>
        {renderInline(line)}
      </p>
    );
  });

  return elements;
}

function renderInline(text: string): React.ReactNode {
  // Handle inline code, bold, and italic
  const parts: React.ReactNode[] = [];
  let remaining = text;
  let keyIdx = 0;

  while (remaining.length > 0) {
    // Inline code
    const codeMatch = remaining.match(/`([^`]+)`/);
    // Bold
    const boldMatch = remaining.match(/\*\*([^*]+)\*\*/);
    // Italic
    const italicMatch = remaining.match(/(?<!\*)\*([^*]+)\*(?!\*)/);

    // Find the earliest match
    let earliest: { type: string; index: number; match: RegExpMatchArray } | null = null;
    for (const m of [
      codeMatch ? { type: "code", index: codeMatch.index!, match: codeMatch } : null,
      boldMatch ? { type: "bold", index: boldMatch.index!, match: boldMatch } : null,
      italicMatch ? { type: "italic", index: italicMatch.index!, match: italicMatch } : null,
    ].filter(Boolean) as any[]) {
      if (!earliest || m.index < earliest.index) earliest = m;
    }

    if (!earliest) {
      parts.push(remaining);
      break;
    }

    // Push text before match
    if (earliest.index > 0) {
      parts.push(remaining.slice(0, earliest.index));
    }

    if (earliest.type === "code") {
      parts.push(
        <code key={`c${keyIdx++}`} style={styles.inlineCode}>
          {earliest.match[1]}
        </code>
      );
    } else if (earliest.type === "bold") {
      parts.push(
        <strong key={`b${keyIdx++}`} style={{ fontWeight: 600 }}>
          {earliest.match[1]}
        </strong>
      );
    } else if (earliest.type === "italic") {
      parts.push(
        <em key={`i${keyIdx++}`} style={{ fontStyle: "italic" }}>
          {earliest.match[1]}
        </em>
      );
    }

    remaining = remaining.slice(earliest.index + earliest.match[0].length);
  }

  return parts.length === 1 ? parts[0] : parts;
}

export function MessageBubble({ role, content, isStreaming }: MessageBubbleProps) {
  const rendered = useMemo(() => renderContent(content), [content]);

  if (role === "user") {
    return (
      <div style={styles.userRow}>
        <div style={styles.userBubble}>{content}</div>
      </div>
    );
  }

  if (role === "system") {
    return (
      <div style={styles.systemRow}>
        <div style={styles.systemBubble}>{content}</div>
      </div>
    );
  }

  // Assistant
  return (
    <div style={styles.assistantRow}>
      <div style={styles.assistantBubble}>
        {rendered}
        {isStreaming && <span style={styles.cursor}>▊</span>}
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  userRow: {
    display: "flex",
    justifyContent: "flex-end",
    padding: "4px 0",
  },
  userBubble: {
    background: "#2a2a3a",
    borderRadius: 16,
    padding: "10px 16px",
    maxWidth: "75%",
    fontSize: 14,
    lineHeight: 1.5,
    color: "#e0e0e0",
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-word" as const,
  },
  assistantRow: {
    display: "flex",
    justifyContent: "flex-start",
    padding: "4px 0",
  },
  assistantBubble: {
    maxWidth: "85%",
    fontSize: 14,
    lineHeight: 1.6,
    color: "#d0d0d0",
  },
  systemRow: {
    display: "flex",
    justifyContent: "center",
    padding: "4px 0",
  },
  systemBubble: {
    fontSize: 12,
    color: "#666",
    fontStyle: "italic",
    padding: "4px 12px",
  },
  cursor: {
    animation: "blink 1s infinite",
    color: "#4a9eff",
    fontWeight: 300,
  },
  paragraph: {
    marginBottom: 4,
  },
  bullet: {
    display: "flex",
    gap: 8,
    paddingLeft: 16,
    marginBottom: 2,
  },
  bulletDot: {
    color: "#4a9eff",
    flexShrink: 0,
  },
  bulletNum: {
    color: "#4a9eff",
    flexShrink: 0,
    minWidth: 20,
  },
  h3: {
    fontSize: 16,
    fontWeight: 600,
    color: "#e0e0e0",
    marginTop: 12,
    marginBottom: 6,
  },
  h4: {
    fontSize: 14,
    fontWeight: 600,
    color: "#ccc",
    marginTop: 8,
    marginBottom: 4,
  },
  codeBlock: {
    background: "#0d0d0d",
    border: "1px solid #2a2a2a",
    borderRadius: 8,
    padding: "12px 16px",
    margin: "8px 0",
    overflowX: "auto",
    fontSize: 13,
    fontFamily: "'SF Mono', 'Fira Code', Consolas, monospace",
    lineHeight: 1.5,
    color: "#c8d6e5",
  },
  inlineCode: {
    background: "#2a2a2a",
    borderRadius: 4,
    padding: "1px 5px",
    fontSize: 13,
    fontFamily: "'SF Mono', 'Fira Code', Consolas, monospace",
    color: "#e06c75",
  },
};
