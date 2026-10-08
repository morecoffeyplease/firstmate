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
analysis_limit=1500
if [ "$scenario" = serial ]; then analysis_limit=2400; fi

printf 'phase=analysis-admission\noutcome=in-progress\n' > "$root/analysis-launch-status.txt"
if ! python3 "$recipe_root/admission-check.py" "$root/admission-before-analysis.json"; then
  printf 'phase=analysis-admission\nresult=refused\n' > "$root/analysis-launch-status.txt"
  exit 125
fi
test -x "$source_root/bin/fm-lint.sh"
test -f "$step_file"
printf 'phase=supervisor-launch\noutcome=in-progress\n' > "$root/analysis-launch-status.txt"
printf 'run_id=%s\nrun_attempt=%s\nscenario=%s\nsource_commit=%s\nanalysis_limit_seconds=%s\njob_start_epoch=%s\n' \
  "$run_id" "$run_attempt" "$scenario" "$source_commit" "$analysis_limit" "$job_start" \
  > "$root/analysis-launch.txt"
exec python3 "$recipe_root/analysis-supervisor.py" \
  "$scenario" "$analysis_limit" "$((job_start + 43 * 60))" \
  "$root" "$source_root" "$recipe_root" "$step_file" /usr/bin/bash
