#!/usr/bin/env bash
set -uo pipefail

EXPECTED_HEAD=1ec486cee92a9a93045895c21b067ae37e08f3af
GITHUB_WORKSPACE="$(printenv GITHUB_WORKSPACE 2>/dev/null || true)"
RUNNER_TEMP="$(printenv RUNNER_TEMP 2>/dev/null || true)"
EVIDENCE="$(printenv FM_EVIDENCE_DIR 2>/dev/null || true)"
[[ -n "$RUNNER_TEMP" ]] || RUNNER_TEMP=/tmp
[[ -n "$EVIDENCE" ]] || EVIDENCE="$RUNNER_TEMP/fm-calm-evidence"
SOURCE="$GITHUB_WORKSPACE/source"
SOURCE_FIXTURE="$SOURCE/tests/fm-calm-pi-extension.test.sh"
WORK="$RUNNER_TEMP/fm-calm-worktree-$$"
EXTRACTED="$RUNNER_TEMP/fm-calm-extracted-$$"
COUNTERFACTUAL="$RUNNER_TEMP/fm-calm-counterfactual-$$"
SCRATCH="$RUNNER_TEMP/fm-calm-scratch-$$"
COUNTERFACTUAL_FIXTURE_SOURCE="$GITHUB_WORKSPACE/tests/fixtures/linux-calm-reproduction/fm-calm-pi-extension.counterfactual.test.sh"
COUNTERFACTUAL_FIXTURE="$COUNTERFACTUAL/tests/fm-calm-pi-extension.test.sh"
COUNTERFACTUAL_FIXTURE_SHA256=024a7d9597bd0e660f5fd0eaa37483a594dd827080c24898576772e276fdb0f2
COUNTERFACTUAL_OBSERVER="$COUNTERFACTUAL/tests/fm-calm-pi-extension.linux-observe.test.sh"
OBSERVER="$COUNTERFACTUAL_OBSERVER"
PRIMARY_STATUS=70
CLEANUP_STATUS=0
SAFE_TO_REMOVE_TASK_DIRS=1
OBSERVER_CUSTODY_STATUS=not-verified
COUNTERFACTUAL_CUSTODY_STATUS=not-verified
COUNTERFACTUAL_FINAL_RESULT=not-attempted
TREE_EXPECTED=
SOURCE_PRESERVE=0
SOURCE_FINAL_RESULT=not-attempted
EXTRACTED_FINAL_RESULT=not-attempted
WORK_FINAL_RESULT=not-attempted
mkdir -p "$EVIDENCE" || exit 70

record() {
  printf '%s\t%s\t%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" "$2" >> "$EVIDENCE/stages.tsv" 2>/dev/null || true
}

write_checked_receipt() {
  local target=$1 label=$2 content=$3 temp="${1}.tmp.$$" rc
  if printf '%s\n' "$content" > "$temp" &&
     [[ "$(< "$temp")" == "$content" ]] &&
     mv -f -- "$temp" "$target" &&
     [[ "$(< "$target")" == "$content" ]]; then
    return 0
  else
    rc=$?
    printf 'receipt_write_failure=%s exit=%s target=%s\n' "$label" "$rc" "$target" >> "$EVIDENCE/receipt-write-failures.log" 2>/dev/null || true
    return 1
  fi
}

verify_counterfactual_candidate_input() {
  local phase=$1 source_hash_rc=125 evidence_hash_rc=125 compare_rc=125 source_hash='' evidence_hash='' content
  local source_receipt="$EVIDENCE/counterfactual-candidate-$phase-source.sha256"
  local evidence_receipt="$EVIDENCE/counterfactual-candidate-$phase-evidence.sha256"
  if [[ -f "$COUNTERFACTUAL_FIXTURE_SOURCE" && ! -L "$COUNTERFACTUAL_FIXTURE_SOURCE" ]] &&
     timeout --signal=TERM --kill-after=2s 8s sha256sum "$COUNTERFACTUAL_FIXTURE_SOURCE" > "$source_receipt" 2> "$source_receipt.stderr"; then source_hash_rc=0; else source_hash_rc=$?; fi
  if [[ -f "$EVIDENCE/counterfactual-fixture-recipe-input.sh" && ! -L "$EVIDENCE/counterfactual-fixture-recipe-input.sh" ]] &&
     timeout --signal=TERM --kill-after=2s 8s sha256sum "$EVIDENCE/counterfactual-fixture-recipe-input.sh" > "$evidence_receipt" 2> "$evidence_receipt.stderr"; then evidence_hash_rc=0; else evidence_hash_rc=$?; fi
  if [[ "$source_hash_rc" -eq 0 && "$evidence_hash_rc" -eq 0 ]] &&
     read -r source_hash _ < "$source_receipt" && read -r evidence_hash _ < "$evidence_receipt" &&
     [[ "$source_hash" == "$COUNTERFACTUAL_FIXTURE_SHA256" && "$evidence_hash" == "$COUNTERFACTUAL_FIXTURE_SHA256" ]]; then
    if timeout --signal=TERM --kill-after=2s 8s cmp -s -- "$COUNTERFACTUAL_FIXTURE_SOURCE" "$EVIDENCE/counterfactual-fixture-recipe-input.sh" 2> "$EVIDENCE/counterfactual-candidate-$phase-compare.stderr"; then compare_rc=0; else compare_rc=$?; fi
  fi
  content="phase=$phase"$'\n'"pinned_sha256=$COUNTERFACTUAL_FIXTURE_SHA256"$'\n'"source_hash_exit=$source_hash_rc"$'\n'"evidence_hash_exit=$evidence_hash_rc"$'\n'"byte_compare_exit=$compare_rc"$'\n'"source_hash=$source_hash"$'\n'"evidence_hash=$evidence_hash"
  if [[ "$source_hash_rc" -eq 0 && "$evidence_hash_rc" -eq 0 && "$compare_rc" -eq 0 && "$source_hash" == "$evidence_hash" ]]; then
    write_checked_receipt "$EVIDENCE/counterfactual-candidate-$phase.status" counterfactual-candidate-input "$content" || return 1
    return 0
  fi
  write_checked_receipt "$EVIDENCE/counterfactual-candidate-$phase.failure" counterfactual-candidate-input-failure "$content" || true
  return 1
}

