---
name: project-rules
description: >-
  Agent-only procedure for project-rules alarms and refusals.
  Use on any `project-rules: <task-id> <alarm>` check wake, when a spawn is refused by project rules admission, and when `fm-pr-check.sh` or `fm-merge-local.sh` refuses a task for an owed receipt or skill read.
  Maps each alarm to what it means for the worker and the one action that resolves it.
user-invocable: false
metadata:
  internal: true
---

# project-rules

`../../../docs/project-rules.md` owns the contract and the alarm meanings; this skill owns what the supervising Firstmate does about each one.
Read the task's record first with `../../../bin/fm-project-rules.sh status <state-dir> <task-id>`; it never prints the receipt codes.

Never quote a receipt row, a part code, or any block text to a worker.
The worker must take those from its own context, or the receipt proves nothing.

## Alarms

| Alarm | Action |
|---|---|
| `stage-unanswered` | Steer the worker to run its `next` command and do what it prints. If it stays open after one steer, treat the worker as stuck and load `stuck-crewmate-recovery`. |
| `no-delivery-evidence` | The worker may not hold the rules. Stop relying on its work since that generation, relaunch it through `../../../bin/fm-control.sh <task-id> relaunch`, and report it if the relaunch alarms again. |
| `native-missing` | The project's declared list claims a file the tool did not load. Relaunch does not fix it; report the rule id to the project's owner as a defect in the list or the tool version. |
| `witness-mismatch` | A compaction was seen by one signal only. Check that the stage for that compaction exists and is answered; if the hook or log format changed, run the live guard and report the tool version. |
| `rules-changed` | A rule file changed under a running session. Relaunch the worker so it is admitted on the new content. |
| `trigger-skipped` | A Codex worker ran a triggering command without the required skill. Steer it to load the skill named in the alarm, and judge the work done in between against that skill before accepting it. |
| `worked-before-admission`, `worked-before-refresh` | A Codex worker acted before answering a stage. Steer it to run `next`, and review what it did in that window. |
| `same-turn-calls` | Informational: note the count beside the task and review that window if the work touched an area with a required skill. |
| `receipt-table-exhausted` | Relaunch the worker; a relaunch renders a new table. |
| `unqualified-version` | Run `../../../tests/fm-project-rules-live-e2e.test.sh` with its opt-in variable on this machine, then relaunch workers that need children. |

## Refusals

A spawn refused by admission launched nothing.
The error names the cause: fix a project-side cause through that project's own delivery path, never by editing its files from here, and retry the spawn.
An unqualified tool and backend pair is resolved by running the live guard, not by changing the backend.

A readiness refusal lists what the worker still owes.
Steer the worker to run `next`; do not mark the task ready around the refusal.
