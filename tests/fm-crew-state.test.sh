#!/usr/bin/env bash
# Behavior tests for backend and status-log current-state reconciliation.
set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

CREW_STATE="$ROOT/bin/fm-crew-state.sh"
TMP_ROOT=$(fm_test_tmproot fm-crew-state)

new_case() {  # <name> <id> -> echoes the case directory
  local dir="$TMP_ROOT/$1" id=$2
  mkdir -p "$dir/state" "$dir/worktree" "$dir/fakebin"
  fm_write_meta "$dir/state/$id.meta" \
    "window=test-session:fm-$id" \
    "worktree=$dir/worktree" \
    "kind=ship" \
    "harness=claude"
  cat > "$dir/fakebin/tmux" <<'SH'
#!/usr/bin/env bash
case "${1:-}" in
  display-message)
    [ "${FM_TEST_PANE_MISSING:-0}" != 1 ] || exit 1
    printf '%%1\n'
    ;;
  list-windows)
    [ "${FM_TEST_PANE_MISSING:-0}" = 1 ] || printf 'fm-%s\n' "${FM_TEST_TASK_ID:-unknown}"
    exit 0
    ;;
  capture-pane)
    printf 'idle pane\n'
    ;;
esac
exit 0
SH
  chmod +x "$dir/fakebin/tmux"
  printf '%s\n' "$dir"
}

run_state() {  # <case-dir> <id>
  PATH="$1/fakebin:$PATH" FM_STATE_OVERRIDE="$1/state" FM_TEST_TASK_ID="$2" "$CREW_STATE" "$2"
}

new_codex_case() {  # <name> <id>
  local dir session=fm-lab-fake
  dir=$(new_case "$1" "$2")
  fm_write_meta "$dir/state/$2.meta" \
    "window=$session:w1:p1" \
    "worktree=$dir/worktree" \
    "kind=ship" \
    "harness=codex" \
    "backend=herdr" \
    "spawn_gen=s1791529200.101.1" \
    "herdr_session=$session" \
    "herdr_workspace_id=w1" \
    "herdr_tab_id=w1:t1" \
    "herdr_pane_id=w1:p1" \
    "endpoint_task_id=$2"
  mkdir -p "$dir/codex/sessions/2026/10/09"
  cat > "$dir/state/$2.codex-session" <<EOF
sessions_root=$dir/codex/sessions
workspace_root=$dir/worktree
binding_id=test-binding
EOF
  cat > "$dir/fakebin/herdr" <<'SH'
#!/usr/bin/env bash
set -u
case "${1:-} ${2:-}" in
  'status --json') printf '{"server":{"running":true}}\n' ;;
  'pane get') printf '{"result":{"pane":{"pane_id":"w1:p1"}}}\n' ;;
  'pane read') printf '%s\n' "${FM_TEST_CODEX_CAPTURE:-› Ask Codex to do anything}" ;;
  'pane process-info')
    printf '{"result":{"type":"pane_process_info","process_info":{"pane_id":"w1:p1","shell_pid":100,"foreground_processes":[{"pid":101,"name":"codex","argv0":"codex","argv":["/usr/bin/codex"]}]}}}\n'
    ;;
  *) printf '{"error":{"code":"unexpected_test_command"}}\n' ;;
esac
SH
  chmod +x "$dir/fakebin/herdr"
  printf '%s\n' "$dir"
}

run_codex_state() {  # <case-dir> <id> [pane-capture]
  PATH="$1/fakebin:$PATH" \
    FM_STATE_OVERRIDE="$1/state" \
    CODEX_HOME="$1/codex" \
    FM_TEST_CODEX_CAPTURE="${3:-}" \
    "$CREW_STATE" "$2"
}

write_codex_rollout() {  # <case-dir> <id> <jsonl-records>
  local dir=$1 id=$2 records=$3
  printf '%s\n' "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$dir/worktree\"}}" \
    > "$dir/codex/sessions/2026/10/09/rollout-2026-10-09T00-00-00-test.jsonl"
  printf '%b\n' "$records" >> "$dir/codex/sessions/2026/10/09/rollout-2026-10-09T00-00-00-test.jsonl"
}

