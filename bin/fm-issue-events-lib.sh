# shellcheck shell=bash
# Bounded typed task lifecycle events used by the local Issues projection.
#
# Sourced by lifecycle owners. Event records survive endpoint teardown in
# data/<task>/events.jsonl. A failure is returned to the caller, which invokes
# this writer best-effort so an event outage never changes existing results.

FM_ISSUE_EVENT_MAX_FILE_BYTES=4194304

fm_issue_event_append() { # <task-dir> <task-id> <generation> <kind> <fields-json> [class]
  local dir=${1-} task=${2-} generation=${3-} kind=${4-} fields=${5-} class=${6:-event}
  local file lock tmp epoch attempt owner lock_age
  case "$task" in ''|.*|*[!A-Za-z0-9._-]*) return 1 ;; esac
  case "$generation" in ''|*[!A-Za-z0-9._-]*) return 1 ;; esac
  case "$kind" in started|done|reopened|held|answered|released|reconciled|blocked-by|unblocked|pr-bound|merge-requested|decision-resolved|status-seen) ;; *) return 1 ;; esac
  case "$class" in event|detected) ;; *) return 1 ;; esac
  [ -d "$dir" ] && [ ! -L "$dir" ] || return 1
  [ "$(LC_ALL=C printf '%s' "$fields" | wc -c | tr -d ' ')" -le 2048 ] || return 1
  printf '%s' "$fields" | jq -e --arg kind "$kind" '
    type == "object" and length <= 8
    and all(to_entries[]; ((.value | tostring | length) <= 512))
    and (keys - (if $kind == "started" then ["kind"]
      elif $kind == "done" or $kind == "reopened" then ["transition"]
      elif $kind == "held" or $kind == "answered" or $kind == "released" or $kind == "reconciled" then ["source"]
      elif $kind == "pr-bound" then ["url","head"]
      elif $kind == "merge-requested" then ["url","authority"]
      elif $kind == "decision-resolved" then ["key"]
      elif $kind == "blocked-by" or $kind == "unblocked" then ["blocker"]
      else ["state","key","from_epoch","to_epoch"] end) | length == 0)
    and (if $kind == "started" then (.kind | IN("ship","scout"))
      elif $kind == "done" or $kind == "reopened" then (.transition | IN("close","retain"))
      elif $kind == "held" or $kind == "answered" or $kind == "released" or $kind == "reconciled" then (.source | type == "string" and length > 0)
      elif $kind == "pr-bound" then (.url | type == "string" and length > 0) and (.head | type == "string" and length > 0)
      elif $kind == "merge-requested" then (.url | type == "string" and length > 0) and (.authority | IN("yolo","away-grant","attended"))
      elif $kind == "decision-resolved" then (.key | type == "string" and test("^[A-Za-z0-9._-]{1,80}$"))
      elif $kind == "blocked-by" or $kind == "unblocked" then (.blocker | type == "string" and length > 0)
      elif $kind == "status-seen" then (.state | IN("working","needs-decision","blocked","paused","done","failed","resolved","note","receipt","waiting","busy","running","complete","completed","unknown")) and (.key == null or (.key | type == "string" and test("^[A-Za-z0-9._-]{1,80}$"))) and (.from_epoch == null or (.from_epoch|type)=="number") and (.to_epoch|type)=="number"
      else false end)' >/dev/null 2>&1 || return 1
  file="$dir/events.jsonl"
  lock="$dir/.events.lock"
  [ ! -L "$file" ] && [ ! -L "$lock" ] || return 1
  if [ -e "$file" ] && { [ ! -f "$file" ] || [ "$(wc -c < "$file" | tr -d ' ')" -gt "$FM_ISSUE_EVENT_MAX_FILE_BYTES" ]; }; then
    return 1
  fi
  epoch=$(date -u +%s) || return 1
  attempt=0
  while ! mkdir "$lock" 2>/dev/null; do
    [ -d "$lock" ] && [ ! -L "$lock" ] || return 1
    owner=$(cat "$lock/pid" 2>/dev/null || true)
    case "$owner" in ''|*[!0-9]*)
      lock_age=$(($(date +%s) - $(stat -f %m "$lock" 2>/dev/null || stat -c %Y "$lock" 2>/dev/null || date +%s)))
      if [ "$lock_age" -gt 2 ]; then rm -rf -- "$lock" 2>/dev/null || true; fi
      ;;
      *) kill -0 "$owner" 2>/dev/null || rm -rf -- "$lock" 2>/dev/null || true ;;
    esac
    attempt=$((attempt + 1))
    [ "$attempt" -lt 25 ] || return 1
    sleep 0.04
  done
  printf '%s\n' "$$" > "$lock/pid" || { rm -rf -- "$lock"; return 1; }
  tmp=$(umask 077; mktemp "$dir/.events.XXXXXX") || { rm -rf -- "$lock"; return 1; }
  if [ -f "$file" ]; then cat "$file" > "$tmp" || { rm -f "$tmp"; rm -rf -- "$lock"; return 1; }; fi
  jq -cn --arg task "$task" --arg generation "$generation" --arg kind "$kind" --arg class "$class" --argjson at "$epoch" --argjson fields "$fields" \
    '{schema:"fm-task-event.v1",at_epoch:$at,class:$class,task:$task,generation:$generation,kind:$kind,fields:$fields}' >> "$tmp" || { rm -f "$tmp"; rm -rf -- "$lock"; return 1; }
  chmod 600 "$tmp" || { rm -f "$tmp"; rm -rf -- "$lock"; return 1; }
  # A bounded append-only journal is capped by bytes; do not replace old facts
  # or let one task consume unbounded home storage.
  [ "$(wc -c < "$tmp" | tr -d ' ')" -le "$FM_ISSUE_EVENT_MAX_FILE_BYTES" ] || { rm -f "$tmp"; rm -rf -- "$lock"; return 1; }
  mv -f -- "$tmp" "$file" || { rm -f "$tmp"; rm -rf -- "$lock"; return 1; }
  rm -rf -- "$lock"
}

