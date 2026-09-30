#!/usr/bin/env bash
# tests/fm-wake-drain-open-decisions.test.sh - behavior tests for the OPEN
# DECISIONS section bin/fm-wake-drain.sh prints on every drain (including the
# empty-queue fast path). The section is pure wiring around
# fm-classify-lib.sh's status_open_decisions fold (the ONE authoritative
# open/resolved statement); these tests exercise the real drain script over
# crafted status logs and assert on its printed output, not on the fold's own
# source text.
set -u

# shellcheck source=tests/wake-helpers.sh
. "$(dirname "${BASH_SOURCE[0]}")/wake-helpers.sh"

DRAIN="$ROOT/bin/fm-wake-drain.sh"

TMP_ROOT=$(fm_test_tmproot fm-wake-drain-open-decisions-tests)

test_buried_decision_still_surfaces() {
  local dir state out
  dir=$(make_case buried)
  state="$dir/state"
  out="$dir/drain.out"
  # The needs-decision line sits under later routine and unrelated-key lines,
  # exactly the burial scenario the fix targets: last-line-only reads would
  # show "resolved [key=other]" and hide the still-open api-shape decision.
  printf 'needs-decision [key=api-shape]: pick REST or RPC\n' > "$state/task1.status"
  printf 'working: continuing other work\n' >> "$state/task1.status"
  printf 'resolved [key=other]: unrelated decision closed\n' >> "$state/task1.status"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed on a buried decision"

  grep -F 'OPEN DECISIONS' "$out" >/dev/null || fail "buried decision produced no OPEN DECISIONS section"
  grep -F 'task1' "$out" | grep -F '[key=api-shape]' | grep -F 'pick REST or RPC' >/dev/null \
    || fail "buried needs-decision was not surfaced with its task, key, and note"
  grep -F "close one by answering it: bin/fm-send.sh <task> --resolve-key <key>" "$out" >/dev/null \
    || fail "open section is missing the answerer-closes hint"
  pass "a needs-decision buried under later routine/other-key lines still reports as open"
}

test_explicit_resolution_closes_it() {
  local dir state out
  dir=$(make_case resolved)
  state="$dir/state"
  out="$dir/drain.out"
  printf 'needs-decision [key=api-shape]: pick REST or RPC\n' > "$state/task2.status"
  printf 'resolved [key=api-shape]: went with REST\n' >> "$state/task2.status"
  printf 'done: shipped\n' >> "$state/task2.status"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed after an explicit resolution"

  if grep -F 'OPEN DECISIONS' "$out" >/dev/null; then
    fail "an explicitly resolved decision still printed as open: $(cat "$out")"
  fi
  pass "an explicit resolved [key=X] closes the keyed decision"
}

test_reserved_key_namespace_is_owned_by_its_library() {
  local dir state out
  dir=$(make_case reserved-key)
  state="$dir/state"
  out="$dir/drain.out"
  # `pending-reply-<id>` names a decision bin/fm-pending-reply-lib.sh raises and
  # is the only writer that closes it. Every writer reaches this same stream - a
  # local mate appends into it directly, and a remote mate's lines are mirrored
  # into it verbatim - so another writer must not be able to take that key over
  # or clear it just by naming it.
  printf 'blocked [key=pending-reply-abcdef0123456789]: pending-reply-missed: task=ios pending-reply-id=abcdef0123456789 request=ship it\n' > "$state/task9.status"
  printf 'blocked [key=pending-reply-abcdef0123456789]: shipping is blocked on infra\n' >> "$state/task9.status"
  printf 'resolved [key=pending-reply-abcdef0123456789]: all good now\n' >> "$state/task9.status"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed on reserved-key lines"

  grep -F 'pending-reply-id=abcdef0123456789' "$out" >/dev/null \
    || fail "a foreign resolution cleared a reserved decision it does not own: $(cat "$out")"
  if grep -F 'shipping is blocked on infra' "$out" >/dev/null; then
    fail "a foreign line took over a reserved decision key: $(cat "$out")"
  fi

  # The owner's own resolution, which speaks that namespace's vocabulary, closes it.
  printf 'resolved [key=pending-reply-abcdef0123456789]: pending-reply-resolved: task=ios pending-reply-id=abcdef0123456789 via=status\n' >> "$state/task9.status"
  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed after the owner closed its decision"
  if grep -F 'OPEN DECISIONS' "$out" >/dev/null; then
    fail "the owner's own resolution did not close its reserved decision: $(cat "$out")"
  fi
  pass "a reserved decision key can only be opened or closed by its owning library"
}

