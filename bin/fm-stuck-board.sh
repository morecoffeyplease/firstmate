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
# shellcheck source=bin/fm-agent-process-lib.sh
. "$SCRIPT_DIR/fm-agent-process-lib.sh"

fail() { printf 'stuck-board-error: %s\n' "$*"; printf 'fm-stuck-board: %s\n' "$*" >&2; exit 2; }
usage() { sed -n '2,/^set -u$/s/^# \{0,1\}//p' "$0"; }

if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then usage; exit 0; fi
[ "${1:-}" = scan ] && [ "$#" -eq 1 ] || fail 'usage: fm-stuck-board.sh scan'
[ -d "$STATE" ] && [ ! -L "$STATE" ] || exit 0

HEARTBEAT_SECS=${FM_STUCK_HEARTBEAT_SECS:-900}
PROGRESS_SECS=${FM_STUCK_PROGRESS_SECS:-3600}
COMMAND_SECS=${FM_STUCK_COMMAND_SECS:-2700}
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
      command_seconds) COMMAND_SECS=$value ;;
      draft_pr_seconds) DRAFT_PR_SECS=$value ;;
      review_seconds) REVIEW_SECS=$value ;;
      failure_repeats) FAILURE_REPEATS=$value ;;
      ready_pr_seconds) READY_PR_SECS=$value ;;
      *) fail "unknown config/stuck-board key: $key" ;;
    esac
  done < "$CONFIG/stuck-board"
}

config_read
for value in "$HEARTBEAT_SECS" "$PROGRESS_SECS" "$COMMAND_SECS" "$DRAFT_PR_SECS" \
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

elapsed_seconds() {
  awk -v value="$1" 'BEGIN {
    days = 0
    if (value ~ /-/) { split(value, dayparts, "-"); days = dayparts[1] + 0; value = dayparts[2] }
    parts = split(value, clock, ":")
    if (parts == 2) seconds = clock[1] * 60 + clock[2]
    else if (parts == 3) seconds = clock[1] * 3600 + clock[2] * 60 + clock[3]
    else exit 1
    print seconds + days * 86400
  }'
}

pane_process_root() {
  local meta=$1 backend target session pane info
  backend=$(fm_backend_of_meta "$meta")
  case "$backend" in
    tmux)
      target=$(fm_backend_target_of_meta "$meta")
      [ -n "$target" ] || return 1
      tmux display-message -p -t "$target" '#{pane_pid}' 2>/dev/null
      ;;
    herdr)
      session=$(fm_meta_get "$meta" herdr_session)
      pane=$(fm_meta_get "$meta" herdr_pane_id)
      [ -n "$session" ] && [ -n "$pane" ] || return 1
      fm_backend_source herdr || return 1
      info=$(fm_backend_herdr_cli "$session" pane process-info --pane "$pane" 2>/dev/null) || return 1
      printf '%s' "$info" | jq -er --arg pane "$pane" '
        if .result.type == "pane_process_info" and .result.process_info.pane_id == $pane then
          .result.process_info.shell_pid | select(type == "number" and . > 1) | floor
        else empty end
      ' 2>/dev/null
      ;;
    *) return 1 ;;
  esac
}

process_descendants() {  # <ps-output> <root-pid>
  awk -v root="$2" '
    NF >= 5 {
      pid[NR] = $1
      parent[NR] = $2
      elapsed[NR] = $3
      comm[NR] = $4
      args = $5
      for (i = 6; i <= NF; i++) args = args " " $i
      command[NR] = args
      index_of[$1] = NR
    }
    END {
      if (!(root in index_of)) exit 1
      root_row = index_of[root]
      descendant[root] = 1
      depth[root] = 0
      for (pass = 0; pass < 64; pass++) {
        changed = 0
        for (i = 1; i <= NR; i++) {
          if ((parent[i] in descendant) && !(pid[i] in descendant)) {
            descendant[pid[i]] = 1
            depth[pid[i]] = depth[parent[i]] + 1
            changed = 1
          }
        }
        if (!changed) break
      }
      for (i = 1; i <= NR; i++) {
        if (pid[i] in descendant)
          printf "%s\t%s\t%s\t%s\t%s\n", pid[i], depth[pid[i]], elapsed[i], comm[i], command[i]
      }
    }
  ' <<< "$1"
}

