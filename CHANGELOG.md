# Changelog

## 0.1.0

The first public release.

- `send-to-codex`, `send-to-claude` and `log-receipt`: messages between Claude Code and Codex CLI sessions, with a `Msg-ID` on each and receipts recorded in a local log.
- `send-to-codex` confirms delivery: it wakes a spawned Codex agent the daemon unloaded, with its recorded settings, and waits until a turn takes the message.
- `spawn-peer`, `retire-peer` and `codex-reply`: named peer agents, read-only by default, created and retired on the record.
- `agent-menu`: a tmux sidebar and popup for every agent, with rate limits, a pane highlight for the selected agent, and Quit and Resume.
- Hardening before release, after an independent review: private logs, no settings or hooks in the menu's own `claude` call, a wake that no line written later can widen, clean handling of torn logs, long messages and interrupts, and exit codes that no longer clash across tools.