test_later_unrelated_terminal_line_does_not_close_it() {
  local dir state out
  dir=$(make_case unrelated-terminal)
  state="$dir/state"
  out="$dir/drain.out"
  # A later done: with no matching [key=...] token opens/closes only the
  # "default" key; it must never clear the still-open api-shape decision.
  printf 'needs-decision [key=api-shape]: pick REST or RPC\n' > "$state/task3.status"
  printf 'done: unrelated later milestone\n' >> "$state/task3.status"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed after an unrelated terminal line"

  grep -F 'task3' "$out" | grep -F '[key=api-shape]' | grep -F 'pick REST or RPC' >/dev/null \
    || fail "a later unrelated terminal line incorrectly cleared the open decision"
  pass "a later unrelated terminal line never clears an open decision"
}

test_no_open_decisions_prints_nothing() {
  local dir state out
  dir=$(make_case none-open)
  state="$dir/state"
  out="$dir/drain.out"
  printf 'working: on it\n' > "$state/task4.status"
  printf 'resolved: shipped clean\n' > "$state/task5.status"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed with no open decisions"

  if grep -F 'OPEN DECISIONS' "$out" >/dev/null; then
    fail "the empty case printed an OPEN DECISIONS section: $(cat "$out")"
  fi
  [ ! -s "$out" ] || fail "the empty case with no queued wakes was not silent: $(cat "$out")"
  pass "no open decisions across the fleet prints nothing"
}

test_open_decision_surfaces_even_with_an_unrelated_queued_wake() {
  local dir state out
  dir=$(make_case fleet-wide)
  state="$dir/state"
  out="$dir/drain.out"
  # task6 has a buried, still-open decision but generates NO new queue record
  # this turn; task7 is what actually wakes the drain. The fleet-wide scan
  # must still catch task6's decision alongside task7's own raw row.
  printf 'needs-decision [key=migration]: pick the rollout plan\n' > "$state/task6.status"
  printf 'working: continuing\n' >> "$state/task6.status"
  printf 'blocked: waiting on credentials\n' > "$state/task7.status"
  append_wake "$state" signal task7.status "blocked: waiting on credentials" \
    || fail "queueing the unrelated wake failed"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed with a mixed fleet"

  grep "$(printf '\tsignal\ttask7.status\t')" "$out" >/dev/null || fail "task7's own raw row is missing"
  grep -F 'task6' "$out" | grep -F '[key=migration]' >/dev/null \
    || fail "task6's buried decision was not surfaced even though only task7 queued a wake"
  pass "the open-decision section is fleet-wide, not scoped to this drain's own queued records"
}

test_buried_decision_surfaces_on_the_empty_queue_fast_path() {
  local dir state out
  dir=$(make_case empty-queue-fast-path)
  state="$dir/state"
  out="$dir/drain.out"
  # No wake is queued at all (the empty-queue exit), but the decision is still
  # open on disk - session-start relies on exactly this path.
  printf 'needs-decision [key=api-shape]: pick REST or RPC\n' > "$state/task8.status"
  printf 'working: continuing\n' >> "$state/task8.status"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "empty-queue drain failed"

  grep -F 'task8' "$out" | grep -F '[key=api-shape]' >/dev/null \
    || fail "the empty-queue fast path did not surface a still-open decision"
  pass "a buried open decision surfaces even when the wake queue itself is empty"
}

test_status_symlink_is_not_followed() {
  local dir state out
  dir=$(make_case status-symlink)
  state="$dir/state"
  out="$dir/drain.out"
  mkdir -p "$dir/outside"
  printf 'needs-decision [key=local]: keep this visible\n' > "$state/local.status"
  printf 'needs-decision [key=foreign]: do not expose this\n' > "$dir/outside/foreign.status"
  ln -s ../outside/foreign.status "$state/linked.status"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed with a symlinked status file"

  grep -F 'local [key=local] needs-decision: keep this visible' "$out" >/dev/null \
    || fail "the valid local decision did not surface alongside a rejected status symlink"
  if grep -F 'do not expose this' "$out" >/dev/null; then
    fail "the fleet scan followed a status symlink outside the state directory"
  fi
  pass "the fleet-wide decision scan does not follow status symlinks"
}

