#!/usr/bin/env bash
# Real-Herdr regression for the projected-cleanup focus flash (upstream
# ogulcancelik/herdr#1621 family, live on 0.7.5 stable).
# Part A reproduces the OLD path: an explicit last-pane close that empties a
# non-focused workspace steals the focused workspace.
# Part B proves the mitigation: the focus-safe emptying-close plan
# (repositioning move plus pane-death removal) removes the doomed workspace
# with no focus change and no corrective tab focus at all.
# Part C covers the branch Part B structurally cannot reach - a doomed pane
# whose shell holds a persistent child, so the lone-idle-shell proof fails and
# the plan falls back to the plain explicit close - in the geometry where the
# closing workspace's right neighbour is not the anchor. It then checks the
# version floor that decides whether an unconfigured home is projected at all,
# against what Part A measured about this very release.
# On a future release whose explicit close preserves focus, Part A records
# that and Part C still requires readable samples and exact focus restoration,
# while the positive control adapts to whether this release steals focus.
# Every CLI operation is routed through one guarded named non-default lab, and
# lab teardown verifies that the default fleet session is byte-identical.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERDR_LAB_HELPER=${HERDR_LAB_HELPER:-$ROOT/bin/fm-herdr-lab.sh}

fail() { printf 'not ok - %s\n' "$1" >&2; exit 1; }
pass() { printf 'ok - %s\n' "$1"; }

command -v herdr >/dev/null 2>&1 || { echo 'skip: herdr not found'; exit 0; }
command -v jq >/dev/null 2>&1 || { echo 'skip: jq not found'; exit 0; }
command -v python3 >/dev/null 2>&1 || { echo 'skip: python3 not found'; exit 0; }
[ -x "$HERDR_LAB_HELPER" ] || { echo "skip: Herdr lab helper not executable at $HERDR_LAB_HELPER"; exit 0; }

HERDR_ORIGINAL_PATH=$PATH
TMP_ROOT=$(mktemp -d "$(cd "${TMPDIR:-/tmp}" && pwd -P)/fm-herdr-focus-flash-e2e.XXXXXX")
FAKEBIN="$TMP_ROOT/fakebin"
mkdir -p "$FAKEBIN"

HERDR_LAB_SESSION=$("$HERDR_LAB_HELPER" name fm-herdr-focus-flash-regression-r1)
export HERDR_LAB_HELPER HERDR_LAB_SESSION HERDR_ORIGINAL_PATH
SAMPLER_PID=
SAMPLER_STOP=
cleanup() {
  local status=$?
  if [ -n "$SAMPLER_STOP" ]; then
    : > "$SAMPLER_STOP"
  fi
  if [ -n "$SAMPLER_PID" ]; then
    wait "$SAMPLER_PID" 2>/dev/null || true
  fi
  env PATH="$HERDR_ORIGINAL_PATH" "$HERDR_LAB_HELPER" teardown "$HERDR_LAB_SESSION" || status=1
  rm -rf "$TMP_ROOT"
  exit "$status"
}
trap cleanup EXIT
"$HERDR_LAB_HELPER" provision "$HERDR_LAB_SESSION"

