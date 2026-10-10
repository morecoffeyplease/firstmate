# Project rules for workers

A project can declare the instruction files and skills its workers must hold.
Firstmate then delivers them to every Claude and Codex worker it launches in that project, keeps them present after context compaction, and checks each tool's own log to confirm it.
`bin/fm-project-rules.sh` implements this, and its `--help` owns the command syntax.

A project that declares nothing is untouched: no block, no gate, no record, and no alarm.

## Why it exists

Neither tool loads everything a project calls its rules.
Codex loads only the root `AGENTS.md`, and silently truncates a project instruction chain above 32 KiB.
Claude loads the root `CLAUDE.md` files and unscoped `.claude/rules`, loads nested files only when it touches their folder, and drops nested files, path-scoped rules and its skill listing at compaction.
Neither loads rule files that ship inside a Claude plugin.
`docs/verification/project-rules.md` holds the measurements.

## The declared list

The project tracks `.agents/project-rules.json` at its repository root.
A file that does not parse, or that carries an unknown field, refuses the launch, so a typo cannot silently drop a rule.

```json
{
  "version": 1,
  "budget_bytes": 64000,
  "prepare": { "argv": ["bash", "scripts/agents-prepare.sh"], "timeout_s": 180 },
  "rules": [
    { "id": "core", "path": "AGENTS.md", "tools": ["codex"], "native": ["codex"] },
    { "id": "map", "path": "CLAUDE.md", "native": ["claude"] },
    { "id": "area-web", "path": "apps/web/CLAUDE.md" },
    { "id": "rule-mobile", "resolve": ["node", "tools/resolve.mjs", "rules/mobile.md"], "tools": ["codex"] }
  ],
  "skills": [
    { "name": "workflow", "description": "How work proceeds here.", "invocation": "manual",
      "body": { "path": "skills/workflow/SKILL.md" }, "required": { "at": "start" } },
    { "name": "local-tests", "description": "How to run product tests.", "invocation": "model",
      "body": { "path": "skills/local-tests/SKILL.md" },
      "references": [ { "path": "skills/local-tests/reference.md" } ],
      "required": { "before_commands": ["(^|[;&| ])bun run test( |$)"] } },
    { "name": "query-audit", "description": "Query cost audit.", "invocation": "manual",
      "body": { "path": "skills/query-audit/SKILL.md" }, "required": { "on_paths": ["apps/api/src/server/**"] } }
  ],
  "dispatched_child_types": ["general-purpose"]
}
```

- `version` must be `1`.
- `budget_bytes` is the payload budget the project's own check enforces; Firstmate only reports it.
- `prepare` is optional: an argv and a timeout from 1 to 900 seconds.
- `rules` is an ordered, non-empty array. Each entry has a unique `id` and exactly one of `path` (repo-relative) or `resolve` (an argv that prints one absolute path).
- `tools` limits an entry to `claude`, `codex`, or both; absent means both.
- `native` names the tools that load the file by themselves at start and after compaction.
- `skills` is the catalog. `invocation` is `model` when the tool may invoke the skill on its own and `manual` otherwise. `body` and each `references` entry is a `path` or a `resolve`.
- `required` is optional and holds exactly one trigger: `{"at": "start"}`, `{"before_commands": [...]}` with POSIX extended patterns matched against a whole command line, or `{"on_paths": [...]}` with globs using `*`, `?` and `**`.
- `dispatched_child_types` lists the child agent types the project's skills dispatch. It informs the live guard and grants nothing.

## Admission, before the worker starts