verify_counterfactual_binding() {
  local phase=$1 head_rc=125 tree_rc=125 status_rc=125 fixture_hash_rc=125 actual_head='' actual_tree='' actual_status='' fixture_hash='' expected_status content
  local prefix="$EVIDENCE/counterfactual-$phase"
  local expected_fixture_status=' M tests/fm-calm-pi-extension.test.sh'
  if [[ -e "$COUNTERFACTUAL_OBSERVER" || -L "$COUNTERFACTUAL_OBSERVER" ]]; then
    expected_fixture_status+=$'\n?? tests/fm-calm-pi-extension.linux-observe.test.sh'
  fi
  if [[ -d "$COUNTERFACTUAL" && ! -L "$COUNTERFACTUAL" ]]; then
    if git -C "$COUNTERFACTUAL" rev-parse HEAD > "$prefix.head" 2> "$prefix.head.stderr"; then head_rc=0; else head_rc=$?; fi
    if git -C "$COUNTERFACTUAL" rev-parse 'HEAD^{tree}' > "$prefix.tree" 2> "$prefix.tree.stderr"; then tree_rc=0; else tree_rc=$?; fi
    if git -C "$COUNTERFACTUAL" status --porcelain --untracked-files=all > "$prefix.status" 2> "$prefix.status.stderr"; then status_rc=0; else status_rc=$?; fi
  fi
  if [[ -f "$COUNTERFACTUAL_FIXTURE" && ! -L "$COUNTERFACTUAL_FIXTURE" ]] &&
     timeout --signal=TERM --kill-after=2s 8s sha256sum "$COUNTERFACTUAL_FIXTURE" > "$prefix.fixture.sha256" 2> "$prefix.fixture.sha256.stderr"; then fixture_hash_rc=0; else fixture_hash_rc=$?; fi
  if [[ "$head_rc" -eq 0 ]]; then actual_head="$(< "$prefix.head")"; fi
  if [[ "$tree_rc" -eq 0 ]]; then actual_tree="$(< "$prefix.tree")"; fi
  if [[ "$status_rc" -eq 0 ]]; then actual_status="$(< "$prefix.status")"; fi
  if [[ "$fixture_hash_rc" -eq 0 ]]; then read -r fixture_hash _ < "$prefix.fixture.sha256" || fixture_hash=''; fi
  content="phase=$phase"$'\n'"result=failed"$'\n'"expected_head=$EXPECTED_HEAD"$'\n'"actual_head=$actual_head"$'\n'"expected_tree=$TREE_EXPECTED"$'\n'"actual_tree=$actual_tree"$'\n'"expected_candidate_sha256=$COUNTERFACTUAL_FIXTURE_SHA256"$'\n'"actual_candidate_sha256=$fixture_hash"$'\n'"head_exit=$head_rc"$'\n'"tree_exit=$tree_rc"$'\n'"status_exit=$status_rc"$'\n'"fixture_hash_exit=$fixture_hash_rc"$'\n'"expected_status=$expected_fixture_status"$'\n'"actual_status=$actual_status"
  if [[ "$head_rc" -eq 0 && "$tree_rc" -eq 0 && "$status_rc" -eq 0 && "$fixture_hash_rc" -eq 0 &&
        "$actual_head" == "$EXPECTED_HEAD" && "$actual_tree" == "$TREE_EXPECTED" &&
        "$fixture_hash" == "$COUNTERFACTUAL_FIXTURE_SHA256" && "$actual_status" == "$expected_fixture_status" ]] &&
     verify_counterfactual_candidate_input "$phase"; then
    content="phase=$phase"$'\n'"result=verified"$'\n'"expected_head=$EXPECTED_HEAD"$'\n'"actual_head=$actual_head"$'\n'"expected_tree=$TREE_EXPECTED"$'\n'"actual_tree=$actual_tree"$'\n'"candidate_sha256=$fixture_hash"$'\n'"head_exit=0"$'\n'"tree_exit=0"$'\n'"status_exit=0"$'\n'"fixture_hash_exit=0"$'\n'"git_status=$actual_status"
    write_checked_receipt "$prefix.binding.status" counterfactual-binding "$content" || return 1
    return 0
  fi
  write_checked_receipt "$prefix.binding.failure" counterfactual-binding-failure "$content" || true
  return 1
}

git_receipt() {
  local label=$1 path=$2 expected_head=$3 expected_tree=${4:-} tree_required=${5:-no}
  local prefix="$EVIDENCE/git-$label" head_rc tree_rc status_rc failed=0 unknown_tree=0
  if git -C "$path" rev-parse HEAD > "$prefix.head" 2> "$prefix.head.stderr"; then head_rc=0; else head_rc=$?; failed=1; fi
  if ! write_checked_receipt "$prefix.head.exit" "$label-head-exit" "$head_rc" || [[ "$(cat "$prefix.head.exit" 2>/dev/null)" != "$head_rc" ]]; then failed=1; fi
  if git -C "$path" rev-parse 'HEAD^{tree}' > "$prefix.tree" 2> "$prefix.tree.stderr"; then tree_rc=0; else tree_rc=$?; failed=1; fi
  if ! write_checked_receipt "$prefix.tree.exit" "$label-tree-exit" "$tree_rc" || [[ "$(cat "$prefix.tree.exit" 2>/dev/null)" != "$tree_rc" ]]; then failed=1; fi
  if git -C "$path" status --porcelain > "$prefix.status" 2> "$prefix.status.stderr"; then status_rc=0; else status_rc=$?; failed=1; fi
  if ! write_checked_receipt "$prefix.status.exit" "$label-status-exit" "$status_rc" || [[ "$(cat "$prefix.status.exit" 2>/dev/null)" != "$status_rc" ]]; then failed=1; fi
  if [[ "$head_rc" -ne 0 || "$tree_rc" -ne 0 || "$status_rc" -ne 0 ]]; then
    failed=1
  else
    local actual_head actual_tree
    actual_head="$(< "$prefix.head")"
    actual_tree="$(< "$prefix.tree")"
    if [[ "$actual_head" != "$expected_head" || -n "$expected_tree" && "$actual_tree" != "$expected_tree" || -s "$prefix.status" ]]; then
      failed=1
    fi
  fi
  if [[ "$tree_required" == required && -z "$expected_tree" ]]; then
    failed=1
    unknown_tree=1
  fi
  if [[ "$failed" -eq 0 ]]; then
    if write_checked_receipt "$prefix.result" "$label-result" "result=verified"$'\nexpected_head='"$expected_head"$'\nexpected_tree='"$expected_tree"; then
      return 0
    fi
    return 1
  fi
  if [[ "$unknown_tree" -eq 1 ]]; then
    write_checked_receipt "$prefix.result" "$label-result" "result=unknown"$'\nreason=expected-tree-identity-unavailable\nexpected_head='"$expected_head" || true
  else
    write_checked_receipt "$prefix.result" "$label-result" "result=failed"$'\nexpected_head='"$expected_head"$'\nexpected_tree='"$expected_tree" || true
  fi
  return 1
}

