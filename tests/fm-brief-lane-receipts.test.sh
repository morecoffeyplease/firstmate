#!/usr/bin/env bash
# Regression check for the exact task-local lane-receipt path in worker briefs.
set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TMP_ROOT=$(fm_test_tmproot fm-brief-lane-receipts)
home="$TMP_ROOT/home"
id='brief-lane-receipts'

mkdir -p "$home/data"
FM_HOME="$home" "$ROOT/bin/fm-brief.sh" "$id" sample-project --mode direct-PR >/dev/null \
  || fail "worker brief scaffold failed"

brief="$home/data/$id/brief.md"
assert_contains "$(cat "$brief")" \
  "2. Stay inside this worktree; modify nothing outside it except the status file and this exact lane receipt directory: $home/data/$id/lane-receipts." \
  "worker brief did not name its exact lane receipt directory"
pass "fm-brief: worker brief scopes lane receipts to the exact task data path"