fm_issue_event_validate_file() { # <events.jsonl> <task-id>
  local file=$1 task=$2
  [ ! -L "$file" ] && [ -f "$file" ] || return 1
  [ "$(wc -c < "$file" | tr -d ' ')" -le "$FM_ISSUE_EVENT_MAX_FILE_BYTES" ] || return 1
  jq -se --arg task "$task" '
    def fields_ok:
      if .kind == "started" then (.fields.kind | IN("ship","scout"))
      elif .kind == "done" or .kind == "reopened" then (.fields.transition | IN("close","retain"))
      elif .kind == "held" or .kind == "answered" or .kind == "released" or .kind == "reconciled" then (.fields.source | type == "string" and length > 0)
      elif .kind == "pr-bound" then (.fields.url | type == "string" and length > 0) and (.fields.head | type == "string" and length > 0)
      elif .kind == "merge-requested" then (.fields.url | type == "string" and length > 0) and (.fields.authority | IN("yolo","away-grant","attended"))
      elif .kind == "decision-resolved" then (.fields.key | type == "string" and test("^[A-Za-z0-9._-]{1,80}$"))
      elif .kind == "blocked-by" or .kind == "unblocked" then (.fields.blocker | type == "string" and length > 0)
      elif .kind == "status-seen" then (.fields.state | IN("working","needs-decision","blocked","paused","done","failed","resolved","note","receipt","waiting","busy","running","complete","completed","unknown")) and (.fields.key == null or (.fields.key | type == "string" and test("^[A-Za-z0-9._-]{1,80}$"))) and (.fields.from_epoch == null or (.fields.from_epoch|type)=="number") and (.fields.to_epoch|type)=="number"
      else false end;
    all(.[]; .schema == "fm-task-event.v1" and .task == $task
      and (.generation | type == "string" and length > 0 and length <= 96)
      and (.at_epoch | type == "number" and . >= 0)
      and (.class | IN("event","detected"))
      and (.kind | IN("started","done","reopened","held","answered","released","reconciled","blocked-by","unblocked","pr-bound","merge-requested","decision-resolved","status-seen"))
      and (.fields | type == "object" and length <= 8) and fields_ok)' "$file" >/dev/null
}