verify_observer_custody() {
  local phase=$1 original_hash_rc copy_hash_rc compare_rc original_hash='' copy_hash='' initial_original_hash='' initial_copy_hash='' initial_match=not-applicable
  local original_receipt="$EVIDENCE/observer-$phase-original.sha256"
  local copy_receipt="$EVIDENCE/observer-$phase-copy.sha256"
  if timeout --signal=TERM --kill-after=2s 8s sha256sum "$OBSERVER" > "$original_receipt" 2> "$original_receipt.stderr"; then original_hash_rc=0; else original_hash_rc=$?; fi
  if ! printf '%s\n' "$original_hash_rc" > "$original_receipt.exit"; then original_hash_rc=125; fi
  if timeout --signal=TERM --kill-after=2s 8s sha256sum "$EVIDENCE/observer-copy.sh" > "$copy_receipt" 2> "$copy_receipt.stderr"; then copy_hash_rc=0; else copy_hash_rc=$?; fi
  if ! printf '%s\n' "$copy_hash_rc" > "$copy_receipt.exit"; then copy_hash_rc=125; fi
  compare_rc=125
  if [[ "$original_hash_rc" -eq 0 && "$copy_hash_rc" -eq 0 ]]; then
    if read -r original_hash _ < "$original_receipt" && read -r copy_hash _ < "$copy_receipt" &&
       [[ "$original_hash" =~ ^[0-9a-f]{64}$ && "$copy_hash" =~ ^[0-9a-f]{64}$ ]]; then
      if timeout --signal=TERM --kill-after=2s 8s cmp -s -- "$OBSERVER" "$EVIDENCE/observer-copy.sh" 2> "$EVIDENCE/observer-$phase-byte-compare.stderr"; then compare_rc=0; else compare_rc=$?; fi
    fi
  fi
  if ! printf '%s\n' "$compare_rc" > "$EVIDENCE/observer-$phase-byte-compare.exit"; then compare_rc=125; fi
  if [[ "$phase" == cleanup ]]; then
    if [[ -f "$EVIDENCE/observer-before-fixture-custody.status" &&
          -f "$EVIDENCE/observer-before-fixture-original.sha256.exit" &&
          -f "$EVIDENCE/observer-before-fixture-copy.sha256.exit" &&
          "$(< "$EVIDENCE/observer-before-fixture-original.sha256.exit")" == 0 &&
          "$(< "$EVIDENCE/observer-before-fixture-copy.sha256.exit")" == 0 ]] &&
       read -r initial_original_hash _ < "$EVIDENCE/observer-before-fixture-original.sha256" &&
       read -r initial_copy_hash _ < "$EVIDENCE/observer-before-fixture-copy.sha256" &&
       [[ "$original_hash" == "$initial_original_hash" && "$copy_hash" == "$initial_copy_hash" ]]; then
      initial_match=yes
    else
      initial_match=no-or-unknown
    fi
  fi
  if [[ "$original_hash_rc" -eq 0 && "$copy_hash_rc" -eq 0 && "$compare_rc" -eq 0 && "$original_hash" == "$copy_hash" && "$initial_match" != no-or-unknown ]]; then
    if printf 'phase=%s\noriginal_hash_exit=0\ncopy_hash_exit=0\nbyte_compare_exit=0\nsha256_equal=yes\ninitial_hashes_unchanged=%s\n' "$phase" "$initial_match" > "$EVIDENCE/observer-$phase-custody.status"; then
      return 0
    fi
    return 1
  fi
  printf 'phase=%s\noriginal_hash_exit=%s\ncopy_hash_exit=%s\nbyte_compare_exit=%s\nsha256_equal=no-or-unknown\ninitial_hashes_unchanged=%s\n' \
    "$phase" "$original_hash_rc" "$copy_hash_rc" "$compare_rc" "$initial_match" > "$EVIDENCE/observer-$phase-custody.failure" || true
  return 1
}

preserve_observer_after_custody_failure() {
  local copy="$EVIDENCE/observer-original-after-custody-failure.sh" rc content
  if [[ ! -f "$OBSERVER" ]]; then
    content=$'original_observer=unavailable\nreason=generated-observer-path-missing'
    write_checked_receipt "$EVIDENCE/observer-original-preservation.status" observer-original-unavailable "$content" || return 1
    return 1
  fi
  if [[ -e "$copy" ]]; then
    content=$'original_observer_copy=already-present-unverified\nexact_custody=unresolved'
    write_checked_receipt "$EVIDENCE/observer-original-preservation.status" observer-copy-already-present "$content" || return 1
    return 1
  fi
  if timeout --signal=TERM --kill-after=2s 8s cp -- "$OBSERVER" "$copy" 2> "$EVIDENCE/observer-original-preservation.stderr"; then
    if timeout --signal=TERM --kill-after=2s 8s cmp -s -- "$OBSERVER" "$copy" 2> "$EVIDENCE/observer-original-preservation-compare.stderr"; then
      content=$'original_observer=byte-identical-copy-retained\ncopy_exit=0\ncompare_exit=0'
      write_checked_receipt "$EVIDENCE/observer-original-preservation.status" observer-copy-verified "$content" || return 1
      return 0
    else
      rc=$?
      content="original_observer_copy_compare_exit=$rc"
      write_checked_receipt "$EVIDENCE/observer-original-preservation.failure" observer-copy-compare-failed "$content" || true
      return 1
    fi
  else
    rc=$?
    content="original_observer_copy_exit=$rc"
    write_checked_receipt "$EVIDENCE/observer-original-preservation.failure" observer-copy-failed "$content" || true
    return 1
  fi
}

fixture_evidence_complete() {
  local status="$EVIDENCE/fixture-cleanup-capture.status" active alternate checkpoint capture_errors fixture_root
  [[ -f "$status" && ! -e "$EVIDENCE/fixture-cleanup-receipt-write-failures.log" && ! -e "$EVIDENCE/observer-receipt-write-failures.log" ]] || return 1
  grep -Fxq 'evidence_transfer=complete' "$status" 2>/dev/null || return 1
  grep -Fxq 'receipt_writes=complete' "$status" 2>/dev/null || return 1
  grep -Fxq 'fixture_tmp=recursively-copied-before-original-cleanup' "$status" 2>/dev/null || return 1
  grep -Fxq 'session_jsonl=byte-identical' "$status" 2>/dev/null || return 1
  grep -Eq '^pane_capture_errors=(none|recorded)$' "$status" 2>/dev/null || return 1
  [[ "$(cat "$EVIDENCE/fixture-cleanup-capture.started" 2>/dev/null)" == capture_started ]] || return 1
  [[ "$(cat "$EVIDENCE/fixture-tmp-evidence.copy" 2>/dev/null)" == copy_exit=0 ]] || return 1
  [[ -d "$EVIDENCE/fixture-tmp-evidence" && ! -L "$EVIDENCE/fixture-tmp-evidence" ]] || return 1
  [[ "$(cat "$EVIDENCE/fixture-tmp-evidence.session-check" 2>/dev/null)" == session_jsonl=byte-identical ]] || return 1
  [[ "$(cat "$EVIDENCE/tmux-session" 2>/dev/null)" == fm-calm-e2e ]] || return 1
  [[ "$(cat "$EVIDENCE/tmux-socket" 2>/dev/null)" =~ ^fm-calm-[0-9]+$ ]] || return 1
  fixture_root="$(cat "$EVIDENCE/fixture-tmp-root" 2>/dev/null)"
  case "$fixture_root" in "$SCRATCH"/tmp/fm-calm-pi-extension.*) ;; *) return 1 ;; esac
  if [[ "$(cat "$EVIDENCE/default-checkpoint.reached" 2>/dev/null)" == reached ]]; then
    [[ "$(cat "$EVIDENCE/default-checkpoint.started" 2>/dev/null)" == started ]] || return 1
  else
    checkpoint="$(cat "$EVIDENCE/default-checkpoint.outcome" 2>/dev/null)"
    [[ "$checkpoint" == observer-started-but-original-checkpoint-not-completed || "$checkpoint" == not-reached-before-original-cleanup ]] || return 1
  fi
  active="$(sed -n 's/^active_capture_exit=//p' "$status" 2>/dev/null)"
  alternate="$(sed -n 's/^alternate_capture_exit=//p' "$status" 2>/dev/null)"
  capture_errors="$(sed -n 's/^pane_capture_errors=//p' "$status" 2>/dev/null)"
  if [[ "$active" == unavailable && "$alternate" == unavailable ]]; then
    [[ "$capture_errors" == recorded && "$(cat "$EVIDENCE/fixture-exit-capture.unavailable" 2>/dev/null)" == tmux-unavailable ]]
    return $?
  fi
  [[ "$active" =~ ^[0-9]+$ && "$alternate" =~ ^[0-9]+$ ]] || return 1
  [[ "$(cat "$EVIDENCE/fixture-exit-active.exit" 2>/dev/null)" == "$active" ]] || return 1
  [[ "$(cat "$EVIDENCE/fixture-exit-alternate.exit" 2>/dev/null)" == "$alternate" ]] || return 1
  if [[ "$active" -eq 0 && "$alternate" -eq 0 ]]; then
    [[ "$capture_errors" == none ]] || return 1
  else
    [[ "$capture_errors" == recorded ]] || return 1
  fi
  [[ -f "$EVIDENCE/fixture-exit-active.stderr" && -f "$EVIDENCE/fixture-exit-alternate.stderr" ]]
}

