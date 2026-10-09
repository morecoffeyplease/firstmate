#!/usr/bin/env python3
"""Create an observer-only copy of the exact Calm fixture."""

import argparse
import hashlib
import shutil
from pathlib import Path

DEFAULT = b'  assert_contains "$(cat "$default_snapshot")" "CALM_E2E_OUTPUT" "calm mode was not off by default"\n'
EXPANDED = (
    b'  wait_for_text "$expanded_snapshot" "escape to interrupt" '
    + b"\\"
    + b'\n    || fail "Ctrl+O did not retain Pi\'s ordinary startup and tool expansion behavior"\n'
)
CLEANUP = (
    b'  if command -v tmux >/dev/null 2>&1; then\n'
    b'    tmux -L "$TMUX_SOCKET" kill-server 2>/dev/null || true\n'
    b'  fi\n'
    b'  fm_test_cleanup\n'
)
OBSERVER = br"""  fm_observer_receipt() {
    local target=$1 label=$2 value=$3 temp="${1}.tmp.$$" rc
    if printf '%s\n' "$value" > "$temp"; then
      if [[ "$(cat "$temp" 2>/dev/null)" == "$value" ]] && mv -f -- "$temp" "$target" && [[ "$(cat "$target" 2>/dev/null)" == "$value" ]]; then
        return 0
      else
        rc=$?
      fi
    else
      rc=$?
    fi
    FM_OBSERVER_RECEIPT_STATUS=failed
    printf 'receipt_write_failure=%s exit=%s target=%s\n' "$label" "$rc" "$target" >> "$FM_EVIDENCE_DIR/observer-receipt-write-failures.log" 2>/dev/null || true
    return 1
  }
  fm_observer_receipt "$FM_EVIDENCE_DIR/default-checkpoint.started" default-checkpoint-started started || fail "observer could not retain its start receipt"
  fm_observer_receipt "$FM_EVIDENCE_DIR/tmux-socket" identity "$TMUX_SOCKET" || fail "observer could not retain tmux socket identity"
  fm_observer_receipt "$FM_EVIDENCE_DIR/tmux-session" identity "$TMUX_SESSION" || fail "observer could not retain tmux session identity"
  fm_observer_receipt "$FM_EVIDENCE_DIR/fixture-tmp-root" identity "$TMP_ROOT" || fail "observer could not retain fixture root identity"
  fm_observer_receipt "$FM_EVIDENCE_DIR/process-identity.txt" process-identity "fixture_shell_pid=$$ parent_pid=$PPID" || fail "observer could not retain process identity"
  if cp "$default_snapshot" "$FM_EVIDENCE_DIR/default-active.txt" 2> "$FM_EVIDENCE_DIR/default-active-copy.stderr"; then
    fm_observer_receipt "$FM_EVIDENCE_DIR/default-active-copy.exit" default-active-copy-exit 0 || fail "observer could not retain default copy exit"
  else
    rc=$?
    fm_observer_receipt "$FM_EVIDENCE_DIR/default-active-copy.exit" default-active-copy-exit "$rc" || true
    fail "observer could not retain the default pane snapshot"
  fi
  tmux -L "$TMUX_SOCKET" display-message -p -t "$TMUX_SESSION" 'session=#{session_name} pane=#{pane_id} width=#{pane_width} height=#{pane_height} alternate_on=#{alternate_on} history_size=#{history_size} tty=#{pane_tty} pane_pid=#{pane_pid} command=#{pane_current_command}' > "$FM_EVIDENCE_DIR/pane-metadata.txt" 2> "$FM_EVIDENCE_DIR/pane-metadata.stderr" || { rc=$?; fm_observer_receipt "$FM_EVIDENCE_DIR/pane-metadata.exit" pane-metadata-exit "$rc" || true; fail "pane metadata unavailable"; }
  tmux -L "$TMUX_SOCKET" show-options -w -gv -t "$TMUX_SESSION" alternate-screen > "$FM_EVIDENCE_DIR/alternate-screen-option.txt" 2> "$FM_EVIDENCE_DIR/alternate-screen-option.stderr" || { rc=$?; fm_observer_receipt "$FM_EVIDENCE_DIR/alternate-screen-option.exit" alternate-screen-exit "$rc" || true; fail "alternate-screen option unavailable"; }
  tmux -L "$TMUX_SOCKET" show-options -w -gv -t "$TMUX_SESSION" history-limit > "$FM_EVIDENCE_DIR/history-limit-option.txt" 2> "$FM_EVIDENCE_DIR/history-limit-option.stderr" || { rc=$?; fm_observer_receipt "$FM_EVIDENCE_DIR/history-limit-option.exit" history-limit-exit "$rc" || true; fail "history-limit option unavailable"; }
  tmux -L "$TMUX_SOCKET" show-options -s -gv default-terminal > "$FM_EVIDENCE_DIR/default-terminal-option.txt" 2> "$FM_EVIDENCE_DIR/default-terminal-option.stderr" || { rc=$?; fm_observer_receipt "$FM_EVIDENCE_DIR/default-terminal-option.exit" default-terminal-exit "$rc" || true; fail "default-terminal option unavailable"; }
  tmux -L "$TMUX_SOCKET" show-environment -t "$TMUX_SESSION" TERM > "$FM_EVIDENCE_DIR/session-term.txt" 2> "$FM_EVIDENCE_DIR/session-term.stderr" || { rc=$?; fm_observer_receipt "$FM_EVIDENCE_DIR/session-term.exit" session-term-exit "$rc" || true; fail "session TERM unavailable"; }
  tmux -L "$TMUX_SOCKET" capture-pane -p -t "$TMUX_SESSION" -S -600 > "$FM_EVIDENCE_DIR/default-active-recapture.txt" 2> "$FM_EVIDENCE_DIR/default-active-recapture.stderr" || { rc=$?; fm_observer_receipt "$FM_EVIDENCE_DIR/default-active-recapture.exit" default-recapture-exit "$rc" || true; fail "active pane recapture unavailable"; }
  if tmux -L "$TMUX_SOCKET" capture-pane -a -p -t "$TMUX_SESSION" -S -600 > "$FM_EVIDENCE_DIR/default-alternate.txt" 2> "$FM_EVIDENCE_DIR/default-alternate.stderr"; then rc=0; else rc=$?; fi
  fm_observer_receipt "$FM_EVIDENCE_DIR/default-alternate.exit" default-alternate-exit "$rc" || fail "observer could not retain alternate capture exit"
  if ! grep -Fq -- "CALM_E2E_OUTPUT" "$default_snapshot"; then
    fm_viewport_polls=0
    fm_viewport_budget_ticks=0
    fm_viewport_index=0
    fm_viewport_page_count=0
    fm_viewport_top=unknown
    fm_viewport_bottom=unknown
    fm_viewport_marker=absent
    fm_viewport_outcome=UNKNOWN
    fm_viewport_candidate="$FM_EVIDENCE_DIR/viewport-poll.txt"
    fm_viewport_tail="$FM_EVIDENCE_DIR/viewport-current-tail.txt"
    fm_viewport_stable="$FM_EVIDENCE_DIR/viewport-stable.txt"
    fm_observer_receipt "$FM_EVIDENCE_DIR/viewport-diagnostic.started" viewport-diagnostic-started started || true
    fm_viewport_compare() {
      local rc
      if cmp -s -- "$1" "$2"; then
        return 0
      else
        rc=$?
      fi
      case "$rc" in
        1) return 1 ;;
        *) return 2 ;;
      esac
    }
    fm_viewport_usable() {
      local rc
      [[ -s "$1" ]] || return 1
      if grep -Eq '[^[:space:]]' "$1"; then
        return 0
      else
        rc=$?
      fi
      case "$rc" in
        1) return 1 ;;
        *) return 2 ;;
      esac
    }
    fm_viewport_capture() {
      local destination=$1 label=$2 poll_limit=$3 rc
      if (( fm_viewport_budget_ticks >= poll_limit )); then
        fm_observer_receipt "$destination.capture.exit" "$label-capture-exit" shared-budget-exhausted || true
        return 1
      fi
      ((fm_viewport_budget_ticks += 1))
      ((fm_viewport_polls += 1))
      if timeout --signal=TERM --kill-after=1s 2s tmux -L "$TMUX_SOCKET" capture-pane -p -t "$TMUX_SESSION" > "$fm_viewport_candidate" 2> "$destination.stderr"; then
        rc=0
      else
        rc=$?
        fm_observer_receipt "$destination.capture.exit" "$label-capture-exit" "$rc" || true
        return 1
      fi
      fm_observer_receipt "$destination.capture.exit" "$label-capture-exit" 0 || return 1
      sleep 0.05
      return 0
    }
    fm_viewport_send_key() {
      local key=$1 receipt=$2 limit=$3 rc key_cost=5
      if (( fm_viewport_budget_ticks > limit - key_cost )); then
        fm_observer_receipt "$receipt" viewport-key-exit shared-budget-exhausted || true
        return 1
      fi
      ((fm_viewport_budget_ticks += key_cost))
      if timeout --signal=TERM --kill-after=0.05s 0.25s tmux -L "$TMUX_SOCKET" send-keys -t "$TMUX_SESSION" "$key" 2> "${receipt%.exit}.stderr"; then
        rc=0
      else
        rc=$?
      fi
      fm_observer_receipt "$receipt" viewport-key-exit "$rc" || true
      [[ "$rc" == 0 ]]
    }
    fm_viewport_wait_navigation() {
      local previous=$1 destination=$2 poll_limit=$3 polls=0 stable=0 stable_valid=0 usable_rc compare_rc
      while (( fm_viewport_budget_ticks < poll_limit )); do
        ((polls += 1))
        if ! fm_viewport_capture "$destination" viewport-frame "$poll_limit"; then return 1; fi
        if fm_viewport_usable "$fm_viewport_candidate"; then
          :
        else
          usable_rc=$?
          stable=0
          stable_valid=0
          if [[ "$usable_rc" != 1 ]]; then
            fm_observer_receipt "$destination.capture.usable" viewport-capture-usable comparison-error || true
            return 1
          fi
          continue
        fi
        if fm_viewport_compare "$fm_viewport_candidate" "$previous"; then
          stable=0
          stable_valid=0
          continue
        else
          compare_rc=$?
        fi
        if [[ "$compare_rc" != 1 ]]; then
          fm_observer_receipt "$destination.capture.compare" viewport-capture-compare comparison-error || true
          return 1
        fi
        if (( stable_valid == 1 )); then
          if fm_viewport_compare "$fm_viewport_candidate" "$fm_viewport_stable"; then
            ((stable += 1))
          else
            compare_rc=$?
            if [[ "$compare_rc" != 1 ]]; then
              fm_observer_receipt "$destination.stable.compare" viewport-stable-compare comparison-error || true
              return 1
            fi
            if ! cp -- "$fm_viewport_candidate" "$fm_viewport_stable"; then return 1; fi
            stable=1
          fi
        else
          if ! cp -- "$fm_viewport_candidate" "$fm_viewport_stable"; then return 1; fi
          stable=1
          stable_valid=1
        fi
        if (( stable >= 2 )); then
          if ! cp -- "$fm_viewport_candidate" "$destination"; then return 1; fi
          fm_observer_receipt "$destination.capture.exit" viewport-frame-capture-exit 0 || return 1
          fm_observer_receipt "$destination.stable-polls" viewport-frame-stable-polls "$polls" || return 1
          return 0
        fi
      done
      fm_observer_receipt "$destination.capture.exit" viewport-frame-capture-exit shared-budget-exhausted || true
      return 1
    }
    fm_viewport_wait_restore() {
      local expected=$1 destination=$2 poll_limit=$3 polls=0 stable=0 usable_rc compare_rc
      while (( fm_viewport_budget_ticks < poll_limit )); do
        ((polls += 1))
        if ! fm_viewport_capture "$destination" viewport-restore "$poll_limit"; then return 1; fi
        if fm_viewport_usable "$fm_viewport_candidate"; then
          :
        else
          usable_rc=$?
          stable=0
          if [[ "$usable_rc" != 1 ]]; then
            fm_observer_receipt "$destination.capture.usable" viewport-restore-usable comparison-error || true
            return 1
          fi
          continue
        fi
        if fm_viewport_compare "$fm_viewport_candidate" "$expected"; then
          ((stable += 1))
        else
          compare_rc=$?
          stable=0
          if [[ "$compare_rc" != 1 ]]; then
            fm_observer_receipt "$destination.capture.compare" viewport-restore-compare comparison-error || true
            return 1
          fi
          continue
        fi
        if (( stable >= 2 )); then
          if ! cp -- "$fm_viewport_candidate" "$destination"; then return 1; fi
          fm_observer_receipt "$destination.capture.exit" viewport-restore-capture-exit 0 || return 1
          fm_observer_receipt "$destination.tail-matching-polls" viewport-tail-matching-polls "$polls" || return 1
          return 0
        fi
      done
      fm_observer_receipt "$destination.capture.exit" viewport-restore-capture-exit shared-budget-exhausted || true
      return 1
    }
    if fm_viewport_capture "$fm_viewport_tail" viewport-tail-baseline 112 && fm_viewport_usable "$fm_viewport_candidate" && grep -Fq -- "The deterministic tool example is complete." "$fm_viewport_candidate" && cp -- "$fm_viewport_candidate" "$fm_viewport_tail"; then
      fm_observer_receipt "$FM_EVIDENCE_DIR/viewport-tail-baseline.status" viewport-tail-baseline-status captured-current-tail || true
    else
      fm_viewport_tail=
      fm_observer_receipt "$FM_EVIDENCE_DIR/viewport-tail-baseline.status" viewport-tail-baseline-status UNKNOWN || true
    fi
    if [[ -n "$fm_viewport_tail" ]] && fm_viewport_send_key C-Home "$FM_EVIDENCE_DIR/viewport-ctrl-home.exit" 112; then
      if fm_viewport_wait_navigation "$fm_viewport_tail" "$FM_EVIDENCE_DIR/viewport-frame-00-top.txt" 112; then
        fm_viewport_index=1
        if grep -Fq -- "Show a deterministic tool example." "$FM_EVIDENCE_DIR/viewport-frame-00-top.txt"; then
          fm_viewport_top=visible
        fi
        if grep -Fq -- "CALM_E2E_OUTPUT" "$FM_EVIDENCE_DIR/viewport-frame-00-top.txt"; then
          fm_viewport_marker=viewport-frame-00-top.txt
        fi
        while (( fm_viewport_index <= 16 && fm_viewport_budget_ticks < 112 )) && [[ "$fm_viewport_top" == visible && "$fm_viewport_bottom" != visible ]]; do
          local_frame=$(printf '%02d' "$fm_viewport_index")
          previous_frame=$(printf '%02d' "$((fm_viewport_index - 1))")
          previous_path="$FM_EVIDENCE_DIR/viewport-frame-${previous_frame}-$( [[ "$previous_frame" == 00 ]] && printf top || printf page ).txt"
          if grep -Fq -- "The deterministic tool example is complete." "$previous_path"; then
            fm_viewport_bottom=visible
            break
          fi
          if ! fm_viewport_send_key NPage "$FM_EVIDENCE_DIR/viewport-npage-${local_frame}.exit" 112; then break; fi
          current_path="$FM_EVIDENCE_DIR/viewport-frame-${local_frame}-page.txt"
          if ! fm_viewport_wait_navigation "$previous_path" "$current_path" 112; then break; fi
          fm_viewport_page_count=$((fm_viewport_page_count + 1))
          fm_viewport_index=$((fm_viewport_index + 1))
          if grep -Fq -- "CALM_E2E_OUTPUT" "$current_path" && [[ "$fm_viewport_marker" == absent ]]; then
            fm_viewport_marker="$(basename "$current_path")"
          fi
          if grep -Fq -- "The deterministic tool example is complete." "$current_path"; then
            fm_viewport_bottom=visible
          fi
        done
      fi
    fi
    if [[ "$fm_viewport_top" == visible && "$fm_viewport_bottom" == visible ]]; then
      if [[ "$fm_viewport_marker" == absent ]]; then
        fm_viewport_outcome=complete-traversal-marker-absent
      else
        fm_viewport_outcome=complete-traversal-marker-visible
      fi
    fi
    fm_observer_receipt "$FM_EVIDENCE_DIR/viewport-traversal.status" viewport-traversal-status "outcome=$fm_viewport_outcome top=$fm_viewport_top bottom=$fm_viewport_bottom marker=$fm_viewport_marker pages=$fm_viewport_page_count polls=$fm_viewport_polls budget_ticks=$fm_viewport_budget_ticks" || true
    fm_viewport_restore_key=failed
    if fm_viewport_send_key C-End "$FM_EVIDENCE_DIR/viewport-ctrl-end.exit" 120; then
      fm_viewport_restore_key=sent
    fi
    if [[ -n "$fm_viewport_tail" ]] && fm_viewport_wait_restore "$fm_viewport_tail" "$FM_EVIDENCE_DIR/viewport-restored-tail.txt" 120 && [[ "$fm_viewport_restore_key" == sent ]]; then
      fm_observer_receipt "$FM_EVIDENCE_DIR/viewport-restored-tail.status" viewport-restored-tail-status byte-identical-to-initial-current-viewport || true
    else
      fm_observer_receipt "$FM_EVIDENCE_DIR/viewport-restored-tail.status" viewport-restored-tail-status UNKNOWN || true
      fm_viewport_outcome=UNKNOWN
    fi
    fm_observer_receipt "$FM_EVIDENCE_DIR/viewport-diagnostic.status" viewport-diagnostic-status "outcome=$fm_viewport_outcome top=$fm_viewport_top bottom=$fm_viewport_bottom marker=$fm_viewport_marker pages=$fm_viewport_page_count polls=$fm_viewport_polls budget_ticks=$fm_viewport_budget_ticks" || true
    rm -f -- "$fm_viewport_candidate" "$fm_viewport_stable"
  fi
  fm_observer_receipt "$FM_EVIDENCE_DIR/default-checkpoint.reached" default-checkpoint-reached reached || fail "observer could not retain checkpoint completion"
"""
OBSERVER_CLEANUP = br"""  fm_calm_capture_before_cleanup() {
    local capture_status=0 custody_status=0 rc destination active_exit=unavailable alternate_exit=unavailable
    local session_result=missing fixture_tmp_result=not-copied final_status status_temp
    if [[ "${FM_OBSERVER_RECEIPT_STATUS:-not-started}" == failed ]]; then custody_status=1; fi
    fm_calm_write_receipt() {
      local target=$1 label=$2 value=$3 temp="${1}.tmp.$$" write_rc
      if printf '%s\n' "$value" > "$temp"; then
        if [[ "$(cat "$temp" 2>/dev/null)" == "$value" ]] && mv -f -- "$temp" "$target" && [[ "$(cat "$target" 2>/dev/null)" == "$value" ]]; then
          return 0
        else
          write_rc=$?
        fi
      else
        write_rc=$?
      fi
      custody_status=1
      printf 'receipt_write_failure=%s exit=%s target=%s\n' "$label" "$write_rc" "$target" >> "$FM_EVIDENCE_DIR/fixture-cleanup-receipt-write-failures.log" 2>/dev/null || true
      return 1
    }
    fm_calm_write_receipt "$FM_EVIDENCE_DIR/tmux-socket" tmux-socket "$TMUX_SOCKET" || true
    fm_calm_write_receipt "$FM_EVIDENCE_DIR/tmux-session" tmux-session "$TMUX_SESSION" || true
    fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-tmp-root" fixture-root "$TMP_ROOT" || true
    fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-cleanup-capture.started" cleanup-capture-started capture_started || true
    if command -v tmux >/dev/null 2>&1; then
      if timeout --signal=TERM --kill-after=2s 8s tmux -L "$TMUX_SOCKET" capture-pane -p -t "$TMUX_SESSION" -S -600 > "$FM_EVIDENCE_DIR/fixture-exit-active.txt" 2> "$FM_EVIDENCE_DIR/fixture-exit-active.stderr"; then rc=0; else rc=$?; capture_status=1; fi
      active_exit=$rc
      fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-exit-active.exit" active-capture-exit "$rc" || true
      if timeout --signal=TERM --kill-after=2s 8s tmux -L "$TMUX_SOCKET" capture-pane -a -p -t "$TMUX_SESSION" -S -600 > "$FM_EVIDENCE_DIR/fixture-exit-alternate.txt" 2> "$FM_EVIDENCE_DIR/fixture-exit-alternate.stderr"; then rc=0; else rc=$?; capture_status=1; fi
      alternate_exit=$rc
      fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-exit-alternate.exit" alternate-capture-exit "$rc" || true
      [[ -f "$FM_EVIDENCE_DIR/fixture-exit-active.stderr" && -f "$FM_EVIDENCE_DIR/fixture-exit-alternate.stderr" ]] || custody_status=1
    else
      fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-exit-capture.unavailable" tmux-unavailable tmux-unavailable || true
      capture_status=1
    fi
    if [[ ! -f "$FM_EVIDENCE_DIR/default-checkpoint.reached" ]]; then
      if [[ -f "$FM_EVIDENCE_DIR/default-checkpoint.started" ]]; then
        fm_calm_write_receipt "$FM_EVIDENCE_DIR/default-checkpoint.outcome" checkpoint-outcome observer-started-but-original-checkpoint-not-completed || true
      else
        if [[ -e "$FM_EVIDENCE_DIR/observer-receipt-write-failures.log" ]]; then
          fm_calm_write_receipt "$FM_EVIDENCE_DIR/default-checkpoint.outcome" checkpoint-outcome start-receipt-unavailable || true
          custody_status=1
        else
          fm_calm_write_receipt "$FM_EVIDENCE_DIR/default-checkpoint.outcome" checkpoint-outcome not-reached-before-original-cleanup || true
        fi
      fi
    fi
    case "$TMP_ROOT" in
      "$TMPDIR"/fm-calm-pi-extension.*)
        if [[ -d "$TMP_ROOT" && ! -L "$TMP_ROOT" && -f "$TMP_ROOT/.fm-test-fixture" && ! -L "$TMP_ROOT/.fm-test-fixture" ]]; then
          destination="$FM_EVIDENCE_DIR/fixture-tmp-evidence"
          if mkdir "$destination" 2> "$FM_EVIDENCE_DIR/fixture-tmp-evidence.mkdir.stderr"; then
            if timeout --signal=TERM --kill-after=5s 25s cp -a -- "$TMP_ROOT/." "$destination/" 2> "$FM_EVIDENCE_DIR/fixture-tmp-evidence.copy.stderr"; then
              fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-tmp-evidence.copy" tmp-root-copy-exit copy_exit=0 || true
              fixture_tmp_result=recursively-copied
              if [[ -f "$TMP_ROOT/calm-session.jsonl" && -f "$destination/calm-session.jsonl" ]]; then
                if timeout --signal=TERM --kill-after=2s 8s cmp -s -- "$TMP_ROOT/calm-session.jsonl" "$destination/calm-session.jsonl" 2> "$FM_EVIDENCE_DIR/fixture-tmp-evidence.session-check.stderr"; then
                  fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-tmp-evidence.session-check" session-jsonl-check session_jsonl=byte-identical || true
                  session_result=byte-identical
                else
                  rc=$?
                  fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-tmp-evidence.session-check" session-jsonl-check "session_jsonl_cmp_exit=$rc" || true
                  session_result=mismatch-or-unreadable
                  custody_status=1
                fi
              elif [[ ! -f "$TMP_ROOT/calm-session.jsonl" ]]; then
                fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-tmp-evidence.session-check" session-jsonl-check session_jsonl=absent-at-original-cleanup || true
                session_result=absent
                custody_status=1
              else
                fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-tmp-evidence.session-check" session-jsonl-check session_jsonl=copy-missing || true
                session_result=copy-missing
                custody_status=1
              fi
            else
              rc=$?
              fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-tmp-evidence.copy" tmp-root-copy-exit "copy_exit=$rc" || true
              fixture_tmp_result=copy-failed
              custody_status=1
            fi
          else
            rc=$?
            fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-tmp-evidence.failure" tmp-evidence-directory-failed "fixture-evidence-directory_exit=$rc" || true
            fixture_tmp_result=directory-failed
            custody_status=1
          fi
        else
          fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-tmp-evidence.failure" tmp-root-invalid fixture-root-marker-or-type-invalid || true
          fixture_tmp_result=root-invalid
          custody_status=1
        fi
        ;;
      *)
        fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-tmp-evidence.failure" tmp-root-outside-task-tmpdir fixture-root-outside-task-tmpdir || true
        fixture_tmp_result=root-outside-task-tmpdir
        custody_status=1
        ;;
    esac
    if [[ "$active_exit" == unavailable || "$alternate_exit" == unavailable ]]; then
      [[ -f "$FM_EVIDENCE_DIR/fixture-exit-capture.unavailable" ]] || custody_status=1
    else
      [[ "$(cat "$FM_EVIDENCE_DIR/fixture-exit-active.exit" 2>/dev/null)" == "$active_exit" && "$active_exit" =~ ^[0-9]+$ ]] || custody_status=1
      [[ "$(cat "$FM_EVIDENCE_DIR/fixture-exit-alternate.exit" 2>/dev/null)" == "$alternate_exit" && "$alternate_exit" =~ ^[0-9]+$ ]] || custody_status=1
    fi
    [[ "$(cat "$FM_EVIDENCE_DIR/tmux-socket" 2>/dev/null)" == "$TMUX_SOCKET" ]] || custody_status=1
    [[ "$(cat "$FM_EVIDENCE_DIR/tmux-session" 2>/dev/null)" == "$TMUX_SESSION" ]] || custody_status=1
    [[ "$(cat "$FM_EVIDENCE_DIR/fixture-tmp-root" 2>/dev/null)" == "$TMP_ROOT" ]] || custody_status=1
    [[ "$(cat "$FM_EVIDENCE_DIR/fixture-cleanup-capture.started" 2>/dev/null)" == capture_started ]] || custody_status=1
    [[ ! -e "$FM_EVIDENCE_DIR/observer-receipt-write-failures.log" ]] || custody_status=1
    [[ "$(cat "$FM_EVIDENCE_DIR/fixture-tmp-evidence.copy" 2>/dev/null)" == copy_exit=0 ]] || custody_status=1
    [[ "$session_result" == byte-identical ]] || custody_status=1
    if [[ "$custody_status" -eq 0 ]]; then
      final_status=$'evidence_transfer=complete\nreceipt_writes=complete\nfixture_tmp=recursively-copied-before-original-cleanup\nsession_jsonl=byte-identical\npane_capture_errors='
      if [[ "$capture_status" -eq 0 ]]; then final_status+='none'; else final_status+='recorded'; fi
      final_status+=$'\nactive_capture_exit='"$active_exit"$'\nalternate_capture_exit='"$alternate_exit"
      if ! fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-cleanup-capture.status" cleanup-capture-status "$final_status"; then
        custody_status=1
      fi
    fi
    if [[ "$custody_status" -ne 0 ]]; then
      final_status=$'evidence_transfer=partial-or-failed\nreceipt_writes=failed-or-unknown\nfixture_tmp='
      final_status+="$fixture_tmp_result"$'\nsession_jsonl='"$session_result"$'\npane_capture_errors='
      if [[ "$capture_status" -eq 0 ]]; then final_status+='none'; else final_status+='recorded'; fi
      final_status+=$'\nactive_capture_exit='"$active_exit"$'\nalternate_capture_exit='"$alternate_exit"
      fm_calm_write_receipt "$FM_EVIDENCE_DIR/fixture-cleanup-capture.status" cleanup-capture-status-partial "$final_status" || true
    fi
    return "$custody_status"
  }
  fm_calm_capture_before_cleanup || true
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--expected-source-sha256", required=True)
    args = parser.parse_args()
    source = args.source.read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    if source_hash != args.expected_source_sha256:
        parser.error("source hash mismatch")
    if args.output.exists() or not args.evidence.is_dir():
        parser.error("observer destination exists or evidence directory is missing")
    for name, anchor in (("DEFAULT", DEFAULT), ("EXPANDED", EXPANDED), ("CLEANUP", CLEANUP)):
        if source.count(anchor) != 1:
            parser.error(f"fixture {name} anchor changed")
    output = source.replace(DEFAULT, OBSERVER + DEFAULT, 1)
    output = output.replace(
        EXPANDED,
        EXPANDED
        + b'  if cp "$expanded_snapshot" "$FM_EVIDENCE_DIR/expanded-after-ctrl-o.txt" 2> "$FM_EVIDENCE_DIR/expanded-snapshot-copy.stderr"; then\n'
        + b'    fm_observer_receipt "$FM_EVIDENCE_DIR/expanded-snapshot-copy.exit" expanded-copy-exit 0 || fail "observer could not retain expanded copy exit"\n'
        + b'  else\n'
        + b'    rc=$?\n'
        + b'    fm_observer_receipt "$FM_EVIDENCE_DIR/expanded-snapshot-copy.exit" expanded-copy-exit "$rc" || true\n'
        + b'    fail "observer could not retain the expanded pane snapshot"\n'
        + b'  fi\n',
        1,
    )
    output = output.replace(CLEANUP, OBSERVER_CLEANUP + CLEANUP, 1)
    args.output.write_bytes(output)
    shutil.copyfile(args.output, args.evidence / "observer-copy.sh")
    (args.evidence / "observer-source.sha256").write_text(source_hash + "\n")
    (args.evidence / "observer-copy.sha256").write_text(hashlib.sha256(output).hexdigest() + "\n")


if __name__ == "__main__":
    main()
