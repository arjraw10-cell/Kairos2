import React, { useEffect, useState, useCallback, useMemo } from "react";
import { useGateway } from "./hooks/useGateway";
import { Sidebar } from "./components/Sidebar";
import { TabBar } from "./components/TabBar";
import { SplitPane } from "./components/SplitPane";
import { PanePicker } from "./components/PanePicker";
import { ChatArea } from "./components/ChatArea";
import { ChatInput } from "./components/ChatInput";
import { StatusBar } from "./components/StatusBar";
import { WorkspacePicker } from "./components/WorkspacePicker";
import type { SessionView } from "./types";

export function App() {
  const gw = useGateway();
  const [pickWorkspace, setPickWorkspace] = useState(true);
  const [split, setSplit] = useState(false);
  const [leftPaneId, setLeftPaneId] = useState<string | null>(null);
  const [rightPaneId, setRightPaneId] = useState<string | null>(null);
  const [focusedPane, setFocusedPane] = useState<"left" | "right">("left");
  const [panePicker, setPanePicker] = useState<"left" | "right" | null>(null);

  // Focused pane is the target for keyboard Escape and new messages.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        const focusedId = focusedPane === "left" ? leftPaneId : rightPaneId;
        const focusedView = focusedId ? gw.sessionViews[focusedId] : null;
        if (focusedView?.streaming) {
          e.preventDefault();
          gw.interrupt(focusedId || undefined);
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [focusedPane, leftPaneId, rightPaneId, gw.sessionViews, gw.interrupt]);

  useEffect(() => {
    if (gw.activeSessionId) setPickWorkspace(false);
  }, [gw.activeSessionId]);

  // Keep pane assignments valid as tabs are closed or unloaded.
  useEffect(() => {
    if (leftPaneId && !gw.sessionViews[leftPaneId]) setLeftPaneId(null);
    if (rightPaneId && !gw.sessionViews[rightPaneId]) setRightPaneId(null);
  }, [gw.sessionViews, leftPaneId, rightPaneId]);

  // When split is enabled, put the focused session on the left and a second
  // loaded session on the right. With one session, the right pane stays empty
  // and offers an explicit thread picker rather than duplicating the chat.
  useEffect(() => {
    if (!split) return;
    const ids = gw.tabSessions.map((tab) => tab.id);
    const focused = gw.activeSessionId && ids.includes(gw.activeSessionId) ? gw.activeSessionId : null;
    setLeftPaneId((current) => current && ids.includes(current) ? current : focused || ids[0] || null);
    setRightPaneId((current) => {
      if (current && ids.includes(current)) return current;
      const left = focused || leftPaneId || ids[0];
      return ids.find((id) => id !== left) || null;
    });
  }, [split, gw.activeSessionId, gw.tabSessions, leftPaneId]);

  const handleNewThread = useCallback(() => {
    if (split) {
      setPanePicker(null);
      if (focusedPane === "right") {
        setRightPaneId(null);
        setPanePicker("right");
      } else {
        setLeftPaneId(null);
        setPanePicker("left");
      }
    } else {
      setPickWorkspace(true);
      gw.openNewTab();
    }
  }, [focusedPane, gw.openNewTab, split]);

  const handlePickWorkspace = useCallback(
    (ws: string) => {
      setPanePicker(null);
      setPickWorkspace(false);
      gw.newSession(ws);
    },
    [gw.newSession]
  );

  const selectTab = useCallback(
    (id: string) => {
      setPickWorkspace(false);
      if (!split) {
        gw.loadSession(id);
        return;
      }

      const pane = leftPaneId === id ? "left" : rightPaneId === id ? "right" : focusedPane;
      if (pane === "left") setLeftPaneId(id);
      else setRightPaneId(id);
      setFocusedPane(pane);
      setPanePicker(null);
      gw.loadSession(id);
    },
    [focusedPane, gw.loadSession, leftPaneId, rightPaneId, split]
  );

  const choosePaneSession = useCallback(
    (pane: "left" | "right", id: string) => {
      if (pane === "left") setLeftPaneId(id);
      else setRightPaneId(id);
      setFocusedPane(pane);
      setPanePicker(null);
      gw.loadSession(id);
    },
    [gw.loadSession]
  );

  const toggleSplit = useCallback(() => {
    setSplit((current) => {
      if (current) {
        setPanePicker(null);
        return false;
      }
      setFocusedPane("left");
      return true;
    });
  }, []);

  const closePane = useCallback((pane: "left" | "right") => {
    if (pane === "left") setLeftPaneId(null);
    else setRightPaneId(null);
    setPanePicker(null);
  }, []);

  const leftView: SessionView | null = leftPaneId ? gw.sessionViews[leftPaneId] || null : null;
  const rightView: SessionView | null = rightPaneId ? gw.sessionViews[rightPaneId] || null : null;
  const isPicker = pickWorkspace && gw.status === "connected" && !gw.activeSessionId;

  const focusedView = focusedPane === "left" ? leftView : rightView;
  const focusedId = focusedPane === "left" ? leftPaneId : rightPaneId;

  // Split panes send directly to their own session instead of relying on one
  // global active-session callback.
  const sendToFocused = useCallback(
    (content: string, imageUrls?: string[]) => {
      if (focusedId) gw.sendMessage(focusedId, content, imageUrls);
    },
    [focusedId, gw.sendMessage]
  );

  const splitPaneTabs = useMemo(
    () => gw.tabSessions.filter((tab) => tab.id !== (focusedPane === "left" ? rightPaneId : leftPaneId)),
    [focusedPane, gw.tabSessions, leftPaneId, rightPaneId]
  );

  return (
    <div className="app">
      {gw.error && (
        <div className="toast">
          <span style={{ fontSize: 15 }}>⚠</span>
          {gw.error}
        </div>
      )}

      {gw.notifications.map((n) => (
        <div key={n.id} className={`toast-info${n.exiting ? " toast-exit" : ""}`}>
          <span className="toast-info-icon">✓</span>
          {n.message}
        </div>
      ))}

      <Sidebar
        sessions={gw.sessions}
        activeSessionId={gw.activeSessionId}
        streaming={gw.streaming}
        onSelectSession={(id) => {
          setPickWorkspace(false);
          selectTab(id);
        }}
        onCloseSession={gw.unloadSession}
        onNewThread={handleNewThread}
        homeDir={gw.homeDir}
        workspace={gw.workspace}
      />

      <div className="main-panel">
        <TabBar
          tabs={gw.tabSessions}
          activeSessionId={gw.activeSessionId}
          split={split}
          onSelectTab={selectTab}
          onCloseTab={gw.closeTab}
          onNewTab={handleNewThread}
          onToggleSplit={toggleSplit}
        />

        {isPicker ? (
          <>
            <WorkspacePicker
              workspaces={gw.workspaces}
              homeDir={gw.homeDir}
              onSelect={handlePickWorkspace}
              connectionStatus={gw.status}
            />
            <StatusBar
              connectionStatus={gw.status}
              tokens={null}
              workspace=""
              streaming={false}
              homeDir={gw.homeDir}
            />
          </>
        ) : split ? (
          <div className="split-layout">
            <SplitPane
              view={leftView}
              paneLabel="Left pane"
              workspaces={gw.workspaces}
              homeDir={gw.homeDir}
              connectionStatus={gw.status}
              pickingWorkspace={panePicker === "left"}
              onPickWorkspace={handlePickWorkspace}
              onSend={(content, images) => leftPaneId && gw.sendMessage(leftPaneId, content, images)}
              onInterrupt={() => leftPaneId && gw.interrupt(leftPaneId)}
              onChooseSession={() => {
                setFocusedPane("left");
                setPanePicker("left");
              }}
              onClose={() => closePane("left")}
              onFocus={() => setFocusedPane("left")}
            />
            <div className="split-divider" aria-hidden="true" />
            <SplitPane
              view={rightView}
              paneLabel="Right pane"
              workspaces={gw.workspaces}
              homeDir={gw.homeDir}
              connectionStatus={gw.status}
              pickingWorkspace={panePicker === "right"}
              onPickWorkspace={handlePickWorkspace}
              onSend={(content, images) => rightPaneId && gw.sendMessage(rightPaneId, content, images)}
              onInterrupt={() => rightPaneId && gw.interrupt(rightPaneId)}
              onChooseSession={() => {
                setFocusedPane("right");
                setPanePicker("right");
              }}
              onClose={() => closePane("right")}
              onFocus={() => setFocusedPane("right")}
            />
            {panePicker && (
              <div className="pane-picker-popover">
                <PanePicker
                  tabs={splitPaneTabs}
                  paneLabel={panePicker === "left" ? "Left pane" : "Right pane"}
                  homeDir={gw.homeDir}
                  onSelect={(id) => choosePaneSession(panePicker, id)}
                  onNewThread={() => {
                    setPanePicker(null);
                    setPickWorkspace(true);
                    gw.openNewTab();
                  }}
                />
              </div>
            )}
          </div>
        ) : (
          <>
            <div className="main-header titlebar-drag">
              <div className="main-header-title">
                {gw.activeSessionId ? gw.messages[0]?.content?.slice(0, 60) || "New thread" : "New thread"}
              </div>
            </div>
            <ChatArea
              messages={gw.messages}
              streaming={gw.streaming}
              streamText={gw.streamText}
              toolCalls={gw.toolCalls}
              completedSteps={gw.completedSteps}
              workspace={gw.workspace}
              hasSession={!!gw.activeSessionId}
              loadingSession={gw.loadingSession}
            />
            <ChatInput
              onSend={(content, images) => gw.activeSessionId && gw.sendMessage(gw.activeSessionId, content, images)}
              onInterrupt={() => gw.interrupt()}
              streaming={gw.streaming}
              disabled={gw.status !== "connected" || !gw.activeSessionId}
            />
            <StatusBar
              connectionStatus={gw.status}
              tokens={gw.tokens}
              workspace={gw.workspace}
              streaming={gw.streaming}
              homeDir={gw.homeDir}
            />
          </>
        )}
      </div>
    </div>
  );
}