capture_preserved_task_paths() {
  local -a paths=() tar_args=()
  local path receipt_content status_content source_missing=0 receipt_ok=1 path_receipt_ok=0 rc
  if [[ "$SOURCE_PRESERVE" -eq 1 ]]; then
    if [[ -e "$SOURCE" || -L "$SOURCE" ]]; then
      paths+=("$SOURCE")
      tar_args+=(-C "$GITHUB_WORKSPACE" source)
    else
      source_missing=1
    fi
  fi
  for path in "$WORK" "$EXTRACTED" "$COUNTERFACTUAL" "$SCRATCH"; do
    if [[ -e "$path" || -L "$path" ]]; then
      paths+=("$path")
      tar_args+=(-C "$RUNNER_TEMP" "${path##*/}")
    fi
  done
  if [[ "${#paths[@]}" -eq 0 ]]; then
    status_content='preserved_paths=none-present'
    if [[ "$source_missing" -eq 1 ]]; then status_content+=$'\nsource_checkout=missing-at-preservation-time'; fi
    write_checked_receipt "$EVIDENCE/preserved-task-paths.status" preserved-paths-none "$status_content" || return 1
    [[ "$source_missing" -eq 0 ]]
    return $?
  fi
  receipt_content='preserved_paths=present'
  for path in "${paths[@]}"; do receipt_content+=$'\n'"path=$path"; done
  if [[ "$source_missing" -eq 1 ]]; then receipt_content+=$'\nsource_checkout=missing-at-preservation-time'; receipt_ok=0; fi
  if write_checked_receipt "$EVIDENCE/preserved-task-paths.receipt" preserved-paths-list "$receipt_content"; then
    path_receipt_ok=1
  else
    path_receipt_ok=0
    receipt_ok=0
  fi
  if timeout --signal=TERM --kill-after=5s 25s tar -cf "$EVIDENCE/preserved-task-paths.tar" "${tar_args[@]}" \
    2> "$EVIDENCE/preserved-task-paths.tar.stderr"; then
    if [[ "$path_receipt_ok" -eq 1 ]]; then
      status_content=$'archive=created\npath_receipt=verified\npaths=listed-in-preserved-task-paths.receipt\narchive_consistency=not-atomic-concurrent-writes-not-ruled-out'
    else
      status_content=$'archive=created\npath_receipt=failed-or-unknown\narchive_consistency=not-atomic-concurrent-writes-not-ruled-out'
    fi
    if [[ "$source_missing" -eq 1 ]]; then status_content+=$'\nsource_checkout=missing-at-preservation-time'; fi
    if ! write_checked_receipt "$EVIDENCE/preserved-task-paths.status" preserved-paths-archive-created "$status_content"; then receipt_ok=0; fi
    [[ "$receipt_ok" -eq 1 ]]
    return $?
  else
    rc=$?
    status_content="archive=failed-or-partial"$'\n'"exit=$rc"$'\noriginal_paths=retained-until-runner-termination'
    if [[ "$source_missing" -eq 1 ]]; then status_content+=$'\nsource_checkout=missing-at-preservation-time'; fi
    if ! write_checked_receipt "$EVIDENCE/preserved-task-paths.status" preserved-paths-archive-failed "$status_content"; then receipt_ok=0; fi
    return 1
  fi
}

