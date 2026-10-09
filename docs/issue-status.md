# Local Issues view

`bin/fm-issues.sh` starts the local Firstmate Issues table and opens it in the default browser.
The service binds only to IPv4 loopback and exits when its terminal process is stopped.

Use `bin/fm-issues.sh --project <registered-project> --terminal` for a terminal table or add `--json` for the same deterministic projection as JSON.
The project name selects a registered clone, then the launcher validates that clone's Git origin before using its canonical GitHub repository identity.
The initial project is the first registered project when no `--project` is supplied.

The projection combines the canonical fleet snapshot with a separately cached, paginated issue catalog.
Open and closed issues appear even when no local task is linked, and unlinked local tasks appear in their own section.
Validated secondmate home summaries also contribute bounded queued, in-flight, completed, metadata-only, and unlinked task records, including tasks whose current activity is unknown.
Only issue links whose canonical repository URL matches the selected registered clone are attached to its issue rows; omitted or unvalidated remote summaries are shown as partial coverage.
The catalog revalidates at most once per hour by default, while the page reads the coalesced projection cache every 15 seconds and pauses while hidden.
The projection cache is rebuilt at most once per minute per project unless an explicit refresh is requested.
Coverage reports the number of cached issue identities, the identities observed in the latest read, and the repository total only when pagination completed.
An interrupted page read keeps visible observations, marks coverage partial, and leaves the total unknown.
An explicit Refresh remains coalesced and cannot run a full fleet snapshot more than once per minute.
When collection fails, the last good snapshot is marked stale with the latest error; a first-load failure is shown as unavailable.

The time column distinguishes forge event times, bounded local observation times, and unknown history.
Times are stored as UTC epochs and displayed in America/Los_Angeles with PST or PDT.
Polling does not advance a status-change time.
Owner scripts record typed task lifecycle events in `data/<task>/events.jsonl`, including blocker changes made through `bin/fm-tasks-axi.sh` and status changes first seen by the watcher.

The verification details show CI evidence from the forge and local focused, full, and verify receipts from `bin/fm-lane-run.sh`.
Local lanes without task launch instrumentation or a wrapped run remain not instrumented or not run, and unwrapped commands are invisible.
Configure full and verify argument arrays in `config/project-lanes.json`; see [product verification lanes](configuration.md#product-verification-lanes-configproject-lanesjson).
Source acceptance and canonical journey evidence stay unknown until an identified authority records typed evidence.

**Request Manual Update** durably records one request for every selected registered project and adds a typed note to the main-home inbox.
The browser handler does not route or send messages.
At the inbox wake, the main-home owner resolves each project through the validated registry and sends `request=<request-id> project=<project> Request Manual Update. At composition start, read the current structured projection and use its fingerprint, observation time, and evidence as the summary basis. Please provide a concise written project summary through the correlated parent status channel.` through `fm-send`.
After the send creates its durable pending-reply record, the main-home owner records its target and correlation with `fm-issues.sh summary route <request-id> <project> --target <task-id> --correlation <correlation-id>`; this command verifies the correlation against that record.
Requests without a matching registered secondmate remain assigned to the main-home inbox.
Each project's route, correlation, result, and failure reason is shown separately, requests deduplicate independently, and requests expire after 30 minutes.
When composition begins, the summary author reads that project's current fingerprint and observation time from `fm-issues.sh --project <project> --json`.
The author writes the text with `fm-issues.sh summary put`; the command compares the supplied basis fingerprint with current status and marks a changed basis outdated immediately.
A later meaningful project fingerprint change also hides or collapses the summary while retaining its labeled historical text.
Each summary records its author, basis fingerprint, basis observation time, written time, repository, catalog check, and snapshot observation used for the basis comparison, and its text is never parsed into automatic status fields.
Requests expose pending, written, outdated, failed, unavailable, and expired states together with supervisor availability.
Routine collection and rendering never call an AI model.

The browser's one write endpoint requires its per-launch token and exact loopback Origin and Host.
The endpoint accepts only a selected list of currently registered project names.