# Keep the lab helper as the only CLI transport. Production adapter calls have
# already appended the exact session; this shim strips that pair, refuses every
# other caller-supplied session, and delegates the command to helper run.
cat > "$FAKEBIN/herdr" <<'SH'
#!/usr/bin/env bash
set -u
args=("$@")
last=$((${#args[@]} - 1))
flag=$((last - 1))
if [ "${#args[@]}" -ge 2 ] \
  && [ "${args[$flag]}" = --session ] \
  && [ "${args[$last]}" = "$HERDR_LAB_SESSION" ]; then
  unset "args[$last]" "args[$flag]"
fi
set -- "${args[@]}"
for arg in "$@"; do
  case "$arg" in --session|--session=*) exit 9 ;; esac
done
exec env PATH="$HERDR_ORIGINAL_PATH" "$HERDR_LAB_HELPER" run "$HERDR_LAB_SESSION" "$@"
SH
chmod +x "$FAKEBIN/herdr"

lab() { env PATH="$HERDR_ORIGINAL_PATH" "$HERDR_LAB_HELPER" run "$HERDR_LAB_SESSION" "$@"; }
mkws() {  # <label> -> "<workspace_id> <tab_id> <pane_id>"
  lab workspace create --cwd "$ROOT" --label "$1" --no-focus \
    | jq -er '"\(.result.workspace.workspace_id) \(.result.tab.tab_id) \(.result.root_pane.pane_id)"'
}
focus_snapshot() {
  local list
  list=$(lab workspace list) || return 1
  # Take the workspace and its active tab from one Herdr snapshot. Separate
  # workspace.list and tab.list calls can straddle the short focus transition
  # under test and turn a valid state into a false unreadable sample.
  printf '%s' "$list" | jq -er '
    [.result.workspaces[] | select(.focused == true)]
    | select(length == 1)
    | .[0]
    | select((.workspace_id | type) == "string" and (.workspace_id | length) > 0)
    | select((.active_tab_id | type) == "string" and (.active_tab_id | length) > 0)
    | [.workspace_id, .active_tab_id]
    | @tsv
  '
}
focus_samples_verdict() {  # <anchor workspace<TAB>tab> <samples file>
  local anchor=$1 samples=$2 line workspace tab count=0 wrong=0
  [ -s "$samples" ] || return 2
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      UNREADABLE) return 3 ;;
    esac
    case "$line" in
      *$'\t'*) ;;
      *) return 3 ;;
    esac
    workspace=${line%%$'\t'*}
    tab=${line#*$'\t'}
    [ -n "$workspace" ] && [ -n "$tab" ] || return 3
    case "$tab" in *$'\t'*) return 3 ;; esac
    count=$((count + 1))
    [ "$line" = "$anchor" ] || wrong=$((wrong + 1))
  done < "$samples"
  [ "$count" -gt 0 ] || return 2
  if [ "$wrong" -gt 0 ]; then
    printf 'wrong:%s/%s\n' "$wrong" "$count"
  else
    printf 'anchor:%s\n' "$count"
  fi
}
focus_trial_disposition() {  # <release> <invariants-passed:0|1> <verdict> <attempt> <budget>
  local release=$1 invariants=$2 verdict=$3 attempt=$4 budget=$5
  [ "$invariants" = 0 ] || { printf 'fail\n'; return 0; }
  case "$release:$verdict" in
    defective:anchor:*)
      if [ "$attempt" -lt "$budget" ]; then printf 'retry\n'; else printf 'fail\n'; fi
      ;;
    defective:wrong:*) printf 'pass\n' ;;
    preserving:anchor:*) printf 'pass\n' ;;
    preserving:wrong:*) printf 'fail\n' ;;
    *) printf 'fail\n' ;;
  esac
}
start_focus_sampler() {  # <samples> <active marker> <ready marker> <stop marker>
  local samples=$1 active=$2 ready=$3 stop=$4
  : > "$samples"
  rm -f "$ready" "$stop"
  (
    : > "$ready"
    while [ ! -e "$stop" ]; do
      if [ -e "$active" ]; then
        if SAMPLE=$(focus_snapshot); then
          printf '%s\n' "$SAMPLE" >> "$samples"
        else
          printf '%s\n' UNREADABLE >> "$samples"
        fi
      fi
    done
  ) &
  SAMPLER_PID=$!
  SAMPLER_STOP=$stop
  local attempt=0
  while [ ! -e "$ready" ] && [ "$attempt" -lt 100 ]; do
    sleep 0.01
    attempt=$((attempt + 1))
  done
  [ -e "$ready" ]
}
stop_focus_sampler() {  # <active marker> <stop marker>
  rm -f "$1"
  : > "$2"
  wait "$SAMPLER_PID" 2>/dev/null || true
  SAMPLER_PID=
  SAMPLER_STOP=
}
wait_for_focus_sample() {  # <samples file>
  local attempt=0
  while [ ! -s "$1" ] && [ "$attempt" -lt 100 ]; do
    sleep 0.01
    attempt=$((attempt + 1))
  done
  [ -s "$1" ]
}
focus_sampler_self_test() {
  local cases=$1 anchor=$2 output first_disposition later_disposition
  printf '%s\n' "$anchor" "$anchor" > "$cases/anchor"
  output=$(focus_samples_verdict "$anchor" "$cases/anchor") || return 1
  [ "$output" = anchor:2 ] || return 1
  printf '%s\n' "$anchor" "$(printf 'other-workspace\t%s' "${anchor#*$'\t'}")" > "$cases/wrong"
  output=$(focus_samples_verdict "$anchor" "$cases/wrong") || return 1
  [ "$output" = wrong:1/2 ] || return 1
  printf '%s\n' UNREADABLE > "$cases/unreadable"
  if focus_samples_verdict "$anchor" "$cases/unreadable" >/dev/null; then return 1; fi
  printf '%s\n' "$anchor" UNREADABLE > "$cases/mixed"
  if focus_samples_verdict "$anchor" "$cases/mixed" >/dev/null; then return 1; fi
  : > "$cases/empty"
  if focus_samples_verdict "$anchor" "$cases/empty" >/dev/null; then return 1; fi
  printf '%s\n' 'not-a-pair' > "$cases/malformed"
  if focus_samples_verdict "$anchor" "$cases/malformed" >/dev/null; then return 1; fi
  [ "$(focus_trial_disposition defective 0 anchor:2 1 3)" = retry ] || return 1
  [ "$(focus_trial_disposition defective 0 anchor:2 2 3)" = retry ] || return 1
  [ "$(focus_trial_disposition defective 0 anchor:2 3 3)" = fail ] || return 1
  [ "$(focus_trial_disposition defective 0 wrong:1/2 2 3)" = pass ] || return 1
  [ "$(focus_trial_disposition preserving 0 anchor:2 1 1)" = pass ] || return 1
  [ "$(focus_trial_disposition preserving 0 wrong:1/2 1 1)" = fail ] || return 1
  # The failed invariant is terminal; a hypothetical later good trace cannot
  # convert the failed trial into a retry or pass.
  first_disposition=$(focus_trial_disposition defective 1 anchor:2 1 3)
  later_disposition=not-run
  if [ "$first_disposition" = retry ]; then
    later_disposition=$(focus_trial_disposition defective 0 wrong:1/2 2 3)
  fi
  [ "$first_disposition" = fail ] && [ "$later_disposition" = not-run ] || return 1
}
ws_order() { lab workspace list | jq -er '[.result.workspaces[].workspace_id] | join(",")'; }
wait_ws_gone() {  # <workspace_id>
  local i=0
  while [ "$i" -lt 80 ]; do
    lab workspace get "$1" >/dev/null 2>&1 || return 0
    sleep 0.1
    i=$((i + 1))
  done
  return 1
}

# Pin verdict parsing and retry eligibility before relying on the live sampler.
FOCUS_SELFTEST_ANCHOR=$(printf 'anchor-workspace\tanchor-tab')
focus_sampler_self_test "$TMP_ROOT" "$FOCUS_SELFTEST_ANCHOR" \
  || fail 'deterministic focus sampler/verdict cases failed'
pass 'focus verdict: valid samples, unreadable traces, empty traces, and retry eligibility are classified fail-closed'

# Calibrate the exact asynchronous sampler while focus is held on two known
# states. This catches empty, unreadable, and constant-anchor samplers even if
# the short Part C excursion happens to be missed.
read -r CAL_ANCHOR_WS CAL_ANCHOR_TAB _ <<<"$(mkws flash-calibration-anchor)" \
  || fail 'could not create the focus sampler calibration anchor'
read -r CAL_OTHER_WS CAL_OTHER_TAB _ <<<"$(mkws flash-calibration-other)" \
  || fail 'could not create the focus sampler calibration non-anchor'
calibrate_focus_sampler() {  # <label> <workspace> <tab>
  local label=$1 workspace=$2 tab=$3 before samples active ready stop verdict
  lab tab focus "$tab" >/dev/null || fail "$label calibration could not focus its known tab"
  before=$(focus_snapshot) || fail "$label calibration could not read its held focus"
  [ "$before" = "$(printf '%s\t%s' "$workspace" "$tab")" ] \
    || fail "$label calibration did not hold its intended workspace and tab"
  samples="$TMP_ROOT/calibration-$label.samples"
  active="$TMP_ROOT/calibration-$label.active"
  ready="$TMP_ROOT/calibration-$label.ready"
  stop="$TMP_ROOT/calibration-$label.stop"
  start_focus_sampler "$samples" "$active" "$ready" "$stop" \
    || fail "$label calibration sampler did not start"
  : > "$active"
  wait_for_focus_sample "$samples" \
    || fail "$label calibration sampler produced no sample while focus was held"
  stop_focus_sampler "$active" "$stop"
  verdict=$(focus_samples_verdict "$before" "$samples") \
    || fail "$label calibration sampler produced an unreadable or malformed observation"
  case "$verdict" in
    anchor:*) : ;;
    *) fail "$label calibration expected the held focus, got $verdict" ;;
  esac
  pass "$label calibration: sampler captured the held focus $before ($verdict samples)"
}
calibrate_focus_sampler anchor "$CAL_ANCHOR_WS" "$CAL_ANCHOR_TAB"
calibrate_focus_sampler non-anchor "$CAL_OTHER_WS" "$CAL_OTHER_TAB"

