import { contextBridge } from "electron";

// Expose minimal API to renderer if needed in the future.
// For now, all WebSocket communication happens directly from the renderer.
contextBridge.exposeInMainWorld("electronAPI", {
  platform: process.platform,
});