process_is_descendant() {  # <ps-output> <child-pid> <ancestor-pid>
  awk -v child="$2" -v ancestor="$3" '
    NF >= 2 { parent[$1] = $2 }
    END {
      current = child
      for (depth = 0; depth < 64 && current in parent; depth++) {
        current = parent[current]
        if (current == ancestor) exit 0
      }
      exit 1
    }
  ' <<< "$1"
}

process_is_persistent_helper() {  # <comm> <command-line>
  awk -v comm="$1" -v args="$2" 'BEGIN {
    command = tolower(comm " " args)
    if (command ~ /(^|[\/. _-])mcp([\/. _-]|$)/ || command ~ /mcp[-_]server/ \
      || command ~ /language[-_ ]server/ || command ~ /(^|[\/. _-])lsp([\/. _-]|$)/ \
      || command ~ /extension[-_ ]host/ || command ~ /plugin[-_ ]host/ \
      || command ~ /watchman/ || command ~ /file[-_ ]watcher/ \
      || command ~ /cua[-_]repl/ || command ~ /node_repl/ || command ~ /codex-code-mode-host/) exit 0
    exit 1
  }'
}

process_runs_shell_command() {  # <command-line>
  awk -v args="$1" 'BEGIN {
    command = tolower(args)
    if (command ~ /(^|[[:space:]])-[a-z]*c([[:space:]]|$)/) exit 0
    exit 1
  }'
}

command_age() {
  local meta=$1 root table rows harness agent_pid='' agent_depth=999
  local pid depth etime comm args argv0 verdict age maximum=0
  root=$(pane_process_root "$meta") || return 1
  case "$root" in ''|*[!0-9]*) return 1 ;; esac
  table=$(ps -axo pid=,ppid=,etime=,comm=,args= 2>/dev/null) || return 1
  rows=$(process_descendants "$table" "$root") || return 1
  harness=$(fm_meta_get "$meta" harness)
  while IFS=$'\t' read -r pid depth etime comm args; do
    [ -n "$pid" ] || continue
    argv0=${args%% *}
    verdict=$(fm_agent_process_classify "$comm" "$argv0" "$args" "$pid")
    [ "$verdict" = agent ] || continue
    case "$harness:$comm:$argv0:$args" in
      claude:*claude*|codex:*codex*) ;;
      claude:*|codex:*) continue ;;
      *) ;;
    esac
    if [ "$depth" -lt "$agent_depth" ]; then
      agent_pid=$pid
      agent_depth=$depth
    fi
  done <<EOF_AGENT
$rows
EOF_AGENT
  [ -n "$agent_pid" ] || return 1

  while IFS=$'\t' read -r pid depth etime comm args; do
    [ -n "$pid" ] && [ "$depth" -gt "$agent_depth" ] || continue
    process_is_descendant "$table" "$pid" "$agent_pid" || continue
    verdict=$(fm_agent_process_classify "$comm" "${args%% *}" "$args" "$pid")
    [ "$verdict" != agent ] || continue
    process_is_persistent_helper "$comm" "$args" && continue
    [ "$verdict" != shell ] || process_runs_shell_command "$args" || continue
    age=$(elapsed_seconds "$etime" || echo 0)
    [ "$age" -gt "$maximum" ] && maximum=$age
  done <<EOF_COMMANDS
