#!/usr/bin/env bash
# tmux entry point. Add `run-shell <path to this file>` to tmux.conf, or list the repository in TPM.
# Binds <prefix> O to open, focus or close the agent sidebar in the current window.
# Options, set before this runs: @agent-menu-key (default O), @agent-menu-width (default 34).
# @agent-menu-keys off hides the sidebar's tmux key reminder; `s` in the sidebar toggles it.
tmux() { command "${TMUX_BIN:-tmux}" "$@"; }
HERE=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
key=$(tmux show-option -gqv @agent-menu-key)
tmux bind-key "${key:-O}" run-shell -b "'$HERE/bin/agent-menu-toggle' '#{pane_id}'"