# The per-item cut (and its exemption for needs-decision/blocked verbs) comes
# from bin/fm-line-cap-lib.sh's fm_cap_status_line_var, shared with
# bin/fm-session-start.sh's status tails so one truncation marker means the
# same thing wherever an agent meets it. These two tests pin the drain's own
# end of that contract (issue #19): a decision line past the routine bound
# still prints whole, and one too large for the section's own byte budget
# falls back to a pointer instead of being silently dropped.
test_over_long_decision_note_prints_in_full() {
  local dir state out line expected note
  dir=$(make_case long-note)
  state="$dir/state"
  out="$dir/drain.out"
  # 220 repeats of " and-then-some" (14 chars each) is 3,080 characters plus
  # the lede, clearing the issue's stated 3,000+ character acceptance bar, not
  # just the old 220-character cap.
  note=$(awk 'BEGIN { printf "pick REST or RPC padding-to-clear-3000-chars"; while (i++ < 220) printf " and-then-some" }')
  [ "${#note}" -ge 3000 ] || fail "test fixture note is only ${#note} chars, below the 3,000+ acceptance bar"
  printf 'needs-decision [key=api-shape]: %s\n' "$note" > "$state/task-long.status"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed on an over-long decision note"

  # A needs-decision line carries the context, options, and recommendation the
  # captain must relay onward verbatim, so it is exempt from the routine
  # per-line cut - past the old 220-character bound but nowhere near the
  # section's own byte budget, it prints whole, byte-for-byte, with no
  # truncation marker (issue #19).
  expected="task-long [key=api-shape] needs-decision: $note"
  line=$(grep -F 'task-long' "$out")
  [ "$line" = "$expected" ] \
    || fail "an over-long decision note did not print byte-for-byte: got [$line]"

  printf 'needs-decision [key=short]: brief enough to keep whole\n' > "$state/task-short.status"
  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed on a short decision note"
  grep -F 'task-short [key=short] needs-decision: brief enough to keep whole' "$out" >/dev/null \
    || fail "a decision note already under the cap was altered"
  if grep -F 'brief enough to keep whole [truncated]' "$out" >/dev/null; then
    fail "a decision note already under the cap was marked truncated"
  fi

  pass "an over-long open decision prints in full, byte-for-byte, instead of being cut to the routine per-item budget"
}

test_decision_note_too_large_for_the_section_budget_points_at_its_source() {
  local dir state out line expected_prefix lineno byteoff content
  dir=$(make_case huge-note)
  state="$dir/state"
  out="$dir/drain.out"
  # 5000 'x' characters clears the section's whole 4000-byte budget on its
  # own, so it cannot be printed in full without starving every other
  # section item; the acceptance bar is "readable in full, or via one
  # printed pointer" (issue #19), so this is the pointer path - and that
  # pointer must be exact (a file plus line number and byte offset), not just
  # "read this file", which issue #19's own review flagged as ambiguous once a
  # key can open more than once in the same log.
  awk 'BEGIN { printf "needs-decision [key=huge]: "; while (i++ < 5000) printf "x"; printf "\n" }' \
    > "$state/task-huge.status"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed on a section-busting decision note"

  line=$(grep -F 'task-huge' "$out")
  [ -n "$line" ] || fail "a section-busting decision note produced no OPEN DECISIONS line at all: $(cat "$out")"
  case "$line" in
    *xxxxxxxxxx*) fail "a section-busting decision note printed inline instead of falling back to a pointer: $line" ;;
  esac
  expected_prefix="task-huge [key=huge] needs-decision: too long to print in full here (5000 bytes) - read it in full at $state/task-huge.status:"
  case "$line" in
    "$expected_prefix"*) : ;;
    *) fail "a section-busting decision note's pointer did not match the expected exact-locator shape: $line" ;;
  esac
  lineno=${line#"$expected_prefix"}
  lineno=${lineno%% *}
  [ "$lineno" = 1 ] || fail "the pointer named the wrong line number: $line"
  byteoff=$(printf '%s' "$line" | grep -oE 'byte offset [0-9]+' | grep -oE '[0-9]+')
  [ "$byteoff" = 0 ] || fail "the pointer named the wrong byte offset for the file's only line: $line"
  content=$(dd if="$state/task-huge.status" bs=1 skip="$byteoff" count=27 2>/dev/null)
  [ "$content" = 'needs-decision [key=huge]: ' ] \
    || fail "the pointer's byte offset did not land on the decision line: got [$content]"

  pass "a decision note too large for the section budget points at its exact source line instead of being dropped"
}

