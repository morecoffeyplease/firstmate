# Secondmate parent channel

This note records why a secondmate home's captain-facing outcomes are delivered by scripts instead of by the mate model, and which script delivers each one.
`bin/fm-parent-channel-lib.sh` owns the channel contract: where the channel lives, how a line is appended, and the return codes every publisher shares.
[`remote-secondmates.md`](remote-secondmates.md) owns the transport that carries the remote form of the channel back to the parent.

## The problem

A secondmate is a firstmate in its own home, and nobody reads its chat: the captain and the main firstmate see only what is appended to the parent channel.
On 2026-09-02 four outcomes across two mate homes never reached the captain.
The watcher had delivered the parent's request within a minute each time, the mate did the work, and then the mate addressed "captain" in its own chat instead of appending to the channel.
The cause is structural rather than a one-off lapse: the mate can satisfy the [address rule in `AGENTS.md`](../AGENTS.md#firstmate) in local chat while missing the charter's later return-channel instruction.
The captain's framing of the requirement was: "the root problem is not specific to PRs, right? it looks like any message or outcomes from second mates can miss. we need to make sure our fixes are addressing this in a principled, fundamental way, not surgically treating the symptoms of just this PR update miss."
A PR-ready report was the observed symptom, but a finding, a decision, a blocker, and a failure all fail the same way, because every one of them depended on the mate model remembering to write one line.

The design goal is therefore: the parent channel must not depend on the model remembering to write to it.

Project Firstmates add a bounded routing hop: worker terminal, PR-ready, and merged outcomes stay in the project home's own task ledgers and are absorbed rather than appended to the root Firstmate's persistent-supervisor status file.
Every absorbed worker outcome is also appended idempotently to the project home's `state/project-outcomes.log`, and the publisher returns the distinct status 5 only after that durable local write succeeds.
Only calls through the correlated-answer, captain-hold, project-summary, project-decision, project-blocker, and project-milestone publishers may cross that boundary, while the project Firstmate remains responsible for classifying its own workers locally.
Raw child outcomes always stay local, regardless of marker-like text in a note.
`bin/fm-parent-channel-lib.sh` owns the typed publisher contract, and `tests/fm-inactive-reconcile.test.sh` covers both forged markers and permitted upward summaries.

## The design

The delivery rule has one sentence: the scripts report facts, the mate reports judgement.
Every captain-facing outcome that leaves durable evidence in the mate home is published on the channel by the script that records that evidence, at record time or on the next supervision poll, and the charter reserves the mate's own appends for judgement.

| Outcome | Durable evidence in the mate home | Published by |
|---|---|---|
| Ship child PR ready | the child's `done: PR <url> ...` line; `pr=` in the child's record once registered | `bin/fm-inactive-reconcile.sh` on the next poll with the child's line; `bin/fm-pr-check.sh` at registration with the canonical URL |
| Scout child findings | the child's `done:` line plus `data/<child>/report.md` | `bin/fm-inactive-reconcile.sh` on the next poll, with the report pointer |
| Child failed | the child's `failed:` line | `bin/fm-inactive-reconcile.sh` on the next poll |
| Child decision escalated to the captain | the task held for the captain in the mate backlog | `bin/fm-captain-hold.sh hold`, and its answer by `answer` |
| A project implementation decision answered by the captain | the authority home's durable PID record and guarded owner-task transition | `bin/fm-product-decision.sh`, through the typed project-decision publisher |
| PR merged | the merge poll or the mate's own merge | `bin/fm-merge-outcome-lib.sh` |
| Child leaving the home | its final ledger line | `bin/fm-teardown.sh`, which refuses to remove the child while that line is undelivered |
| Child ended silently | terminal current state with a silent ledger | the existing inactive-outcome scan in `bin/fm-inactive-reconcile.sh` |
| Answer to a marked request | a correlated line guarded by the pending-reply record | `bin/fm-secondmate-report.sh`, which resolves the parent channel from the mate home; the pending-reply guard repairs a line stranded in the local mate's same-basename status file before recovery or escalation |
| An outcome that exists only in the mate's reasoning | none | the charter and the `AGENTS.md` carve-outs only |

The ledger delivery reads files only: it calls no harness, no forge, and no current-state reader, so it is identical for every harness and runtime backend.
Each delivery is keyed with the first eight hexadecimal characters of its receipt fingerprint and appended at most once by exact line, and the ledger path reuses the inactive scan's per-fingerprint receipts, so a replayed poll or restart cannot deliver an event twice while a genuinely new terminal event is delivered again.
A duplicate line is harmless and a missed one is not, so the mate may still append its own judgement about a delivered outcome, and the parent reads the script's line as the fact and the mate's line as commentary.
For marked replies, the report helper accepts no caller-selected destination and uses the channel resolver for both local and remote homes; its script header owns the exact invocation contract.
The pending-reply guard may restate only the correlated line from a local mate's `state/<mate-id>.status` onto the parent channel, which repairs the common parent-home versus mate-home mixup without accepting arbitrary mate-home sightings as acknowledgement.
Other correlated mate-home status lines remain wrong-home evidence, while a remote home's routed `state/parent-replies.status` is already the parent channel and is not classified as wrong-home.
A missed-reply escalation includes the complete first sighting path and line number in readable shell-escaped form.

## Delivery-confirmation absorb

The channel above is deliberately every append: today every line a mate writes to `state/<id>.status` wakes the parent, because the watcher cannot otherwise tell a routed reply or a newly raised decision from routine chatter (`bin/fm-classify-lib.sh`'s `signal_crew_provably_working`).
During the ButterTrip Home campaign roughly half of the parent's wake-handling turns were a mate confirming it had carried out an instruction the parent had just sent - "relayed to Surfaces", "dispatched ..." - with nothing left for the parent to act on, each still costing a full drain, reconcile, and acknowledge cycle (issue #18).
A mate marks exactly that case, and only that case, by tagging its `done:` line `done [confirmation]: {what you delivered}` (`bin/fm-brief.sh`'s charter text owns the exact wording a mate is told).
`bin/fm-classify-lib.sh`'s `status_is_delivery_confirmation` recognizes the tag, but only when the line's own text carries no outcome or decision content of its own - none of the legacy captain-relevance tokens (`done:`, `needs-decision:`, `blocked:`, `failed:`, `PR ready`, `checks green`, `ready in branch`, `merged`) and no bare URL scheme, so a mate cannot smuggle a real outcome, a PR or merge link, or an embedded `needs-decision:`/`blocked:`/`failed:` line past the tag (PR #27 review finding P1).
That predicate is deliberately NOT wired into the shared, kind-agnostic `status_is_captain_relevant`: a tagged `done:` line stays fully captain-relevant there, exactly like an untagged one, for every task kind.
The absorb happens one layer up, in `bin/fm-watch.sh`, and only for a verified `kind=secondmate` status file whose entire unseen span is clean confirmations (`signal_files_actionable`'s downgrade and `signal_secondmate_confirmation_files`, both built on the same `_signal_secondmate_confirmation_only` check) - an ordinary ship or scout crewmate's tagged `done:` line is unaffected even while provably working (PR #27 review finding P2).
Every other verb, an untagged `done:`, and a span mixing a confirmation with real content are unaffected and keep surfacing exactly as before.
The tag has no effect on `needs-decision:`, `blocked:`, or `failed:`, so a mate that mistags a real outcome still wakes the parent rather than silently dropping it.
The line still lands in `state/<id>.status` and reads back in the fleet-state digest tail; only the immediate wake is skipped.
A confirmation bundled into the same poll as that task's own bare turn-end still surfaces: the turn-end keeps its ordinary provably-working proof requirement, on the same conservative footing as any other no-verb turn-end, so a confirmation appended right at the end of a mate's turn does not by itself exempt that turn-end from the swallowed-finish guard (PR #27 review finding P2, decided rather than left ambiguous).

## What is deliberately not built

- No mirror of the mate's chat: chat can mix outcomes with other conversation, so choosing which sentence is an outcome would itself be model behavior, and every harness exposes turn text differently.
- No threshold escalation of a child's open decision or blocker: a decision the mate escalates is a captain hold, which is published; a decision the mate neither answers nor escalates is a supervision-quality question, separable from channel delivery.
- No second watcher or standalone scanner: a lightweight ledger pass runs inside the existing inactive-outcome command on every watcher poll and reuses its receipts and upstream append.
- No orphan lifecycle: teardown refuses instead of removing an undelivered outcome, the same way it refuses on other unlanded conditions.

## Regression coverage

`tests/fm-inactive-reconcile.test.sh` covers the ledger delivery against real ledgers with no harness: immediate done and failed delivery with note, PR, mode, posture, and report pointer, once-only delivery across polls, a line still being appended, the remote route, the yield of the inactive path to a terminal ledger, and the real watcher poll driving it.
`tests/fm-captain-hold-lifecycle.test.sh` covers a mate home publishing a hold, its answer, and a distinct occurrence on re-hold, and a main home publishing nothing.
`tests/fm-pr-merge.test.sh` covers the PR-ready line at registration and the merge outcome's upward report.
`tests/fm-teardown.test.sh` covers teardown delivering a child's final line and refusing when the channel cannot be written.
`tests/fm-brief.test.sh` pins the charter's channel rule.
`tests/fm-pending-reply.test.sh` covers helper-selected local routing, remote-channel classification, same-basename restatement before false escalation, readable wrong-home diagnostics, and the rule that arbitrary mate-home sightings never acknowledge a reply.
`tests/fm-watch-triage.test.sh` covers the `[confirmation]` tag: an untagged `done:`, a tagged `needs-decision:`/`blocked:`/`failed:`, and a tagged `done:` whose own text carries a merge URL or an embedded decision all still wake; a clean tagged `done:` stays captain-relevant for the shared kind-agnostic classifier but is absorbed only for a verified secondmate; a real watcher poll absorbs a confirmation-only secondmate status append even while NOT provably working, still surfaces an ordinary crewmate's tagged `done:` even while provably working (the P2 leak the review caught), and still surfaces any mixed batch or outcome-carrying line in full.

## Live verification

[`verification/secondmate-parent-channel.md`](verification/secondmate-parent-channel.md) records the dated live run: real tmux panes, both real watchers re-armed after each wake, and no model, with every delivered parent line and the parent wake it produced.
