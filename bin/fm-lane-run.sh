#!/usr/bin/env bash
# Run an isolated focused, full, or verify lane and record an honest receipt.
#
# Usage: fm-lane-run.sh focused [--artifact <existing-command-output>] -- <command> [args...]
#        fm-lane-run.sh full [--artifact <existing-command-output>]
#        fm-lane-run.sh verify [--artifact <existing-command-output>]
#
# Full and verify commands are read from config/project-lanes.json. Child cwd,
# environment, argv and standard streams are inherited unchanged.
set -eu
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/fm-lane-run.py" "$@"
