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

# Issue #19 shape review, Verdict B (data/rev25-astra/report.md): the routine
# per-line cut (bin/fm-line-cap-lib.sh's fm_cap_line_var, unchanged, no
# decision-verb exemption) still applies to every OPEN DECISIONS row, and the
# section stays a short bounded preview. What issue #19 actually asks for -
# every selected decision's complete payload readable from the drain output
# alone, or via one precise pointer - is met by a separate, unbounded,
# content-addressed attachment file this drain publishes from the SAME
# selection pass (no second scan of status history, so a rejected or
# since-superseded line in the raw log can never be mistaken for the current
# one). A truncated preview row names its own line inside that attachment; the
# section's footer names the shared attachment path once.
test_over_long_decision_note_is_capped_with_an_attachment_pointer() {
  local dir state out line note attach_path attach_line
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

  line=$(grep -F 'task-long' "$out")
  case "$line" in
    *' [truncated] (full: L'*')') : ;;
    *) fail "an over-long decision note was not capped with an attachment-line reference: $line" ;;
  esac
  [ "${#line}" -le 260 ] || fail "a capped decision item ran unexpectedly long: ${#line} chars: $line"

  attach_path=$(grep -F 'full payloads:' "$out" | awk '{print $NF}')
  [ -n "$attach_path" ] && [ -f "$attach_path" ] \
    || fail "no readable attachment path was printed for the over-long decision: $(cat "$out")"
  attach_line=${line##*'(full: L'}
  attach_line=${attach_line%')'}
  [ "$(sed -n "${attach_line}p" "$attach_path")" = "[open-decision] task task-long [key=api-shape] needs-decision: $note" ] \
    || fail "the attachment's referenced line did not hold the complete, byte-for-byte decision"

  printf 'needs-decision [key=short]: brief enough to keep whole\n' > "$state/task-short.status"
  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed on a short decision note"
  grep -F 'task-short [key=short] needs-decision: brief enough to keep whole' "$out" >/dev/null \
    || fail "a decision note already under the cap was altered"
  if grep -F 'brief enough to keep whole [truncated]' "$out" >/dev/null; then
    fail "a decision note already under the cap was marked truncated"
  fi
  if grep -F 'task-short' "$out" | grep -F '(full: L' >/dev/null; then
    fail "a decision note already under the cap carried an unnecessary attachment reference"
  fi

  pass "an over-long open decision is capped to the routine per-item budget, with its complete payload in the attachment"
}

test_reopened_key_attachment_holds_the_current_note() {
  local dir state out line attach_path note2
  dir=$(make_case reopened-key)
  state="$dir/state"
  out="$dir/drain.out"
  # Two DISTINCT 5,000-character needs-decision events share one key with no
  # resolution between them (a legal reopen - see
  # _fm_decision_key_transition_allowed). The attachment is built from the
  # fold's own already-selected note, never a second scan of the raw log, so
  # it can never be misled into recording the superseded first line
  # (issue #19 review finding P1 against the source-locator design this
  # replaces).
  note2=$(awk 'BEGIN { while (i++ < 5000) printf "m" }')
  {
    awk 'BEGIN { printf "needs-decision [key=ambiguous]: "; while (i++ < 5000) printf "n"; printf "\n" }'
    printf 'working: continuing\n'
    printf 'needs-decision [key=ambiguous]: %s\n' "$note2"
  } > "$state/multi.status"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed on a reopened decision key"

  line=$(grep -F 'multi' "$out")
  [ -n "$line" ] || fail "a reopened decision key produced no OPEN DECISIONS line: $(cat "$out")"
  attach_path=$(grep -F 'full payloads:' "$out" | awk '{print $NF}')
  [ -n "$attach_path" ] && [ -f "$attach_path" ] \
    || fail "no readable attachment path was printed for the reopened key: $(cat "$out")"
  grep -qF "[open-decision] task multi [key=ambiguous] needs-decision: $note2" "$attach_path" \
    || fail "the attachment did not hold the current (second) note in full"
  if grep -qF 'nnnnnnnnnn' "$attach_path"; then
    fail "the attachment held the superseded first note instead of only the current one"
  fi

  pass "a decision key reopened without an intervening resolution records only the current note"
}