# --- Part A: the OLD path (plain explicit close) steals focus on 0.7.5 -----
# The spacer keeps the focused anchor away from the doomed workspace's right
# neighbor, where the 0.7.5 explicit close would land by coincidence.
read -r A_DOOMED_WS _ A_DOOMED_PANE <<<"$(mkws flash-a-doomed)" || fail 'could not create the Part A doomed workspace'
read -r _ _ _ <<<"$(mkws flash-a-spacer)" || fail 'could not create the Part A spacer workspace'
read -r A_ANCHOR_WS A_ANCHOR_TAB _ <<<"$(mkws flash-a-anchor)" || fail 'could not create the Part A anchor workspace'
read -r _ _ _ <<<"$(mkws flash-a-tail)" || fail 'could not create the Part A tail workspace'
lab tab focus "$A_ANCHOR_TAB" >/dev/null || fail 'could not focus the Part A anchor'
A_BEFORE=$(focus_snapshot) || fail 'could not capture the Part A pre-close focus'
[ "$A_BEFORE" = "$(printf '%s\t%s' "$A_ANCHOR_WS" "$A_ANCHOR_TAB")" ] \
  || fail 'Part A anchor focus does not match the intended workspace and tab'
lab pane close "$A_DOOMED_PANE" >/dev/null || fail 'Part A explicit close failed'
wait_ws_gone "$A_DOOMED_WS" || fail 'Part A doomed workspace survived the explicit close'
A_AFTER=$(focus_snapshot) || fail 'could not capture the Part A post-close focus'
STEAL_LIVE=0
if [ "$A_AFTER" != "$A_BEFORE" ]; then
  STEAL_LIVE=1
  pass "old path: the explicit last-pane close of a non-focused workspace stole focus ($A_BEFORE -> $A_AFTER)"
  lab tab focus "$A_ANCHOR_TAB" >/dev/null || fail 'could not restore the Part A anchor focus'
