#!/usr/bin/env bash
# Best-effort executable leaf for typed issue lifecycle instrumentation.
# Lifecycle owners call this command instead of sourcing the journal library,
# so a missing journal dependency cannot prevent their normal operation.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bin/fm-issue-events-lib.sh
. "$SCRIPT_DIR/fm-issue-events-lib.sh"

case "${1:-}" in
append)
  shift
  [ "$#" -ge 5 ] || exit 2
  exec python3 "$SCRIPT_DIR/fm_issue_event_guard.py" append "$1" "$2" \
    "$SCRIPT_DIR/fm-issue-event.sh" append-locked "$1" "$2" "${@:3}"
  ;;
append-locked)
  shift
  fm_issue_event_append "$@"
  ;;
validate)
  shift
  fm_issue_event_validate_file "$@"
  ;;
*)
  echo "usage: fm-issue-event.sh append <task-dir> <task-id> <generation> <kind> <fields-json> [class] | validate <events-file> <task-id>" >&2
  exit 2
  ;;
esac