write_idle_record() {  # <case-dir> <id>
  local dir=$1 id=$2 generation
  generation=$("$ROOT/bin/fm-busy-event.sh" arm "$dir/state" "$id") || fail "could not arm busy record"
  "$ROOT/bin/fm-busy-event.sh" apply "$dir/state" "$id" idle --gen "$generation" \
    --source claude-hook --event stop || fail "could not write idle record"
}

write_busy_record() {  # <case-dir> <id>
  local dir=$1 id=$2 generation
  generation=$("$ROOT/bin/fm-busy-event.sh" arm "$dir/state" "$id") || fail "could not arm busy record"
  "$ROOT/bin/fm-busy-event.sh" apply "$dir/state" "$id" busy --gen "$generation" \
    --source claude-hook --event user-prompt-submit || fail "could not write busy record"
}

test_busy_record_is_current_work() {
  local dir out
  dir=$(new_case busy busy)
  write_busy_record "$dir" busy
  out=$(run_state "$dir" busy)
  assert_contains "$out" 'state: working' 'a current semantic busy record reports working'
  assert_contains "$out" 'source: pane' 'busy state identifies the pane source'
  assert_contains "$out" 'claude-hook' 'busy state identifies its trusted writer'
  pass 'semantic busy record reports current work'
}

test_idle_record_allows_status_log_fallback() {
  local dir out
  dir=$(new_case idle idle)
  write_idle_record "$dir" idle
  printf 'needs-decision [key=storage]: choose a storage backend\nnote: unrelated progress\n' > "$dir/state/idle.status"
  out=$(run_state "$dir" idle)
  assert_contains "$out" 'state: parked' 'idle endpoint retains the open status-log decision'
  assert_contains "$out" 'source: status-log' 'idle endpoint identifies the status-log source'
  assert_contains "$out" 'choose a storage backend' 'status detail is preserved'
  pass 'idle endpoint reconciles the latest status event'
}

test_paused_status_is_distinct() {
  local dir out
  dir=$(new_case paused paused)
  write_idle_record "$dir" paused
  printf 'paused: waiting on an upstream release\nThe release window opens tomorrow.\n\n' > "$dir/state/paused.status"
  out=$(run_state "$dir" paused)
  assert_contains "$out" 'state: paused' 'a declared external wait remains distinct'
  assert_contains "$out" 'waiting on an upstream release' 'pause reason is preserved'
  pass 'paused status remains distinguishable from a wedge'
}

test_terminal_status_supersedes_stale_decision() {
  local dir out
  dir=$(new_case terminal terminal)
  write_idle_record "$dir" terminal
  printf 'needs-decision [key=choice]: choose one\ndone: shipped the selected option\n' > "$dir/state/terminal.status"
  out=$(run_state "$dir" terminal)
  assert_contains "$out" 'state: done' 'a single-owner terminal event supersedes its stale decision'
  assert_contains "$out" 'shipped the selected option' 'terminal detail is preserved'
  pass 'terminal declarations supersede stale decisions'
}

test_unrecognized_status_event_is_not_current_state() {
  local dir out
  dir=$(new_case resolved resolved)
  write_idle_record "$dir" resolved
  printf 'needs-decision [key=choice]: choose one\nresolved [key=choice]: chose one\n' > "$dir/state/resolved.status"
  out=$(run_state "$dir" resolved)
  assert_contains "$out" 'state: unknown' 'decision-closing event is not a state'
  assert_contains "$out" 'source: none' 'decision-closing event does not become a source'
  assert_not_contains "$out" 'chose one' 'resolution detail is not rendered as current state'
  pass 'decision-closing events do not replace current state'
}

test_missing_busy_record_does_not_infer_from_stale_log() {
  local dir out
  dir=$(new_case missing-busy missing)
  printf 'done: an old completion\n' > "$dir/state/missing.status"
  out=$(run_state "$dir" missing)
  assert_contains "$out" 'state: unknown' 'missing semantic state remains unknown'
  assert_not_contains "$out" 'state: done' 'stale status cannot override unknown endpoint state'
  pass 'missing semantic state does not trust a stale log'
}