else
  pass 'old path note: this Herdr release preserves focus across the explicit close; continuing with outcome-only assertions'
fi

# --- Part B: the mitigation in the dangerous geometry ----------------------
# The doomed workspace sits BEFORE the focused anchor and the anchor is not
# last, the exact shape where an unrepositioned pane death also steals focus.
read -r B_DOOMED_WS _ B_DOOMED_PANE <<<"$(mkws flash-b-doomed)" || fail 'could not create the Part B doomed workspace'
read -r B_ANCHOR_WS B_ANCHOR_TAB _ <<<"$(mkws flash-b-anchor)" || fail 'could not create the Part B anchor workspace'
read -r _ _ _ <<<"$(mkws flash-b-tail)" || fail 'could not create the Part B tail workspace'
lab tab focus "$B_ANCHOR_TAB" >/dev/null || fail 'could not focus the Part B anchor'
B_BEFORE=$(focus_snapshot) || fail 'could not capture the Part B pre-close focus'
[ "$B_BEFORE" = "$(printf '%s\t%s' "$B_ANCHOR_WS" "$B_ANCHOR_TAB")" ] \
  || fail 'Part B anchor focus does not match the intended workspace and tab'
B_SURVIVOR_ORDER=$(ws_order | tr ',' '\n' | grep -v "^$B_DOOMED_WS\$" | paste -sd, -) \
  || fail 'could not capture the Part B survivor order'

