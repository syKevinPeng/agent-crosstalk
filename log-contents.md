# `log/` contents (tracked manifest)

**Since 2026-10-23 the record is kept on the lab workstation**, where all agent messaging now runs (workspace
`AGENTS.md` rule 8). Its tools write `~/.local/state/agent-comms/` there, chosen by `AGENT_COMMS_LOG`,
`AGENT_COMMS_SPAWN_LOG`, `AGENT_MENU_ACTIONS_LOG` and `AGENT_MENU_USAGE_CWD` (set in that machine's shell
profile, its byobu tmux config and its Codex daemon's unit). That folder holds the same files as `log/` below,
has the same permissions, and the same rules apply: it **cannot be rebuilt**, it is a backup target, and no
nightly backup covers it yet. `log/` in this repository is the record up to 2026-10-23. It still gains lines
only for Codex threads that began before that date, whose receipts and retirements are logged where their
sends were. A receipt is always logged on the machine that logged the send.

**Cannot be rebuilt.** `log/` is the only record of the messages, receipts and spawned agents. It is git-ignored and
kept private: the folder is 0700 and the JSONL logs are 0600 (the three dated Markdown files are 0664). It is a
backup target.

As of 2026-10-21 (a snapshot; the logs keep growing):

| File | Size | Count | What |
|---|---|---|---|
| `messages.jsonl` | about 400 KiB | 885 lines | every send and receipt (`send-to-codex`, `send-to-claude`, `log-receipt`) |
| `spawned.jsonl` | about 60 KiB | 176 lines | every agent `spawn-peer` created, and each retirement |
| `menu-actions.jsonl` | 3 KiB | 16 lines | actions taken from the agent menu |
| `2026-09-17.md`, `2026-09-18.md`, `2026-09-19.md` | 28 / 4.5 / 0.3 KiB | 3 files | dated Markdown logs of 2026-09-17 to 09-19 |
| `usage-calls/` | empty | 0 files | rate-limit lookups (cache) |

- **Rebuild:** impossible; the files are written as events happen.
- **Depends on it:** the receipt protocol (a message without a receipt counts as undelivered) and every report that
  cites a Msg-ID.
- **Backup:** no nightly backup covers this repository's ignored files.