test_missing_endpoint_does_not_trust_status_log() {
  local dir out
  dir=$(new_case gone gone)
  write_idle_record "$dir" gone
  printf 'done: old completion\n' > "$dir/state/gone.status"
  out=$(FM_TEST_PANE_MISSING=1 run_state "$dir" gone)
  assert_contains "$out" 'state: unknown' 'missing pane reports unknown'
  assert_contains "$out" 'backend target gone' 'positive absence is identified'
  assert_not_contains "$out" 'state: done' 'a stale status log is not used for a missing pane'
  pass 'missing endpoint cannot reuse a stale state event'
}

test_codex_herdr_busy_never_reads_idle_during_background_command() {
  local dir out
  dir=$(new_codex_case codex-working codex-working)
  write_codex_rollout "$dir" codex-working \
    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-1"}}'
  out=$(run_codex_state "$dir" codex-working)
  assert_contains "$out" 'state: working' 'a live Codex turn waiting on a command remains working'
  assert_contains "$out" 'harness busy (codex-rollout)' 'working state identifies Codex rollout events'
  pass 'a busy Codex worker with an empty-looking composer never reads idle'
}

test_codex_herdr_long_tool_call_never_reads_idle() {
  local dir out
  dir=$(new_codex_case codex-command codex-command)
  write_codex_rollout "$dir" codex-command \
    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-1"}}\n{"type":"response_item","payload":{"type":"function_call","call_id":"call-1","name":"exec_command"}}'
  out=$(run_codex_state "$dir" codex-command)
  assert_contains "$out" 'state: working' 'an open Codex tool call means the worker is working'
  assert_not_contains "$out" 'state: idle' 'a tool call waiting for output cannot be idle'
  pass 'Codex remains working while a long tool call has no matching output'
}

test_codex_herdr_idle_at_prompt() {
  local dir out
  dir=$(new_codex_case codex-idle codex-idle)
  write_codex_rollout "$dir" codex-idle \
    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-1"}}\n{"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-1"}}'
  printf 'working: old prompt submission\n' > "$dir/state/codex-idle.status"
  out=$(run_codex_state "$dir" codex-idle)
  assert_contains "$out" 'state: idle' 'the settled Codex rollout supersedes an old status event'
  assert_contains "$out" 'harness idle at prompt (codex-rollout)' 'idle identifies the Codex event source'
  pass 'Codex Herdr reports the settled idle prompt'
}

test_codex_herdr_needs_input_prompt_is_blocked() {
  local dir out
  dir=$(new_codex_case codex-input codex-input)
  write_codex_rollout "$dir" codex-input \
    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-1"}}\n{"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-1"}}'
  out=$(run_codex_state "$dir" codex-input 'Would you like to run the following command?
› 1. Yes, proceed (y)
  2. Yes, and do not ask again
  3. No, and tell Codex what to do differently (esc)
Press enter to confirm or esc to cancel')
  assert_contains "$out" 'state: blocked' 'a visible Codex input prompt is needs-input state'
  assert_contains "$out" 'harness needs input (codex-pane-input)' 'blocked state identifies its pane source'
  pass 'Codex pane capture reports a needs-input prompt as blocked'
}

test_codex_herdr_excludes_pre_spawn_rollouts() {
  local dir out
  dir=$(new_codex_case codex-prior codex-prior)
  mkdir -p "$dir/codex/sessions/2026/10/08"
  rm -f "$dir/state/codex-prior.codex-session"
  printf '%s\n%s\n' \
    "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$dir/worktree\"}}" \
    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"old-turn"}}' \
    > "$dir/codex/sessions/2026/10/08/rollout-2026-10-08T23-59-59-prior.jsonl"
  write_codex_rollout "$dir" codex-prior \
    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-1"}}\n{"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-1"}}'
  out=$(run_codex_state "$dir" codex-prior)
  assert_contains "$out" 'state: idle' 'a prior active rollout must not bind to a replacement worker'
  pass 'Codex rollout resolution excludes pre-spawn sessions without a sidecar'
}