test_reopened_key_pointer_locates_the_current_decision() {
  local dir state out line lineno note2
  dir=$(make_case reopened-key)
  state="$dir/state"
  out="$dir/drain.out"
  # Two DISTINCT 5,000-character needs-decision events share one key with no
  # resolution between them (a legal reopen - see
  # _fm_decision_key_transition_allowed). A bare "read this file" pointer
  # cannot tell the captain which of the two is current; the exact-locator
  # pointer must name the SECOND (currently open) line, not the first
  # (issue #19 review finding P1).
  note2=$(awk 'BEGIN { while (i++ < 5000) printf "m" }')
  {
    awk 'BEGIN { printf "needs-decision [key=ambiguous]: "; while (i++ < 5000) printf "n"; printf "\n" }'
    printf 'working: continuing\n'
    printf 'needs-decision [key=ambiguous]: %s\n' "$note2"
  } > "$state/multi.status"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed on a reopened decision key"

  line=$(grep -F 'multi' "$out")
  [ -n "$line" ] || fail "a reopened decision key produced no OPEN DECISIONS line: $(cat "$out")"
  case "$line" in
    *"(5000 bytes)"*) : ;;
    *) fail "reopened-key pointer did not report the current (second) note's length: $line" ;;
  esac
  case "$line" in
    *"$state/multi.status:3 "*) : ;;
    *) fail "reopened-key pointer did not name line 3 (the current, second needs-decision): $line" ;;
  esac
  lineno=$(sed -n '3p' "$state/multi.status")
  [ "$lineno" = "needs-decision [key=ambiguous]: $note2" ] \
    || fail "test fixture's line 3 is not the second needs-decision event"

  pass "a decision key reopened without an intervening resolution points at its current line, not an earlier one"
}

test_many_oversized_decisions_stay_within_the_section_budget() {
  local dir state out i section_bytes count
  dir=$(make_case many-huge)
  state="$dir/state"
  out="$dir/drain.out"
  # 32 distinct 5,000-character decisions: individually each falls back to a
  # pointer (above the section's own 4,000-byte budget), but printing every
  # pointer unconditionally would still let the section grow without bound -
  # exactly the regression issue #19's review flagged (P2). The section must
  # stay near its advertised budget regardless of how many oversized decisions
  # are open, and every decision omitted past that must still be named.
  i=1
  while [ "$i" -le 32 ]; do
    awk -v n="$i" 'BEGIN { printf "needs-decision [key=k]: "; while (j++ < 5000) printf "x"; printf "\n" }' \
      > "$state/task-$i.status"
    i=$((i + 1))
  done

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed on many oversized decisions"

  section_bytes=$(awk '/^OPEN DECISIONS \(/{f=1} f{print} /answering it:/{exit}' "$out" | wc -c | tr -d ' ')
  [ "$section_bytes" -lt 6000 ] \
    || fail "OPEN DECISIONS grew unbounded with many oversized decisions: $section_bytes bytes"
  grep -F 'OPEN DECISIONS:' "$out" | grep -F 'more omitted (byte cap)' >/dev/null \
    || fail "an over-budget section of oversized decisions did not report bounded omission: $(cat "$out")"
  count=$(grep -c '^task-[0-9]* \[key=k\] needs-decision:' "$out")
  [ "$count" -gt 0 ] && [ "$count" -lt 32 ] \
    || fail "unexpected number of individually-printed decisions: $count"
  # Every task must appear either as a printed pointer or in the omitted list -
  # never neither.
  i=1
  while [ "$i" -le 32 ]; do
    grep -qF "task-$i " "$out" || grep -qF "task-$i]" "$out" || grep -qF "task-$i," "$out" \
      || fail "task-$i is missing from both the printed decisions and the omitted list"
    i=$((i + 1))
  done

  pass "many oversized open decisions stay within the section's byte budget, with every one still findable"
}

test_buried_decision_still_surfaces
test_over_long_decision_note_prints_in_full
test_decision_note_too_large_for_the_section_budget_points_at_its_source
test_reopened_key_pointer_locates_the_current_decision
test_many_oversized_decisions_stay_within_the_section_budget
test_explicit_resolution_closes_it
test_later_unrelated_terminal_line_does_not_close_it
test_reserved_key_namespace_is_owned_by_its_library
test_no_open_decisions_prints_nothing
test_open_decision_surfaces_even_with_an_unrelated_queued_wake
test_buried_decision_surfaces_on_the_empty_queue_fast_path
test_status_symlink_is_not_followed