finish_cleanup() {
  trap - EXIT
  if [[ -f "$EVIDENCE/fixture-started" ]]; then
    if [[ -f "$EVIDENCE/tmux-socket" && -f "$EVIDENCE/tmux-session" ]]; then
      socket="$(cat "$EVIDENCE/tmux-socket" 2>/dev/null || true)"
      session="$(cat "$EVIDENCE/tmux-session" 2>/dev/null || true)"
      if [[ "$socket" == fm-calm-[0-9]* && "$session" == fm-calm-e2e ]]; then
        timeout --signal=TERM --kill-after=2s 8s tmux -L "$socket" kill-session -t "$session" >> "$EVIDENCE/cleanup.log" 2>&1
        kill_rc=$?
        printf 'kill_session_exit=%s\n' "$kill_rc" >> "$EVIDENCE/cleanup.log"
        timeout --signal=TERM --kill-after=2s 8s tmux -L "$socket" has-session -t "$session" >> "$EVIDENCE/cleanup.log" 2>&1
        verify_rc=$?
        printf 'session_verify_exit=%s\n' "$verify_rc" >> "$EVIDENCE/cleanup.log"
        if [[ "$verify_rc" -eq 0 ]]; then
          CLEANUP_STATUS=1
          SAFE_TO_REMOVE_TASK_DIRS=0
        elif [[ "$verify_rc" -ne 1 ]]; then
          CLEANUP_STATUS=1
          SAFE_TO_REMOVE_TASK_DIRS=0
        fi
      else
        echo 'cleanup failed: invalid recorded socket/session identity' >> "$EVIDENCE/cleanup.log"
        CLEANUP_STATUS=1
        SAFE_TO_REMOVE_TASK_DIRS=0
      fi
    else
      echo 'cleanup failed: socket/session identity receipt missing after fixture start' >> "$EVIDENCE/cleanup.log"
      CLEANUP_STATUS=1
      SAFE_TO_REMOVE_TASK_DIRS=0
    fi
    if [[ -f "$EVIDENCE/fixture-tmp-root" ]]; then
      root="$(cat "$EVIDENCE/fixture-tmp-root" 2>/dev/null || true)"
      case "$root" in
        "$SCRATCH"/tmp/fm-calm-pi-extension.*) if [[ -e "$root" ]]; then CLEANUP_STATUS=1; SAFE_TO_REMOVE_TASK_DIRS=0; fi ;;
        *) CLEANUP_STATUS=1; SAFE_TO_REMOVE_TASK_DIRS=0 ;;
      esac
    else
      CLEANUP_STATUS=1
      SAFE_TO_REMOVE_TASK_DIRS=0
    fi
  fi
  if [[ -f "$EVIDENCE/fixture-started" ]] && ! fixture_evidence_complete; then
    if [[ ! -e "$EVIDENCE/fixture-cleanup-capture.status" ]]; then
      write_checked_receipt "$EVIDENCE/fixture-cleanup-capture.status" fixture-cleanup-receipt-missing $'evidence_transfer=unknown\nreason=pre-cleanup-evidence-receipt-missing' || true
    fi
    CLEANUP_STATUS=1
    SAFE_TO_REMOVE_TASK_DIRS=0
  fi
  if [[ "$COUNTERFACTUAL_CUSTODY_STATUS" == verified ]]; then
    if ! verify_counterfactual_binding before-cleanup; then
      echo 'candidate fixture binding failed before cleanup; preserving all task paths' >> "$EVIDENCE/cleanup.log"
      COUNTERFACTUAL_CUSTODY_STATUS=failed
      CLEANUP_STATUS=1
      SAFE_TO_REMOVE_TASK_DIRS=0
    fi
  else
    echo 'candidate fixture custody was not verified; preserving all task paths' >> "$EVIDENCE/cleanup.log"
    CLEANUP_STATUS=1
    SAFE_TO_REMOVE_TASK_DIRS=0
  fi
  if [[ -e "$OBSERVER" ]]; then
    if [[ "$SAFE_TO_REMOVE_TASK_DIRS" -eq 1 && "$OBSERVER_CUSTODY_STATUS" == verified ]]; then
      if verify_observer_custody cleanup; then
        if timeout --signal=TERM --kill-after=2s 8s rm -f -- "$OBSERVER"; then
          :
        else
          echo 'generated observer removal failed; preserving task paths' >> "$EVIDENCE/cleanup.log"
          CLEANUP_STATUS=1
          SAFE_TO_REMOVE_TASK_DIRS=0
        fi
      else
        echo 'final observer custody verification failed; preserving original and task paths' >> "$EVIDENCE/cleanup.log"
        CLEANUP_STATUS=1
        SAFE_TO_REMOVE_TASK_DIRS=0
        preserve_observer_after_custody_failure || true
      fi
    else
      echo 'observer custody was not verified; preserving original and task paths' >> "$EVIDENCE/cleanup.log"
      CLEANUP_STATUS=1
      SAFE_TO_REMOVE_TASK_DIRS=0
      preserve_observer_after_custody_failure || true
    fi
  elif [[ "$OBSERVER_CUSTODY_STATUS" == verified ]]; then
    echo 'verified generated observer disappeared before cleanup; preserving task paths' >> "$EVIDENCE/cleanup.log"
    CLEANUP_STATUS=1
    SAFE_TO_REMOVE_TASK_DIRS=0
  fi
  if [[ "$SAFE_TO_REMOVE_TASK_DIRS" -eq 1 ]]; then
    if ! verify_counterfactual_binding final; then
      echo 'final candidate fixture binding failed; preserving all task paths' >> "$EVIDENCE/cleanup.log"
      COUNTERFACTUAL_CUSTODY_STATUS=failed
      CLEANUP_STATUS=1
      SAFE_TO_REMOVE_TASK_DIRS=0
    fi
  fi
  if [[ "$SAFE_TO_REMOVE_TASK_DIRS" -eq 1 ]]; then
    local task_path
    for task_path in "$WORK" "$EXTRACTED" "$COUNTERFACTUAL" "$SCRATCH"; do
      if [[ -e "$task_path" || -L "$task_path" ]] && { [[ ! -d "$task_path" ]] || [[ -L "$task_path" ]]; }; then
        printf 'task_path_type=unexpected\npath=%s\n' "$task_path" >> "$EVIDENCE/cleanup.log"
        CLEANUP_STATUS=1
        SAFE_TO_REMOVE_TASK_DIRS=0
      fi
    done
  fi
  final_git_proof() {
    local label=$1 path=$2 expected_tree=$3 outcome=unknown reason content
    local git_dir="$path/.git"
    if [[ ! -e "$path" && ! -L "$path" ]]; then
      outcome=unknown
      reason='path-not-present'
    elif [[ -L "$path" || ! -d "$path" ]]; then
      outcome=unknown
      reason='path-type-unexpected'
    elif [[ ! -e "$git_dir" && ! -L "$git_dir" ]]; then
      outcome=unknown
      reason=git-metadata-not-present
    elif [[ -L "$git_dir" || ! -d "$git_dir" ]]; then
      outcome=unknown
      reason=git-metadata-type-unexpected
    elif [[ -z "$expected_tree" ]]; then
      git_receipt "$label-final" "$path" "$EXPECTED_HEAD" "" required || true
      outcome=unknown
      reason=expected-tree-identity-unavailable
    elif git_receipt "$label-final" "$path" "$EXPECTED_HEAD" "$expected_tree" required; then
      outcome=verified
      reason=head-tree-and-clean-status-match
    else
      outcome=failed
      reason=git-command-failed-or-head-tree-status-mismatch
    fi
    if [[ "$label" == counterfactual && -e "$OBSERVER" ]]; then
      content=$'retained_observer=yes\nreason=observer-preserved-for-custody\ncounterfactual_status_expected=dirty-fixture-and-observer'
      if ! write_checked_receipt "$EVIDENCE/git-work-final-retained-observer.txt" work-retained-observer "$content"; then
        outcome=failed
        reason=retained-observer-note-write-failed
      fi
    fi
    content="path=$path"$'\n'"result=$outcome"$'\n'"reason=$reason"
    if ! write_checked_receipt "$EVIDENCE/git-$label-final.path-status" "$label-final-path-status" "$content"; then
      outcome=failed
      reason=final-path-status-write-failed
    fi
    case "$label" in
      source) SOURCE_FINAL_RESULT="$outcome:$reason" ;;
      extracted) EXTRACTED_FINAL_RESULT="$outcome:$reason" ;;
      work) WORK_FINAL_RESULT="$outcome:$reason" ;;
    esac
    if [[ "$label" == source && "$outcome" != verified ]]; then SOURCE_PRESERVE=1; fi
    if [[ "$outcome" != verified ]]; then
      CLEANUP_STATUS=1
      SAFE_TO_REMOVE_TASK_DIRS=0
      return 1
    fi
    return 0
  }
  final_git_proof source "$SOURCE" "${TREE_EXPECTED:-}" || true
  final_git_proof extracted "$EXTRACTED" "${TREE_EXPECTED:-}" || true
  final_git_proof work "$WORK" "${TREE_EXPECTED:-}" || true
  if [[ -e "$COUNTERFACTUAL" || -L "$COUNTERFACTUAL" ]]; then
    if [[ "$COUNTERFACTUAL_CUSTODY_STATUS" == verified ]] && verify_counterfactual_binding before-removal; then
      COUNTERFACTUAL_FINAL_RESULT=verified-before-removal
    else
      COUNTERFACTUAL_FINAL_RESULT=preserved-custody-unverified
      CLEANUP_STATUS=1
      SAFE_TO_REMOVE_TASK_DIRS=0
    fi
  else
    COUNTERFACTUAL_FINAL_RESULT=missing-before-removal
    CLEANUP_STATUS=1
    SAFE_TO_REMOVE_TASK_DIRS=0
  fi
  if [[ "$SAFE_TO_REMOVE_TASK_DIRS" -eq 1 ]]; then
    if [[ -d "$WORK" ]]; then
      if timeout --signal=TERM --kill-after=2s 8s rm -rf -- "$WORK"; then :; else CLEANUP_STATUS=1; SAFE_TO_REMOVE_TASK_DIRS=0; fi
    fi
    if [[ "$SAFE_TO_REMOVE_TASK_DIRS" -eq 1 && -d "$EXTRACTED" ]]; then
      if timeout --signal=TERM --kill-after=2s 8s rm -rf -- "$EXTRACTED"; then :; else CLEANUP_STATUS=1; SAFE_TO_REMOVE_TASK_DIRS=0; fi
    fi
    if [[ "$SAFE_TO_REMOVE_TASK_DIRS" -eq 1 && -d "$COUNTERFACTUAL" ]]; then
      if timeout --signal=TERM --kill-after=2s 8s rm -rf -- "$COUNTERFACTUAL"; then COUNTERFACTUAL_FINAL_RESULT=removed-after-custody-verified; else CLEANUP_STATUS=1; SAFE_TO_REMOVE_TASK_DIRS=0; fi
    fi
    if [[ "$SAFE_TO_REMOVE_TASK_DIRS" -eq 1 && -d "$SCRATCH" ]]; then
      if timeout --signal=TERM --kill-after=2s 8s rm -rf -- "$SCRATCH"; then :; else CLEANUP_STATUS=1; SAFE_TO_REMOVE_TASK_DIRS=0; fi
    fi
  fi
  if [[ ! -e "$COUNTERFACTUAL" && ! -L "$COUNTERFACTUAL" &&
        "$SAFE_TO_REMOVE_TASK_DIRS" -eq 1 && "$COUNTERFACTUAL_CUSTODY_STATUS" == verified ]]; then
    COUNTERFACTUAL_FINAL_RESULT=removed-after-custody-verified
  elif [[ -e "$COUNTERFACTUAL" || -L "$COUNTERFACTUAL" ]]; then
    if [[ "$COUNTERFACTUAL_CUSTODY_STATUS" == verified ]] && verify_counterfactual_binding final-preserved; then
      COUNTERFACTUAL_FINAL_RESULT=preserved-with-exact-fixture-delta
    else
      COUNTERFACTUAL_FINAL_RESULT=preserved-custody-unverified
      CLEANUP_STATUS=1
    fi
  else
    COUNTERFACTUAL_FINAL_RESULT=unknown
    CLEANUP_STATUS=1
  fi
  write_checked_receipt "$EVIDENCE/counterfactual-final.path-status" counterfactual-final-path "result=$COUNTERFACTUAL_FINAL_RESULT" || CLEANUP_STATUS=1
  if [[ "$SAFE_TO_REMOVE_TASK_DIRS" -eq 0 ]]; then
    capture_preserved_task_paths || CLEANUP_STATUS=1
  fi
  local cleanup_content
  cleanup_content="initiating_exit=$PRIMARY_STATUS"
  cleanup_content+=$'\n'"cleanup_status=$CLEANUP_STATUS"
  cleanup_content+=$'\n'"final_source=$SOURCE_FINAL_RESULT"
  cleanup_content+=$'\n'"final_extracted=$EXTRACTED_FINAL_RESULT"
  cleanup_content+=$'\n'"final_work=$WORK_FINAL_RESULT"
  cleanup_content+=$'\n'"final_counterfactual=$COUNTERFACTUAL_FINAL_RESULT"
  cleanup_content+=$'\n'"source_archive_requested=$([[ "$SOURCE_PRESERVE" -eq 1 ]] && echo yes || echo no)"
  cleanup_content+=$'\n'"observer_copy_retained=$([[ -f "$EVIDENCE/observer-copy.sh" ]] && echo yes || echo no)"
  cleanup_content+=$'\n'"fixture_source_retained=$([[ -f "$EVIDENCE/fixture-source.sh" ]] && echo yes || echo no)"
  cleanup_content+=$'\n'"archive_retained=$([[ -f "$EVIDENCE/source-head.tar" || -f "$EVIDENCE/preserved-task-paths.tar" ]] && echo yes || echo no)"
  cleanup_content+=$'\n'"preserved_worktree=$([[ -e "$WORK" || -L "$WORK" ]] && echo yes || echo no)"
  cleanup_content+=$'\n'"preserved_counterfactual=$([[ -e "$COUNTERFACTUAL" || -L "$COUNTERFACTUAL" ]] && echo yes || echo no)"
  cleanup_content+=$'\n'"preserved_extracted_tree=$([[ -e "$EXTRACTED" || -L "$EXTRACTED" ]] && echo yes || echo no)"
  cleanup_content+=$'\n'"preserved_scratch=$([[ -e "$SCRATCH" || -L "$SCRATCH" ]] && echo yes || echo no)"
  if ! write_checked_receipt "$EVIDENCE/cleanup-receipt.txt" cleanup-summary "$cleanup_content"; then
    CLEANUP_STATUS=1
  fi
  if [[ "$PRIMARY_STATUS" -eq 0 && "$CLEANUP_STATUS" -ne 0 ]]; then exit 70; fi
  exit "$PRIMARY_STATUS"
}
trap finish_cleanup EXIT
trap 'PRIMARY_STATUS=130; record signal-int 130; exit 130' INT
trap 'PRIMARY_STATUS=143; record signal-term 143; exit 143' TERM

