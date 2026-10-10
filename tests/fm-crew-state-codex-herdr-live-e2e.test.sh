#!/usr/bin/env bash
# Prompt-submitting live guard for Codex current-state classification on Herdr.
#
# This opt-in test launches the installed Codex in an isolated named Herdr lab
# session, checks on-read binding for an existing worker, normal turns, a long
# shell command, an actual approval prompt, and ordinary prose that mentions input.
# It spends model tokens and never targets the default Herdr session.
set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fm_live_gate opt-in FM_CREW_STATE_CODEX_HERDR_LIVE_E2E codex herdr jq node

fail() { printf 'not ok - %s\n' "$1" >&2; exit 1; }
pass() { printf 'ok - %s\n' "$1"; }
note() { printf '# %s\n' "$1"; }

HERDR_LAB_HELPER="$ROOT/bin/fm-herdr-lab.sh"
HERDR_LAB_SESSION=$("$HERDR_LAB_HELPER" name fm-52-codex-state-live)
SCRATCH="$ROOT/.fm-codex-herdr-live-$$"
SHIM="$SCRATCH/fakebin"
STATE="$SCRATCH/state"
mkdir -p "$SHIM" "$STATE" "$SCRATCH/workspace"
cleanup() {
  local status=$?
  "$HERDR_LAB_HELPER" teardown "$HERDR_LAB_SESSION" || status=1
  rm -rf -- "$SCRATCH"
  exit "$status"
}
trap cleanup EXIT
"$HERDR_LAB_HELPER" provision "$HERDR_LAB_SESSION" || fail "could not provision isolated Herdr lab"

HERDR_VERSION=$("$HERDR_LAB_HELPER" run "$HERDR_LAB_SESSION" status --json | jq -r '.client.version // "unknown"')
CODEX_VERSION=$(codex --version 2>/dev/null | head -1)
[ -n "$CODEX_VERSION" ] || CODEX_VERSION=unknown
note "$CODEX_VERSION on Herdr $HERDR_VERSION"

WORKSPACE=$("$HERDR_LAB_HELPER" run "$HERDR_LAB_SESSION" workspace create \
  --label fm-codex-state --cwd "$SCRATCH/workspace") || fail "could not create lab workspace"
PANE=$(printf '%s' "$WORKSPACE" | jq -r '.result.root_pane.pane_id // empty')
[ -n "$PANE" ] || fail "workspace create returned no root pane"
TARGET="$HERDR_LAB_SESSION:$PANE"

# Remove the shim from PATH before the helper resolves the real Herdr
# executable, and refuse every call that lacks the exact trailing session.
cat > "$SHIM/herdr" <<SH
#!/usr/bin/env bash
set -u
helper='$HERDR_LAB_HELPER'
session='$HERDR_LAB_SESSION'
if [ "\$#" -lt 2 ] || [ "\${@: -2:1}" != --session ] || [ "\${@: -1}" != "\$session" ]; then
  echo "refusing Herdr call outside the task lab" >&2
  exit 97
