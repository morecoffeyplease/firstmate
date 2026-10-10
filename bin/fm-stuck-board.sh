#!/usr/bin/env bash
# Evaluate deterministic stuck-board signals for this home's ship and scout tasks.
# Usage: fm-stuck-board.sh scan
#   Prints one `stuck: <task-id> <rule>` line per newly breached rule episode.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FM_ROOT="${FM_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
FM_HOME="${FM_HOME:-${FM_ROOT_OVERRIDE:-$FM_ROOT}}"
STATE="${FM_STATE_OVERRIDE:-$FM_HOME/state}"
CONFIG="${FM_CONFIG_OVERRIDE:-$FM_HOME/config}"
# shellcheck source=bin/fm-pr-lib.sh
. "$SCRIPT_DIR/fm-pr-lib.sh"
# shellcheck source=bin/fm-backend.sh
. "$SCRIPT_DIR/fm-backend.sh"

fail() { printf 'stuck-board-error: %s\n' "$*"; printf 'fm-stuck-board: %s\n' "$*" >&2; exit 2; }
usage() { sed -n '2,/^set -u$/s/^# \{0,1\}//p' "$0"; }

if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then usage; exit 0; fi
[ "${1:-}" = scan ] && [ "$#" -eq 1 ] || fail 'usage: fm-stuck-board.sh scan'
[ -d "$STATE" ] && [ ! -L "$STATE" ] || exit 0

HEARTBEAT_SECS=${FM_STUCK_HEARTBEAT_SECS:-900}
PROGRESS_SECS=${FM_STUCK_PROGRESS_SECS:-3600}
DRAFT_PR_SECS=${FM_STUCK_DRAFT_PR_SECS:-14400}
REVIEW_SECS=${FM_STUCK_REVIEW_SECS:-7200}
FAILURE_REPEATS=${FM_STUCK_FAILURE_REPEATS:-2}
READY_PR_SECS=${FM_STUCK_READY_PR_SECS:-86400}

config_read() {
  local key value line
  if [ ! -e "$CONFIG/stuck-board" ] && [ ! -L "$CONFIG/stuck-board" ]; then return 0; fi
  [ -f "$CONFIG/stuck-board" ] && [ ! -L "$CONFIG/stuck-board" ] \
    && [ -r "$CONFIG/stuck-board" ] || fail 'config/stuck-board must be a readable regular file'
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; *=*) key=${line%%=*}; value=${line#*=} ;; *) fail "invalid config/stuck-board line" ;; esac
    case "$key" in
      heartbeat_seconds) HEARTBEAT_SECS=$value ;;
      progress_seconds) PROGRESS_SECS=$value ;;
      draft_pr_seconds) DRAFT_PR_SECS=$value ;;
      review_seconds) REVIEW_SECS=$value ;;
      failure_repeats) FAILURE_REPEATS=$value ;;
      ready_pr_seconds) READY_PR_SECS=$value ;;
      *) fail "unknown config/stuck-board key: $key" ;;
    esac
  done < "$CONFIG/stuck-board"
}

config_read
for value in "$HEARTBEAT_SECS" "$PROGRESS_SECS" "$DRAFT_PR_SECS" \
  "$REVIEW_SECS" "$FAILURE_REPEATS" "$READY_PR_SECS"; do
  case "$value" in ''|*[!0-9]*|0) fail 'stuck-board thresholds must be positive whole numbers' ;; esac
done

now=$(date +%s)
stat_mtime() {
  if stat -f %m "$1" >/dev/null 2>&1; then stat -f %m "$1" 2>/dev/null
  else stat -c %Y "$1" 2>/dev/null
  fi
}
task_start() {
  local id=$1 meta=$2 file value
  file="$STATE/$id.started"
  if [ -f "$file" ] && [ ! -L "$file" ]; then
    IFS= read -r value < "$file" || value=
    case "$value" in ''|*[!0-9]*) ;; *) printf '%s' "$value"; return ;; esac
  fi
  stat_mtime "$meta"
}
breach() {
  local id=$1 rule=$2 marker="$STATE/.stuck-$1-$2"
  [ -e "$marker" ] && return 0
  (umask 077; set -C; printf '%s\n' "$now" > "$marker") 2>/dev/null || return 0
  printf 'stuck: %s %s\n' "$id" "$rule"
}
clear_rule() { rm -f -- "$STATE/.stuck-$1-$2"; }
elapsed() { [ "$now" -ge "$1" ] && printf '%s' "$((now - $1))" || printf '0'; }

last_status_progress() {
  local file=$1 stamp
  [ -f "$file" ] || { printf '0'; return; }
  stamp=$(stat_mtime "$file") || stamp=0
  printf '%s' "${stamp:-0}"
}

