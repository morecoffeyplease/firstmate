#!/usr/bin/env bash
set -Eeuo pipefail

recipe_root=${LAB_RECIPE_ROOT:?}
source_git_root=${SOURCE_GIT_ROOT:?}
scenario=${LAB_SCENARIO:?}
expected_tree=fedd0f74ae63ce5963940a105b744497e630259b
root=${RUNNER_TEMP:?}/fm-capacity/$scenario
source_root=$root/source
tool_bin=$RUNNER_TEMP/bin
mkdir -p "$root" "$tool_bin"
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
test "${RUNNER_OS:-}" = Linux
test "${RUNNER_ARCH:-}" = X64

record_command() {
  local name=$1
  shift
  local status
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
printf 'preflight=pass\n' > "$root/preflight-result.txt"
