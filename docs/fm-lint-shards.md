# Full-analysis ShellCheck shards

CI runs the canonical ShellCheck inventory as four deterministic shards on four isolated `ubuntu-latest` runners.
Each runner uses one worker and starts one ShellCheck process per root, with source following and extended analysis enabled.
The `Lint` aggregate check requires the matrix result to be successful, and the shard matrix uses `fail-fast: false` so every selected partition reports its result.
Backend purity and pinned actionlint still run through `bin/fm-lint.sh` on every shard.
The lint timeout remains 25 minutes.

The two exit-143 attempts in run [37974779912](https://github.com/morecoffeyplease/firstmate/actions/runs/37974779912) stopped after 12m09s and 12m33s, before the configured timeout.
The job records failure rather than timeout or cancellation, and its log does not identify who sent SIGTERM.
Runner memory pressure was measured during a later successful run, but the cause of the historical SIGTERM remains unknown.
Separate runners reduce concurrent memory demand between roots while retaining the same analysis for every root.
This does not lower the peak memory required by the largest individual root.

## Partition owner and local behavior

`bin/fm-lint.sh` owns canonical inventory enumeration, byte-weighted assignment, root invocation, diagnostic replay, and exit selection.
CI invokes `bin/fm-lint.sh --shard N/4` for each value from `1/4` through `4/4`.
Shard selection always enumerates the full canonical CI inventory, independent of local branch changes.
The option rejects explicit paths, `--fast`, `--jobs`, `--list-files`, invalid indices, and duplicate selections.
A shard that receives no roots fails with a diagnostic.

No-argument local invocation keeps changed-file selection, main or merge-base-less full fallback, `--fast`, explicit paths, `--list-files`, and the existing `--jobs 1|2` behavior.
Both Codex and Claude use the same `bin/fm-lint.sh` owner and workflow invocation.

## Reproduce the prior profile estimate

The measurement fixture `fm-lint-shard-measurements.tsv` contains one row for each of the 415 canonical roots at analyzed merge SHA `e8ab41d0a632bad4ce92967ccce9f26059d5ca28`.
It records each root's Git blob byte size and observed wall time and maximum RSS from successful full-analysis run [37985969114](https://github.com/morecoffeyplease/firstmate/actions/runs/37985969114).
Run `python3 docs/fm-lint-shard-simulation.py` to reproduce the deterministic largest-byte-first, lowest-current-byte-load assignment used by the owner.
The script rejects a mismatched merge SHA, duplicate root, wrong inventory size, or byte total that differs from the analyzed tree.

The measured root-time simulation predicts shard durations of 8m44s, 10m46s, 8m29s, and 6m48s.
These are estimates from roots measured under two-worker contention, not promises for one-worker hosted runs.
The summed root work is 34m48s, approximately 1.82 times the 19m07s occupied time of the two-worker profile before repeated checkout, installs, auxiliary checks, queueing, and runner rounding.
The four-runner design consumes three additional concurrent job slots, and its actual dollar cost depends on the repository's Actions entitlement.

## Hosted validation evidence

The current branch validation records two full same-head hosted rounds in the per-shard `fm-lint-runner-profile-*` artifacts.
Each artifact includes the checkout SHA, runner name and platform, shard selection, selected root count, per-root invocation and timing rows, available-memory samples, sampled ShellCheck process counts, runner memory before and after, and final exit status.
Validation must show four successful shard results in each round, one active ShellCheck process per runner, complete canonical root coverage, the largest roots completing, substantial wall-time margin below 25 minutes, and measured available-memory headroom.
Runner names may differ between rounds, but every artifact must identify its own runner.

## Behavioral acceptance cases

| Case | Required result |
| --- | --- |
| Four production shards | Their executed root multisets equal the canonical inventory exactly once. |
| Added or removed canonical root | The next shard manifests include the added root or omit the removed root. |
| Equal-byte roots and repeated assignment | Original canonical order breaks ties, and repeated runs assign roots identically. |
| Invalid, missing, out-of-range, or duplicate shard | The command exits with usage error before ShellCheck starts. |
| Unsupported option combination | Shard mode rejects fast mode, explicit paths, worker override, and list-files mode. |
| Empty selected production shard | The command fails with the selected shard in its diagnostic. |
| Sourced library and consuming root on different shards | Pinned ShellCheck follows the library in the consumer's shard, and the library's own finding remains attributed and failing in its assigned shard. |
| Full-analysis invocation | Every root receives one process with `--norc`, `--external-sources`, default severity, and extended analysis; no local cross-file exclusions are applied. |
| Finding in an early root | Later roots still run, findings retain root attribution, and a nonzero ShellCheck result fails that shard. |
| Missing worker result or signal | The shard fails, and worker temporary state is cleaned up. |
| No local changed shell roots | Backend purity and pinned actionlint still run through the no-argument local path. |
