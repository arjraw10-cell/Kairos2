/**
 * Convert an absolute path to a ~/relative label.
 * e.g. C:\Users\arjra\Documents\foo → ~/Documents/foo
 */
export function toHomeRelative(path: string, homeDir: string): string {
  if (!path) return "default";
  if (homeDir && path.startsWith(homeDir)) {
    const rest = path.slice(homeDir.length).replace(/^[/\\]/, "");
    return rest ? `~/${rest}` : "~";
  }
  const parts = path.split(/[/\\]/).filter(Boolean);
  if (parts.length <= 2) return parts.join("/");
  return `\u2026/${parts.slice(-2).join("/")}`;
}

/**
 * Convert an absolute path to a short folder name for sidebar display.
 * e.g. C:\Users\arjra\Documents\Agent2 → Agent2
 */
export function toFolderName(path: string): string {
  if (!path) return "default";
  const parts = path.split(/[/\\]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}
