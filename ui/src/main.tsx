import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

// Inject global styles
const style = document.createElement("style");
style.textContent = `
  @keyframes blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0; }
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(-8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  /* Scrollbar styling */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: #444; }
  /* Selection */
  ::selection { background: rgba(74, 158, 255, 0.3); }
`;
document.head.appendChild(style);

const root = createRoot(document.getElementById("root")!);
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