CALL_LOG="$TMP_ROOT/call.log"
B_FOCUS_SAMPLES="$TMP_ROOT/focus.samples"
B_OPERATION_ACTIVE="$TMP_ROOT/operation.active"
B_SAMPLER_READY="$TMP_ROOT/sampler.ready"
SAMPLER_STOP="$TMP_ROOT/sampler.stop"
: > "$CALL_LOG"
: > "$B_FOCUS_SAMPLES"
start_focus_sampler "$B_FOCUS_SAMPLES" "$B_OPERATION_ACTIVE" "$B_SAMPLER_READY" "$SAMPLER_STOP" \
  || fail 'the Part B focus sampler did not start'
: > "$B_OPERATION_ACTIVE"
B_OUT=$(PATH="$FAKEBIN:$HERDR_ORIGINAL_PATH" FM_FLASH_CALL_LOG="$CALL_LOG" bash -c '
  . "$1/bin/backends/herdr.sh"
  fm_backend_herdr_cli() {
    local session=$1
    shift
    printf "%s\n" "$*" >> "$FM_FLASH_CALL_LOG"
    HERDR_SESSION="$session" herdr "$@" --session "$session"
  }
  fm_backend_herdr_projection_close_pane_focus_preserving "$2" "$3"
' _ "$ROOT" "$HERDR_LAB_SESSION" "$B_DOOMED_PANE" 2>&1)
B_STATUS=$?
stop_focus_sampler "$B_OPERATION_ACTIVE" "$SAMPLER_STOP"
[ "$B_STATUS" -eq 0 ] || fail "the production focus-preserving close failed (status $B_STATUS): $B_OUT"
B_SAMPLE_VERDICT=$(focus_samples_verdict "$B_BEFORE" "$B_FOCUS_SAMPLES") \
  || fail 'the Part B sampler captured no readable focus sample during the production close'
case "$B_SAMPLE_VERDICT" in
  anchor:*) : ;;
  *) fail "the mitigation exposed a wrong in-operation focus sample ($B_BEFORE -> $B_SAMPLE_VERDICT)" ;;
esac
wait_ws_gone "$B_DOOMED_WS" || fail 'the mitigation left the doomed workspace behind'
if lab pane get "$B_DOOMED_PANE" >/dev/null 2>&1; then
  fail 'the mitigation left the doomed pane behind'
fi
B_AFTER=$(focus_snapshot) || fail 'could not capture the Part B post-close focus'
[ "$B_AFTER" = "$B_BEFORE" ] \
  || fail "the mitigation changed the exact focused workspace or tab ($B_BEFORE -> $B_AFTER)"
[ "$(ws_order)" = "$B_SURVIVOR_ORDER" ] \
  || fail "the mitigation left a lasting workspace order change ($B_SURVIVOR_ORDER -> $(ws_order))"
grep -q '^pane process-info' "$CALL_LOG" || fail 'the idle-shell proof never ran'
pass "mitigation: every in-operation sample preserved exact focus ($B_SAMPLE_VERDICT) while the doomed workspace was removed"

if [ "$STEAL_LIVE" = 1 ]; then
  grep -q '^tab focus' "$CALL_LOG" \
    && fail 'the corrective tab focus fired, so a wrong-focus interval existed on the defective release'
  grep -q '^pane close' "$CALL_LOG" \
    && fail 'the focus-unsafe explicit close was used on the defective release'
  pass 'mitigation: no explicit close and no corrective focus were needed on the defective release'
fi

