import React, { useRef, useCallback, useEffect } from "react";
import type { Session } from "../types";

interface TabBarProps {
  tabs: Session[];
  activeSessionId: string | null;
  split: boolean;
  onSelectTab: (id: string) => void;
  onCloseTab: (id: string) => void;
  onNewTab: () => void;
  onToggleSplit: () => void;
}

export function TabBar({
  tabs,
  activeSessionId,
  split,
  onSelectTab,
  onCloseTab,
  onNewTab,
  onToggleSplit,
}: TabBarProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!scrollRef.current || !activeSessionId) return;
    const activeTab = scrollRef.current.querySelector(`[data-session-id="${activeSessionId}"]`);
    activeTab?.scrollIntoView({ inline: "nearest", block: "nearest" });
  }, [activeSessionId]);

  const handleMouseDown = useCallback(
    (e: React.MouseEvent, id: string) => {
      if (e.button === 1) {
        e.preventDefault();
        onCloseTab(id);
      }
    },
    [onCloseTab]
  );

  return (
    <div className="tab-bar">
      <div className="tab-bar-scroll" ref={scrollRef}>
        {tabs.map((tab) => {
          const isActive = tab.id === activeSessionId;
          return (
            <div
              key={tab.id}
              data-session-id={tab.id}
              className={`tab${isActive ? " active" : ""}`}
              onClick={() => onSelectTab(tab.id)}
              onMouseDown={(e) => handleMouseDown(e, tab.id)}
              title={tab.preview || "New thread"}
            >
              {tab.active && <span className="tab-streaming-dot" />}
              <span className="tab-label">{tab.preview || "New thread"}</span>
              <button
                className="tab-close"
                onClick={(e) => {
                  e.stopPropagation();
                  onCloseTab(tab.id);
                }}
                title="Close tab"
              >
                <svg width="8" height="8" viewBox="0 0 16 16" fill="currentColor">
                  <path d="M4.646 4.646a.5.5 0 0 1 .708 0L8 7.293l2.646-2.647a.5.5 0 0 1 .708.708L8.707 8l2.647 2.646a.5.5 0 0 1-.708.708L8 8.707l-2.646 2.647a.5.5 0 0 1-.708-.708L7.293 8 4.646 5.354a.5.5 0 0 1 0-.708z" />
                </svg>
              </button>
            </div>
          );
        })}
      </div>

      <button className="tab-new" onClick={onNewTab} title="New thread">
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
          <path d="M8 1v14M1 8h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      </button>
      <button
        className={`tab-split-toggle${split ? " active" : ""}`}
        onClick={onToggleSplit}
        title={split ? "Collapse split view" : "Open split view"}
        aria-label={split ? "Collapse split view" : "Open split view"}
      >
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
          <rect x="2" y="2" width="12" height="12" rx="1.5" stroke="currentColor" strokeWidth="1.3" />
          <path d="M8 2v12" stroke="currentColor" strokeWidth="1.3" />
        </svg>
      </button>
    </div>
  );
}
