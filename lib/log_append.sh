# Sourced by the bash tools. The local logs hold message summaries, so they are private to their user:
# files 0600 in folders 0700, the same rule lib/private_log.py follows for the Python tools.

# log_prepare <file> <repo log folder>: create the folder 0700 and the file 0600 if missing. A file you
# own that others can read is tightened. A folder is tightened only when it is the repository's own
# log folder: a log you point elsewhere, such as your home folder, never changes that folder.
log_prepare() {
  local file=$1 repo_log=$2 dir
  dir=$(dirname -- "$file")
  if [[ ! -d $dir ]]; then
    (umask 077 && mkdir -p -- "$dir") || return 1
  elif [[ -O $dir && $(realpath -- "$dir") == "$(realpath -m -- "$repo_log")" ]]; then
    chmod go-rwx -- "$dir" 2>/dev/null || true
  fi
  (umask 077 && : >>"$file") || return 1
  if [[ -O $file ]]; then chmod go-rwx -- "$file" 2>/dev/null || true; fi
}

# A writer that died mid-line leaves a last line with no newline. Close it, under the lock, so the next
# line stands on its own instead of joining the torn one.
_end_torn_line() {
  if [[ -s $1 && $(tail -c 1 -- "$1" | wc -l) -eq 0 ]]; then printf '\n' >&9; fi
}

# log_append <file> <line>: append one line under an exclusive lock, then sync. Non-zero if it was not
# written. Note the shape: with `if ! { ...; } 9>>file`, bash skips the `!` when the redirection itself
# fails, so an unwritable log would read as written.
log_append() {
  local file=$1 line=$2
  if { flock -x -w 30 9 && _end_torn_line "$file" && printf '%s\n' "$line" >&9 && sync -- "$file"; } 9>>"$file"; then
    return 0
  fi
  return 1
}