# --- Part C: the plain-close FALLBACK, the case Part B cannot reach ---------
# Part B uses an idle pane and takes the focus-preserving pane-death route.
# Part C gives the doomed pane a persistent foreground child so the idle-shell
# proof fails and production uses its plain explicit-close fallback.
# Each trial creates fresh geometry and IDs; only an otherwise-complete,
# readable, anchor-only trial on a defective release can be retried.
run_focus_fallback_trial() {  # <attempt> -> global C_TRIAL_VERDICT
  local attempt=$1 label="flash-c-$1" order_lines anchor_index doomed_index
  local child_identity='' previous_child_identity='' child_attempt=0 child_stable=0
  local call_log="$TMP_ROOT/call-c-$attempt.log"
  local samples="$TMP_ROOT/focus-c-$attempt.samples"
  local active="$TMP_ROOT/operation-c-$attempt.active"
  local ready="$TMP_ROOT/sampler-c-$attempt.ready"
  local stop="$TMP_ROOT/sampler-c-$attempt.stop"
  local proof_polls=3 out status proof_calls before after right_neighbour survivor_order
  local anchor_ws anchor_tab doomed_ws doomed_pane spacer_ws

  read -r anchor_ws anchor_tab _ <<<"$(mkws "$label-anchor")" \
    || fail "Part C attempt $attempt could not create its anchor workspace"
  read -r doomed_ws _ doomed_pane <<<"$(mkws "$label-doomed")" \
    || fail "Part C attempt $attempt could not create its doomed workspace"
  read -r spacer_ws _ _ <<<"$(mkws "$label-spacer")" \
    || fail "Part C attempt $attempt could not create its spacer workspace"
  read -r _ _ _ <<<"$(mkws "$label-tail")" \
    || fail "Part C attempt $attempt could not create its tail workspace"
  lab tab focus "$anchor_tab" >/dev/null \
    || fail "Part C attempt $attempt could not focus the anchor"
  before=$(focus_snapshot) \
    || fail "Part C attempt $attempt could not capture the pre-close focus"
  [ "$before" = "$(printf '%s\t%s' "$anchor_ws" "$anchor_tab")" ] \
    || fail "Part C attempt $attempt anchor focus did not match the intended workspace and tab"

  order_lines=$(ws_order | tr ',' '\n') \
    || fail "Part C attempt $attempt could not read workspace order"
  anchor_index=$(printf '%s\n' "$order_lines" | grep -n -Fx "$anchor_ws" | cut -d: -f1)
  doomed_index=$(printf '%s\n' "$order_lines" | grep -n -Fx "$doomed_ws" | cut -d: -f1)
  [ -n "$anchor_index" ] && [ -n "$doomed_index" ] \
    || fail "Part C attempt $attempt geometry omitted its anchor or doomed workspace"
  [ "$doomed_index" -gt "$anchor_index" ] \
    || fail "Part C attempt $attempt geometry placed the doomed workspace before its anchor"
  right_neighbour=$(printf '%s\n' "$order_lines" | grep -A1 -Fx "$doomed_ws" | tail -1)
  [ "$right_neighbour" = "$spacer_ws" ] \
    || fail "Part C attempt $attempt needs its spacer immediately right of the doomed workspace, got '$right_neighbour'"
  [ "$right_neighbour" != "$anchor_ws" ] \
    || fail "Part C attempt $attempt geometry is vacuous because the anchor is the right neighbour"
  survivor_order=$(printf '%s\n' "$order_lines" | grep -v -Fx "$doomed_ws" | paste -sd, -) \
    || fail "Part C attempt $attempt could not capture survivor order"

  lab pane run "$doomed_pane" 'cd / && sleep 3000' >/dev/null \
    || fail "Part C attempt $attempt could not start the persistent-child command"
  while [ "$child_attempt" -lt 100 ]; do
    child_identity=$(lab pane process-info --pane "$doomed_pane" 2>/dev/null \
      | jq -er '
        .result.process_info as $process
        | $process.foreground_processes
        | map(select(.pid != $process.shell_pid))
        | select(length > 0)
        | [$process.shell_pid, .[0].pid]
        | @tsv
      ' 2>/dev/null) || child_identity=
    if [ -n "$child_identity" ] && [ "$child_identity" = "$previous_child_identity" ]; then
      child_stable=$((child_stable + 1))
      [ "$child_stable" -ge 2 ] && break
    else
      child_stable=0
    fi
    previous_child_identity=$child_identity
    sleep 0.1
    child_attempt=$((child_attempt + 1))
  done
  [ "$child_stable" -ge 2 ] \
    || fail "Part C attempt $attempt never observed a stable persistent child process"

  : > "$call_log"
  start_focus_sampler "$samples" "$active" "$ready" "$stop" \
    || fail "Part C attempt $attempt focus sampler did not start"
  : > "$active"
  out=$(PATH="$FAKEBIN:$HERDR_ORIGINAL_PATH" FM_FLASH_CALL_LOG="$call_log" \
    FM_BACKEND_HERDR_IDLE_SHELL_PROOF_POLLS="$proof_polls" bash -c '
    . "$1/bin/backends/herdr.sh"
    fm_backend_herdr_cli() {
      local session=$1
      shift
      printf "%s\n" "$*" >> "$FM_FLASH_CALL_LOG"
      HERDR_SESSION="$session" herdr "$@" --session "$session"
    }
    fm_backend_herdr_projection_close_pane_focus_preserving "$2" "$3"
  ' _ "$ROOT" "$HERDR_LAB_SESSION" "$doomed_pane" 2>&1)
  status=$?
  stop_focus_sampler "$active" "$stop"
  [ "$status" -eq 0 ] \
    || fail "Part C attempt $attempt production close failed (status $status): $out"
  wait_ws_gone "$doomed_ws" \
    || fail "Part C attempt $attempt fallback left the doomed workspace behind"
  if lab pane get "$doomed_pane" >/dev/null 2>&1; then
    fail "Part C attempt $attempt fallback left the doomed pane behind"
  fi
  [ "$(ws_order)" = "$survivor_order" ] \
    || fail "Part C attempt $attempt fallback changed survivor order ($survivor_order -> $(ws_order))"
  proof_calls=$(grep -c '^pane process-info' "$call_log" || true)
  [ "$proof_calls" -eq "$proof_polls" ] \
    || fail "Part C attempt $attempt did not exhaust the idle-shell proof ($proof_calls of $proof_polls samples)"
  grep -q '^pane close' "$call_log" \
    || fail "Part C attempt $attempt never reached the plain explicit close"
  after=$(focus_snapshot) \
    || fail "Part C attempt $attempt could not capture post-close focus"
  [ "$after" = "$before" ] \
    || fail "Part C attempt $attempt fallback left focus off the exact anchor ($before -> $after)"
  C_TRIAL_VERDICT=$(focus_samples_verdict "$before" "$samples") \
    || fail "Part C attempt $attempt had empty, unreadable, or malformed in-operation samples"
  pass "fallback attempt $attempt: persistent child exhausted $proof_polls proof polls, explicit close removed the doomed pane, and focus returned to anchor; samples=$C_TRIAL_VERDICT"
}

# Three complete attempts bound extra lifecycle work while allowing two fresh
# trials after an inconclusive but otherwise valid positive-control trace.
C_FOCUS_ATTEMPT_BUDGET=3
C_ATTEMPT_COUNTS=
if [ "$STEAL_LIVE" = 1 ]; then
  C_ATTEMPT=1
  while [ "$C_ATTEMPT" -le "$C_FOCUS_ATTEMPT_BUDGET" ]; do
    run_focus_fallback_trial "$C_ATTEMPT"
    C_ATTEMPT_COUNTS="${C_ATTEMPT_COUNTS}${C_ATTEMPT_COUNTS:+, }$C_ATTEMPT:$C_TRIAL_VERDICT"
    C_DISPOSITION=$(focus_trial_disposition defective 0 "$C_TRIAL_VERDICT" "$C_ATTEMPT" "$C_FOCUS_ATTEMPT_BUDGET")
    case "$C_DISPOSITION" in
      pass)
        pass "fallback positive control: valid wrong focus observed on attempt $C_ATTEMPT/$C_FOCUS_ATTEMPT_BUDGET; samples=[$C_ATTEMPT_COUNTS]"
        break
        ;;
      retry)
        if [ "$C_ATTEMPT" -eq "$C_FOCUS_ATTEMPT_BUDGET" ]; then
          fail "fallback positive control exhausted $C_FOCUS_ATTEMPT_BUDGET complete anchor-only trials; samples=[$C_ATTEMPT_COUNTS]"
        fi
        pass "fallback positive control attempt $C_ATTEMPT was readable and anchor-only; retrying with fresh geometry"
        ;;
      fail)
        fail "fallback positive control exhausted $C_FOCUS_ATTEMPT_BUDGET complete anchor-only trials; samples=[$C_ATTEMPT_COUNTS]"
        ;;
      *) fail "fallback positive control rejected its trial verdict $C_TRIAL_VERDICT" ;;
    esac
    C_ATTEMPT=$((C_ATTEMPT + 1))
  done