test_codex_reused_worktree_selects_latest_rollout_after_spawn_read_only() {
  local dir out current newer binding
  dir=$(new_codex_case codex-reused codex-reused)
  binding="$dir/state/codex-reused.codex-session"
  rm -f "$binding"
  mkdir -p "$dir/codex/sessions/2026/10/08"
  printf '%s\n%s\n' \
    "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$dir/worktree\"}}" \
    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"old"}}' \
    > "$dir/codex/sessions/2026/10/08/rollout-2026-10-08T23-59-59-old.jsonl"
  current="$dir/codex/sessions/2026/10/09/rollout-2026-10-09T00-00-01-current.jsonl"
  newer="$dir/codex/sessions/2026/10/09/rollout-2026-10-09T00-00-02-restart.jsonl"
  printf '%s\n%s\n' \
    "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$dir/worktree\"}}" \
    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"current"}}' > "$current"
  printf '%s\n%s\n%s\n' \
    "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$dir/worktree\"}}" \
    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"restart"}}' \
    '{"type":"event_msg","payload":{"type":"task_complete","turn_id":"restart"}}' > "$newer"
  touch -t 202610090000.01 "$current"
  touch -t 202610090000.02 "$newer"
  out=$(run_codex_state "$dir" codex-reused)
  assert_contains "$out" 'state: idle' 'the newest restarted Codex rollout wins for a reused worktree'
  [ ! -e "$binding" ] || fail 'reading current state must not create a Codex binding sidecar'
  pass 'Codex state selects the newest post-spawn rollout without writing on read'
}

test_codex_simultaneous_rollouts_stay_unknown() {
  local dir out first second
  dir=$(new_codex_case codex-simultaneous codex-simultaneous)
  rm -f "$dir/state/codex-simultaneous.codex-session"
  first="$dir/codex/sessions/2026/10/09/rollout-2026-10-09T00-00-03-first.jsonl"
  second="$dir/codex/sessions/2026/10/09/rollout-2026-10-09T00-00-04-second.jsonl"
  printf '%s\n%s\n' \
    "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$dir/worktree\"}}" \
    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"first"}}' > "$first"
  printf '%s\n%s\n' \
    "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$dir/worktree\"}}" \
    '{"type":"event_msg","payload":{"type":"task_started","turn_id":"second"}}' > "$second"
  touch -t 202610090000.05 "$first" "$second"
  out=$(run_codex_state "$dir" codex-simultaneous)
  assert_contains "$out" 'state: unknown' 'equally recent eligible rollouts are ambiguous'
  assert_not_contains "$out" 'state: idle' 'ambiguous rollouts cannot claim idle'
  assert_not_contains "$out" 'state: working' 'ambiguous rollouts cannot claim working'
  pass 'Codex stays unknown when eligible rollout writes cannot be distinguished'
}

test_codex_herdr_unknown_without_rollout_stays_unknown() {
  local dir out
  dir=$(new_codex_case codex-unknown codex-unknown)
  out=$(run_codex_state "$dir" codex-unknown)
  assert_contains "$out" 'state: unknown' 'a missing rollout does not prove an idle prompt'
  assert_contains "$out" 'codex-rollout-unavailable' 'missing evidence is identified'
  pass 'Codex Herdr stays unknown when no task-bound rollout is available'
}

test_usage_error_is_distinct() {
  local out rc
  out=$("$CREW_STATE" 2>&1); rc=$?
  expect_code 2 "$rc" 'missing task id is a usage error'
  assert_contains "$out" 'usage: fm-crew-state.sh' 'usage diagnostic names the command'
  pass 'usage errors retain their explicit exit code'
}

test_busy_record_is_current_work
test_idle_record_allows_status_log_fallback
test_paused_status_is_distinct
test_terminal_status_supersedes_stale_decision
test_unrecognized_status_event_is_not_current_state
test_missing_busy_record_does_not_infer_from_stale_log
test_missing_endpoint_does_not_trust_status_log
test_codex_herdr_busy_never_reads_idle_during_background_command
test_codex_herdr_long_tool_call_never_reads_idle
test_codex_herdr_idle_at_prompt
test_codex_reused_worktree_selects_latest_rollout_after_spawn_read_only
test_codex_simultaneous_rollouts_stay_unknown
test_codex_herdr_needs_input_prompt_is_blocked
test_codex_herdr_excludes_pre_spawn_rollouts
test_codex_herdr_unknown_without_rollout_stays_unknown
test_usage_error_is_distinct

echo 'all fm-crew-state tests passed'
