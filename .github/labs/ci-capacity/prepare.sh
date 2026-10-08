#!/usr/bin/env bash
set -Eeuo pipefail

phase=prepare-bootstrap
runner_temp=${RUNNER_TEMP:-}
run_id=${GITHUB_RUN_ID:-}
run_attempt=${GITHUB_RUN_ATTEMPT:-}
scenario=${LAB_SCENARIO:-}
root=
if [[ -n "$runner_temp" && -n "$run_id" && -n "$run_attempt" && -n "$scenario" ]]; then
  root=$runner_temp/fm-capacity/$run_id/$run_attempt/$scenario
fi
prepare_finish() {
  local status=$?
  trap - EXIT
  if [[ -n "$root" && -d "$root" ]]; then
    {
      printf 'phase=%s\nexit=%s\n' "$phase" "$status"
      printf 'finished_utc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    } > "$root/prepare-status.txt.tmp" 2>/dev/null || true
    mv "$root/prepare-status.txt.tmp" "$root/prepare-status.txt" 2>/dev/null || true
    printf '%s\t%s\texit=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$phase" "$status" \
      >> "$root/phase-journal.tsv" 2>/dev/null || true
    if [[ -n "${recipe_root:-}" && -f "$recipe_root/readiness-gate.py" ]]; then
      python3 "$recipe_root/readiness-gate.py" prepare-end "$root" \
        >> "$root/prepare-readiness.log" 2>&1 || true
    fi
  else
    printf 'prepare evidence unavailable before failure: phase=%s exit=%s\n' "$phase" "$status" >&2
  fi
  exit "$status"
}
trap prepare_finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -z "$runner_temp" || -z "$run_id" || -z "$run_attempt" || -z "$scenario" ]]; then
  printf 'required run identity unavailable before evidence setup\n' >&2
  exit 2
fi
recipe_root=${LAB_RECIPE_ROOT:-}
source_git_root=${SOURCE_GIT_ROOT:-}
if [[ -z "$recipe_root" || -z "$source_git_root" ]]; then
  printf 'recipe or immutable source checkout path unavailable\n' >&2
  exit 2
fi
expected_tree=fedd0f74ae63ce5963940a105b744497e630259b
source_root=$root/source
tool_bin=$RUNNER_TEMP/bin
if ! mkdir -p "$root" "$tool_bin"; then
  printf 'could not create diagnostic evidence or tool directories: %s %s\n' "$root" "$tool_bin" >&2
  exit 2
fi
phase=prepare-start
printf 'phase=%s\noutcome=in-progress\n' "$phase" > "$root/prepare-status.txt"
printf '%s\t%s\t%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$phase" in-progress \
  >> "$root/phase-journal.tsv"
set_phase() {
  phase=$1
  printf '%s\t%s\tin-progress\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$phase" \
    >> "$root/phase-journal.tsv"
  printf 'phase=%s\noutcome=in-progress\n' "$phase" > "$root/prepare-status.txt"
}
set_phase admission-check
python3 "$recipe_root/admission-check.py" "$root/admission-before-prepare.json"
set_phase runner-boundary-check
{
  printf 'uname=%s\n' "$(uname -a)"
  printf 'runner_os=%s\nrunner_arch=%s\n' "${RUNNER_OS:-unavailable}" "${RUNNER_ARCH:-unavailable}"
} > "$root/runner-boundary.txt"
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
test "${RUNNER_OS:-}" = Linux
test "${RUNNER_ARCH:-}" = X64

record_command() {
  local name=$1
  shift
  local status
  set_phase "command-$name"
  set +e
  "$@" > "$root/$name.stdout" 2> "$root/$name.stderr"
  status=$?
  set -e
  printf '%s\n' "$status" > "$root/$name.exit"
  return "$status"
}

{
  printf 'lab_head_sha\t%s\n' "${LAB_HEAD_SHA:?}"
  printf 'lab_merge_sha\t%s\n' "${LAB_MERGE_SHA:?}"
  printf 'source_commit_requested\t%s\n' "${SOURCE_COMMIT:?}"
  printf 'source_checkout_head\t%s\n' "$(git -C "$source_git_root" rev-parse HEAD)"
  printf 'source_checkout_tree\t%s\n' "$(git -C "$source_git_root" rev-parse 'HEAD^{tree}')"
  printf 'lab_checkout_head\t%s\n' "$(git rev-parse HEAD)"
  printf 'lab_checkout_tree\t%s\n' "$(git rev-parse 'HEAD^{tree}')"
  printf 'lab_checkout_status\t%s\n' "$(git status --short --branch | tr '\n' ' ')"
} > "$root/source-identities.tsv"
test "$(git -C "$source_git_root" rev-parse HEAD)" = "$SOURCE_COMMIT"
test "$(git -C "$source_git_root" rev-parse 'HEAD^{tree}')" = "$expected_tree"

git -C "$source_git_root" archive --format=tar "$SOURCE_COMMIT" > "$root/permanent-source.tar"
sha256sum "$root/permanent-source.tar" > "$root/archive-sha256.txt"
mkdir -p "$source_root"
tar -xf "$root/permanent-source.tar" -C "$source_root"
printf 'source_root=%s\narchive=%s\n' "$source_root" "$root/permanent-source.tar" \
  > "$root/extraction.txt"
set_phase runner-facts