else
  C_ATTEMPT=1
  run_focus_fallback_trial "$C_ATTEMPT"
  [ "$(focus_trial_disposition preserving 0 "$C_TRIAL_VERDICT" "$C_ATTEMPT" "$C_ATTEMPT")" = pass ] \
    || fail "focus-preserving release exposed a wrong focus sample ($C_TRIAL_VERDICT)"
  pass "fallback on a focus-preserving release: exact focus held throughout; samples=$C_TRIAL_VERDICT"
fi

# The live guard on the version floor itself: Part A measured whether THIS
# release steals focus. Every above-floor release must preserve focus, while a
# below-floor release may conservatively include the known post-fix protocol-18
# preview without weakening the stated 0.8.0 policy floor.
STATUS=$(lab status --json) || fail 'could not read final named-lab version evidence'
LIVE_VERSION=$(printf '%s' "$STATUS" | jq -r '.client.version')
LIVE_PROTOCOL=$(printf '%s' "$STATUS" | jq -r '.client.protocol')
FLOOR_VERDICT=$(bash -c '
  . "$0/bin/backends/herdr.sh"
  status=0
  fm_backend_herdr_release_floor_verdict "$1" "$2" || status=$?
  printf "%s\n" "$status"
' "$ROOT" "$LIVE_PROTOCOL" "$LIVE_VERSION")
case "$FLOOR_VERDICT" in
  0)
    [ "$STEAL_LIVE" = 0 ] \
      || fail "herdr $LIVE_VERSION (protocol $LIVE_PROTOCOL) is at or above the floor but steals focus on the explicit close"
    pass "version floor: herdr $LIVE_VERSION protocol $LIVE_PROTOCOL is at or above the floor and preserves focus"
    ;;
  1)
    pass "version floor: herdr $LIVE_VERSION protocol $LIVE_PROTOCOL remains conservatively below the floor with steal_live=$STEAL_LIVE"
    ;;
  *) fail "herdr $LIVE_VERSION (protocol $LIVE_PROTOCOL) could not be classified against the presentation floor" ;;
