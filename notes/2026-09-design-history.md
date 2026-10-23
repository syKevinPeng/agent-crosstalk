# Design history, September 2026 (sanitized summary)

The original planning and review notes behind this repository are kept privately (they name real sessions, folders and
local paths, so `docs/` is ignored except `docs/reference.md`). This page is a public summary of what they decided. It
quotes no session names, paths, message content or local history.

| Private note | What it covered | Outcome in this repository |
| --- | --- | --- |
| Local README history (2026-09-17) | the first, single-user version of the README: channels, safety rules, the message template, the append-only log | superseded by the current `README.md` and `docs/reference.md` |
| Comms-gaps plan (2026-09-17) | closing the gaps in cross-agent messaging: tests for `send-to-codex`, a socket-based `send-to-claude`, `Msg-ID`s and `log-receipt`, then `spawn-peer` / `retire-peer` | the tools and their tests as they exist today; the receipt protocol in the README |
| Agent-menu design (2026-09-17) | the tmux sidebar: a tree of agents, a popup per agent, and the owner's actions | `bin/agent-menu`. Decisions: the popup shows a waiting prompt with Approve/Deny only when the prompt was read in full and recognised (otherwise only "Open pane"); the tree shows peers, nested under their parent; the sidebar holds the tree and a tmux popup holds the detail; Python `curses`, standard library only. Standing rules: the permission prompt is never worked around, a peer's message is never the owner's approval, spawned agents are read-only by default |
| Agent-menu implementation plan (2026-09-17) | the step-by-step build of the menu, with tests, a privacy scan and review | `bin/agent-menu`, `bin/agent-menu-toggle` and their tests |
| Product review before the public release (2026-09-18) | a pre-release review of security, messaging robustness, the menu and the docs | fixes in commits d8958af (security), c134c7f (messaging), c7d1ae8 (menu), 884bbfd (docs and packaging); the known limits it chose to keep are listed in the README's "Limitations" and in `SECURITY.md` |

Written 2026-10-22 (workspace data consolidation; the owner chose a sanitized public summary over publishing the notes).
