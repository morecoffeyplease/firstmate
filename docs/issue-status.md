# Local Issues view

`bin/fm-issues.sh` starts the local Firstmate Issues table and opens it in the default browser.
The service binds only to IPv4 loopback and exits when its terminal process is stopped.

Use `bin/fm-issues.sh --project <registered-project> --terminal` for a terminal table or add `--json` for the same deterministic projection as JSON.
The project name selects a registered clone, then the launcher validates that clone's Git origin before using its canonical GitHub repository identity.
The initial project is the first registered project when no `--project` is supplied.

The projection combines the canonical fleet snapshot with a separately cached, paginated issue catalog.
Open and closed issues appear even when no local task is linked, and unlinked local tasks appear in their own section.
The catalog revalidates at most once per hour by default, while the page refreshes its cached projection every 15 seconds and pauses while hidden.
An explicit Refresh remains coalesced and cannot run a full fleet snapshot more than once per minute.
When collection fails, the last good snapshot is marked stale with the latest error; a first-load failure is shown as unavailable.

The time column distinguishes forge event times, bounded local observation times, and unknown history.
Times are stored as UTC epochs and displayed in America/Los_Angeles with PST or PDT.
Polling does not advance a status-change time.

The verification details show CI evidence from the forge and local focused, full, and verify receipts from `bin/fm-lane-run.sh`.
Local lanes without task launch instrumentation or a wrapped run remain not instrumented or not run, and unwrapped commands are invisible.
Configure full and verify argument arrays in `config/project-lanes.json`; see [product verification lanes](configuration.md#product-verification-lanes-configproject-lanesjson).
Source acceptance and canonical journey evidence stay unknown until an identified authority records typed evidence.

**Request Manual Update** queues a durable request through the home inbox for every registered project.
Firstmate authors each optional written summary from the structured issue projection and records the fingerprint it read before composition.
A summary whose fingerprint differs from current status is labeled outdated and kept as historical text.
Requests expose pending, written, outdated, failed, unavailable, and expired states together with supervisor availability.
Routine collection and rendering never call an AI model.

The browser's one write endpoint requires its per-launch token and exact loopback Origin and Host.
The endpoint accepts only a selected list of currently registered project names.