fail_stage() {
  stage=$1
  code=$2
  record "$stage" "$code"
  PRIMARY_STATUS=$code
  exit "$code"
}

record wrapper-start started
if (
  set -e
  printf 'utc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf 'image_os=%s\nimage_version=%s\nworkspace=%s\nrunner_temp=%s\n' \
    "$(printenv ImageOS 2>/dev/null || echo unknown)" \
    "$(printenv ImageVersion 2>/dev/null || echo unknown)" "$GITHUB_WORKSPACE" "$RUNNER_TEMP"
  uname -a
  cat /etc/os-release
  id
  node --version
  tmux -V
  printf 'TERM=%s\n' "$(printenv TERM 2>/dev/null || echo unset)"
) > "$EVIDENCE/runner-receipt.txt" 2>&1; then
  record runner-receipt 0
else
  rc=$?
  printf 'runner_receipt_exit=%s\n' "$rc" > "$EVIDENCE/runner-receipt.exit"
  fail_stage runner-receipt "$rc"
fi

while IFS='|' read -r source_rel target_name; do
  [[ -n "$source_rel" ]] || continue
  if cp -- "$GITHUB_WORKSPACE/$source_rel" "$EVIDENCE/$target_name"; then
    record "preserve-$target_name" 0
  else
    rc=$?
    fail_stage "preserve-$target_name" "$rc"
  fi