record_command runner-uname uname -a
record_command runner-os cat /etc/os-release
record_command runner-lscpu lscpu
record_command runner-free free -b
record_command runner-meminfo cat /proc/meminfo
record_command runner-swaps cat /proc/swaps
record_command runner-cgroup cat /proc/self/cgroup
record_command runner-mountinfo cat /proc/self/mountinfo
record_command runner-shell-version /usr/bin/bash --version
record_command runner-time-version /usr/bin/time --version
record_command runner-timeout-version /usr/bin/timeout --version
record_command runner-ps-version ps --version
record_command runner-git-version git --version
record_command runner-tar-version tar --version
{
  printf 'ImageOS=%s\n' "${ImageOS:-unavailable}"
  printf 'ImageVersion=%s\n' "${ImageVersion:-unavailable}"
  printf 'RUNNER_OS=%s\n' "${RUNNER_OS:-unavailable}"
  printf 'RUNNER_ARCH=%s\n' "${RUNNER_ARCH:-unavailable}"
  printf 'RUNNER_NAME=%s\n' "${RUNNER_NAME:-unavailable}"
  printf 'default_run_shell=bash -e {0} (GitHub-hosted Linux default)\n'
  printf 'prepare_bash=%s\n' "$BASH_VERSION"
  printf 'prepare_shell_flags=%s\n' "$-"
  printf 'prepare_shell_pid=%s\n' "$$"
  printf 'prepare_shell_process='; ps -ww -p "$$" -o pid=,ppid=,pgid=,args=
  printf 'PATH=%s\n' "$PATH"
} > "$root/runner-identity.txt"
python3 "$recipe_root/cgroup-snapshot.py" > "$root/cgroup-preflight.txt"

expected_source_tree=$(git -C "$source_git_root" rev-parse 'HEAD^{tree}')
printf 'source_commit=%s\nsource_tree=%s\n' "$SOURCE_COMMIT" "$expected_source_tree" \
  > "$root/source-git-identity.txt"

set +e
set_phase canonical-inventory
CI=true GITHUB_ACTIONS=true LC_ALL=C /usr/bin/bash "$source_root/bin/fm-lint.sh" --list-files \
  > "$root/canonical-roots.txt" 2> "$root/canonical-roots.stderr"
list_status=$?
set -e
printf '%s\n' "$list_status" > "$root/canonical-roots.exit"
test "$list_status" -eq 0
test "$(wc -l < "$root/canonical-roots.txt" | tr -d '[:space:]')" = 412
while IFS= read -r path; do
  printf '%s\t%s\n' "$(sha256sum "$source_root/$path" | awk '{print $1}')" "$path"
done < "$root/canonical-roots.txt" > "$root/canonical-root-sha256.tsv"
diff -u "$recipe_root/expected-roots.tsv" "$root/canonical-root-sha256.tsv" \
  > "$root/root-manifest-diff.txt"
printf 'root_count=%s\nroot_manifest_sha256=%s\n' \
  "$(wc -l < "$root/canonical-root-sha256.tsv" | tr -d '[:space:]')" \
  "$(sha256sum "$root/canonical-root-sha256.tsv" | awk '{print $1}')" \
  > "$root/root-manifest-result.txt"
find "$source_root/.github/workflows" -maxdepth 1 -type f \
  \( -name '*.yml' -o -name '*.yaml' \) -print \
  | sed "s#^$source_root/##" | LC_ALL=C sort > "$root/source-workflows.txt"
diff -u "$recipe_root/expected-workflows.txt" "$root/source-workflows.txt" \
  > "$root/workflow-manifest-diff.txt"

record_command shellcheck-required-version /usr/bin/bash "$source_root/bin/fm-lint.sh" --required-version
record_command actionlint-required-version /usr/bin/bash "$source_root/bin/fm-lint-workflows.sh" --required-version
grep -Fx '0.11.0' "$root/shellcheck-required-version.stdout"
grep -Fx '1.7.12' "$root/actionlint-required-version.stdout"

set_phase install-pinned-tools
set +e
"$source_root/bin/fm-install-shellcheck.sh" "$tool_bin" > "$root/install-shellcheck.log" 2>&1
shellcheck_install_status=$?
set -e
printf '%s\n' "$shellcheck_install_status" > "$root/install-shellcheck.exit"
test "$shellcheck_install_status" -eq 0
set +e
"$source_root/bin/fm-install-actionlint.sh" "$tool_bin" > "$root/install-actionlint.log" 2>&1
actionlint_install_status=$?
set -e
printf '%s\n' "$actionlint_install_status" > "$root/install-actionlint.exit"
test "$actionlint_install_status" -eq 0

PATH="$tool_bin:$PATH" shellcheck --version > "$root/shellcheck-version.txt"
PATH="$tool_bin:$PATH" actionlint -version > "$root/actionlint-version.txt" 2>&1
sha256sum "$tool_bin/shellcheck" "$tool_bin/actionlint" > "$root/tool-binary-sha256.txt"
{
  printf 'shellcheck_path=%s\n' "$(PATH="$tool_bin:$PATH" command -v shellcheck)"
  printf 'actionlint_path=%s\n' "$(PATH="$tool_bin:$PATH" command -v actionlint)"
  printf 'workflow_path_before_github_path=%s\n' "$PATH"
} > "$root/tool-paths.txt"
printf '%s\n' "$tool_bin" >> "$GITHUB_PATH"
set_phase preflight-complete
printf 'preflight=pass\n' > "$root/preflight-result.txt"
