# Security

agent-crosstalk passes messages between AI agents on one machine and can start and stop agents. This page says what it protects against, what it does not, and how to report a problem.

## Reporting a problem

Please report a vulnerability privately, not in a public issue: use GitHub's private vulnerability reporting on this repository if it is turned on, or write to the maintainer through the contact on their GitHub profile. Include the tool, the commit, what you did and what happened.

## Threat model

The main worry is an agent doing something you did not approve because another agent asked it to. So a peer agent is treated as untrusted. It controls the text of its messages, its own session name and folder, and anything it can write to disk. Content an agent reads from the web or a repository can also try to steer it, and a peer's message is one more place such content can arrive.

What the tools do about it:

- **A message is never approval.** Every message starts with `Teammate message from <sender>` and `not user approval`. The tools never answer a permission prompt, and never change a session's permissions on a peer's behalf.
- **Read-only by default.** `spawn-peer` starts Codex agents in the `read-only` sandbox. Claude has no sandbox, so a read-only Claude agent runs in plan mode, without `Edit`, `Write`, `NotebookEdit` and `Bash`, and without the folder's own `.claude` settings and hooks.
- **Write access needs a recorded reason.** `--write` requires `--approval`, and full-access modes are never offered. No agent is started inside this repository, `~/.claude`, `~/.codex`, or a folder containing one of them.
- **Waking an agent never widens it.** `send-to-codex` reloads an agent the Codex daemon unloaded only if `spawn-peer` created it. It uses the first line `spawn-peer` recorded for it, never a later one. The record must not be writable by other users, the folder must not be a protected one, and the daemon's own record must agree: same folder, and not a thread a person started. Menu Resume passes the same recorded settings, or the strictest ones where the record is silent.
- **Credentials.** `send-to-claude` reads a session's inbox credential into memory for one send. It checks first that exactly one registry entry and one credential match, that the process is alive, and that the socket's peer (`SO_PEERCRED`) is that process and your own user. It never prints or logs the credential, or passes it on a command line.
- **Untrusted text.** Session names, folders, message lines and agent answers are stripped of control, escape and invisible format characters before they reach a terminal, stdout or tmux. tmux format characters are escaped.
- **The menu's own `claude` call.** The 15-minute rate-limit reading runs `claude -p /usage` with `--safe-mode`, `--setting-sources user` and `--no-session-persistence` in a private folder, so settings or hooks planted there never run.
- **Private logs.** Logs are created 0600 in 0700 folders, and a log left readable by others is tightened when it is next written.

## What is not covered

- **Anything running as you.** A program running under your user can read your logs, write the spawn record, import these modules, or type into any tmux pane with `tmux send-keys`. The menu's buttons guard against accidents, not against such a program.
- **The auto-review default.** Spawned Codex agents default to `--approvals auto-review`: Codex's own reviewer decides each approval request, and it can approve actions outside the sandbox. A prompt-injected peer could ask for one. Use `--approvals never` when the sandbox must be a hard limit.
- **Read-only Claude agents.** Plan mode plus removed tools is weaker than a sandbox. MCP connectors that can write, and subagents, are not covered.
- **Sender identity.** The sender label on a message is a claim, and nothing proves which agent wrote it.
- **Other local users.** `codex queue` takes the message as a command-line argument, so another user can read it with `ps` while it runs, unless `/proc` is mounted with `hidepid`. The spawn record is trusted only when no other user can write it.
- **Upstream changes.** The Codex app-server protocol and the Claude inbox socket are not documented as stable. The tools refuse or fail on answers they do not recognise, but a change in meaning that keeps the same shape could go unnoticed.

## Supported versions

Only the latest commit on `main` is supported.
