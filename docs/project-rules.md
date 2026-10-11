# Project rules for workers

A project can declare the instruction files and skills its workers must hold.
Firstmate then delivers them to every Claude and Codex worker it launches in that project, keeps them present after context compaction, and checks each tool's own log to confirm it.
`bin/fm-project-rules.sh` implements this, and its `--help` owns the command syntax.

A project that declares nothing is untouched: no block, no gate, no record, and no alarm.
That is decided in shell before anything else runs, so such a project never depends on `node`.

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
- `required` is optional and holds exactly one trigger: `{"at": "start"}`, `{"before_commands": [...]}` with POSIX extended patterns matched against each line of a command, or `{"on_paths": [...]}` with globs using `*`, `?` and `**`.
- `dispatched_child_types` lists the child agent types the project's skills dispatch. It informs the live guard and grants nothing.

## Admission, before the worker starts

`bin/fm-spawn.sh` admits every ship and scout launch after the task copy is on its final base.
Admission runs `prepare`, then each `resolve`, in the copy with stdin closed, a timeout, and an environment reduced to `HOME`, `PATH`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_ALL`, `TMPDIR`, `CLAUDE_CONFIG_DIR` and `CLAUDE_PLUGINS_DIR`.

The launch is refused, and no agent starts, when:

- the prepare step fails or times out;
- the prepare step or any resolve command changes the copy's working tree, compared by status and by file content with how it was before they ran, so a relaunch of a copy holding the worker's own uncommitted work is still admitted;
- a declared file is missing or is not a readable regular file;
- an inlined file contains an `@path` import line, which a verbatim copy cannot expand;
- the rendered block is larger than 79,000 bytes;
- for Codex, the root `AGENTS.override.md` or `AGENTS.md` exceeds `project_doc_max_bytes` (32 KiB by default), or the user or project Codex config already sets `developer_instructions`;
- the tool is not Claude or Codex, including a raw launch command;
- the tool and backend pair has never passed the live guard on this machine.

A tool version the live guard has not seen still launches, with a notice and a one-time alarm, and its children are denied until it is qualified.

## Trust boundary

The prepare and resolve commands come from the project's own default branch and run under the operator's account, in the launcher's process, before any worker exists.
That is the same trust a worker in that project already has, since it runs the project's code with the operator's account, but it happens earlier and outside the worker.
Register only projects whose default branch you would let a worker run code from.
A declared file may live outside the copy, which is how pinned plugin assets are reached, and its content is given to the worker verbatim.

A project's command patterns are matched under a time limit, and a pattern that runs out of time counts as a match.
While a stage is open on Claude, the only shell command allowed is one simple command, with no shell operators, substitutions or redirections, that runs one of the worker's own Firstmate commands by exact path: this helper's `next`, `ack` and `serve`, the heartbeat, and a decision event.
An environment prefix is accepted only when it names Firstmate's own root, home and state for that task, so it cannot point the script at other code.

## The always-on block

Every rule entry that applies to the tool and is not `native` for it goes into one block, in full and in list order, followed by the skill catalog, the list of required skills with the moment each is due and the command that loads it, and a 64-row receipt table.
Claude receives the block appended to its system prompt, after the worker statement.
Codex receives it as a `developer_instructions` override, and keeps its hook layer off.
Both channels are resent by the tool itself after a compaction, so the block needs no refresh.

Payload bytes are the inlined rule files plus each catalog skill's name and description.
`fm-project-rules.sh size <copy> <tool>` prints the payload and the rendered size, so a project's own budget check can be compared with Firstmate's number.

## Stages and receipts

A stage is `start` or one compaction, `c1`, `c2`, and so on.
It closes when the worker has quoted one randomly chosen unused receipt row, read every skill required at start, and then read its task brief through the helper.

The worker is launched on an admission message alone: its role, and the instruction to run `fm-project-rules.sh next` and do what it prints.
Its task is not in that message.
`next` lists the receipt row and the required reads, and offers the task brief only once those are recorded, so a worker that has not finished reading has no task to start on.
The brief itself carries a short gate that sends the worker back to `next` after any compaction.
Every part but the last ends with a line saying it is not finished and naming the exact next command.
Serving the last part records the read and ends with `RECORDED`, so no step follows it for a worker to miss.

A quoted row shows that the end of the block is in context, or that its text was copied into a summary, so it is supporting evidence only.
The proof is the tool's own log holding the whole block for that generation, compared after removing trailing newlines:

- Claude: a `prompt_snapshot` record in the session transcript.
- Codex: a developer message in the session log, or the `replacement_history` of a compaction that happened inside a turn.

A file declared `native` is checked by content: every significant line must appear, in order, in what the tool recorded as loaded for that generation.
Each stage uses a new row, so after 64 stages the task needs a relaunch, which renders a new table.

## Skills

The catalog is in the block, so a worker knows which skills exist at start and after every compaction.
Reading a skill is the worker's choice unless the list marks it required.

A required skill is read through `fm-project-rules.sh serve`, which delivers the declared body and references in parts of about 10 KB.
Each part but the last ends with the code that requests the next one, so no earlier part can be skipped and one cut short by a tool's output limit breaks the chain.
A read is recorded for the current compaction generation and lapses at the next compaction.

| Trigger | Claude | Codex |
|---|---|---|
| `at: start` | gates the stage | gates the stage |
| `before_commands` | a matching command is refused until the skill is read | a matching command without the read is interrupted at its start, the worker is told what to load, and readiness is blocked until it is corrected |
| `on_paths` | a matching edit is refused until the skill is read | a matching edit without the read is interrupted and the worker is told what to load; also checked at readiness against the work's changed files |

## Compaction

Claude runs Firstmate's `SessionStart` hook, which opens the next stage and tells the worker to run `next`.
While a stage is open, Claude's `PreToolUse` hook refuses every tool except the helper's own commands and file reads.
Any failure inside that hook, such as a locked or unreadable record or a missing `node`, also refuses the call, because Claude treats only exit 2 as a refusal.

Codex has no hooks, so nothing can refuse a tool call.
Each Codex worker instead has a responder: a detached process that reads the worker's session log as it grows and acts within about a second.
`bin/fm-spawn.sh` starts it with the worker, the watcher starts one on every cycle for any Codex task that lacks it, and it ends with the task.
The log it follows is the worker's own session: a sub-agent's session log in the same copy is traced back to the session that spawned it and is never read as the worker's.
A command a Codex sub-agent runs is therefore not read at all, which is why the block tells the worker to run required-skill commands itself.

- On a `compacted` record it interrupts the turn through `bin/fm-control.sh`, then steers the worker through its inbox to run `next`.
- On the start of a command or an edit that matches a declared trigger whose skill is not read in the current generation, it interrupts the turn, then steers the worker to load that skill with the exact command and to run the command again.

A worker that is between turns is steered without an interrupt.
The responder acts on nothing else: only the declared patterns and paths are matched, and a Claude worker keeps its hook and has no responder.
The watcher's scan and the readiness check deliver the same actions when they see them first, so a responder that is not running delays an action and does not lose it.
Calls made in the same turn as a compaction inside that turn are counted and reported.
A project call in a later turn, before the stage is answered, is reported as a violation.

Every command in a Codex tool call is read: JSON arguments, each command of a batched script in any quote style, and a shell action.
When a call holds a command that cannot be read as a literal, such as a variable, the whole call text is inspected as well.
Following Firstmate's own doorbell is not counted as work: a plain `ls`, `cat`, `head`, `tail`, `mv` or `mkdir` whose every path lies inside the task's inbox.
Neither is a call that carries no command, such as a wait.
A helper call written as a template is still a helper call when every other string in that call is a plain word.
A call that starts with Firstmate's own commands and then works, or that finishes reading a skill and then runs its command, is in order when the stage or the read is complete by the time the call's output is logged.

## Children

A child agent does not receive the block.
A Claude general-purpose child loads the same native files as its parent, so anything a child must hold has to be declared `native` for Claude.
Every other Claude child type is refused until the live guard qualifies it for the installed version.
A command or an edit that triggers a required skill is refused inside a Claude child and must be done by the worker itself, because a child never inherits the worker's read.
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
| `rules-changed` | the declaration, a rule file, or a skill body or reference changed on disk after the session was given it; a changed skill also loses its recorded read |
| `scan-error`, `scan-failed` | the tool's own log, or the task's record, could not be read |
| `trigger-skipped` | Codex ran a triggering command without the required skill; when the responder caught it at its start, this is raised only if the worker has not read the skill and run the command again within 180 seconds |
| `worked-before-admission`, `worked-before-refresh` | Codex made a project call before a stage was answered |
| `same-turn-calls` | the count of calls between a compaction inside a turn and its refresh |
| `receipt-table-exhausted` | all 64 rows are used and the task needs a relaunch |
| `unqualified-version` | the installed tool version has not passed the live guard |

The two compaction signals are the hook and the transcript's `compact_boundary` record for Claude, and the `compacted` record and the `ContextCompaction` event for Codex.

## Readiness

`bin/fm-pr-check.sh` and `bin/fm-merge-local.sh` refuse while any of these holds for the task:

- a stage is open;
- the tool's own log does not show the block, or a file declared native, for the current generation;
- a declared source changed on disk, or the tool's own log or the task's record could not be read;
- a violation has been recorded but has not yet reached the supervisor as an alarm;
- a command ran without its required skill in the current generation, and the skill has not since been read and the command run again after it;
- the work changed files under a required skill's paths and that skill is not read in the current generation.

## Qualification (`config/project-rules-qualified`)

The live guard `tests/fm-project-rules-live-e2e.test.sh` merges its rows into this file only when every leg passed for a tool, with 2 sessions per live leg: the start and forced-compaction sessions, the automatic-compaction sessions, the independent coverage check, the prepare rerun, and the refusals.
A leg that was left out or could not be measured qualifies nothing.
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
- A command pattern is matched on the text of a command, so it holds a worker that forgot a skill, not one that disguises the command.
- A command run by a Codex sub-agent is not read.
