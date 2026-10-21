# `log/` contents (tracked manifest)

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