fi
set -- "\${@:1:\$#-2}"
clean_path=\${PATH#'$SHIM:'}
exec env PATH="\$clean_path" "\$helper" run "\$session" "\$@"
SH
chmod +x "$SHIM/herdr"

cat > "$STATE/codex.meta" <<EOF
window=$TARGET
worktree=$SCRATCH/workspace
kind=ship
harness=codex
backend=herdr
herdr_session=$HERDR_LAB_SESSION
herdr_pane_id=$PANE
EOF

CODEX_HOME_ROOT=${CODEX_HOME:-$HOME/.codex}
CODEX_HOME_ROOT=$(cd "$CODEX_HOME_ROOT" && pwd -P) || fail "Codex home is not readable"
"$HERDR_LAB_HELPER" run "$HERDR_LAB_SESSION" pane run "$PANE" codex --no-alt-screen \
  --sandbox read-only --ask-for-approval on-request \
  || fail "could not launch Codex in the lab pane"

pane_capture() {
  "$HERDR_LAB_HELPER" run "$HERDR_LAB_SESSION" pane read "$PANE" \
    --source recent --lines 80
}
wait_for_composer() {
  local i=0 cap verdict dismissed=0
  while [ "$i" -lt 90 ]; do
    cap=$(pane_capture 2>/dev/null || true)
    if [ "$dismissed" -eq 0 ] && printf '%s' "$cap" | grep -qi 'Update available'; then
      # The Codex release dialog defaults to running an update on Enter.
      # Escape selects no update and spends no input on the dialog.
      "$HERDR_LAB_HELPER" run "$HERDR_LAB_SESSION" pane send-keys "$PANE" Escape
      dismissed=1
      sleep 2
    else
      verdict=$(PATH="$SHIM:$PATH" CODEX_HOME="$CODEX_HOME_ROOT" \
        FM_ROOT_OVERRIDE="$ROOT" FM_HOME="$SCRATCH" FM_STATE_OVERRIDE="$STATE" \
        bash -c '. "$1/bin/fm-tmux-lib.sh"; . "$1/bin/fm-backend.sh"; . "$1/bin/fm-composer-lib.sh"; fm_backend_source herdr; fm_backend_herdr_parse_target "$2"; fm_backend_herdr_composer_state "$2"' _ "$ROOT" "$TARGET" 2>/dev/null || true)
      [ "$verdict" = empty ] && return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  fail "Codex did not reach its idle composer after startup (verdict: ${verdict:-none}; pane: $(printf '%s' "$cap" | tail -18 | tr '\n' ' '))"
}

state_line() {
  PATH="$SHIM:$PATH" CODEX_HOME="$CODEX_HOME_ROOT" FM_ROOT_OVERRIDE="$ROOT" \
    FM_HOME="$SCRATCH" FM_STATE_OVERRIDE="$STATE" "$ROOT/bin/fm-crew-state.sh" codex
}
wait_state() {  # <state> <seconds>
  local wanted=$1 budget=$2 got i=0
  while [ "$i" -lt "$budget" ]; do
    got=$(state_line)
    case "$got" in
      "state: $wanted"*) return 0 ;;
    esac
    sleep 1
    i=$((i + 1))
  done
  local process_info pane_text
  process_info=$("$HERDR_LAB_HELPER" run "$HERDR_LAB_SESSION" pane process-info --pane "$PANE" 2>&1 \
    | jq -c '.result.process_info.foreground_processes // .' 2>/dev/null || true)
  pane_text=$("$HERDR_LAB_HELPER" run "$HERDR_LAB_SESSION" pane read "$PANE" \
    --source recent --lines 40 2>&1 | tail -18 | tr '\n' ' ')
  fail "Codex did not reach state $wanted (last: ${got:-none}; foreground: ${process_info:-unknown}; pane: ${pane_text:-unreadable})"
}
send_turn() {  # <prompt>
  local pane_text
  "$HERDR_LAB_HELPER" run "$HERDR_LAB_SESSION" pane send-text "$PANE" "$1"
  PATH="$SHIM:$PATH" CODEX_HOME="$CODEX_HOME_ROOT" FM_ROOT_OVERRIDE="$ROOT" \
    FM_HOME="$SCRATCH" FM_STATE_OVERRIDE="$STATE" bash -c '
      . "$1/bin/fm-tmux-lib.sh"
      . "$1/bin/fm-backend.sh"
      . "$1/bin/fm-composer-lib.sh"
      fm_backend_source herdr
      fm_backend_submit_exact_pending herdr "$2" "$3" 3 1
    ' _ "$ROOT" "$TARGET" "$1" \
    || {
      pane_text=$(pane_capture 2>/dev/null | tail -16 | tr '\n' ' ')
      fail "Codex did not submit the exact prompt through its composer (pane: ${pane_text:-unreadable})"
    }
}

wait_for_composer
if [ -e "$STATE/codex.codex-session" ]; then
  fail "the live guard must start without a prewritten Codex rollout binding"
fi
send_turn 'Use the shell to run sleep 8, then reply with exactly READY and make no changes.'
wait_state working 60
if [ ! -s "$STATE/codex.codex-session" ]; then
  fail "reading a pre-existing live Codex worker did not write its rollout binding"
fi
pass "real $CODEX_VERSION worker spawned without a binding is discovered on read"
WORKING=$(state_line)
case "$WORKING" in
  *"state: working"*"codex-rollout"*) pass "real $CODEX_VERSION reports a started Codex turn as working" ;;
  *) fail "real $CODEX_VERSION turn did not classify as working: $WORKING" ;;
esac
wait_state idle 90
IDLE=$(state_line)
case "$IDLE" in
  *"state: idle"*"codex-rollout"*) pass "real $CODEX_VERSION settled prompt reads idle" ;;
  *) fail "real $CODEX_VERSION idle prompt did not classify as idle: $IDLE" ;;
esac

send_turn 'Use the shell to run sleep 8, then say MIDTURN.'
wait_state working 30
MIDTURN=$(state_line)
case "$MIDTURN" in
  *"state: working"*) pass "real $CODEX_VERSION remains working mid-turn" ;;
  *) fail "real $CODEX_VERSION mid-turn did not classify as working: $MIDTURN" ;;
esac
wait_state idle 90

send_turn 'Use the shell to run sleep 30, then say finished.'
wait_state working 30
LONG_COMMAND=$(state_line)
case "$LONG_COMMAND" in
  *"state: working"*"codex-rollout"*) pass "real $CODEX_VERSION stays working during a long command" ;;
  *) fail "real $CODEX_VERSION long command did not classify as working: $LONG_COMMAND" ;;
esac
wait_state idle 90

send_turn 'Ask me to choose between A and B, then end your final answer with the exact words: Waiting for your input.'
wait_state idle 90
PROSE=$(state_line)
case "$PROSE" in
  *"state: idle"*"codex-rollout"*) pass "ordinary assistant prose mentioning input remains idle" ;;
  *) fail "ordinary assistant prose was mistaken for a pending input prompt: $PROSE" ;;
esac

send_turn 'Use the shell tool to create a file named approval-probe in the current workspace. Wait without choosing an approval option.'
wait_state blocked 90
NEEDS_INPUT=$(state_line)
case "$NEEDS_INPUT" in
  *"state: blocked"*"codex-pane-input"*) pass "real $CODEX_VERSION approval widget marks a needs-input prompt as blocked" ;;
  *) fail "real $CODEX_VERSION needs-input prompt did not classify as blocked: $NEEDS_INPUT" ;;
esac
