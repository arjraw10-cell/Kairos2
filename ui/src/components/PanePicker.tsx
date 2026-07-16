import React from "react";
import type { Session } from "../types";
import { toHomeRelative } from "../utils";

interface PanePickerProps {
  tabs: Session[];
  paneLabel: string;
  homeDir: string;
  onSelect: (id: string) => void;
  onNewThread: () => void;
}

export function PanePicker({ tabs, paneLabel, homeDir, onSelect, onNewThread }: PanePickerProps) {
  return (
    <div className="pane-picker">
      <div className="pane-picker-title">Choose a thread for {paneLabel.toLowerCase()}</div>
      <div className="pane-picker-subtitle">Keep both conversations visible and interactive.</div>
      <div className="pane-picker-list">
        {tabs.map((tab) => (
          <button key={tab.id} className="pane-picker-item" onClick={() => onSelect(tab.id)}>
            <span className="pane-picker-item-dot" />
            <span className="pane-picker-item-info">
              <span className="pane-picker-item-label">{tab.preview || "New thread"}</span>
              {tab.workspace && <span className="pane-picker-item-path">{toHomeRelative(tab.workspace, homeDir)}</span>}
            </span>
            {tab.active && <span className="pane-picker-item-status">running</span>}
          </button>
        ))}
      </div>
      <button className="pane-picker-new" onClick={onNewThread}>
        <span>+</span>
        New thread
      </button>
    </div>
  );
}