`bin/fm-spawn.sh` admits every ship and scout launch after the task copy is on its final base.
Admission runs `prepare`, then each `resolve`, in the copy with stdin closed, a timeout, and an environment reduced to `HOME`, `PATH`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_ALL`, `TMPDIR`, `CLAUDE_CONFIG_DIR` and `CLAUDE_PLUGINS_DIR`.

The launch is refused, and no agent starts, when:

- the prepare step fails, times out, or leaves the copy with uncommitted changes;
- a declared file is missing or is not a readable regular file;
- an inlined file contains an `@path` import line, which a verbatim copy cannot expand;
- the rendered block is larger than 79,000 bytes;
- for Codex, the root `AGENTS.override.md` or `AGENTS.md` exceeds `project_doc_max_bytes` (32 KiB by default), or the user or project Codex config already sets `developer_instructions`;
- the tool is not Claude or Codex, including a raw launch command;
- the tool and backend pair has never passed the live guard on this machine.

A tool version the live guard has not seen still launches, with a notice and a one-time alarm, and its children are denied until it is qualified.

## The always-on block

Every rule entry that applies to the tool and is not `native` for it goes into one block, in full and in list order, followed by the skill catalog and a 64-row receipt table.
Claude receives the block appended to its system prompt, after the worker statement.
Codex receives it as a `developer_instructions` override, and keeps its hook layer off.
Both channels are resent by the tool itself after a compaction, so the block needs no refresh.

Payload bytes are the inlined rule files plus each catalog skill's name and description.
`fm-project-rules.sh size <copy> <tool>` prints the payload and the rendered size, so a project's own budget check can be compared with Firstmate's number.

## Stages and receipts

A stage is `start` or one compaction, `c1`, `c2`, and so on.
It closes when the worker has quoted one randomly chosen unused receipt row, read its launch brief again through the helper, and read every skill required at start.
The launch brief carries a short gate that points the worker at `fm-project-rules.sh next`, which prints exactly what is owed.

A quoted row shows that the end of the block is in context, or that its text was copied into a summary, so it is supporting evidence only.
The proof is the tool's own log holding the whole block for that generation, compared after removing trailing newlines:

- Claude: a `prompt_snapshot` record in the session transcript.
- Codex: a developer message in the session log, or the `replacement_history` of a compaction that happened inside a turn.

A file declared `native` is checked by content: every significant line must appear, in order, in what the tool recorded as loaded for that generation.
Each stage uses a new row, so after 64 stages the task needs a relaunch, which renders a new table.

## Skills

The catalog is in the block, so a worker knows which skills exist at start and after every compaction.
Reading a skill is the worker's choice unless the list marks it required.

A required skill is read through `fm-project-rules.sh serve`, which delivers the declared body and references in parts of about 6 KB.
Each part ends with the code that requests the next one, so no part can be skipped and a part cut short by a tool's output limit breaks the chain.
A read is recorded for the current compaction generation and lapses at the next compaction.

| Trigger | Claude | Codex |
|---|---|---|
| `at: start` | gates the stage | gates the stage |
| `before_commands` | a matching command is refused until the skill is read | a matching command without the read is reported and blocks readiness |
| `on_paths` | a matching edit is refused until the skill is read | checked at readiness against the work's changed files |

## Compaction

Claude runs Firstmate's `SessionStart` hook, which opens the next stage and tells the worker to run `next`.
While a stage is open, Claude's `PreToolUse` hook refuses every tool except the helper's own commands and file reads.

Codex has no hooks, so nothing can refuse a tool call.
A compaction is detected from the `compacted` record in the session log, at each turn end and on the watcher's cadence.
Calls made in the same turn as a compaction inside that turn are counted and reported.
A project call in a later turn, before the stage is answered, is reported as a violation.

## Children

A child agent does not receive the block.
A Claude general-purpose child loads the same native files as its parent, so anything a child must hold has to be declared `native` for Claude.
Every other Claude child type is refused until the live guard qualifies it for the installed version.
A command that triggers a required skill is refused inside a Claude child and must run in the worker itself.
A Codex child receives the block and the root `AGENTS.md`.
A child's own compaction is not measured.

## Alarms

`fm-project-rules.sh scan` runs on the watcher's slow-check cadence and prints one line per new episode, which reaches Firstmate as a `check` wake.

| Alarm | Meaning |
|---|---|
| `stage-unanswered` | a stage has been open longer than 900 seconds |
| `no-delivery-evidence` | the tool's own log does not hold the block for a generation |
| `native-missing` | a file declared native is not in what the tool recorded as loaded |
| `witness-mismatch` | the two compaction signals for the tool disagree |
| `rules-changed` | a declared file changed on disk after the session was given it |
| `trigger-skipped` | Codex ran a triggering command without the required skill |
| `worked-before-admission`, `worked-before-refresh` | Codex made a project call before a stage was answered |
| `same-turn-calls` | the count of calls between a compaction inside a turn and its refresh |
| `receipt-table-exhausted` | all 64 rows are used and the task needs a relaunch |
| `unqualified-version` | the installed tool version has not passed the live guard |

The two compaction signals are the hook and the transcript's `compact_boundary` record for Claude, and the `compacted` record and the `ContextCompaction` event for Codex.

## Readiness

`bin/fm-pr-check.sh` and `bin/fm-merge-local.sh` refuse while the task's current generation has an open stage, or a required skill whose trigger fired or whose paths the work touched is not read.

## Qualification (`config/project-rules-qualified`)

The live guard `tests/fm-project-rules-live-e2e.test.sh` writes this file when it passes.
It is local, gitignored, and inherited by secondmate homes.

```text
tool claude 2.1.296 herdr
tool codex 0.161.0 herdr
child claude 2.1.296 general-purpose
```

A `tool` line qualifies a tool, version and backend; a `child` line qualifies one Claude child type for one version.
Run the guard again after a tool update.

## Limits

- Only Claude and Codex are covered. A project with a declared list refuses every other tool.
- Supervisors are not workers and receive nothing here.
- The operator's own global instruction files are not part of a project's rules.
- Codex workers run with hooks off, so a project's own Codex hooks do not run for them.
