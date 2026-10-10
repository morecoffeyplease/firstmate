# Project rules verification

Repeatable evidence for what Claude and Codex load, keep and record, which the project-rules delivery in [`../project-rules.md`](../project-rules.md) depends on.
The live guard `tests/fm-project-rules-live-e2e.test.sh` is the command that refreshes this record; run it after a tool update.

Date: 2026-10-10.
Versions: Claude Code 2.1.296, codex-cli 0.161.0, Herdr 0.9.3, macOS.

## What each tool loads by itself

Method: nine canary lines committed in a throwaway clone of a real project, one per instruction surface, then a worker of each tool launched by `bin/fm-spawn.sh --scout --backend herdr` into an isolated Herdr lab.
Each worker listed the canaries in its context before any tool use, after touching one area folder, and after `/compact`; the lists were checked against the Codex session log and a Claude `InstructionsLoaded` hook.

| Surface | Codex start | Codex after compaction | Claude start | Claude after touching the area | Claude after compaction |
|---|---|---|---|---|---|
| Root `AGENTS.md` | yes | yes | no | no | no |
| Root `CLAUDE.md` and `.claude/CLAUDE.md` | no | no | yes | yes | yes |
| Nested area `CLAUDE.md` | no | no | no | yes | not reloaded as a rule |
| Nested `AGENTS.md` | no | no | no | no | no |
| Unscoped `.claude/rules` file | no | no | yes | yes | yes |
| Path-scoped `.claude/rules` file | no | no | no | yes | no |
| Skill listing | yes | yes | yes | yes | no |
| Launch brief, literal | yes | yes | yes | yes | summarized only |

Codex truncated a 46,386-byte `AGENTS.md` without any message: canaries at byte 0 and 21,543 arrived and the one past 32 KiB did not.

## The two delivery channels

A block of 79,029 bytes containing backticks, both quote characters and non-ASCII text was passed with `claude --append-system-prompt "$(cat block)"` and with `codex -c "developer_instructions=$(jq -Rs . < block)"`.
On both tools a line from its interior and a receipt row never typed in the session were quoted correctly at start and after `/compact`.

```console
$ # Codex session log, records holding the whole block
3 developer message holds block: True
18 compacted; replacement_history holds block: False
25 developer message holds block: True
$ # Claude transcript, records holding the block without its final newline
27 prompt_snapshot: True
35 prompt_snapshot: True
51 compact_boundary
83 prompt_snapshot: True
91 prompt_snapshot: True
```

With `-c model_auto_compact_token_limit=45000` Codex compacted inside a turn, ran three more tool calls in that turn, and still quoted an unused row.
That `compacted` record carried the whole block in `replacement_history`, and no developer message followed before the turn ended.
Each `compacted` record was followed by an `item_completed` event of type `ContextCompaction`.

## Hooks

Codex with `--disable hooks` did not run a `SessionStart` hook supplied with `-c hooks.SessionStart=[...]`.
Without the flag the same launch stopped at "Hooks need review, 1 hook is new or changed", and Escape skipped it with the hook unrun.
`-c 'projects."<path>".trust_level="trusted"'` did not suppress Codex's folder-trust prompt.

Claude ran a `SessionStart` hook with matcher `compact` supplied through `--settings`; its payload carried `source: "compact"` and `transcript_path`, and its output was visible to the model.

## Children

The parent's prompt gave no canary to either child; the results below are from the children's own transcripts.

| Child | Root instruction files | Area file on touch | Appended block |
|---|---|---|---|
| Claude general-purpose | yes | yes | no |
| Claude Explore | no | yes | no |
| Codex sub-agent | root `AGENTS.md` yes | not applicable | yes |

## Live guard

`tests/fm-project-rules-live-e2e.test.sh` ran on 2026-10-10 against its built-in fixture project, with each worker launched by the real `bin/fm-spawn.sh` into an isolated Herdr lab on a fresh pool copy.
Claude ran as `--model sonnet --effort low` and Codex as its default model at low effort; a project's own dispatch models are exercised when the guard is pointed at that project.

```console
$ FM_PROJECT_RULES_LIVE_E2E=1 FM_PROJECT_RULES_LIVE_RUNS=2 tests/fm-project-rules-live-e2e.test.sh
ok - claude 2.1.296 stage A: report and log agree (block 3229 bytes, generation 0)
ok - claude 2.1.296 stage B: report and log agree (block 3229 bytes, generation 1)
ok - claude 2.1.296 stage C: report and log agree (block 3269 bytes, generation 1)
ok - claude 2.1.296 stage D: report and log agree (block 3269 bytes, generation 2)
ok - codex 0.161.0 stage A: report and log agree (block 3435 bytes, generation 0)
ok - codex 0.161.0 stage B: report and log agree (block 3435 bytes, generation 1)
ok - codex 0.161.0 stage C: report and log agree (block 3471 bytes, generation 1)
ok - codex 0.161.0 stage D: report and log agree (block 3471 bytes, generation 2)
```

Each of those stages passed in 2 sessions per tool.
Stage A is admission, B follows a forced compaction, C follows an automatic compaction in a second session, and D follows a forced compaction in that same session.
At every stage the worker reported each canary it should hold, a child held the native files (and on Claude the area rule after touching that folder), the triggering command had its skill read in that generation, an Explore child was refused on Claude, and the scan of the tool's own log raised no alarm.
The same run passed the independent coverage check, the prepare rerun, and the refusals for both tools.
The Claude and Codex results come from two invocations on the same engine code, because the first stopped at a fault in the guard's own stage message after Claude had finished.

Automatic compaction was produced by lowering the tool's own limit on a replay of the launch `fm-spawn` builds: `CLAUDE_CODE_AUTO_COMPACT_WINDOW=160000` for Claude and `-c model_auto_compact_token_limit=60000` for Codex.
An earlier Claude session under the same setting compacted twice by itself and then once on request, and passed at generation 3.

## Not yet measured

A child's own compaction, plugin-defined Claude child types, and block sizes above 79 KB.
The live guard has not yet run against ButterTrip's own declared list, which is the acceptance run that lifts the pause.

## Suites

```console
$ bash tests/fm-project-rules.test.sh | tail -1
# all fm-project-rules tests passed
```

`tests/fm-project-rules.test.sh` (31 cases) covers admission refusals, the block, receipts, chained reads, Claude hook verdicts, every Codex command shape, both log formats, drift, readiness, failures inside the hook and the scan, the settings merge, and three cases that drive the real `bin/fm-spawn.sh` against a fake pane.
It touches no real tool.