done <<'FILES'
.github/workflows/fm-linux-calm-repro.yml|workflow-source.yml
bin/fm-linux-calm-repro.sh|runner-source.sh
tests/fixtures/linux-calm-reproduction/observe.py|observer-generator.py
tests/fixtures/linux-calm-reproduction/fm-calm-pi-extension.counterfactual.test.sh|counterfactual-fixture-recipe-source.sh
FILES
sha256sum "$EVIDENCE/workflow-source.yml" "$EVIDENCE/runner-source.sh" "$EVIDENCE/observer-generator.py" "$EVIDENCE/counterfactual-fixture-recipe-source.sh" > "$EVIDENCE/recipe-source.sha256" || fail_stage recipe-hash 70
if [[ ! -f "$COUNTERFACTUAL_FIXTURE_SOURCE" || -L "$COUNTERFACTUAL_FIXTURE_SOURCE" ]] ||
   ! cp -- "$COUNTERFACTUAL_FIXTURE_SOURCE" "$EVIDENCE/counterfactual-fixture-recipe-input.sh"; then
  echo 'pinned counterfactual fixture input unavailable or not a regular file' > "$EVIDENCE/counterfactual-candidate-source.failure"
  fail_stage counterfactual-candidate-source 66
fi
if ! verify_counterfactual_candidate_input recipe-source; then
  echo 'pinned counterfactual fixture source or retained bytes failed digest/custody verification' > "$EVIDENCE/counterfactual-candidate-source.failure"
  SAFE_TO_REMOVE_TASK_DIRS=0
  fail_stage counterfactual-candidate-source 64
fi
printf 'recipe_candidate_fixture_sha256=%s\n' "$COUNTERFACTUAL_FIXTURE_SHA256" > "$EVIDENCE/counterfactual-candidate-pinned.sha256" || fail_stage counterfactual-candidate-receipt 70

if [[ -f "$SOURCE_FIXTURE" ]]; then
  if cp -- "$SOURCE_FIXTURE" "$EVIDENCE/fixture-source.sh"; then
    FIXTURE_HASH="$(sha256sum "$SOURCE_FIXTURE" | cut -d ' ' -f 1)" || fail_stage source-fixture-hash 70
    printf '%s\n' "$FIXTURE_HASH" > "$EVIDENCE/fixture-source.sha256"
    record preserve-source-fixture 0
  else
    rc=$?
    fail_stage preserve-source-fixture "$rc"
  fi
else
  echo 'source fixture unavailable for custody' > "$EVIDENCE/source-fixture.failure"
  fail_stage preserve-source-fixture 66
fi
if [[ ! -d "$SOURCE/.git" || -L "$SOURCE" || -L "$SOURCE/.git" ]]; then echo 'source checkout metadata unavailable or source path metadata is a symlink' > "$EVIDENCE/source-qualification.failure"; SAFE_TO_REMOVE_TASK_DIRS=0; SOURCE_PRESERVE=1; fail_stage source-git-metadata 64; fi
if ! git_receipt source-initial "$SOURCE" "$EXPECTED_HEAD"; then
  echo 'source checkout Git identity or status was unreadable or did not match' > "$EVIDENCE/source-qualification.failure"
  SAFE_TO_REMOVE_TASK_DIRS=0
  SOURCE_PRESERVE=1
  fail_stage source-checkout 64
fi
TREE_EXPECTED="$(< "$EVIDENCE/git-source-initial.tree")"
if timeout --signal=TERM --kill-after=5s 25s git -C "$SOURCE" archive --format=tar "$EXPECTED_HEAD" > "$EVIDENCE/source-head.tar" 2> "$EVIDENCE/source-archive.stderr"; then
  record source-archive 0
else
  rc=$?
  printf '%s\n' "$rc" > "$EVIDENCE/source-archive.exit"
  fail_stage source-archive "$rc"
fi
sha256sum "$EVIDENCE/source-head.tar" > "$EVIDENCE/source-archive.sha256" || fail_stage archive-hash 70
mkdir "$EXTRACTED" || fail_stage extracted-directory 70
if timeout --signal=TERM --kill-after=5s 25s tar -xf "$EVIDENCE/source-head.tar" --no-same-owner -C "$EXTRACTED" > "$EVIDENCE/source-extract.log" 2>&1; then
  record archive-extract 0
else
  rc=$?
  printf '%s\n' "$rc" > "$EVIDENCE/source-extract.exit"
  fail_stage archive-extract "$rc"
fi
if timeout --signal=TERM --kill-after=5s 25s cp -a "$SOURCE/.git" "$EXTRACTED/.git"; then record restore-commit-metadata 0; else rc=$?; fail_stage restore-commit-metadata "$rc"; fi
if ! git_receipt extracted-qualified "$EXTRACTED" "$EXPECTED_HEAD" "$TREE_EXPECTED"; then
  echo 'extracted archive Git identity or status was unreadable or did not match' > "$EVIDENCE/archive-qualification.failure"
  SAFE_TO_REMOVE_TASK_DIRS=0
  fail_stage archive-qualification 64
fi
printf 'commit=%s\ntree=%s\narchive_sha256=%s\n' "$EXPECTED_HEAD" "$TREE_EXPECTED" "$(cut -d ' ' -f 1 "$EVIDENCE/source-archive.sha256")" > "$EVIDENCE/archive-receipt.txt"
FIXTURE="$EXTRACTED/tests/fm-calm-pi-extension.test.sh"
EXTRACTED_FIXTURE_HASH="$(sha256sum "$FIXTURE" | cut -d ' ' -f 1)" || fail_stage extracted-fixture-hash 70
if [[ "$EXTRACTED_FIXTURE_HASH" != "$FIXTURE_HASH" ]]; then
  echo 'extracted fixture bytes differ from the checked-out source fixture' > "$EVIDENCE/archive-fixture.failure"
  fail_stage archive-fixture-verify 64
fi
printf 'source_fixture_sha256=%s\nextracted_fixture_sha256=%s\n' "$FIXTURE_HASH" "$EXTRACTED_FIXTURE_HASH" > "$EVIDENCE/archive-fixture-receipt.txt"
record archive-fixture-verify 0

source /etc/os-release
if [[ "$(uname -s)" != Linux || "$ID" != ubuntu || "$VERSION_ID" != 24.04 ||
      "$(node --version 2>&1)" != v22.23.3 || "$(tmux -V 2>&1)" != 'tmux 3.4' ]]; then
  echo 'runtime qualification mismatch; fixture not started' > "$EVIDENCE/runtime-qualification.failure"
  fail_stage runtime-qualification 64
fi
record runtime-qualification 0

mkdir "$SCRATCH" || fail_stage scratch-create 70
mkdir "$SCRATCH/npm" "$SCRATCH/tmp" || fail_stage scratch-layout 70
if timeout --signal=TERM --kill-after=5s 25s cp -a "$EXTRACTED" "$WORK"; then record worktree-copy 0; else rc=$?; fail_stage worktree-copy "$rc"; fi
if [[ "$(sha256sum "$WORK/tests/fm-calm-pi-extension.test.sh" | cut -d ' ' -f 1)" != "$FIXTURE_HASH" ]] ||
   ! git_receipt work-qualified "$WORK" "$EXPECTED_HEAD" "$TREE_EXPECTED"; then
  echo 'execution worktree Git identity, fixture bytes, or status was unreadable or did not match' > "$EVIDENCE/worktree-qualification.failure"
  SAFE_TO_REMOVE_TASK_DIRS=0
  fail_stage worktree-qualification 64
fi
record worktree-qualification 0
if [[ -e "$COUNTERFACTUAL" || -L "$COUNTERFACTUAL" ]]; then
  echo 'counterfactual path already exists' > "$EVIDENCE/counterfactual-path.failure"
  SAFE_TO_REMOVE_TASK_DIRS=0
  fail_stage counterfactual-path 64