failure_repeats() {
  local file=$1 count
  [ -f "$file" ] || return 1
  count=$(awk -v threshold="$FAILURE_REPEATS" '
    { latest = $0 }
    /^failed:/ {
      failures[$0]++
    }
    END {
      if (latest ~ /^failed:/ && failures[latest] >= threshold) print 1
      else print 0
    }
  ' "$file") || return 1
  [ "$count" -eq 1 ]
}

since_for_head() {  # <task-id> <rule> <head-oid>
  local id=$1 rule=$2 head=$3 marker="$STATE/.stuck-$1-$2-since" old_head old_ts tmp
  if [ -f "$marker" ] && [ ! -L "$marker" ]; then
    IFS=$'\t' read -r old_head old_ts < "$marker" || old_head=
    if [ "$old_head" = "$head" ]; then
      case "$old_ts" in ''|*[!0-9]*) ;; *) printf '%s' "$old_ts"; return 0 ;; esac
    fi
  fi
  tmp=$(mktemp "$STATE/.$1.$2-since.XXXXXX") || return 1
  if (umask 077; printf '%s\t%s\n' "$head" "$now" > "$tmp" && chmod 0600 "$tmp") \
    && mv -f -- "$tmp" "$marker"; then :; else rm -f -- "$tmp"; return 1; fi
  printf '%s' "$now"
}

cached_head_timestamp() {  # <task-id> <rule>
  local marker="$STATE/.stuck-$1-$2-since" old_head old_ts
  [ -f "$marker" ] && [ ! -L "$marker" ] || { printf '0'; return; }
  IFS=$'\t' read -r old_head old_ts < "$marker" || old_ts=0
  case "$old_ts" in ''|*[!0-9]*) printf '0' ;; *) printf '%s' "$old_ts" ;; esac
}

pr_snapshot() {
  local url=$1 result
  result=$(gh pr view "$url" --json state,isDraft,reviewDecision,mergeStateStatus,headRefOid,reviews 2>/dev/null) || return 1
  printf '%s' "$result"
}

latest_status_state() {
  local file=$1 line
  [ -f "$file" ] || return 1
  line=$(tail -n 1 "$file" 2>/dev/null || true)
  case "$line" in done:*|failed:*|needs-decision:*|blocked:*|paused:*) return 0 ;; esac
  return 1
}

scan_local_task() {
  local id=$1 meta=$2 kind start hb hb_ts status_file status_ts commit_ts progress_ts
  local worktree push_ts head_ts
  kind=$(fm_meta_get "$meta" kind)
  case "$kind" in ship|scout) ;; *) return 0 ;; esac
  start=$(task_start "$id" "$meta") || start=$now
  status_file="$STATE/$id.status"
  if latest_status_state "$status_file"; then
    clear_rule "$id" heartbeat
    clear_rule "$id" no-progress
  else
    hb="$STATE/$id.heartbeat"
    hb_ts=0
    if [ -f "$hb" ] && [ ! -L "$hb" ]; then
      IFS=$'\t' read -r hb_ts _ < "$hb" || hb_ts=0
      case "$hb_ts" in ''|*[!0-9]*) hb_ts=0 ;; esac
    fi
    if [ "$hb_ts" -eq 0 ]; then hb_ts=$start; fi
    if [ "$(elapsed "$hb_ts")" -gt "$HEARTBEAT_SECS" ]; then breach "$id" heartbeat; else clear_rule "$id" heartbeat; fi
    # Evaluate progress before forge reads, so failed or hanging network reads
    # cannot prevent locally observable signals from reaching the board.
    status_ts=$(last_status_progress "$status_file")
    commit_ts=0
    worktree=$(fm_meta_get "$meta" worktree)
    if [ -n "$worktree" ] && [ -d "$worktree" ]; then
      commit_ts=$(git -C "$worktree" log -1 --format=%ct 2>/dev/null || echo 0)
    fi
    progress_ts=$start
    [ "$status_ts" -le "$progress_ts" ] || progress_ts=$status_ts
    [ "$commit_ts" -le "$progress_ts" ] || progress_ts=$commit_ts
    push_ts=$(cached_head_timestamp "$id" push)
    head_ts=$(cached_head_timestamp "$id" progress)
    [ "$push_ts" -le "$progress_ts" ] || progress_ts=$push_ts
    [ "$head_ts" -le "$progress_ts" ] || progress_ts=$head_ts
    if [ "$(elapsed "$progress_ts")" -gt "$PROGRESS_SECS" ]; then
      breach "$id" no-progress
    else
      clear_rule "$id" no-progress
    fi
  fi

  if failure_repeats "$status_file"; then breach "$id" repeated-failure; else clear_rule "$id" repeated-failure; fi
}

