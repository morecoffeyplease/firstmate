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

mkdir -p "$home/config"
cat > "$home/config/project-lanes.json" <<'JSON'
{"sample-project":{"full":["make","all"],"verify":["python3","-m","pytest","tests/with space"]}}
JSON
FM_HOME="$home" "$ROOT/bin/fm-brief.sh" brief-lanes-ship sample-project --mode direct-PR >/dev/null \
  || fail "configured ship brief scaffold failed"
brief="$home/data/brief-lanes-ship/brief.md"
assert_contains "$(cat "$brief")" \
  "For a focused command, run \`$ROOT/bin/fm-lane-run.sh focused -- '<command>' '<arg>'\`" \
  "ship brief did not render the explicit focused argv wrapper"
assert_contains "$(cat "$brief")" \
  "For the configured full lane, run \`$ROOT/bin/fm-lane-run.sh full\` (configured argv: \`make all\`)." \
  "ship brief did not render the configured full lane"
assert_contains "$(cat "$brief")" \
  "For the configured verify lane, run \`$ROOT/bin/fm-lane-run.sh verify\` (configured argv: \`python3 -m pytest 'tests/with space'\`)." \
  "ship brief did not quote configured verify argv"
pass "fm-brief: configured ship lanes render exact wrapped argv"

FM_HOME="$home" "$ROOT/bin/fm-brief.sh" brief-lanes-scout sample-project --scout >/dev/null \
  || fail "configured scout brief scaffold failed"
brief="$home/data/brief-lanes-scout/brief.md"
assert_contains "$(cat "$brief")" \
  "For the configured full lane, run \`$ROOT/bin/fm-lane-run.sh full\` (configured argv: \`make all\`)." \
  "scout brief did not render the configured full lane"
assert_contains "$(cat "$brief")" \
  "2. Stay inside this worktree; the only files you may write outside it are the report, status file, and this exact lane receipt directory: $home/data/brief-lanes-scout/lane-receipts." \
  "scout brief did not name its exact lane receipt directory"
pass "fm-brief: configured scout lanes and receipt path are explicit"