fi
if timeout --signal=TERM --kill-after=5s 25s cp -a "$WORK" "$COUNTERFACTUAL"; then record counterfactual-copy 0; else rc=$?; SAFE_TO_REMOVE_TASK_DIRS=0; fail_stage counterfactual-copy "$rc"; fi
if ! git_receipt counterfactual-baseline "$COUNTERFACTUAL" "$EXPECTED_HEAD" "$TREE_EXPECTED"; then
  echo 'counterfactual baseline copy did not retain the qualified original Git identity and clean status' > "$EVIDENCE/counterfactual-baseline.failure"
  SAFE_TO_REMOVE_TASK_DIRS=0
  fail_stage counterfactual-baseline 64
fi
if [[ "$(sha256sum "$COUNTERFACTUAL/tests/fm-calm-pi-extension.test.sh" | cut -d ' ' -f 1)" != "$FIXTURE_HASH" ]]; then
  echo 'counterfactual baseline fixture differs from the original fixture digest' > "$EVIDENCE/counterfactual-original-fixture.failure"
  SAFE_TO_REMOVE_TASK_DIRS=0
  fail_stage counterfactual-original-fixture 64
fi
if ! verify_counterfactual_candidate_input before-copy; then
  echo 'counterfactual recipe input custody failed before copy' > "$EVIDENCE/counterfactual-candidate-before-copy.failure"
  SAFE_TO_REMOVE_TASK_DIRS=0
  fail_stage counterfactual-candidate-before-copy 64
fi
if timeout --signal=TERM --kill-after=2s 8s cp -- "$EVIDENCE/counterfactual-fixture-recipe-input.sh" "$COUNTERFACTUAL_FIXTURE"; then
  record counterfactual-fixture-copy 0
else
  rc=$?
  SAFE_TO_REMOVE_TASK_DIRS=0
  fail_stage counterfactual-fixture-copy "$rc"
fi
if ! verify_counterfactual_binding fixture-only; then
  echo 'derived counterfactual does not contain only the pinned fixture delta' > "$EVIDENCE/counterfactual-fixture-only.failure"
  SAFE_TO_REMOVE_TASK_DIRS=0
  COUNTERFACTUAL_CUSTODY_STATUS=failed
  fail_stage counterfactual-fixture-only 64
fi
COUNTERFACTUAL_CUSTODY_STATUS=verified
record counterfactual-fixture-only 0
if timeout --signal=TERM --kill-after=10s 180s npm install --prefix "$SCRATCH/npm" --no-audit --no-fund '@earendil-works/pi-coding-agent@1.1.0' > "$EVIDENCE/install.log" 2>&1; then
  printf '0\n' > "$EVIDENCE/install.exit"; record pi-install 0
else
  rc=$?; printf '%s\n' "$rc" > "$EVIDENCE/install.exit"; fail_stage pi-install "$rc"
fi
export PATH="$SCRATCH/npm/node_modules/.bin:$PATH"
export FM_PI_PACKAGE_DIR="$SCRATCH/npm/node_modules/@earendil-works/pi-coding-agent"
export TMPDIR="$SCRATCH/tmp"
if node -p "require('$FM_PI_PACKAGE_DIR/package.json').version" > "$EVIDENCE/pi-package-version.txt" 2> "$EVIDENCE/pi-package-version.stderr"; then
  record pi-package-receipt 0
else
  rc=$?; printf '%s\n' "$rc" > "$EVIDENCE/pi-package-version.exit"; fail_stage pi-package-receipt "$rc"
fi
if [[ "$(cat "$EVIDENCE/pi-package-version.txt")" != 1.1.0 ]]; then echo 'Pi package metadata mismatch' > "$EVIDENCE/runtime-qualification.failure"; fail_stage pi-package-version 64; fi
if pi --version > "$EVIDENCE/pi-version.txt" 2>&1; then record pi-cli-version 0; else rc=$?; printf '%s\n' "$rc" > "$EVIDENCE/pi-version.exit"; fail_stage pi-cli-version "$rc"; fi
if npm ls --prefix "$SCRATCH/npm" --all --json > "$EVIDENCE/npm-tree.json" 2> "$EVIDENCE/npm-tree.stderr"; then record npm-dependency-tree 0; else rc=$?; printf '%s\n' "$rc" > "$EVIDENCE/npm-tree.exit"; fail_stage npm-dependency-tree "$rc"; fi
if python3 "$EVIDENCE/observer-generator.py" --source "$COUNTERFACTUAL_FIXTURE" --output "$COUNTERFACTUAL_OBSERVER" --evidence "$EVIDENCE" --expected-source-sha256 "$COUNTERFACTUAL_FIXTURE_SHA256" > "$EVIDENCE/observer-generator.log" 2>&1; then
  record observer-generation 0
else
  rc=$?; printf '%s\n' "$rc" > "$EVIDENCE/observer-generator.exit"; fail_stage observer-generation "$rc"
fi
if [[ "$(cat "$EVIDENCE/observer-source.sha256" 2>/dev/null)" != "$COUNTERFACTUAL_FIXTURE_SHA256" ]] ||
   ! verify_counterfactual_binding observer-generated ||
   ! verify_observer_custody before-fixture; then
  OBSERVER_CUSTODY_STATUS=failed
  COUNTERFACTUAL_CUSTODY_STATUS=failed
  SAFE_TO_REMOVE_TASK_DIRS=0
  echo 'observer input, output, or candidate binding failed custody verification' > "$EVIDENCE/observer-qualification.failure"
  fail_stage observer-retention 70
else
  OBSERVER_CUSTODY_STATUS=verified
  record observer-retention 0
fi
printf 'started\n' > "$EVIDENCE/fixture-started"
export FM_EVIDENCE_DIR="$EVIDENCE"
if ! verify_counterfactual_binding before-fixture; then
  echo 'candidate fixture or observer binding changed before whole-fixture invocation' > "$EVIDENCE/counterfactual-before-run.failure"
  SAFE_TO_REMOVE_TASK_DIRS=0
  COUNTERFACTUAL_CUSTODY_STATUS=failed
  fail_stage counterfactual-before-run 64
fi
if timeout --signal=TERM --kill-after=10s 900s "$COUNTERFACTUAL/bin/fm-test-run.sh" "$COUNTERFACTUAL_OBSERVER" --jobs 1 > "$EVIDENCE/runner.log" 2>&1; then
  PRIMARY_STATUS=0
else
  PRIMARY_STATUS=$?
fi
printf '%s\n' "$PRIMARY_STATUS" > "$EVIDENCE/runner.exit"
record focused-fixture "$PRIMARY_STATUS"
if ! fixture_evidence_complete; then
  if [[ ! -e "$EVIDENCE/fixture-cleanup-capture.status" ]]; then
    write_checked_receipt "$EVIDENCE/fixture-cleanup-capture.status" fixture-cleanup-receipt-missing $'evidence_transfer=unknown\nreason=pre-cleanup-evidence-receipt-missing' || true
  fi
  CLEANUP_STATUS=1
  SAFE_TO_REMOVE_TASK_DIRS=0
fi
exit "$PRIMARY_STATUS"
