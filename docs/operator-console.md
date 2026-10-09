# Local operator console

Run `bin/fm-console.sh` from a Firstmate checkout to open the local Status, Open decisions, and Queue tabs.
The page binds to IPv4 loopback and refreshes from the active home's structured fleet snapshot and GitHub issue and pull request records without starting an agent or using an AI model.

Status lists issues linked from admitted project work, grouped by registered project, with the exact GitHub title and issue state, lane stage, and linked pull request state.
Issue and pull request links are included only when their repository matches the registered clone's GitHub origin.
Unavailable GitHub fields stay labeled unavailable, and a failed fleet refresh retains the last good view as stale.

Open decisions are folded from the existing task status records.
Answering rechecks that the keyed decision is still open, records and sends the answer through `bin/fm-send.sh --resolve-key`, and relies on the existing captain-hold path to durably close it.

Queue shows queued and in-flight backlog items, their project, and recorded dependency blockers.
Admission limits are managed by the project Firstmate and are not independently recomputed by the page.