scan_network_task() {
  local id=$1 meta=$2 kind start worktree branch pushed_head
  local url pr_json pr_state pr_draft pr_decision approved_reviews pr_head review_since ready_since
  kind=$(fm_meta_get "$meta" kind)
  case "$kind" in ship|scout) ;; *) return 0 ;; esac
  start=$(task_start "$id" "$meta") || start=$now
  worktree=$(fm_meta_get "$meta" worktree)
  if [ -n "$worktree" ] && [ -d "$worktree" ]; then
    branch=$(git -C "$worktree" branch --show-current 2>/dev/null || true)
    if [ -n "$branch" ]; then
      pushed_head=$(git -C "$worktree" ls-remote origin "refs/heads/$branch" 2>/dev/null | awk 'NR == 1 {print $1}')
      case "$pushed_head" in
        *[!0-9a-f]*|'') ;;
        *) since_for_head "$id" push "$pushed_head" >/dev/null || true ;;
      esac
    fi
  fi
  url=$(fm_meta_get "$meta" pr)
  pr_json=
  if [ -n "$url" ]; then pr_json=$(pr_snapshot "$url" || true); fi
  pr_state=$(printf '%s' "$pr_json" | jq -r '.state // empty' 2>/dev/null || true)
  pr_draft=$(printf '%s' "$pr_json" | jq -r 'if (.isDraft | type) == "boolean" then .isDraft else empty end' 2>/dev/null || true)
  pr_decision=$(printf '%s' "$pr_json" | jq -r '.reviewDecision // empty' 2>/dev/null || true)
  approved_reviews=$(printf '%s' "$pr_json" | jq -r '[.reviews[]? | select(.state == "APPROVED")] | length' 2>/dev/null || echo 0)
  pr_head=$(printf '%s' "$pr_json" | jq -r '.headRefOid // empty' 2>/dev/null || true)
  if [ -n "$pr_head" ]; then since_for_head "$id" progress "$pr_head" >/dev/null || true; fi

  if [ "$kind" = ship ]; then
    if { [ -z "$url" ] || { [ -n "$pr_json" ] && [ "$pr_state" != OPEN ]; }; } \
      && [ "$(elapsed "$start")" -gt "$DRAFT_PR_SECS" ]; then
      breach "$id" missing-draft-pr
    else
      clear_rule "$id" missing-draft-pr
    fi
  else
    clear_rule "$id" missing-draft-pr
  fi

  if [ -n "$url" ] && [ -z "$pr_json" ]; then
    : # A failed forge read cannot close a previously observed review episode.
  elif [ "$pr_state" = OPEN ] && [ "$pr_draft" = false ]; then
    if [ "$pr_decision" = REVIEW_REQUIRED ] || { [ -z "$pr_decision" ] && [ "$approved_reviews" -eq 0 ]; }; then
      review_since=$(since_for_head "$id" review "$pr_head")
      if [ "$(elapsed "$review_since")" -gt "$REVIEW_SECS" ]; then
        breach "$id" review-wait
      else
        clear_rule "$id" review-wait
      fi
    else
      clear_rule "$id" review-wait
      rm -f -- "$STATE/.stuck-$id-review-since"
    fi
    if [ "$pr_state" = OPEN ] && [ "$pr_draft" = false ]; then
      ready_since=$(since_for_head "$id" ready "$pr_head")
      if [ "$(elapsed "$ready_since")" -gt "$READY_PR_SECS" ]; then
        breach "$id" ready-pr-wait
      else
        clear_rule "$id" ready-pr-wait
      fi
    else
      clear_rule "$id" ready-pr-wait
      rm -f -- "$STATE/.stuck-$id-ready-since"
    fi
  else
    clear_rule "$id" review-wait
    clear_rule "$id" ready-pr-wait
    rm -f -- "$STATE/.stuck-$id-review-since" "$STATE/.stuck-$id-ready-since"
  fi
}

metas=()
for meta in "$STATE"/*.meta; do
  [ -f "$meta" ] && [ ! -L "$meta" ] || continue
  id=${meta##*/}; id=${id%.meta}
  fm_task_id_creation_valid "$id" || continue
  metas+=("$meta")
  scan_local_task "$id" "$meta"
done

meta_count=${#metas[@]}
[ "$meta_count" -gt 0 ] || exit 0
cursor_file="$STATE/.stuck-board-cursor"
cursor=0
if [ -f "$cursor_file" ] && [ ! -L "$cursor_file" ]; then
  IFS= read -r cursor < "$cursor_file" || cursor=0
  case "$cursor" in ''|*[!0-9]*) cursor=0 ;; esac
fi
cursor=$((cursor % meta_count))
next_cursor=$(((cursor + 1) % meta_count))
cursor_tmp=$(mktemp "$STATE/.stuck-board-cursor.XXXXXX") || fail 'could not prepare the network fairness cursor'
if (umask 077; printf '%s\n' "$next_cursor" > "$cursor_tmp" && chmod 0600 "$cursor_tmp") \
  && mv -f -- "$cursor_tmp" "$cursor_file"; then :; else rm -f -- "$cursor_tmp"; fail 'could not publish the network fairness cursor'; fi
offset=0
while [ "$offset" -lt "$meta_count" ]; do
  index=$(((cursor + offset) % meta_count))
  meta=${metas[$index]}
  id=${meta##*/}; id=${id%.meta}
  scan_network_task "$id" "$meta"
  offset=$((offset + 1))
done
