#!/usr/bin/env bash
set -Eeuo pipefail

scenario=${LAB_SCENARIO:?}
source_commit=${SOURCE_COMMIT:?}
recipe_root=${LAB_RECIPE_ROOT:?}
root=${RUNNER_TEMP:?}/fm-capacity/$scenario
source_root=$root/source
job_start=$(cat "$RUNNER_TEMP/fm-capacity/$scenario/job-start-epoch.txt")
analysis_limit=1500
if [ "$scenario" = serial ]; then analysis_limit=2400; fi
now=$(date +%s)
outer_deadline=$((job_start + 43 * 60))
remaining=$((outer_deadline - now))
analysis_budget=$analysis_limit
if [ "$remaining" -lt "$analysis_budget" ]; then analysis_budget=$remaining; fi
printf 'scenario=%s\nsource_commit=%s\nanalysis_limit_seconds=%s\nanalysis_budget_seconds=%s\n' \
  "$scenario" "$source_commit" "$analysis_limit" "$analysis_budget" \
  > "$root/analysis-budget.txt"
{
  printf 'analysis_bash=%s\n' "$BASH_VERSION"
  printf 'analysis_shell_flags=%s\n' "$-"
  printf 'analysis_shell_pid=%s\n' "$$"
  printf 'analysis_shell_process='; ps -ww -p "$$" -o pid=,ppid=,pgid=,args=
  printf 'PATH=%s\n' "$PATH"
  printf 'shellcheck_path=%s\n' "$(command -v shellcheck || true)"
  printf 'actionlint_path=%s\n' "$(command -v actionlint || true)"
} > "$root/analysis-tool-paths.txt"
if [ "$analysis_budget" -le 0 ]; then
  printf 'analysis=not-started\nreason=outer evidence reserve would be consumed\n' \
    > "$root/analysis-result.txt"
  exit 125
fi

# shellcheck disable=SC2329 # Registered by the EXIT trap below.
capture_after() {
  date -u '+%Y-%m-%dT%H:%M:%SZ' > "$root/analysis-finished-utc.txt"
  date +%s > "$root/analysis-finished-epoch.txt"
  cat /proc/meminfo > "$root/meminfo-after.txt"
  cat /proc/swaps > "$root/swaps-after.txt"
  cat /proc/vmstat > "$root/vmstat-after.txt"
  cat /proc/self/cgroup > "$root/cgroup-after.txt"
  python3 "$recipe_root/cgroup-snapshot.py" > "$root/cgroup-metrics-after.txt"
  ps -ww -eo pid,ppid,pgid,etimes,time,pcpu,rss,vsz,stat,args \
    > "$root/processes-after.txt"
}

sample_pid=
# shellcheck disable=SC2329 # Called by the EXIT trap below.
cleanup_sampler() {
  if [ -n "$sample_pid" ]; then
    kill -TERM -- "-$sample_pid" 2>/dev/null || true
    wait "$sample_pid" 2>/dev/null || true
    sample_pid=
  fi
}
# shellcheck disable=SC2329 # Registered by the EXIT trap below.
on_exit() {
  local status=$?
  trap - EXIT HUP INT TERM
  cleanup_sampler
  capture_after
  printf '%s\n' "$status" > "$root/analysis-step.exit"
  exit "$status"
}
trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

date -u '+%Y-%m-%dT%H:%M:%SZ' > "$root/analysis-started-utc.txt"
date +%s > "$root/analysis-started-epoch.txt"
printf 'timeout=/usr/bin/time -v -o %q /usr/bin/timeout --signal=TERM --kill-after=30s %q /usr/bin/env' \
  "$root/lint-time.txt" "$analysis_budget" > "$root/lint-command.txt"
if [ "$scenario" = serial ]; then
  printf ' -u FM_LINT_TELEMETRY FM_LINT_JOBS=1 CI=true GITHUB_ACTIONS=true LC_ALL=C PATH=%q' \
    "$PATH" >> "$root/lint-command.txt"
else
  printf ' -u FM_LINT_JOBS -u FM_LINT_TELEMETRY CI=true GITHUB_ACTIONS=true LC_ALL=C PATH=%q' \
    "$PATH" >> "$root/lint-command.txt"
fi
printf ' /usr/bin/bash -e %q\n' "$source_root/bin/fm-lint.sh" >> "$root/lint-command.txt"
cat /proc/meminfo > "$root/meminfo-before.txt"
cat /proc/swaps > "$root/swaps-before.txt"
cat /proc/vmstat > "$root/vmstat-before.txt"
cat /proc/self/cgroup > "$root/cgroup-before.txt"
python3 "$recipe_root/cgroup-snapshot.py" > "$root/cgroup-metrics-before.txt"
ps -ww -eo pid,ppid,pgid,etimes,time,pcpu,rss,vsz,stat,args \
  > "$root/processes-before.txt"
setsid /usr/bin/bash "$recipe_root/sample.sh" "$root" 10 "$recipe_root" &
sample_pid=$!
printf 'sampler_pid=%s\nsampler_pgid=%s\n' "$sample_pid" \
  "$(ps -o pgid= -p "$sample_pid" | tr -d '[:space:]')" \
  > "$root/sampler-identity.txt"

set +e
if [ "$scenario" = serial ]; then
  /usr/bin/time -v -o "$root/lint-time.txt" \
    /usr/bin/timeout --signal=TERM --kill-after=30s "$analysis_budget" \
    /usr/bin/env -u FM_LINT_TELEMETRY FM_LINT_JOBS=1 CI=true GITHUB_ACTIONS=true LC_ALL=C \
      PATH="$PATH" /usr/bin/bash -e "$source_root/bin/fm-lint.sh" \
      > "$root/lint-stdout.txt" 2> "$root/lint-stderr.txt"
  analysis_status=$?
else
  /usr/bin/time -v -o "$root/lint-time.txt" \
    /usr/bin/timeout --signal=TERM --kill-after=30s "$analysis_budget" \
    /usr/bin/env -u FM_LINT_JOBS -u FM_LINT_TELEMETRY CI=true GITHUB_ACTIONS=true LC_ALL=C \
      PATH="$PATH" /usr/bin/bash -e "$source_root/bin/fm-lint.sh" \
      > "$root/lint-stdout.txt" 2> "$root/lint-stderr.txt"
  analysis_status=$?
fi
set -e
printf '%s\n' "$analysis_status" > "$root/lint-exit.txt"
if [ "$analysis_status" -eq 124 ]; then
  printf 'analysis=censored-at-bound\n' > "$root/analysis-result.txt"
elif [ "$analysis_status" -eq 0 ]; then
  printf 'analysis=complete-success\n' > "$root/analysis-result.txt"
else
  printf 'analysis=complete-nonzero-or-interrupted\n' > "$root/analysis-result.txt"
fi
exit "$analysis_status"
