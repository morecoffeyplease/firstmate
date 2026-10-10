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
command -v node >/dev/null 2>&1 || { echo "fm-project-rules: node is required and was not found on PATH" >&2; exit 1; }
export FM_PROJECT_RULES_HELPER="$SCRIPT_DIR/fm-project-rules.sh"
engine() { node "$SCRIPT_DIR/fm-project-rules.mjs" "$@"; }

# The Codex session log for a task, resolved by the busy-state owner.
rollout_for() {  # <state> <id>
  [ -e "$1/$2.codex-session" ] || return 0
  # shellcheck source=bin/fm-busy-lib.sh
  ( . "$SCRIPT_DIR/fm-busy-lib.sh" && fm_busy_codex_rollout_log "$1" "$2" ) 2>/dev/null || true
}

verb=$1
shift
case "$verb" in
  scan | detect)
    state=${1:-${FM_STATE_OVERRIDE:-${FM_HOME:-$SCRIPT_DIR/..}/state}}
    if [ -n "${2:-}" ]; then
      engine "$verb" "$state" "$2" "$(rollout_for "$state" "$2")"
    else
      for record in "$state"/*.project-rules; do
        [ -f "$record" ] || continue
        id=$(basename "$record" .project-rules)
        engine scan "$state" "$id" "$(rollout_for "$state" "$id")"
      done
    fi
    ;;
  next | ready)
    [ ! -f "$1/$2.project-rules" ] || engine detect "$1" "$2" "$(rollout_for "$1" "$2")" || true
    engine "$verb" "$@"
    ;;
  *) engine "$verb" "$@" ;;
esac
