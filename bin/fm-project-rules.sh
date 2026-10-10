#!/usr/bin/env bash
# Deliver a project's declared rules to a Claude or Codex worker and prove it.
# docs/project-rules.md owns the contract; bin/fm-project-rules.mjs is the engine.
#
# Usage (fm-spawn, watcher, and readiness callers):
#   fm-project-rules.sh admit <state> <id> <copy> <tool> [--brief <file>] [--backend <name>] [--config <dir>]
#     Runs the project's prepare step, resolves every declared file, renders the
#     always-on block, and opens the start stage. Exit 3 means the copy declares
#     no rules and nothing was created; any other nonzero exit refuses the launch.
#   fm-project-rules.sh emit <state> <id>            Codex developer_instructions override.
#   fm-project-rules.sh merge-settings <file> <state> <id>   Claude hooks on stdin, merged into <file>.
#   fm-project-rules.sh scan [<state> [<id>]]        Read the tool's own log; print one
#     "project-rules: <id> <alarm>" line per new alarm episode.
#   fm-project-rules.sh detect <state> <id>          The same read, with no alarm output.
#   fm-project-rules.sh ready <state> <id>           Nonzero while a stage or a required skill is owed.
#   fm-project-rules.sh size <copy> <tool>           Payload and block bytes for a prepared copy.
#   fm-project-rules.sh status <state> <id>          The task's record, without its receipt codes.
#   fm-project-rules.sh hook <state> <id> <session-start|pretool>   Claude hook entry points.
# Usage (the worker, as the block and each command's output direct):
#   fm-project-rules.sh next <state> <id>            What is owed right now.
#   fm-project-rules.sh ack <state> <id> <code>      Quote the receipt row the stage asked for.
#   fm-project-rules.sh serve <state> <id> <brief|skill> [code]   Read the brief or a skill in chained parts.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "${1:-}" in
  -h | --help | '') sed -n '2,/^set -u$/{/^set -u$/d;s/^# \{0,1\}//;p;}' "$0"; exit 0 ;;
esac
# Whether anything is declared is decided here, in shell, so a project that
# declares no rules never depends on the engine or on node.
case "$1" in
  admit) [ -e "${4:-}/.agents/project-rules.json" ] || { rm -rf -- "${2:-/nonexistent}/${3:-x}.project-rules" "${2:-/nonexistent}/${3:-x}.project-rules.d"; exit 3; } ;;
  next | ready | detect | hook | status | serve | ack) [ -e "${2:-}/${3:-}.project-rules" ] || { [ "$1" != next ] || echo "Project rules: nothing is declared for this task."; exit 0; } ;;
  scan) [ -n "${3:-}" ] || ls "${2:-${FM_STATE_OVERRIDE:-${FM_HOME:-$SCRIPT_DIR/..}/state}}"/*.project-rules >/dev/null 2>&1 || exit 0 ;;
esac
if ! command -v node >/dev/null 2>&1; then
  echo "fm-project-rules: node is required and was not found on PATH" >&2
  # Claude treats only exit 2 from a tool hook as a refusal.
  [ "$1" = hook ] && [ "${4:-}" != session-start ] && exit 2
  # A scan that cannot run still has to reach the supervisor, once per record.
  if [ "$1" = scan ]; then
    for record in "${2:-${FM_STATE_OVERRIDE:-${FM_HOME:-$SCRIPT_DIR/..}/state}}"/${3:-*}.project-rules; do
      [ -f "$record" ] || continue
      [ "$(cat "$record.scan-failed" 2>/dev/null)" = "node is required" ] || printf 'project-rules: %s scan-failed node is required and was not found on PATH\n' "$(basename "$record" .project-rules)"
      echo "node is required" > "$record.scan-failed"
    done
  fi
  exit 1
fi
export FM_PROJECT_RULES_HELPER="$SCRIPT_DIR/fm-project-rules.sh"
engine() { node "$SCRIPT_DIR/fm-project-rules.mjs" "$@"; }

# The Codex session log for a task, resolved by the busy-state owner.
rollout_for() {  # <state> <id>
  [ -e "$1/$2.codex-session" ] || return 0
  # shellcheck source=bin/fm-busy-lib.sh
  ( . "$SCRIPT_DIR/fm-busy-lib.sh" && fm_busy_codex_rollout_log "$1" "$2" ) 2>/dev/null || true
}

# Read one task's log. A newly detected Codex compaction comes back as one
# "fm-steer:" line, which is delivered through the task's steering inbox since
# Codex has no hook that could tell the worker itself.
# A read that fails outright is reported once per distinct failure, so a locked
# or corrupt record reaches the supervisor instead of vanishing.
read_log() {  # <scan|detect> <state> <id>
  local line out rc=0 failed="$2/$3.project-rules.scan-failed" reason
  out=$(engine "$1" "$2" "$3" "$(rollout_for "$2" "$3")" 2>"$failed.new") || rc=$?
  rm -f -- "$2/$3.project-rules.detect-failed"
  if [ "$rc" -ne 0 ]; then
    : > "$2/$3.project-rules.detect-failed"
    reason=$(head -1 "$failed.new" | cut -c1-200)
    [ "$1" != scan ] || [ "$(cat "$failed" 2>/dev/null)" = "$reason" ] || printf 'project-rules: %s scan-failed %s\n' "$3" "${reason:-exit $rc}"
    [ "$1" != scan ] || printf '%s\n' "$reason" > "$failed"
  else
    rm -f -- "$failed"
  fi
  rm -f -- "$failed.new"
  while IFS= read -r line; do
    case "$line" in
      "fm-steer: "*)
        FM_HOME="${FM_HOME:-$(dirname "$2")}" "${FM_PROJECT_RULES_SEND:-$SCRIPT_DIR/fm-send.sh}" "$3" "${line#fm-steer: }" >/dev/null 2>&1 || true
        ;;
      '') ;;
      *) printf '%s\n' "$line" ;;
    esac
  done <<EOF_LOG
$out
EOF_LOG
}

verb=$1
shift
case "$verb" in
  scan | detect)
    state=${1:-${FM_STATE_OVERRIDE:-${FM_HOME:-$SCRIPT_DIR/..}/state}}
    if [ -n "${2:-}" ]; then
      read_log "$verb" "$state" "$2"
    else
      for record in "$state"/*.project-rules; do
        [ -f "$record" ] || continue
        read_log scan "$state" "$(basename "$record" .project-rules)"
      done
    fi
    ;;
  next)
    # The worker is already asking, so a compaction found here needs no steer.
    engine detect "$1" "$2" "$(rollout_for "$1" "$2")" >/dev/null || true
    engine next "$@"
    ;;
  ready)
    # A compaction first seen here is steered like any other. Readiness judged
    # on a log that could not be read would rest on stale evidence, so a failed
    # read refuses it.
    read_log detect "$1" "$2" >/dev/null
    if [ -e "$1/$2.project-rules.detect-failed" ]; then
      echo "fm-project-rules: $2 is not ready: the tool's own log or the task's record could not be read" >&2
      exit 1
    fi
    engine ready "$@"
    ;;
  *) engine "$verb" "$@" ;;
esac
