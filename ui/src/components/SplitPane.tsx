import React from "react";
import type { ConnectionStatus, SessionView } from "../types";
import { ChatArea } from "./ChatArea";
import { ChatInput } from "./ChatInput";
import { StatusBar } from "./StatusBar";
import { WorkspacePicker } from "./WorkspacePicker";

interface SplitPaneProps {
  view: SessionView | null;
  paneLabel: string;
  workspaces: string[];
  homeDir: string;
  connectionStatus: ConnectionStatus;
  pickingWorkspace: boolean;
  onPickWorkspace: (workspace: string) => void;
  onSend: (content: string, imageUrls?: string[]) => void;
  onInterrupt: () => void;
  onChooseSession: () => void;
  onClose: () => void;
  onFocus: () => void;
}

export function SplitPane({
  view,
  paneLabel,
  workspaces,
  homeDir,
  connectionStatus,
  pickingWorkspace,
  onPickWorkspace,
  onSend,
  onInterrupt,
  onChooseSession,
  onClose,
  onFocus,
}: SplitPaneProps) {
  const hasSession = !!view;
  const title = view?.preview || paneLabel;

  return (
    <section className="split-pane" onMouseDown={onFocus}>
      <div className="split-pane-header">
        <div className="split-pane-title" title={title}>
          {view?.streaming && <span className="split-pane-streaming-dot" />}
          <span>{title}</span>
        </div>
        <div className="split-pane-actions">
          {!hasSession && (
            <button className="split-pane-action" onClick={onChooseSession} title="Choose a thread">
              Choose thread
            </button>
          )}
          <button className="split-pane-action split-pane-close" onClick={onClose} title="Close pane">
            <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor">
              <path d="M4.646 4.646a.5.5 0 0 1 .708 0L8 7.293l2.646-2.647a.5.5 0 0 1 .708.708L8.707 8l2.647 2.646a.5.5 0 0 1-.708.708L8 8.707l-2.646 2.647a.5.5 0 0 1-.708-.708L7.293 8 4.646 5.354a.5.5 0 0 1 0-.708z" />
            </svg>
          </button>
        </div>
      </div>

      {pickingWorkspace ? (
        <WorkspacePicker
          workspaces={workspaces}
          homeDir={homeDir}
          onSelect={onPickWorkspace}
          connectionStatus={connectionStatus}
        />
      ) : (
        <>
          <ChatArea
            messages={view?.messages || []}
            streaming={view?.streaming || false}
            streamText={view?.streamText || ""}
            toolCalls={view?.toolCalls || []}
            completedSteps={view?.completedSteps || []}
            workspace={view?.workspace || ""}
            hasSession={hasSession}
            loadingSession={view?.loading || false}
          />
          <ChatInput
            onSend={onSend}
            onInterrupt={onInterrupt}
            streaming={view?.streaming || false}
            disabled={connectionStatus !== "connected" || !hasSession}
          />
          <StatusBar
            connectionStatus={connectionStatus}
            tokens={view?.tokens || null}
            workspace={view?.workspace || ""}
            streaming={view?.streaming || false}
            homeDir={homeDir}
          />
        </>
      )}
    </section>
  );
}