$rows
EOF_COMMANDS
  [ "$maximum" -gt 0 ] || return 1
  printf '%s' "$maximum"
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
  local id=$1 meta=$2 kind start hb hb_ts status_file cmd_age
  kind=$(fm_meta_get "$meta" kind)
  case "$kind" in ship|scout) ;; *) return 0 ;; esac
  start=$(task_start "$id" "$meta") || start=$now
  status_file="$STATE/$id.status"
  if latest_status_state "$status_file"; then
    clear_rule "$id" heartbeat
    clear_rule "$id" no-progress
    clear_rule "$id" long-command
  else
    hb="$STATE/$id.heartbeat"
    hb_ts=0
    if [ -f "$hb" ] && [ ! -L "$hb" ]; then
      IFS=$'\t' read -r hb_ts _ < "$hb" || hb_ts=0
      case "$hb_ts" in ''|*[!0-9]*) hb_ts=0 ;; esac
    fi
    if [ "$hb_ts" -eq 0 ]; then hb_ts=$start; fi
    if [ "$(elapsed "$hb_ts")" -gt "$HEARTBEAT_SECS" ]; then breach "$id" heartbeat; else clear_rule "$id" heartbeat; fi
    cmd_age=$(command_age "$meta" 2>/dev/null || echo 0)
    if [ "$cmd_age" -gt "$COMMAND_SECS" ]; then breach "$id" long-command; else clear_rule "$id" long-command; fi
  fi

  if failure_repeats "$status_file"; then breach "$id" repeated-failure; else clear_rule "$id" repeated-failure; fi
}

scan_network_task() {
  local id=$1 meta=$2 kind start status_file status_ts commit_ts progress_ts worktree branch pushed_head pushed_ts
  local url pr_json pr_state pr_draft pr_decision approved_reviews pr_head head_ts review_since ready_since
  kind=$(fm_meta_get "$meta" kind)
  case "$kind" in ship|scout) ;; *) return 0 ;; esac
  start=$(task_start "$id" "$meta") || start=$now
  status_file="$STATE/$id.status"
  worktree=$(fm_meta_get "$meta" worktree)
  status_ts=$(last_status_progress "$status_file")
  commit_ts=0
  if [ -n "$worktree" ] && [ -d "$worktree" ]; then
    commit_ts=$(git -C "$worktree" log -1 --format=%ct 2>/dev/null || echo 0)
    branch=$(git -C "$worktree" branch --show-current 2>/dev/null || true)
    if [ -n "$branch" ]; then
      pushed_head=$(git -C "$worktree" ls-remote origin "refs/heads/$branch" 2>/dev/null | awk 'NR == 1 {print $1}')
      case "$pushed_head" in
        *[!0-9a-f]*|'') ;;
        *) pushed_ts=$(since_for_head "$id" push "$pushed_head" || echo 0); [ "$pushed_ts" -le "$commit_ts" ] || commit_ts=$pushed_ts ;;
      esac
    fi
  fi
  progress_ts=$start
  [ "$status_ts" -le "$progress_ts" ] || progress_ts=$status_ts
  [ "$commit_ts" -le "$progress_ts" ] || progress_ts=$commit_ts

  url=$(fm_meta_get "$meta" pr)
  pr_json=
  if [ -n "$url" ]; then pr_json=$(pr_snapshot "$url" || true); fi
  pr_state=$(printf '%s' "$pr_json" | jq -r '.state // empty' 2>/dev/null || true)
  pr_draft=$(printf '%s' "$pr_json" | jq -r 'if (.isDraft | type) == "boolean" then .isDraft else empty end' 2>/dev/null || true)
  pr_decision=$(printf '%s' "$pr_json" | jq -r '.reviewDecision // empty' 2>/dev/null || true)
  approved_reviews=$(printf '%s' "$pr_json" | jq -r '[.reviews[]? | select(.state == "APPROVED")] | length' 2>/dev/null || echo 0)
  pr_head=$(printf '%s' "$pr_json" | jq -r '.headRefOid // empty' 2>/dev/null || true)
  if [ -n "$pr_head" ]; then
    head_ts=$(since_for_head "$id" progress "$pr_head" || echo 0)
    [ "$head_ts" -le "$progress_ts" ] || progress_ts=$head_ts
  fi
  if latest_status_state "$status_file"; then
    clear_rule "$id" no-progress
  elif [ "$(elapsed "$progress_ts")" -gt "$PROGRESS_SECS" ]; then
    breach "$id" no-progress
  else
    clear_rule "$id" no-progress
  fi

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
