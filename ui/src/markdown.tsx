import React, { useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

/**
 * Markdown renderer using react-markdown with GFM (tables, blockquotes, task lists)
 * and syntax highlighting via rehype-highlight.
 *
 * Code blocks include a copy-to-clipboard button.
 */

function CopyButton({ code }: { code: string }) {
  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(code).catch(() => {});
  }, [code]);

  return (
    <button className="code-copy-btn" onClick={handleCopy} title="Copy code">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
        <path d="M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 010 1.5h-1.5a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-1.5a.75.75 0 011.5 0v1.5A1.75 1.75 0 019.25 16h-7.5A1.75 1.75 0 010 14.25v-7.5z" />
        <path d="M5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0114.25 11h-7.5A1.75 1.75 0 015 9.25v-7.5zm1.75-.25a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-7.5a.25.25 0 00-.25-.25h-7.5z" />
      </svg>
    </button>
  );
}

function extractText(node: any): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (node && typeof node === "object" && node.props) {
    return extractText(node.props.children);
  }
  return "";
}

export function renderMarkdown(text: string): React.ReactNode[] {
  if (!text) return [];

  return [
    <ReactMarkdown
      key="md"
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeHighlight]}
      components={{
        pre: (props: any) => {
          const { children, ...rest } = props;
          let codeText = "";
          let lang = "";

          // Find the <code> child to extract language and text for copy button
          const childArr = React.Children.toArray(children);
          for (const child of childArr) {
            if (child && typeof child === "object" && "type" in child && (child as any).type === "code") {
              codeText = extractText((child as any).props?.children);
              lang = ((child as any).props?.className || "").replace("language-", "");
              break;
            }
          }

          return (
            <div className="code-block-wrapper">
              <div className="code-block-header">
                {lang}
                <CopyButton code={codeText} />
              </div>
              <pre {...rest}>{children}</pre>
            </div>
          );
        },
        table: (props: any) => {
          return <div className="table-wrapper">{props.children}</div>;
        },
        blockquote: (props: any) => {
          return <blockquote className="markdown-blockquote">{props.children}</blockquote>;
        },
        input: (props: any) => {
          const { checked, disabled, ...rest } = props;
          return (
            <input
              type="checkbox"
              checked={checked}
              disabled={disabled || true}
              readOnly
              className="task-checkbox"
              {...rest}
            />
          );
        },
        a: (props: any) => {
          return (
            <a
              href={props.href}
              target="_blank"
              rel="noopener noreferrer"
            >
              {props.children}
            </a>
          );
        },
      }}
    >
      {text}
    </ReactMarkdown>,
  ];
}
