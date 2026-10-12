# Kairos Vision

## The goal

Kairos is my own personal agent: like Claude Code in the terminal, but simpler, and built so that I know every part of it. Everything I need is built in: the tools, a stealth browser, computer use, the system prompt, and memory.

It is a **hybrid of an agent and a chatbot**. It should work for coding the way Claude Code does, and for everyday questions the way the Claude app does. It should know me through long-term memory, as the Claude app does.

It is reachable in several ways, all backed by **one gateway**:

- a terminal CLI that is actually good
- a desktop app
- a personal phone app that connects to the gateway, so I can ask it things from anywhere

Every surface talks to the same gateway, so the conversations, memory and tools are the same wherever I use it.

## Why

I have Claude Max 20x for six months through YC Startup School. When it expires I will still have a lot of API credits, but API credits can't be used in the Claude app. I also want more than the app gives me. Kairos is how I keep that experience, and go beyond it, on API credits.

## Timing

I'm deliberately putting the big build off until the Max plan is close to expiring. By then the models I can use will be better, cheaper and faster, so I can build it faster than I could now. In the meantime, the work is to keep the foundation clean and know exactly what is left.

## Principles

- **One brain, many surfaces.** The gateway owns conversations, memory, tools and the browser. The CLI, desktop app and phone app are thin clients.
- **Simple and fully understood.** Fewer, better tools. No feature I can't explain.
- **Code, not MCP.** Capabilities are built into Kairos directly.
- **Always headed and humanized** for the browser, as in the cloak-mcp rewrite.

## Where it stands (October 2026)

The repository is `arjraw10-cell/Kairos2`. Two gateway designs grew from the same June 27 commit (`1cdd16e`). They are complementary, not one replacing the other.

| Branch | What it is | Use it for |
|---|---|---|
| `main` | Gateway v1 merged in: a thin gateway (4 routes: `/health`, sessions, workspaces, one `/ws`). The CLI is a real WebSocket client of it. Electron + React desktop app in `ui/`. About 9.8k lines of Python. | The Electron app and the CLI-as-client pattern |
| `feature/gateway` | Gateway v1 before its last three commits reached `main` (`main` is 3 commits ahead, 1 behind) | Nothing; superseded by `main` |
| `backup/main-before-gateway` | `main` as it was before gateway v1 was merged | Nothing; a safety snapshot |
| `feature/gateway-v2` | Gateway v2: SQLite-backed conversations, runs, event replay, auth, about 30 REST/WebSocket routes, idle unloading, tests. Also Cua desktop control, tool-result caps, sessions in `~/.kairos`, and terminal improvements. About 13.3k lines. **The CLI runs its own Agent in-process and does not use the gateway.** No desktop app; `ui/index.html` is a static design mockup. | The foundation |
| `feature/browser-v3` | `gateway-v2` + the browser rewrite (AI snapshots, refs, dialogs, diff snapshots, 20 tools) | **Current tip; what `kairos_cli_new` runs** |

Worktrees: `Documents\Agent2` is `main`, `Documents\Agent2-gateway` is `feature/gateway`, and `Documents\Agent2Gateway` is `feature/browser-v3`. `Documents\Agent2-Gatway` (typo) is an empty leftover folder.

| Piece of the vision | Status |
|---|---|
| Agent core (streaming, tools, sub-agents, skills, compaction, resume, hard stop) | Done (v2) |
| Browser | Done (`browser-v3`) |
| Computer use (Cua Driver) | Done (v2) |
| Gateway | Built (v2) but unused: nothing connects to it |
| Terminal CLI | Works, but standalone. Two frontends exist (`cli.py` with Rich, `temp_cli.py` with Textual). |
| Desktop app | Exists for gateway v1 (`main`); not ported to v2 |
| Phone app | Not started |
| Long-term memory | Not started |
| Chatbot mode (workspace-free questions, web search) | Not started (there is no search tool besides the browser) |
| Anthropic API support / prompt caching | Not started (Kairos talks to an OpenAI-compatible endpoint) |

## What's left, in order

1. **Consolidate on one trunk.** Merge `feature/browser-v3` into `main` as the new trunk. Bring over from old `main` only the Electron app and the CLI-as-gateway-client idea. Retire `feature/gateway`, `feature/gateway-v2` and `backup/main-before-gateway`, and delete the empty `Agent2-Gatway` folder.
2. **Make the CLI a gateway client.** Point the CLI at gateway v2's API (`/api/v1/ws` plus REST) instead of creating its own Agent, so terminal conversations live in the gateway like everything else. Pick one frontend (Rich or Textual) and drop the other.
3. **Long-term memory.** A memory store owned by the gateway: small fact files with an index, injected into the system prompt, plus tools to write and update memories and a pass that saves what's worth remembering after a conversation. It must be shared by every surface.
4. **Chatbot mode.** Conversations that don't need a workspace, a fast web-search tool so everyday questions don't need the browser, and a system prompt that handles both "fix this code" and "what should I eat".
5. **Desktop app on gateway v2.** Port the Electron + React app from `main` to the v2 API, using `ui/index.html` as the design reference.
6. **Phone access.** Expose the gateway over Tailscale (already used between my machines) with `KAIROS_AUTH_TOKEN`, and build the phone client. The simplest option is a mobile web app (PWA) served by the gateway itself; a native app can come later.
7. **Models after Max expires.** Native Anthropic API support with prompt caching, to stretch the API credits, while keeping OpenAI-compatible endpoints as an option.
8. **Polish.** One install guide that works on any of my machines, a test run before merging, and cleanup of the old launchers (`kairos_old.bat`, `kairos_nold.bat`).

## Open decisions

- Should the CLI keep a standalone, gateway-free mode for offline use, or always go through the gateway?
- Phone: a PWA first, or straight to a native app?
- Memory format: plain files (readable, editable by hand) or SQLite next to the gateway's database?