test_many_oversized_decisions_stay_within_the_section_budget() {
  local dir state out i section_bytes count attach_path attach_lines
  dir=$(make_case many-huge)
  state="$dir/state"
  out="$dir/drain.out"
  # 32 distinct 5,000-character decisions: individually each is capped in the
  # preview, but the section itself must also stay bounded regardless of how
  # many decisions are open - the regression issue #19's shape review flagged
  # in the prior (now-replaced) design. Every decision not shown in the
  # preview must still be recoverable in full from the one attachment file, a
  # scalar count, and no per-omitted-item identifier list.
  i=1
  while [ "$i" -le 32 ]; do
    awk -v n="$i" 'BEGIN { printf "needs-decision [key=k]: "; while (j++ < 5000) printf "x"; printf "\n" }' \
      > "$state/task-$i.status"
    i=$((i + 1))
  done

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out" || fail "drain failed on many oversized decisions"

  section_bytes=$(awk '/^OPEN DECISIONS \(/{f=1} f{print} /answering it:/{exit}' "$out" | wc -c | tr -d ' ')
  [ "$section_bytes" -le 4000 ] \
    || fail "OPEN DECISIONS grew past its 4,000-byte budget with many oversized decisions: $section_bytes bytes"
  grep -F 'OPEN DECISIONS:' "$out" | grep -F 'more not previewed (byte cap)' >/dev/null \
    || fail "an over-budget section of oversized decisions did not report bounded omission: $(cat "$out")"
  count=$(grep -c '^task-[0-9]* \[key=k\] needs-decision:' "$out")
  [ "$count" -gt 0 ] && [ "$count" -lt 32 ] \
    || fail "unexpected number of individually-printed decisions: $count"

  attach_path=$(grep -F 'full payloads:' "$out" | awk '{print $NF}')
  [ -n "$attach_path" ] && [ -f "$attach_path" ] \
    || fail "no readable attachment path was printed: $(cat "$out")"
  attach_lines=$(wc -l < "$attach_path" | tr -d ' ')
  [ "$attach_lines" -eq 32 ] \
    || fail "the attachment did not hold all 32 decisions in full: $attach_lines lines"
  i=1
  while [ "$i" -le 32 ]; do
    grep -qF "[open-decision] task task-$i [key=k] needs-decision:" "$attach_path" \
      || fail "task-$i's complete decision is missing from the attachment"
    i=$((i + 1))
  done

  pass "many oversized open decisions stay within the section's byte budget, with every one recoverable from the attachment"
}

test_multibyte_decision_note_stays_within_the_byte_budget() {
  local dir state out section_bytes attach_path
  dir=$(make_case multibyte)
  state="$dir/state"
  out="$dir/drain.out"
  # 2,000 emoji (4 bytes each in UTF-8) is 2,000 characters but 8,000+ bytes -
  # under the old character-counted budget this fit "under 4000" while
  # actually running 8KB+ (issue #19 shape review P2). The section's own
  # accounting must be in real bytes even though the per-item cut itself stays
  # locale-aware (never splitting a codepoint).
  python3 -c "print('needs-decision [key=emoji]: ' + chr(0x1F600) * 2000)" > "$state/task-emoji.status" \
    || fail "could not generate the multibyte fixture"

  FM_STATE_OVERRIDE="$state" LC_ALL=en_US.UTF-8 "$DRAIN" > "$out" || fail "drain failed on a multibyte decision note"

  section_bytes=$(awk '/^OPEN DECISIONS \(/{f=1} f{print} /answering it:/{exit}' "$out" | wc -c | tr -d ' ')
  [ "$section_bytes" -le 4000 ] \
    || fail "a multibyte decision blew the section's byte budget: $section_bytes bytes"
  python3 -c "open('$out','rb').read().decode('utf-8')" \
    || fail "the section output was not valid UTF-8 - the per-item cut split a multibyte character"

  attach_path=$(grep -F 'full payloads:' "$out" | awk '{print $NF}')
  [ -n "$attach_path" ] && [ -f "$attach_path" ] \
    || fail "no readable attachment path was printed for the multibyte decision: $(cat "$out")"
  python3 -c "
d = open('$attach_path', 'rb').read()
d.decode('utf-8')
assert d.count('\U0001F600'.encode()) == 2000, 'expected 2000 emoji in the attachment, counted ' + str(d.count('\U0001F600'.encode()))
" || fail "the attachment did not hold all 2,000 emoji, intact and valid UTF-8"

  pass "a multibyte decision note stays within the section's real byte budget with its full payload intact in the attachment"
}

test_unchanged_decision_set_reuses_the_same_attachment() {
  local dir state out1 out2 path1 path2
  dir=$(make_case attachment-reuse)
  state="$dir/state"
  out1="$dir/drain1.out"
  out2="$dir/drain2.out"
  awk 'BEGIN { printf "needs-decision [key=k]: "; while (i++ < 5000) printf "x"; printf "\n" }' \
    > "$state/task-stable.status"

  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out1" || fail "first drain failed"
  FM_STATE_OVERRIDE="$state" "$DRAIN" > "$out2" || fail "second drain failed"

  path1=$(grep -F 'full payloads:' "$out1" | awk '{print $NF}')
  path2=$(grep -F 'full payloads:' "$out2" | awk '{print $NF}')
  [ -n "$path1" ] && [ "$path1" = "$path2" ] \
    || fail "an unchanged decision set published a new attachment instead of reusing the existing one: $path1 vs $path2"
  [ "$(find "$state/drain-decisions" -type f -name '*.txt' | wc -l | tr -d ' ')" = 1 ] \
    || fail "an unchanged decision set left more than one attachment file behind"

  pass "an unchanged decision set reuses its already-published attachment instead of writing a new one"
}

test_buried_decision_still_surfaces
test_over_long_decision_note_is_capped_with_an_attachment_pointer
test_reopened_key_attachment_holds_the_current_note
test_many_oversized_decisions_stay_within_the_section_budget
test_multibyte_decision_note_stays_within_the_byte_budget
test_unchanged_decision_set_reuses_the_same_attachment
test_explicit_resolution_closes_it
test_later_unrelated_terminal_line_does_not_close_it
test_reserved_key_namespace_is_owned_by_its_library
test_no_open_decisions_prints_nothing
test_open_decision_surfaces_even_with_an_unrelated_queued_wake
test_buried_decision_surfaces_on_the_empty_queue_fast_path
test_status_symlink_is_not_followed
