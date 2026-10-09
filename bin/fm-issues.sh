#!/usr/bin/env bash
# Start the local Firstmate Issues table, or render its cached projection.
#
# Usage: fm-issues.sh [--project <registered-name>] [--terminal|--json] [--refresh]
#        fm-issues.sh summary request --project <registered-name>...
#        fm-issues.sh summary put <request-id> <project> --basis-fingerprint <sha256> \
#          --basis-transition-watermark <sha256> --basis-observed-at <epoch> --author <home> --text-file <path>
#        fm-issues.sh summary route <request-id> <project> --target <task-id> --correlation <id>
#        fm-issues.sh summary dispatch <request-id>
#        fm-issues.sh summary list
#
# The browser service binds only to loopback. Routine table collection is
# deterministic and does not start an agent. The summary request is optional
# and enters Firstmate through its durable inbox.
set -eu
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/fm-issues.py" "$@"
