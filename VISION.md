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

The repository is `arjraw10-cell/Kairos2`. `main` was consolidated: the gateway v2 + browser rewrite tree is the trunk, and the old gateway v1 history was merged in with the "ours" strategy (recorded, not kept in the tree). `feature/gateway`, `feature/gateway-v2` and `backup/main-before-gateway` were deleted.

| Branch | What it is |
|---|---|
| `main` | The consolidated trunk: gateway v2, browser v3, Cua desktop control. The Electron app of gateway v1 is only in its history. |
| `v4-claude-standby` | **Current work**, on top of `main`: direct providers, the CLI as a gateway client with an offline mode, web search, workspace folders, background work with wakes. |
| `feature/browser-v3` | The pre-consolidation work branch; it points at the same commit as `main`. |

Folders: `Documents\Kairos` is `v4-claude-standby` and is on PATH (`kairos`, `kairos_offline`). `Documents\Agent2` is the old `main` checkout and provides `kairos_old`. `Documents\Agent2Gateway` is `feature/browser-v3` and is off PATH.

| Piece of the vision | Status |
|---|---|
| Agent core (streaming, tools, skills, compaction, resume, hard stop) | Done |
| Browser | Done |
| Computer use (Cua Driver) | Done |
| Gateway | Done; owns conversations, runtimes, per-conversation model settings and wake runs |
| Terminal CLI | Done: a client of the gateway by default, `--offline` runs the agent in-process. Rich frontend (`cli.py`); the old Textual frontend (`temp_cli.py`) is still in the repo but unused. |
| Models | Done: direct OpenAI (Responses), OpenRouter (Chat Completions) and Anthropic (Messages) formats in `provider.json`, key rotation, per-conversation model and effort. Anthropic is unit-tested only, never called live; no prompt caching yet. |
| Web search | Done (Tavily; falls back to the browser) |
| Workspaces | Done: no path means a fresh folder in `Documents\kairos_workspace` |
| Background work | Done: background sub-agents and terminal notifications wake the idle agent; it never polls |
| Desktop app | Exists for gateway v1 (old `main`); not ported to v2 |
| Phone app | Not started |
| Long-term memory | Not started |

A dedicated chatbot mode is no longer planned: the agent already answers everyday questions with `web_search` and the browser, and a conversation without a workspace gets its own fresh folder.

## What's left, in order

1. **Long-term memory.** I will set it up later. A memory store owned by the gateway, shared by every surface: small fact files with an index, injected into the system prompt, plus tools to write and update memories and a pass that saves what's worth remembering after a conversation.
2. **Desktop app on gateway v2.** Port the Electron + React app from old `main` to the v2 API, using `ui/index.html` as the design reference.
3. **Phone access.** Expose the gateway over Tailscale (already used between my machines) with `KAIROS_AUTH_TOKEN`, and build the phone client. The simplest option is a mobile web app (PWA) served by the gateway itself; a native app can come later.
4. **Anthropic live testing.** Run the Anthropic format against the real API once there is a key, then consider prompt caching to stretch the API credits.
5. **Polish.** One install guide that works on any of my machines, a test run before merging, removing the unused Textual frontend, and retiring `kairos_old` once the new CLI has fully replaced it.

## Open decisions

- ~~Should the CLI keep a standalone, gateway-free mode for offline use?~~ Resolved: kept (`--offline`, `kairos_offline.bat`).
- Phone: a PWA first, or straight to a native app?
- Memory format: plain files (readable, editable by hand) or SQLite next to the gateway's database?