esac

# The end-user gate: an unconfigured home must project only at or above the
# floor, and an explicit opt-in must survive either way.
FLOOR_CONFIG="$TMP_ROOT/floor-config"
FLOOR_STATE="$TMP_ROOT/floor-state"
mkdir -p "$FLOOR_CONFIG" "$FLOOR_STATE"
gate_verdict() {  # <config-dir> -> on|off, warnings on stderr
  PATH="$FAKEBIN:$HERDR_ORIGINAL_PATH" HERDR_SESSION="$HERDR_LAB_SESSION" bash -c '
    . "$0/bin/backends/herdr.sh"
    if fm_backend_herdr_presentation_enabled "$1" "$2"; then printf "on\n"; else printf "off\n"; fi
  ' "$ROOT" "$1" "$FLOOR_STATE"
}
GATE_ERR="$TMP_ROOT/gate.err"
GATE_DEFAULT=$(gate_verdict "$FLOOR_CONFIG" 2>"$GATE_ERR")
printf 'on\n' > "$FLOOR_CONFIG/herdr-presentation-spaces"
GATE_OPT_IN=$(gate_verdict "$FLOOR_CONFIG" 2>/dev/null)
[ "$GATE_OPT_IN" = on ] \
  || fail "an explicit opt-in must stay on for herdr $LIVE_VERSION, got '$GATE_OPT_IN'"
if [ "$FLOOR_VERDICT" = 1 ]; then
  [ "$GATE_DEFAULT" = off ] \
    || fail "an unconfigured home must not be projected on below-floor herdr $LIVE_VERSION, got '$GATE_DEFAULT'"
  grep -q "$LIVE_VERSION" "$GATE_ERR" \
    || fail "the below-floor fallback must name herdr $LIVE_VERSION: $(cat "$GATE_ERR")"
  pass "version floor: an unconfigured home falls back flat on herdr $LIVE_VERSION and the explicit opt-in still projects"
else
  [ "$GATE_DEFAULT" = on ] \
    || fail "an unconfigured home must stay projected on herdr $LIVE_VERSION, got '$GATE_DEFAULT'"
  [ ! -s "$GATE_ERR" ] \
    || fail "a supported release must warn about nothing: $(cat "$GATE_ERR")"
  pass "version floor: an unconfigured home stays projected on herdr $LIVE_VERSION and the explicit opt-in agrees"
fi

printf 'evidence: herdr=%s protocol=%s steal_live=%s floor_verdict=%s default-session-tripwire=armed\n' \
  "$LIVE_VERSION" "$LIVE_PROTOCOL" "$STEAL_LIVE" "$FLOOR_VERDICT"
