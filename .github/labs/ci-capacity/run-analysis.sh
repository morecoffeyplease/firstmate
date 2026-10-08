#!/usr/bin/env bash
set -Eeuo pipefail

scenario=${LAB_SCENARIO:?}
source_commit=${SOURCE_COMMIT:?}
recipe_root=${LAB_RECIPE_ROOT:?}
run_id=${GITHUB_RUN_ID:?}
run_attempt=${GITHUB_RUN_ATTEMPT:?}
root=${RUNNER_TEMP:?}/fm-capacity/$run_id/$run_attempt/$scenario
source_root=$root/source
step_file=$recipe_root/analysis-step.sh
job_start=$(cat "$RUNNER_TEMP/fm-capacity/$run_id/$run_attempt/$scenario/job-start-epoch.txt")
job_start_monotonic_ns=$(cat "$RUNNER_TEMP/fm-capacity/$run_id/$run_attempt/$scenario/job-start-monotonic-ns.txt")
job_start_monotonic=$(python3 - "$job_start_monotonic_ns" <<'PY'
import sys
print(int(sys.argv[1]) / 1_000_000_000)
PY
)
analysis_limit=1500
if [ "$scenario" = serial ]; then analysis_limit=2400; fi
analysis_deadline_monotonic=$(python3 - "$job_start_monotonic" <<'PY'
import sys
print(float(sys.argv[1]) + 41 * 60)
PY
)
cleanup_deadline_monotonic=$(python3 - "$job_start_monotonic" <<'PY'
import sys
print(float(sys.argv[1]) + 44 * 60)
PY
)

printf 'phase=analysis-admission\noutcome=in-progress\n' > "$root/analysis-launch-status.txt"
if ! python3 "$recipe_root/admission-check.py" "$root/admission-before-analysis.json"; then
  printf 'phase=analysis-admission\nresult=refused\n' > "$root/analysis-launch-status.txt"
  exit 125
fi
test -x "$source_root/bin/fm-lint.sh"
test -f "$step_file"
printf 'phase=supervisor-launch\noutcome=in-progress\n' > "$root/analysis-launch-status.txt"
printf 'run_id=%s\nrun_attempt=%s\nscenario=%s\nsource_commit=%s\nanalysis_limit_seconds=%s\njob_start_epoch=%s\njob_start_monotonic_ns=%s\nanalysis_deadline_monotonic=%s\ncleanup_deadline_monotonic=%s\n' \
  "$run_id" "$run_attempt" "$scenario" "$source_commit" "$analysis_limit" "$job_start" \
  "$job_start_monotonic_ns" "$analysis_deadline_monotonic" "$cleanup_deadline_monotonic" \
  > "$root/analysis-launch.txt"
exec python3 "$recipe_root/analysis-supervisor.py" \
  "$scenario" "$analysis_limit" "$analysis_deadline_monotonic" "$cleanup_deadline_monotonic" \
  "$root" "$source_root" "$recipe_root" "$step_file" /usr/bin/bash
