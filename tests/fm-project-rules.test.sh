#!/usr/bin/env bash
# Behavior tests for bin/fm-project-rules.sh: admission refusals, the always-on
# block, receipts, the chained reader, Claude hook verdicts, log scanning for
# both tools, readiness, and the cases that must change nothing.
# shellcheck disable=SC2016 # Fixture text and node one-liners are literal on purpose.
set -u

# shellcheck source=tests/fixtures.sh
. "$(dirname "${BASH_SOURCE[0]}")/fixtures.sh"

command -v node >/dev/null 2>&1 || { echo "skip: node not found"; exit 0; }

PR="$ROOT/bin/fm-project-rules.sh"
TMP=$(fm_test_tmproot fm-project-rules)
export FM_PROJECT_RULES_QUALIFYING=1
unset CODEX_HOME

# new_copy <name>: a clean git repo with one area file, one native file, two
# skills, and a declared list. Echoes its path.
new_copy() {
  local copy="$TMP/$1"
  fm_git_init_commit "$copy"
  mkdir -p "$copy/.agents" "$copy/apps/web" "$copy/skills/tests" "$copy/skills/flow"
  printf '# Root map\n\nAlways use the gizmo.\n' > "$copy/CLAUDE.md"
  printf '# Agents core\n\nNever rename the widget.\n' > "$copy/AGENTS.md"
  printf '# Web area\n\nWeb rule: `keep` the "frame" stable.\n' > "$copy/apps/web/CLAUDE.md"
  printf '# Flow skill\n\nFlow body line.\n' > "$copy/skills/flow/SKILL.md"
  { printf '# Tests skill\n\n'; for i in $(seq 1 400); do printf 'Test procedure line %s: run the canonical runner.\n' "$i"; done; printf 'TAIL-OF-TESTS-SKILL\n'; } > "$copy/skills/tests/SKILL.md"
  cat > "$copy/.agents/project-rules.json" <<'JSON'
{
  "version": 1,
  "budget_bytes": 64000,
  "rules": [
    { "id": "map", "path": "CLAUDE.md", "native": ["claude"] },
    { "id": "core", "path": "AGENTS.md", "tools": ["codex"], "native": ["codex"] },
    { "id": "area-web", "path": "apps/web/CLAUDE.md" }
  ],
  "skills": [
    { "name": "flow", "description": "Workflow to load at start.", "invocation": "manual", "body": { "path": "skills/flow/SKILL.md" }, "required": { "at": "start" } },
    { "name": "tests", "description": "How to run product tests.", "invocation": "model", "body": { "path": "skills/tests/SKILL.md" }, "required": { "before_commands": ["(^|[;&| ])bun run test( |$)"] } },
    { "name": "audit", "description": "Query cost audit.", "invocation": "manual", "body": { "path": "skills/flow/SKILL.md" }, "required": { "on_paths": ["apps/api/src/server/**"] } }
  ],
  "dispatched_child_types": ["general-purpose", "Explore"]
}
JSON
  git -C "$copy" add -A
  git -C "$copy" -c user.name=t -c user.email=t@example.invalid commit -qm rules
  printf '%s\n' "$copy"
}

# admit <case> <tool> [list-edit-jq]: fresh state dir and copy; echoes "<state> <copy>".
STATE=; COPY=
admit() {
  local name=$1 tool=$2
  STATE="$TMP/$name.state"; COPY=$(new_copy "$name.copy")
  mkdir -p "$STATE" "$TMP/$name.data"
  printf 'Brief line one.\nBrief line two.\n' > "$TMP/$name.data/brief.md"
  "$PR" admit "$STATE" t1 "$COPY" "$tool" --brief "$TMP/$name.data/brief.md" --backend herdr --config "$TMP/$name.config"
}
# The node one-liners below evaluate expressions written in this file only, to
# build fixture records; no outside input reaches them.
row_code() { node -e 'const r=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));process.stdout.write(r.table[r.stage.row])' "$STATE/t1.project-rules"; }
field() { node -e 'const r=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));process.stdout.write(String(eval("r."+process.argv[2])))' "$STATE/t1.project-rules" "$1"; }
# read_all <what>: follow the chain to the end; echoes the number of parts read.
read_all() {
  local what=$1 out code parts=0
  out=$("$PR" serve "$STATE" t1 "$what") || return 1
  while :; do
    parts=$((parts + 1))
    case "$out" in *"RECORDED: $what is fully read"*) break ;; esac
    code=$(printf '%s\n' "$out" | awk '/Run this exact command now:/{getline; print $NF}' | tail -1)
    [ -n "$code" ] || return 1
    out=$("$PR" serve "$STATE" t1 "$what" "$code") || return 1
  done
  printf '%s\n' "$parts"
}
close_stage() { "$PR" ack "$STATE" t1 "$(row_code)" >/dev/null && read_all flow >/dev/null && read_all brief >/dev/null; }
edit_list() { node -e 'const fs=require("fs");const f=process.argv[1];const L=JSON.parse(fs.readFileSync(f,"utf8"));eval(process.argv[2]);fs.writeFileSync(f,JSON.stringify(L))' "$1/.agents/project-rules.json" "$2"; git -C "$1" -c user.name=t -c user.email=t@example.invalid commit -qam edit; }
hook() { printf '%s' "$2" | "$PR" hook "$STATE" t1 "$1"; }
# claude_evidence [boundary]: give the task a transcript that holds the block
# and the native file for its current generation.
claude_evidence() {
  local log="$STATE/t1.transcript.jsonl"
  [ -f "$log" ] || hook session-start "{\"source\":\"startup\",\"transcript_path\":\"$log\"}" >/dev/null
  [ -z "${1:-}" ] || claude_log "$log" '[{type:"system",subtype:"compact_boundary"}]'
  claude_log "$log" '[{type:"attachment",attachment:{type:"instructions",files:[{path:copy+"/CLAUDE.md",content:"# Root map\n\nAlways use the gizmo.\n"}]}},{type:"attachment",attachment:{type:"prompt_snapshot",systemPrompt:["intro\n\n"+block]}}]'
}

test_no_declared_list_changes_nothing() {
  local copy="$TMP/plain" state="$TMP/plain.state" rc=0
  fm_git_init_commit "$copy"; mkdir -p "$state"
  "$PR" admit "$state" t1 "$copy" claude >/dev/null 2>&1 || rc=$?
  expect_code 3 "$rc" "a copy with no declared list"
  assert_absent "$state/t1.project-rules" "no record may exist without a declared list"
  assert_equals "" "$("$PR" scan "$state")" "scan with no records must print nothing"
  "$PR" ready "$state" t1 || fail "ready must pass for a task with no record"
  pass "a project with no declared list gets no record, no alarm, and no readiness block"
}

test_admission_refusals() {
  local out rc copy state="$TMP/refuse.state"
  mkdir -p "$state"
  refused() {  # <label> <tool> <needle>
    rc=0; out=$("$PR" admit "$state" t1 "$copy" "$2" --backend herdr 2>&1) || rc=$?
    expect_code 1 "$rc" "$1"
    assert_contains "$out" "$3" "$1 must say why"
    assert_absent "$state/t1.project-rules" "$1 must leave no record"
  }
  copy=$(new_copy r-tool); refused "an unsupported tool" pi "only Claude and Codex"
  copy=$(new_copy r-field); edit_list "$copy" 'L.ruls=[]'; refused "an unknown field" claude "unknown field list.ruls"
  copy=$(new_copy r-parse); printf '{' > "$copy/.agents/project-rules.json"; refused "a malformed list" claude "does not parse"
  copy=$(new_copy r-missing); edit_list "$copy" 'L.rules.push({id:"gone",path:"nope.md"})'; refused "an unreadable path" claude "not a readable regular file"
  copy=$(new_copy r-prep); edit_list "$copy" 'L.prepare={argv:["sh","-c","echo broken >&2; exit 7"],timeout_s:5}'; refused "a failed prepare step" claude "prepare step failed (exit 7)"
  copy=$(new_copy r-dirty); edit_list "$copy" 'L.prepare={argv:["sh","-c","echo x > stray.txt"],timeout_s:5}'; refused "a prepare step that dirties the copy" claude "uncommitted changes"
  copy=$(new_copy r-resolve); printf '#!/bin/sh\necho x > stray.txt\nprintf "%%s/CLAUDE.md\\n" "$(pwd -P)"\n' > "$copy/res.sh"; git -C "$copy" add res.sh; edit_list "$copy" 'L.rules.push({id:"res",resolve:["sh","res.sh"]})'; refused "a resolve command that dirties the copy" claude "uncommitted changes"
  copy=$(new_copy r-slow); edit_list "$copy" 'L.prepare={argv:["sleep","5"],timeout_s:1}'; refused "a prepare step that times out" claude "prepare step failed (exit timeout)"
  copy=$(new_copy r-import); printf '@docs/more.md\n' >> "$copy/apps/web/CLAUDE.md"; git -C "$copy" -c user.name=t -c user.email=t@example.invalid commit -qam i; refused "an import line in an inlined file" claude "@ import line"
  copy=$(new_copy r-big); head -c 80000 /dev/zero | tr '\0' 'x' > "$copy/apps/web/CLAUDE.md"; git -C "$copy" -c user.name=t -c user.email=t@example.invalid commit -qam big; refused "an oversized block" claude "over the 79000-byte size"
  copy=$(new_copy r-chain); head -c 40000 /dev/zero | tr '\0' 'x' > "$copy/AGENTS.md"; git -C "$copy" -c user.name=t -c user.email=t@example.invalid commit -qam chain; HOME="$TMP/emptyhome" refused "an oversized Codex instruction chain" codex "truncate it silently"
  copy=$(new_copy r-dev); mkdir -p "$TMP/devhome/.codex"; printf 'developer_instructions = "x"\n[projects]\n' > "$TMP/devhome/.codex/config.toml"
  rc=0; out=$(HOME="$TMP/devhome" "$PR" admit "$state" t1 "$copy" codex --backend herdr 2>&1) || rc=$?
  expect_code 1 "$rc" "existing developer instructions"; assert_contains "$out" "already sets developer_instructions" "existing developer instructions must be named"
  copy=$(new_copy r-unq); rc=0; out=$(FM_PROJECT_RULES_QUALIFYING='' "$PR" admit "$state" t1 "$copy" claude --backend herdr --config "$TMP/noconfig" 2>&1) || rc=$?
  expect_code 1 "$rc" "a never-qualified tool and backend"; assert_contains "$out" "never passed the project-rules live guard" "the unqualified refusal must name the guard"
  pass "admission refuses unsupported tools, bad lists, failed or dirty prepare, unreadable or oversized rules, and unqualified pairs"
}

test_prepare_runs_scrubbed() {
  STATE="$TMP/scrub.state"; mkdir -p "$STATE"; COPY=$(new_copy scrub.copy)
  edit_list "$COPY" 'L.prepare={argv:["sh","-c","test -z \"${FM_SECRET_PROBE:-}\" && test -n \"$HOME\""],timeout_s:5}'
  FM_SECRET_PROBE=leak "$PR" admit "$STATE" t1 "$COPY" claude --backend herdr >/dev/null || fail "prepare must run without the launcher's other variables and with HOME"
  edit_list "$COPY" 'L.prepare={argv:["sh","-c","test \"$CLAUDE_PLUGINS_DIR\" = /p && test \"$CLAUDE_CONFIG_DIR\" = /c"],timeout_s:5}'
  CLAUDE_PLUGINS_DIR=/p CLAUDE_CONFIG_DIR=/c "$PR" admit "$STATE" t1 "$COPY" claude --backend herdr >/dev/null || fail "prepare must keep the two Claude store variables when the launcher has them"
  pass "the project prepare step runs with a reduced environment that keeps HOME and the Claude store variables"
}

test_block_receipt_and_chained_reads() {
  local block out rc=0 parts
  admit happy claude >/dev/null || fail "a valid copy must be admitted"
  block=$(cat "$STATE/t1.project-rules.d/block.txt")
  assert_contains "$block" 'Web rule: `keep` the "frame" stable.' "a non-native rule must be inlined verbatim"
  assert_not_contains "$block" "Always use the gizmo." "a native rule must not be inlined a second time"
  assert_not_contains "$block" "Never rename the widget." "a rule for the other tool must not be inlined"
  assert_contains "$block" "- flow (manual; required at start): Workflow to load at start." "the catalog must list a manual required skill with its limit"
  assert_contains "$block" "- tests (auto; required before certain commands): How to run product tests." "the catalog must list every declared skill"
  assert_contains "$block" '- tests: before you run any command matching `(^|[;&| ])bun run test( |$)`. Load it with:'$'\n' "the block must say when a required skill is due and how to load it"
  local command_line
  command_line=$(printf '%s\n' "$block" | grep "fm-project-rules.sh' serve '$STATE' 't1' 'tests'")
  assert_equals "'$ROOT/bin/fm-project-rules.sh' serve '$STATE' 't1' 'tests'" "$command_line" "the required skill command must occupy its own line"
  bash -n -c "$command_line" || fail "the standalone required skill command must parse as shell syntax"
  assert_equals 64 "$(printf '%s\n' "$block" | grep -c '^row[0-9][0-9]=[A-Z2-9]\{6\}$')" "the block must end with a 64-row receipt table"
  assert_contains "$block" $'Your launch brief can be read again at any time with:\n' "the block must say how to read the brief again"
  out=$("$PR" next "$STATE" t1)
  assert_contains "$out" "stage start) is OPEN" "next must report the open start stage"
  assert_contains "$out" "Admission stays OPEN until this command prints that nothing is owed" "next must say when admission is really over"
  assert_contains "$out" "Load required skill flow" "next must list the start skill"
  assert_not_contains "$out" "Load required skill tests" "a command-triggered skill is not owed at start"
  assert_not_contains "$out" "serve '$STATE' 't1' brief" "the task brief must not be offered before the required reads are recorded"
  "$PR" serve "$STATE" t1 brief >/dev/null 2>&1 && fail "the task brief must be refused before the receipt row and start skills are recorded"
  "$PR" ack "$STATE" t1 ZZZZZZ >/dev/null 2>&1 || rc=$?
  expect_code 1 "$rc" "a wrong receipt code"
  out=$("$PR" ack "$STATE" t1 "$(node -e 'const r=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));process.stdout.write(r.table[(r.stage.row+1)%r.table.length])' "$STATE/t1.project-rules")" 2>&1) && fail "the code of a neighbouring row must be refused"
  assert_contains "$out" "that is the code on row " "a worker that quoted the neighbouring row must be told which row its code is on"
  assert_equals false "$(field stage.acked)" "a neighbouring row's code must not record the receipt"
  "$PR" ack "$STATE" t1 "$(row_code)" >/dev/null || fail "the right receipt code must be accepted"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "readiness must fail while the brief and start skill are owed"
  rc=0; "$PR" serve "$STATE" t1 tests deadbeef >/dev/null 2>&1 || rc=$?
  expect_code 1 "$rc" "a part code that was never issued"
  read_all flow >/dev/null || fail "the start skill must be readable to the end"
  assert_contains "$("$PR" next "$STATE" t1)" $'Everything required is recorded. Receive your task brief:\n' "next must hand over the brief once the required reads are recorded"
  read_all brief >/dev/null || fail "the brief must be readable to the end"
  assert_not_equals null "$(field stage.closed)" "the stage must close once the row, brief and start skill are done"
  out=$("$PR" next "$STATE" t1)
  assert_contains "$out" "nothing is owed right now" "next must report a closed stage"
  assert_contains "$out" $'Before running any command matching (^|[;&| ])bun run test( |$), load tests:\n' "next must name the exact trigger of a skill not yet read"
  parts=$(read_all tests) || fail "a long skill must be readable to the end"
  [ "$parts" -ge 2 ] || fail "a long skill must be served in several parts, got $parts"
  assert_equals 0 "$(field 'answers.tests.gen')" "a completed skill read is recorded for the current generation"
  pass "the block inlines non-native rules and the catalog, and receipts and chained reads close the start stage"
}

test_a_skipped_part_cannot_complete() {
  local out first second
  admit skip claude >/dev/null
  out=$("$PR" serve "$STATE" t1 tests)
  assert_contains "$out" " - NOT FINISHED. Nothing is recorded yet. Run this exact command now:" "every part must end by saying it is not finished and naming the exact next command"
  first=$(printf '%s\n' "$out" | awk '/Run this exact command now:/{getline; print $NF}')
  out=$("$PR" serve "$STATE" t1 tests "$first")
  assert_contains "$out" "tests: PART 2 OF" "the first code must release exactly the second part"
  assert_not_contains "$out" "TAIL-OF-TESTS-SKILL" "the second part must not contain the end of a longer skill"
  assert_equals undefined "$(field 'answers.tests')" "a partly read skill must not be recorded"
  out=$("$PR" serve "$STATE" t1 flow)
  assert_contains "$out" "RECORDED: flow is fully read (1 part(s))." "serving the last part must record the read, with nothing left to run after it"
  assert_equals 0 "$(field 'answers.flow.gen')" "a one-part skill is recorded by the read that served it"
  second=$(printf '%s\n' "$out" | awk '/Run this exact command now:/{getline; print $NF}')
  assert_not_equals "$first" "$second" "each part must carry its own code"
  pass "a skill is recorded only after every part was requested in order"
}

test_claude_hook_verdicts() {
  local rc out
  mkdir -p "$TMP/hooks.config"
  admit hooks claude >/dev/null
  denied() { rc=0; out=$(hook pretool "$1" 2>&1) || rc=$?; expect_code 2 "$rc" "$2"; assert_contains "$out" "$3" "$2 must say what to do"; }
  allowed() { hook pretool "$1" >/dev/null 2>&1 || fail "$2"; }
  denied '{"tool_name":"Bash","tool_input":{"command":"ls"}}' "a project command while the start stage is open" "next"
  allowed "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"FM_HOME='$(dirname "$STATE")' FM_STATE_OVERRIDE='$STATE' '$ROOT/bin/fm-project-rules.sh' next '$STATE' 't1'\"}}" "the helper itself must stay allowed while a stage is open"
  allowed "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"'$ROOT/bin/fm-task-heartbeat.sh' 't1' '$STATE' \\\"reading the brief\\\"\"}}" "the heartbeat must stay allowed while a stage is open"
  denied "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"FM_ROOT_OVERRIDE='/tmp/evil' '$ROOT/bin/fm-captain-hold.sh' decision-event t1\"}}" "a helper pointed at another root" "next"
  denied "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"'$ROOT/bin/fm-project-rules.sh' merge-settings /tmp/x '$STATE' t1\"}}" "a helper verb that is not the worker's" "next"
  allowed '{"tool_name":"Read","tool_input":{"file_path":"/tmp/x"}}' "file reads must stay allowed while a stage is open"
  denied "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"'$ROOT/bin/fm-project-rules.sh' next '$STATE' 't1'; touch /tmp/x\"}}" "a command chained after the helper" "next"
  denied "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"$ROOT/bin/../bin/fm-spawn.sh x\"}}" "another script reached through the helper's folder" "next"
  denied "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"PATH=/tmp/evil '$ROOT/bin/fm-project-rules.sh' next '$STATE' 't1'\"}}" "the helper under a foreign environment prefix" "next"
  denied "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"echo \$('$ROOT/bin/fm-project-rules.sh' next)\"}}" "a command that only mentions the helper" "next"
  close_stage || fail "the start stage must close"
  allowed '{"tool_name":"Bash","tool_input":{"command":"ls"}}' "an ordinary command must be allowed after admission"
  denied '{"tool_name":"Agent","tool_input":{"subagent_type":"Explore"}}' "an unqualified child type" "general-purpose"
  allowed '{"tool_name":"Agent","tool_input":{"subagent_type":"general-purpose"}}' "a qualified child type must be allowed"
  denied '{"tool_name":"Bash","tool_input":{"command":"cd apps && bun run test"}}' "a triggered command before its skill" "serve"
  denied '{"tool_name":"Bash","tool_input":{"command":"sh -c \"bun run test\""}}' "a triggered command hidden in a quoted wrapper" "serve"
  denied '{"tool_name":"Bash","tool_input":{"command":"cat >> notes.md <<EOF\nsome notes\nEOF\nbun run test"}}' "a triggered command on its own line of a longer script" "serve"
  denied "$(node -e 'console.log(JSON.stringify({tool_name:"Bash",tool_input:{command:"cat >> notes.md <<EOF\n"+"filler line\n".repeat(900)+"EOF\nbun run test"}}))')" "a triggered command after a long here-document" "serve"
  denied '{"tool_name":"Bash","tool_input":{"command":"bun run \\\ntest"}}' "a triggered command continued across lines with a backslash" "serve"
  denied '{"tool_name":"Bash","tool_input":{"command":"ls;\tbun\trun\ttest"}}' "a triggered command written with tabs" "serve"
  denied "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$COPY/apps/api/src/server/a/b.ts\"}}" "an edit on a triggered path before its skill" "audit"
  allowed "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$COPY/apps/web/x.ts\"}}" "an edit outside the triggered paths must be allowed"
  read_all tests >/dev/null
  allowed '{"tool_name":"Bash","tool_input":{"command":"bun run test"}}' "a triggered command must be allowed once its skill is read"
  denied '{"agent_id":"a1","tool_name":"Bash","tool_input":{"command":"bun run test"}}' "a triggered command inside a child" "main worker"
  read_all audit >/dev/null
  allowed "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$COPY/apps/api/src/server/a.ts\"}}" "an edit on a triggered path must be allowed once the worker read its skill"
  denied "{\"agent_id\":\"a1\",\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$COPY/apps/api/src/server/a.ts\"}}" "an edit on a triggered path inside a child, after the worker's own read" "main worker"
  denied '{"tool_name":"Agent","tool_input":{"subagent_type":"Explore"}}' "a child type the project dispatches but nobody qualified" "needs qualifying"
  allowed '{"tool_name":"Bash","tool_input":{"command":"bun run tests-unrelated"}}' "a command that only resembles the trigger must be allowed"
  pass "the Claude hook refuses work before admission, unqualified children, and triggered work before its skill"
}

test_compaction_reopens_and_ages_answers() {
  local first out rc=0
  admit compact claude >/dev/null; claude_evidence; close_stage; read_all tests >/dev/null
  first=$(field stage.row)
  out=$(hook session-start '{"source":"compact"}')
  assert_contains "$out" "stage c1) is open" "the compaction hook must tell the worker a stage is open"
  assert_not_equals "$first" "$(field stage.row)" "a new stage must ask for a row no earlier stage used"
  out=$("$PR" next "$STATE" t1)
  assert_contains "$out" "Load required skill flow" "the start skill is owed again after a compaction"
  hook pretool '{"tool_name":"Bash","tool_input":{"command":"ls"}}' >/dev/null 2>&1 || rc=$?
  expect_code 2 "$rc" "work between a compaction and its refresh"
  close_stage
  rc=0; hook pretool '{"tool_name":"Bash","tool_input":{"command":"bun run test"}}' >/dev/null 2>&1 || rc=$?
  expect_code 2 "$rc" "a triggered command after a compaction, before the skill is read again"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "a closed stage with no block in the tool's own log must not be ready"
  claude_evidence boundary
  "$PR" ready "$STATE" t1 || fail "a skill read before its earlier use is not owed again until its next use"
  pass "a compaction opens a new stage with a fresh row and makes earlier skill reads stale"
}

test_superseded_stage_keeps_obligations() {
  admit super claude >/dev/null; close_stage
  hook session-start '{"source":"compact"}' >/dev/null
  hook session-start '{"source":"compact"}' >/dev/null
  assert_equals c2 "$(field stage.name)" "only the newest stage stays open"
  assert_contains "$("$PR" next "$STATE" t1)" "Load required skill flow" "an unanswered superseded stage must not drop the start skill"
  pass "obligations survive a stage that a later compaction supersedes"
}

test_receipt_table_exhaustion() {
  local i out
  admit exhaust claude >/dev/null
  for i in $(seq 1 64); do hook session-start '{"source":"compact"}' >/dev/null; done
  assert_equals -1 "$(field stage.row)" "the 65th stage has no unused row left"
  out=$("$PR" scan "$STATE" t1)
  assert_contains "$out" "project-rules: t1 receipt-table-exhausted" "an exhausted table must raise the alarm"
  assert_not_contains "$("$PR" scan "$STATE" t1)" "receipt-table-exhausted" "an alarm is raised once per episode"
  pass "an exhausted receipt table alarms once and asks for a relaunch"
}

# claude_log <file> <js>: append transcript records built by a JS expression over (block, copy).
claude_log() {
  node -e 'const fs=require("fs");const [state,copy,file,js]=process.argv.slice(1);const block=fs.readFileSync(state+"/t1.project-rules.d/block.txt","utf8").trimEnd();const rows=eval(js);fs.appendFileSync(file,rows.map(r=>JSON.stringify(r)).join("\n")+"\n")' "$STATE" "$COPY" "$1" "$2"
}

test_claude_scan_evidence_and_witness() {
  local log out
  admit cscan claude >/dev/null
  log="$TMP/cscan.transcript.jsonl"
  hook session-start "{\"source\":\"startup\",\"transcript_path\":\"$log\"}" >/dev/null
  claude_log "$log" '[{type:"attachment",attachment:{type:"instructions",files:[{path:copy+"/CLAUDE.md",content:"# Root map\n\nAlways use the gizmo.\n"}]}},{type:"attachment",attachment:{type:"prompt_snapshot",systemPrompt:["intro\n\n"+block]}}]'
  close_stage
  assert_equals "" "$("$PR" scan "$STATE" t1)" "a session whose own log holds the block and the native file must not alarm"
  claude_log "$log" '[{type:"system",subtype:"compact_boundary"}]'
  out=$(FM_PROJECT_RULES_GRACE_SECS=0 "$PR" scan "$STATE" t1)
  assert_equals c1 "$(field stage.name)" "a compaction the hook never reported must still open a stage"
  assert_contains "$out" "witness-mismatch" "a compaction the hook never reported must alarm"
  close_stage
  out=$("$PR" scan "$STATE" t1)
  assert_contains "$out" "no-delivery-evidence generation 1" "a closed stage with no block in the log must alarm"
  assert_contains "$out" "native-missing map generation 1" "a native file missing after compaction must alarm"
  pass "the Claude scan checks the block and native files in the transcript and catches a missed hook"
}

test_claude_scan_reports_changed_rules_and_stale_stage() {
  local out
  admit drift claude >/dev/null
  out=$(FM_PROJECT_RULES_STAGE_SECS=0 "$PR" scan "$STATE" t1)
  assert_contains "$out" "stage-unanswered start" "a stage open past its limit must alarm"
  printf 'changed\n' >> "$COPY/apps/web/CLAUDE.md"
  assert_contains "$("$PR" scan "$STATE" t1)" "rules-changed area-web" "a rule file changed after launch must alarm"
  pass "the scan reports an unanswered stage and a rule file that changed under a running session"
}

# codex_fixture: a bound session log for the admitted task; sets LOG.
LOG=
codex_fixture() {
  local root="$TMP/$1.codexhome/sessions" day stamp
  day="$root/$(date +%Y/%m/%d)"; mkdir -p "$day"
  stamp=$(date +%Y-%m-%dT%H-%M-%S)
  LOG="$day/rollout-$stamp-x.jsonl"
  root=$(cd "$root" && pwd -P)
  printf '%s\n' "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$COPY\"}}" > "$LOG"
  printf 'sessions_root=%s\nworkspace_root=%s\nbinding_id=b\n' "$root" "$COPY" > "$STATE/t1.codex-session"
  fm_write_meta "$STATE/t1.meta" "worktree=$COPY" "harness=codex" "kind=ship" "spawn_gen=s$(( $(date +%s) - 60 )).1.1"
}
codex_log() {
  node -e 'const fs=require("fs");const [state,copy,bin,file,js]=process.argv.slice(1);const block=fs.readFileSync(state+"/t1.project-rules.d/block.txt","utf8");const dev=(t)=>({type:"response_item",payload:{type:"message",role:"developer",content:[{type:"input_text",text:t}]}});const agents=()=>({type:"response_item",payload:{type:"message",role:"user",content:[{type:"input_text",text:"# AGENTS.md instructions for "+copy+"\n\n# Agents core\n\nNever rename the widget.\n"}]}});const q=(t)=>"\x27"+t+"\x27";const call=(cmd,ts)=>({type:"response_item",timestamp:ts||new Date().toISOString(),payload:{type:"custom_tool_call",name:"exec",input:"text(await tools.exec_command({cmd:"+JSON.stringify(cmd)+"}));\n"}});const started=()=>({type:"event_msg",payload:{type:"task_started"}});const complete=()=>({type:"event_msg",payload:{type:"task_complete"}});const patch=(f)=>({type:"response_item",timestamp:new Date().toISOString(),payload:{type:"custom_tool_call",name:"apply_patch",input:"*** Begin Patch\n*** Update File: "+f+"\n@@\n-a\n+b\n*** End Patch"}});const event=()=>({type:"event_msg",payload:{type:"item_completed",item:{type:"ContextCompaction"}}});const compacted=(history)=>({type:"compacted",payload:{replacement_history:history||[]}});const rows=eval(js);fs.appendFileSync(file,rows.map(r=>typeof r==="string"?r:JSON.stringify(r)).join("\n")+(process.env.NO_NEWLINE?"":"\n"))' "$STATE" "$COPY" "$ROOT/bin" "$LOG" "$1"
}

test_codex_scan_start_and_mid_turn_compaction() {
  local out
  admit xscan codex >/dev/null; codex_fixture xscan
  codex_log '[started(),dev("skills\n"),dev(block),agents(),call(bin+"/fm-project-rules.sh next")]'
  close_stage
  codex_log '[call("ls")]'
  assert_equals "" "$("$PR" scan "$STATE" t1)" "a Codex session with the block, the native file, and work only after admission must not alarm"
  codex_log '[compacted([{type:"message",role:"developer",content:[{text:block}]},{type:"message",role:"user",content:[{text:"# AGENTS.md instructions for x\n\n# Agents core\n\nNever rename the widget.\n"}]}]),event(),call("ls")]'
  printf '#!/bin/sh\nprintf "%%s|%%s\\n" "$1" "$2" >> "%s"\n' "$TMP/xscan.sent" > "$TMP/xscan.send"; chmod +x "$TMP/xscan.send"
  FM_PROJECT_RULES_SEND="$TMP/xscan.send" "$PR" detect "$STATE" t1
  assert_equals c1 "$(field stage.name)" "a compaction record must open a stage"
  assert_equals 0 "$(field same_turn_calls)" "a call before the worker's first helper run in the new generation is not counted"
  "$PR" next "$STATE" t1 >/dev/null
  codex_log '[call("ls"),call("ls")]'
  "$PR" detect "$STATE" t1
  assert_grep "t1|Your context was compacted (stage c1). Before any other action run:" "$TMP/xscan.sent" "a newly detected Codex compaction must be steered to the worker once"
  FM_PROJECT_RULES_SEND="$TMP/xscan.send" "$PR" detect "$STATE" t1
  assert_equals 3 "$(wc -l < "$TMP/xscan.sent" | tr -d ' ')" "a compaction already seen must not be steered again"
  assert_equals true "$(field 'log.evidence[1]')" "a mid-turn compaction record that carries the block is delivery evidence"
  assert_equals 2 "$(field same_turn_calls)" "calls in the same turn as a mid-turn compaction are counted"
  assert_equals 0 "$(field violations.length)" "same-turn calls are counted, not treated as a violation"
  codex_log '[started(),call("ls")]'
  out=$("$PR" scan "$STATE" t1)
  assert_contains "$out" "worked-before-refresh stage c1" "a project call in a later turn before the refresh must alarm"
  close_stage
  assert_contains "$("$PR" scan "$STATE" t1)" "same-turn-calls 2" "the same-turn count is reported once the stage is answered"
  pass "the Codex scan accepts mid-turn compaction evidence, counts same-turn calls, and flags later-turn work before refresh"
}

test_codex_scan_between_turn_compaction_and_triggers() {
  local out
  admit xturn codex >/dev/null; codex_fixture xturn
  codex_log '[started(),dev(block),agents()]'; close_stage
  codex_log '[compacted([]),event()]'
  "$PR" detect "$STATE" t1
  assert_equals undefined "$(field 'log.evidence[1]')" "a between-turn compaction record carries no block"
  codex_log '[started(),dev(block),agents()]'; close_stage
  assert_equals "" "$("$PR" scan "$STATE" t1)" "the block re-sent at the next turn start is delivery evidence"
  codex_log '[call("ls "+bin+"/fm-project-rules.sh && bun run test")]'
  "$PR" detect "$STATE" t1
  out=$("$PR" ready "$STATE" t1 2>&1) && fail "a violation the supervisor has not been told about must block readiness"
  assert_contains "$out" "has not reached the supervisor yet" "the readiness refusal must say the violation is unreported"
  out=$("$PR" scan "$STATE" t1)
  assert_contains "$out" "trigger-skipped tests c1" "a triggered command without its skill must alarm on Codex, even when it mentions the helper"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "a reported violation must still block readiness while its skill is unread"
  read_all tests >/dev/null
  "$PR" ready "$STATE" t1 2>/dev/null && fail "a skipped trigger must block readiness until the command is run again after the skill is read"
  codex_log '[call("bun run test", new Date(Date.now()+2000).toISOString())]'
  "$PR" ready "$STATE" t1 || fail "readiness must pass once the skill is read and the command was run again after it"
  pass "the Codex scan accepts next-turn evidence and catches a triggered command run without its skill"
}

test_codex_scan_reads_every_command_shape() {
  local shape n=0
  for shape in \
    '[{type:"response_item",payload:{type:"function_call",name:"shell",arguments:JSON.stringify({cmd:"bun run test"})}}]' \
    '[{type:"response_item",payload:{type:"function_call",name:"shell",arguments:JSON.stringify({command:["bun","run","test"]})}}]' \
    '[{type:"response_item",payload:{type:"custom_tool_call",name:"exec",input:"await tools.exec_command({cmd:\"pwd\"});\nawait tools.exec_command({cmd:\"bun run test\"});"}}]' \
    '[{type:"response_item",payload:{type:"custom_tool_call",name:"exec",input:"await tools.exec_command({cmd:`bun run test`});"}}]' \
    '[{type:"response_item",payload:{type:"custom_tool_call",name:"exec",input:"await tools.exec_command({cmd:'"'"'bun run test'"'"'});"}}]' \
    '[{type:"response_item",payload:{type:"custom_tool_call",name:"exec",input:"const c = \"bun run test\"; await tools.exec_command({cmd:c});"}}]' \
    '[{type:"response_item",payload:{type:"local_shell_call",action:{command:["sh","-c","bun run test"]}}}]' \
    '[{type:"response_item",payload:{type:"custom_tool_call",name:"exec",input:"const c = \"bun run test\"; await tools.exec_command({cmd:"+JSON.stringify(q(bin+"/fm-project-rules.sh")+" next")+"}); await tools.exec_command({cmd:c});"}}]' \
    '[{type:"response_item",payload:{type:"custom_tool_call",name:"exec",input:"for (const c of [\"bun run test\"]) await tools.exec_command({cmd:`${c}`});"}}]' \
    '[call("cat >> notes.md <<EOF\n\n## A\nsome notes\nEOF\nbun run test")]' \
    '[{type:"response_item",payload:{type:"custom_tool_call",name:"exec",input:"const c = \"echo start\\nbun run test\"; await tools.exec_command({cmd:c});"}}]'; do
    n=$((n + 1))
    admit "xshape$n" codex >/dev/null; codex_fixture "xshape$n"
    codex_log '[started(),dev(block),agents()]'; close_stage
    codex_log "$shape"
    assert_contains "$("$PR" scan "$STATE" t1)" "trigger-skipped tests c0" "Codex command shape $n must be recognized"
  done
  pass "the Codex scan finds a triggering command in every call shape: JSON arguments, batches, each quote style, a variable, a variable beside a helper call, a shell action, and a line of its own in a longer script"
}

test_codex_read_then_run_in_one_call_is_in_order() {
  local read_cmd='"await tools.exec_command({cmd:"+JSON.stringify(q(bin+"/fm-project-rules.sh")+" serve "+q(state)+" "+q("t1")+" tests abc")+"});"'
  local run_cmd='"await tools.exec_command({cmd:\"bun run test\"});"'
  admit xbatch codex >/dev/null; codex_fixture xbatch
  codex_log '[started(),dev(block),agents()]'; close_stage
  codex_log "[{type:'response_item',timestamp:new Date().toISOString(),payload:{type:'custom_tool_call',call_id:'c1',name:'exec',input:$read_cmd+$run_cmd}}]"
  "$PR" detect "$STATE" t1
  assert_equals 1 "$(field triggered.tests)" "the command inside the batched call must be recognized"
  assert_equals 0 "$(field violations.length)" "a call that reads the skill and then runs the command is not judged before its output"
  read_all tests >/dev/null
  codex_log "[{type:'response_item',timestamp:new Date(Date.now()+2000).toISOString(),payload:{type:'custom_tool_call_output',call_id:'c1',output:'ok'}}]"
  assert_equals "" "$("$PR" scan "$STATE" t1)" "a skill read to the end inside the same call, before the command, is in order"
  admit xbatch2 codex >/dev/null; codex_fixture xbatch2
  codex_log '[started(),dev(block),agents()]'; close_stage
  codex_log "[{type:'response_item',timestamp:new Date().toISOString(),payload:{type:'custom_tool_call',call_id:'c2',name:'exec',input:$run_cmd+$read_cmd}}]"
  assert_contains "$("$PR" scan "$STATE" t1)" "trigger-skipped tests c0" "a call that runs the command before reading the skill is a violation"
  pass "one Codex call that finishes the skill and then runs its command is accepted, and the reverse order is not"
}

test_codex_following_the_doorbell_is_not_a_violation() {
  local inbox
  admit xbell codex >/dev/null; codex_fixture xbell
  inbox="$STATE/t1.inbox"
  codex_log '[started(),dev(block),agents()]'; close_stage
  codex_log '[compacted([]),event()]'
  FM_PROJECT_RULES_SEND=true "$PR" ready "$STATE" t1 >/dev/null 2>&1 || true
  assert_equals c1 "$(field stage.name)" "a compaction first seen by a readiness check must open its stage"
  # The shapes real Codex workers use to follow the doorbell.
  codex_log "[started(),dev(block),agents(),call(\"find '$inbox' -name '*.msg' -print | sort -V\"),call(\"for f in '$inbox'/*.msg; do cat \\\"\$f\\\"; done\"),call(\"set -e\\ncd '$inbox'\\nls\\ncat 001.msg\\nmv 001.msg handled/\"),call(\"ls '$inbox' 2>/dev/null\"),call(\"sleep 20\"),{type:'response_item',timestamp:new Date().toISOString(),payload:{type:'function_call',name:'wait',arguments:'{\\\"timeout_ms\\\":10000}'}}]"
  close_stage
  assert_equals "" "$("$PR" scan "$STATE" t1)" "whatever a worker runs to follow the doorbell, before its first helper call, is not charged"
  codex_log '[compacted([]),event()]'; "$PR" detect "$STATE" t1
  "$PR" next "$STATE" t1 >/dev/null
  codex_log '[started(),call("touch elsewhere.txt")]'
  assert_contains "$("$PR" scan "$STATE" t1)" "worked-before-refresh stage c2" "work after the worker's first helper call in that generation, before the refresh is done, still counts"
  pass "a Codex worker is not charged for following the doorbell in any shell shape, and is charged for work once it has started the refresh"
}

test_codex_helper_spelled_through_a_variable() {
  admit xvar codex >/dev/null; codex_fixture xvar
  codex_log "[started(),dev(block),agents(),{type:'response_item',timestamp:new Date().toISOString(),payload:{type:'custom_tool_call',call_id:'v1',name:'exec',input:'text(await tools.exec_command({cmd:load(\\\"p\\\")+\\\" next\\\"}));'}}]"
  "$PR" next "$STATE" t1 >/dev/null
  codex_log "[{type:'response_item',timestamp:new Date(Date.now()+1000).toISOString(),payload:{type:'custom_tool_call_output',call_id:'v1',output:'ok'}}]"
  close_stage
  assert_equals "" "$("$PR" scan "$STATE" t1)" "a call whose command could not be read, during which the helper really ran, is the helper"
  admit xvar2 codex >/dev/null; codex_fixture xvar2
  codex_log "[started(),dev(block),agents(),{type:'response_item',timestamp:new Date(Date.now()-60000).toISOString(),payload:{type:'custom_tool_call',call_id:'v2',name:'exec',input:'text(await tools.exec_command({cmd:load(\\\"p\\\")}));'}},{type:'response_item',timestamp:new Date(Date.now()-50000).toISOString(),payload:{type:'custom_tool_call_output',call_id:'v2',output:'ok'}}]"
  close_stage
  assert_contains "$("$PR" scan "$STATE" t1)" "worked-before-admission stage start" "a call whose command could not be read, with no helper run during it, is work"
  pass "a helper call spelled through a variable is recognized by the helper run the engine noted, and other unreadable calls still count as work"
}

test_codex_admission_then_work_in_one_call_is_in_order() {
  local done_cmd work_cmd
  admit xadmit codex >/dev/null; codex_fixture xadmit
  done_cmd='"await tools.exec_command({cmd:"+JSON.stringify(q(bin+"/fm-project-rules.sh")+" next "+q(state)+" t1")+"});"'
  work_cmd='"await tools.exec_command({cmd:\"ls apps\"});"'
  codex_log "[started(),dev(block),agents(),{type:'response_item',timestamp:new Date().toISOString(),payload:{type:'custom_tool_call',call_id:'a1',name:'exec',input:$done_cmd+$work_cmd}}]"
  "$PR" detect "$STATE" t1
  assert_equals 0 "$(field violations.length)" "a call that starts with Firstmate's own commands is not judged before its output"
  close_stage
  codex_log "[{type:'response_item',timestamp:new Date(Date.now()+2000).toISOString(),payload:{type:'custom_tool_call_output',call_id:'a1',output:'ok'}}]"
  assert_equals "" "$("$PR" scan "$STATE" t1)" "work after admission completed inside the same call is in order"
  admit xadmit2 codex >/dev/null; codex_fixture xadmit2
  codex_log "[started(),dev(block),agents(),{type:'response_item',timestamp:new Date().toISOString(),payload:{type:'custom_tool_call',call_id:'a2',name:'exec',input:$done_cmd+$work_cmd}},{type:'response_item',timestamp:new Date().toISOString(),payload:{type:'custom_tool_call_output',call_id:'a2',output:'ok'}}]"
  close_stage
  assert_contains "$("$PR" scan "$STATE" t1)" "worked-before-admission stage start" "work in a call whose output precedes the end of admission is a violation"
  pass "one Codex call that completes admission and then works is accepted, and one that works before admission ends is not"
}

test_codex_scan_is_idempotent_and_cursor_safe() {
  local before
  admit xcur codex >/dev/null; codex_fixture xcur
  codex_log '[started(),dev(block),agents(),compacted([]),event(),compacted([]),event()]'
  "$PR" detect "$STATE" t1
  assert_equals c2 "$(field stage.name)" "two compactions before a scan leave only the newest stage open"
  before=$(field used.length)
  "$PR" detect "$STATE" t1; "$PR" detect "$STATE" t1
  assert_equals "$before" "$(field used.length)" "re-reading the same log must not open stages again"
  NO_NEWLINE=1 codex_log '["{\"type\":\"compacted\",\"payload\":{\"repl"]'
  "$PR" detect "$STATE" t1
  assert_equals c2 "$(field stage.name)" "a partly written line must be ignored until it is complete"
  printf 'acement_history":[]}}\n' >> "$LOG"
  "$PR" detect "$STATE" t1
  assert_equals c3 "$(field stage.name)" "a line is read once it is complete"
  pass "the Codex scan is idempotent, keeps only the newest stage, and waits for complete lines"
}

test_codex_missing_evidence_and_witness() {
  local out
  admit xmiss codex >/dev/null; codex_fixture xmiss
  codex_log '[started(),dev("no block here"),compacted([])]'
  close_stage
  out=$(FM_PROJECT_RULES_GRACE_SECS=0 "$PR" scan "$STATE" t1)
  assert_contains "$out" "no-delivery-evidence generation 0" "a session log without the block must alarm"
  assert_contains "$out" "native-missing core generation 0" "a native file absent from the session log must alarm"
  assert_contains "$out" "witness-mismatch" "a compaction record with no matching event must alarm"
  pass "the Codex scan alarms on a missing block, a missing native file, and disagreeing compaction records"
}

# responder <case>: start the task's responder the way the watcher does, with
# the control and steering planes replaced by one ordered record; sets ACTS.
ACTS=
responder() {
  ACTS="$TMP/$1.acts"; : > "$ACTS"
  printf '#!/bin/sh\nprintf "interrupt %%s\\n" "$1" >> "%s"\n' "$ACTS" > "$TMP/$1.control"
  printf '#!/bin/sh\nprintf "steer %%s|%%s\\n" "$1" "$2" >> "%s"\n' "$ACTS" > "$TMP/$1.send"
  chmod +x "$TMP/$1.control" "$TMP/$1.send"
  FM_PROJECT_RULES_CONTROL="$TMP/$1.control" FM_PROJECT_RULES_SEND="$TMP/$1.send" "$PR" respond "$STATE"
  local tries=0
  until [ -s "$STATE/t1.project-rules.d/responder.pid" ]; do
    tries=$((tries + 1)); [ "$tries" -lt 50 ] || fail "the responder never claimed its task"
    sleep 0.1
  done
}
# acted <count>: wait for that many actions; the responder acts within about a
# second, and five are allowed.
acted() {
  local tries=0
  until [ "$(wc -l < "$ACTS" | tr -d ' ')" -ge "$1" ]; do
    tries=$((tries + 1)); [ "$tries" -lt 50 ] || return 1
    sleep 0.1
  done
}
# The responder ends with its task, so no test leaves one running.
responder_gone() {
  local pid tries=0
  pid=$(cat "$STATE/t1.project-rules.d/responder.pid")
  rm -f "$STATE/t1.project-rules"
  while kill -0 "$pid" 2>/dev/null; do
    tries=$((tries + 1)); [ "$tries" -lt 50 ] || return 1
    sleep 0.1
  done
}

test_codex_responder_interrupts_a_compaction_and_a_skipped_trigger() {
  local claim command_line
  admit xresp codex >/dev/null; codex_fixture xresp
  codex_log '[started(),dev(block),agents()]'; close_stage
  responder xresp
  claim=$(cat "$STATE/t1.project-rules.d/responder.pid")
  "$PR" respond "$STATE"
  assert_equals "$claim" "$(cat "$STATE/t1.project-rules.d/responder.pid")" "a task that has a live responder must not be given a second one"
  codex_log '[call("bun run test")]'
  acted 2 || fail "the start of a triggering command without its skill must be answered within seconds: $(cat "$ACTS")"
  assert_equals "interrupt t1" "$(sed -n 1p "$ACTS")" "the worker must be interrupted before it is told what to load"
  assert_contains "$(sed -n 2p "$ACTS")" "steer t1|Required skill tests is not loaded in this stage" "the worker must be told which skill the command needs"
  command_line=$(grep -F "steer t1|'$(dirname "$PR")/fm-project-rules.sh' serve '$STATE' 't1' 'tests'" "$ACTS")
  assert_equals "steer t1|'$(dirname "$PR")/fm-project-rules.sh' serve '$STATE' 't1' 'tests'" "$command_line" "the responder must send the exact command on its own line"
  bash -n -c "${command_line#steer t1|}" || fail "the responder's standalone command must parse as shell syntax"
  assert_grep "Then run the command again, and carry on with your task." "$ACTS" "the message must ask the worker to rerun"
  assert_equals "" "$("$PR" scan "$STATE" t1)" "a skip the responder caught at its start is not reported while the worker has time to correct it"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "a caught skip must block readiness until it is corrected"
  assert_contains "$(FM_PROJECT_RULES_CORRECT_SECS=0 "$PR" scan "$STATE" t1)" "trigger-skipped tests c0" "a worker that ignores the reminder must be reported once the time to correct has passed"
  read_all tests >/dev/null
  codex_log '[started(),call("bun run test", new Date(Date.now()+2000).toISOString())]'
  "$PR" ready "$STATE" t1 || fail "readiness must pass once the skill is read and the command was started again"
  codex_log '[compacted([{type:"message",role:"developer",content:[{text:block}]},{type:"message",role:"user",content:[{text:"# AGENTS.md instructions for x\n\n# Agents core\n\nNever rename the widget.\n"}]}]),event()]'
  acted 8 || fail "a compaction inside a turn must be answered within seconds: $(cat "$ACTS")"
  assert_grep "interrupt t1" "$ACTS" "a worker compacted in the middle of a turn must be interrupted"
  assert_contains "$(cat "$ACTS")" "Your context was compacted (stage c1). Before any other action run:" "the worker must be told to refresh"
  close_stage
  codex_log '[started(),call("bun run test", new Date(Date.now()+4000).toISOString())]'
  acted 12 || fail "a skill read before a compaction no longer covers the command after it: $(cat "$ACTS")"
  assert_grep "Required skill tests is not loaded in this stage" "$ACTS" "the skill must be asked for again after the compaction"
  read_all tests >/dev/null
  codex_log '[call("bun run test", new Date(Date.now()+6000).toISOString())]'
  "$PR" ready "$STATE" t1 || fail "readiness must pass once the second skip is corrected"
  assert_equals "" "$(FM_PROJECT_RULES_CORRECT_SECS=0 "$PR" scan "$STATE" t1 | grep -v ' same-turn-calls ')" "a caught skip that was corrected is never reported, however long ago"
  assert_equals 12 "$(wc -l < "$ACTS" | tr -d ' ')" "each compaction and each skipped start is acted on exactly once"
  responder_gone || fail "the responder must end with its task"
  pass "the Codex responder interrupts a mid-turn compaction and the start of a triggering command, names the exact load command, holds the alarm while the worker corrects it, and reports a reminder that was ignored"
}

test_codex_responder_leaves_everything_else_alone() {
  local inbox
  admit xquiet codex >/dev/null; codex_fixture xquiet
  inbox="$STATE/t1.inbox"
  codex_log '[started(),dev(block),agents()]'; close_stage
  read_all tests >/dev/null
  responder xquiet
  # Helper commands, the shapes workers use to follow the doorbell, a command
  # whose skill is read, a command no pattern names, and an edit outside every
  # declared path.
  codex_log '[call(q(bin+"/fm-project-rules.sh")+" next "+q(state)+" t1"),call("bun run test", new Date(Date.now()+2000).toISOString()),call("bun run build"),patch("README.md")]'
  codex_log "[call(\"find '$inbox' -name '*.msg' -print | sort -V\"),call(\"for f in '$inbox'/*.msg; do cat \\\"\$f\\\"; done\"),complete()]"
  # A compaction between turns is told to the worker without an interrupt.
  codex_log '[compacted([]),event()]'
  acted 3 || fail "a compaction between turns must still be told to the worker: $(cat "$ACTS")"
  assert_contains "$(sed -n 1p "$ACTS")" "steer t1|Your context was compacted (stage c1)." "helper commands, inbox reads, a command after its skill, and other paths must cause no action, and an idle worker is not interrupted"
  codex_log '[started(),dev(block),agents()]'; close_stage
  codex_log '[patch("apps/api/src/server/routes.ts")]'
  acted 7 || fail "an edit under a declared path without its skill must be answered: $(cat "$ACTS")"
  assert_equals "interrupt t1" "$(sed -n 4p "$ACTS")" "a worker editing in the middle of a turn must be interrupted first"
  assert_contains "$(sed -n 5p "$ACTS")" "steer t1|Required skill audit is not loaded in this stage" "the worker must be told which skill the edit needs"
  assert_contains "$(sed -n 7p "$ACTS")" "Then check that edit against it, and carry on with your task." "the message for an edit must ask for the edit to be checked"
  assert_equals "" "$("$PR" scan "$STATE" t1 | grep -v ' same-turn-calls ')" "none of it is a violation"
  assert_equals 7 "$(wc -l < "$ACTS" | tr -d ' ')" "nothing else was acted on"
  responder_gone || fail "the responder must end with its task"
  admit xclaude claude >/dev/null
  "$PR" respond "$STATE"
  assert_absent "$STATE/t1.project-rules.d/responder.pid" "a Claude worker keeps its hook and gets no responder"
  pass "the Codex responder acts only on a compaction and on declared triggers: not on helper commands, inbox reads, a command after its skill, other paths, or a Claude worker"
}

test_codex_sub_agent_log_is_never_read_as_the_workers() {
  local child
  admit xchild codex >/dev/null; codex_fixture xchild
  codex_log '[started(),dev(block),agents()]'; close_stage
  read_all tests >/dev/null
  codex_log '[call("bun run test", new Date(Date.now()+2000).toISOString())]'
  codex_log '[compacted([{type:"message",role:"developer",content:[{text:block}]},{type:"message",role:"user",content:[{text:"# AGENTS.md instructions for x\n\n# Agents core\n\nNever rename the widget.\n"}]}]),event()]'
  FM_PROJECT_RULES_OBSERVE=1 "$PR" detect "$STATE" t1; close_stage
  read_all tests >/dev/null
  # A sub-agent's session log in the same copy, written after the worker's own.
  child="$(dirname "$LOG")/rollout-$(date +%Y-%m-%dT%H-%M-%S)-child.jsonl"
  printf '%s\n' "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$COPY\",\"thread_source\":\"subagent\",\"parent_thread_id\":\"x\"}}" '{"type":"compacted","payload":{"replacement_history":[]}}' > "$child"
  node -e 'const t=new Date(Date.now()-60000);require("fs").utimesSync(process.argv[1],t,t)' "$LOG"
  # shellcheck source=bin/fm-busy-lib.sh
  assert_equals "$child" "$( . "$ROOT/bin/fm-busy-lib.sh" && fm_busy_codex_rollout_log "$STATE" t1 )" "the sub-agent's log is the one written last, which is what makes it a candidate"
  FM_PROJECT_RULES_OBSERVE=1 "$PR" detect "$STATE" t1
  assert_equals "$LOG" "$(field log.file)" "a sub-agent's session log must never be adopted as the worker's"
  assert_equals 1 "$(field compactions)" "a compaction record in a sub-agent's log is not the worker's compaction"
  codex_log '[started(),call("bun run test", new Date(Date.now()+4000).toISOString())]'
  assert_equals "" "$("$PR" scan "$STATE" t1 | grep -v ' same-turn-calls ')" "the worker's log is read on from where it was, so an earlier command is not judged again against a later read"
  printf '%s\n' "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$COPY\",\"thread_source\":\"subagent\",\"parent_thread_id\":\"gone\"}}" > "$child"
  node -e 'const t=new Date(Date.now()-60000);require("fs").utimesSync(process.argv[1],t,t)' "$LOG"
  assert_contains "$("$PR" scan "$STATE" t1)" "scan-error the session that spawned sub-agent log" "a sub-agent log whose parent session cannot be found must be reported, not skipped"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "a log that could not be traced to the worker's session must block readiness"
  codex_log '[compacted([]),event()]'; FM_PROJECT_RULES_OBSERVE=1 "$PR" detect "$STATE" t1
  assert_equals 2 "$(field compactions)" "the worker's own log is still read while a sub-agent log cannot be traced"
  # A sub-agent log that names a session in some other copy.
  mkdir -p "$TMP/xchild.other"
  printf '%s\n' "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$TMP/xchild.other\"}}" > "$(dirname "$LOG")/rollout-$(date +%Y-%m-%dT%H-%M-%S)-elsewhere.jsonl"
  printf '%s\n' "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$COPY\",\"thread_source\":\"subagent\",\"parent_thread_id\":\"elsewhere\"}}" > "$child"
  node -e 'const t=new Date(Date.now()-60000);require("fs").utimesSync(process.argv[1],t,t)' "$LOG"
  FM_PROJECT_RULES_OBSERVE=1 "$PR" detect "$STATE" t1
  assert_equals "$LOG" "$(field log.file)" "a sub-agent log must not lead the scan to a session in another copy"
  assert_contains "$(field scan_error)" "not a session in this task" "a trace that leaves the task's copy must be reported"
  printf 'not a session record\n' > "$(dirname "$LOG")/rollout-$(date +%Y-%m-%dT%H-%M-%S)-broken.jsonl"
  printf '%s\n' "{\"type\":\"session_meta\",\"payload\":{\"cwd\":\"$COPY\",\"thread_source\":\"subagent\",\"parent_thread_id\":\"broken\"}}" > "$child"
  node -e 'const t=new Date(Date.now()-60000);require("fs").utimesSync(process.argv[1],t,t)' "$LOG"
  FM_PROJECT_RULES_OBSERVE=1 "$PR" detect "$STATE" t1
  assert_contains "$(field scan_error)" "does not start with a readable session record" "a parent log that names no session must be reported, not taken for the worker's"
  pass "a sub-agent's session log in the same copy is never read as the worker's: the worker's log is found through it and read on from where it was, and one that cannot be traced is reported"
}

test_a_slow_pattern_counts_as_a_match() {
  local rc=0
  admit slow claude >/dev/null
  node -e 'const fs=require("fs");const f=process.argv[1];const r=JSON.parse(fs.readFileSync(f,"utf8"));r.skills.find(s=>s.name==="tests").required.before_commands=["^(a+)+$"];fs.writeFileSync(f,JSON.stringify(r))' "$STATE/t1.project-rules"
  close_stage
  hook pretool '{"tool_name":"Bash","tool_input":{"command":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!"}}' >/dev/null 2>&1 || rc=$?
  expect_code 2 "$rc" "a command whose pattern check ran out of time"
  pass "a project pattern that runs out of time is treated as a match instead of stalling or letting the command through"
}

test_drift_covers_skills_and_the_declaration() {
  local out
  admit skilldrift claude >/dev/null; claude_evidence; close_stage
  printf 'edited\n' >> "$COPY/skills/flow/SKILL.md"
  out=$("$PR" scan "$STATE" t1)
  assert_contains "$out" "rules-changed skill:flow" "a changed skill body must alarm"
  assert_contains "$out" "rules-changed skill:audit" "every skill that uses the changed file must alarm"
  assert_equals undefined "$(field answers.flow)" "a changed skill must lose its recorded read"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "drift must block readiness"
  printf ' ' >> "$COPY/.agents/project-rules.json"
  assert_contains "$("$PR" scan "$STATE" t1)" "rules-changed declared-list" "a changed declaration must alarm"
  pass "drift is watched on rule files, skill bodies and the declaration, and blocks readiness"
}

test_failures_inside_the_hook_and_scan_are_never_silent() {
  local rc=0 out nonode="$TMP/nonode"
  admit infra claude >/dev/null; close_stage
  mkdir -p "$nonode"; for tool in dirname basename sed ls rm cat head cut; do ln -sf "$(command -v "$tool")" "$nonode/$tool"; done
  printf '{}' | PATH="$nonode" /bin/bash "$PR" hook "$STATE" t1 pretool >/dev/null 2>&1 || rc=$?
  expect_code 2 "$rc" "the tool hook with node missing"
  mkdir "$STATE/t1.project-rules.lock"
  rc=0; hook pretool '{"tool_name":"Bash","tool_input":{"command":"ls"}}' >/dev/null 2>&1 || rc=$?
  expect_code 2 "$rc" "the tool hook while the record is locked"
  rmdir "$STATE/t1.project-rules.lock" 2>/dev/null || true
  printf '{broken' > "$STATE/t1.project-rules"
  rc=0; hook pretool '{"tool_name":"Bash","tool_input":{"command":"ls"}}' >/dev/null 2>&1 || rc=$?
  expect_code 2 "$rc" "the tool hook with a corrupt record"
  out=$("$PR" scan "$STATE")
  assert_contains "$out" "project-rules: t1 scan-failed" "a scan that cannot read a record must raise an alarm"
  assert_equals "" "$("$PR" scan "$STATE")" "the same scan failure is reported once"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "readiness must refuse when the record cannot be read"
  out=$(PATH="$nonode" /bin/bash "$PR" scan "$STATE") && fail "a scan with node missing must not report success"
  assert_contains "$out" "project-rules: t1 scan-failed node is required" "a scan with node missing must still raise an alarm"
  assert_equals "" "$(PATH="$nonode" /bin/bash "$PR" scan "$STATE" || true)" "the missing-node alarm is raised once"
  admit infra2 claude >/dev/null; claude_evidence; close_stage
  "$PR" ready "$STATE" t1 || fail "a verified task must be ready before its evidence is removed"
  mv "$STATE/t1.project-rules.d/block.txt" "$STATE/t1.project-rules.d/block.gone"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "readiness must refuse when the log can no longer be checked against the block"
  pass "a failure inside the tool hook refuses the call, a failed scan raises one alarm, and a failed read refuses readiness"
}

test_no_node_is_needed_without_declared_rules() {
  local nonode="$TMP/nonode2" copy="$TMP/nonode.copy" state="$TMP/nonode.state" rc=0
  mkdir -p "$nonode" "$state"; fm_git_init_commit "$copy"
  for tool in dirname basename sed ls rm cat head cut; do ln -sf "$(command -v "$tool")" "$nonode/$tool"; done
  PATH="$nonode" /bin/bash "$PR" admit "$state" t1 "$copy" claude >/dev/null 2>&1 || rc=$?
  expect_code 3 "$rc" "admission of an undeclared copy with node missing"
  PATH="$nonode" /bin/bash "$PR" ready "$state" t1 || fail "readiness of an unrecorded task must pass with node missing"
  assert_equals "" "$(PATH="$nonode" /bin/bash "$PR" scan "$state")" "a scan with no records must be silent with node missing"
  pass "a project with no declared rules never needs node"
}

test_version_drift_launches_with_children_denied() {
  local out rc=0
  STATE="$TMP/vdrift.state"; COPY=$(new_copy vdrift.copy); mkdir -p "$STATE" "$TMP/vdrift.config" "$TMP/vdrift.bin"
  printf '#!/bin/sh\necho "9.9.9 (Claude Code)"\n' > "$TMP/vdrift.bin/claude"; chmod +x "$TMP/vdrift.bin/claude"
  printf 'tool claude 1.0.0 herdr\nchild claude 1.0.0 general-purpose\n' > "$TMP/vdrift.config/project-rules-qualified"
  printf 'Brief.\n' > "$TMP/vdrift.brief"
  out=$(PATH="$TMP/vdrift.bin:$PATH" FM_PROJECT_RULES_QUALIFYING='' "$PR" admit "$STATE" t1 "$COPY" claude --brief "$TMP/vdrift.brief" --backend herdr --config "$TMP/vdrift.config" 2>&1) || fail "a qualified pair on a new tool version must still launch: $out"
  assert_contains "$out" "notice: claude 9.9.9 has not passed" "a new tool version must be announced"
  close_stage
  hook pretool '{"tool_name":"Agent","tool_input":{"subagent_type":"general-purpose"}}' >/dev/null 2>&1 || rc=$?
  expect_code 2 "$rc" "a child of any type on an unqualified tool version"
  assert_contains "$("$PR" scan "$STATE" t1)" "unqualified-version claude 9.9.9" "an unqualified tool version must alarm"
  printf 'tool claude 9.9.9 herdr\nchild claude 9.9.9 general-purpose\n' >> "$TMP/vdrift.config/project-rules-qualified"
  PATH="$TMP/vdrift.bin:$PATH" FM_PROJECT_RULES_QUALIFYING='' "$PR" admit "$STATE" t1 "$COPY" claude --brief "$TMP/vdrift.brief" --backend herdr --config "$TMP/vdrift.config" >/dev/null 2>&1 || fail "a qualified version must be admitted"
  close_stage
  hook pretool '{"tool_name":"Agent","tool_input":{"subagent_type":"general-purpose"}}' >/dev/null 2>&1 || fail "a qualified child type on a qualified version must be allowed"
  pass "a tool version the live guard has not seen launches with children denied until it is qualified"
}

test_relaunch_admits_a_copy_with_uncommitted_work() {
  admit dirty claude >/dev/null
  printf 'work in progress\n' > "$COPY/unsaved.txt"; printf 'more\n' >> "$COPY/README.md"
  "$PR" admit "$STATE" t1 "$COPY" claude --backend herdr >/dev/null || fail "a second admission must accept the worker's own uncommitted work"
  assert_equals start "$(field stage.name)" "a relaunch starts a fresh start stage"
  edit_list "$COPY" 'L.prepare={argv:["sh","-c","echo more >> README.md"],timeout_s:5}'
  "$PR" admit "$STATE" t1 "$COPY" claude --backend herdr >/dev/null 2>&1 && fail "a prepare step that changes a file the worker had already modified must be refused"
  pass "admission refuses only what the project's own commands change, so a relaunch of a dirty copy is admitted"
}

test_readiness_on_touched_paths() {
  admit paths claude >/dev/null; claude_evidence; close_stage
  "$PR" ready "$STATE" t1 || fail "readiness must pass when nothing triggered"
  mkdir -p "$COPY/apps/api/src/server"; printf 'x\n' > "$COPY/apps/api/src/server/q.ts"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "a diff that touches a triggered path must block readiness until its skill is read"
  read_all audit >/dev/null; "$PR" ready "$STATE" t1 || fail "readiness must pass once the path skill is read"
  hook session-start '{"source":"compact"}' >/dev/null; close_stage; claude_evidence boundary
  "$PR" ready "$STATE" t1 2>/dev/null && fail "a path skill read before the last compaction must not satisfy readiness"
  read_all audit >/dev/null; "$PR" ready "$STATE" t1 || fail "readiness must pass once the path skill is read in the current generation"
  pass "readiness requires the skill for any triggered path the work touched, read in the current generation"
}

test_merge_settings_preserves_existing_content() {
  local file="$TMP/settings.local.json" rc=0
  admit merge claude >/dev/null
  printf '{"permissions":{"allow":["Write"]},"hooks":{"Stop":[{"hooks":[{"type":"command","command":"theirs"}]}]}}' > "$file"
  printf '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"ours"}]}]}}' | "$PR" merge-settings "$file" "$STATE" t1 || fail "merging into a valid settings file must succeed"
  assert_equals 'Write theirs ours true true' "$(node -e 'const s=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));console.log(s.permissions.allow[0],s.hooks.Stop[0].hooks[0].command,s.hooks.Stop[1].hooks[0].command,s.hooks.SessionStart.length===1,s.hooks.PreToolUse.length===1)' "$file")" "existing settings must survive beside Firstmate's hooks"
  printf '{broken' > "$file"
  printf '{"hooks":{}}' | "$PR" merge-settings "$file" "$STATE" t1 2>/dev/null || rc=$?
  expect_code 1 "$rc" "an existing settings file that does not parse"
  assert_equals '{broken' "$(cat "$file")" "an unparsable settings file must be left untouched"
  pass "Firstmate's Claude hooks are merged into existing settings, never written over them"
}

test_size_and_emit() {
  local out
  admit size codex >/dev/null
  out=$("$PR" size "$COPY" codex)
  assert_contains "$out" "budget_bytes=64000 cap_bytes=79000" "size must report the project budget and the cap"
  assert_contains "$out" "payload_bytes=$(field payload_bytes) " "size and admission must agree on payload bytes"
  out=$("$PR" emit "$STATE" t1)
  assert_equals "$(cat "$STATE/t1.project-rules.d/block.txt")" "$(node -e 'process.stdout.write(JSON.parse(process.argv[1].replace(/^developer_instructions=/,"")).replace(/\n$/,""))' "$out")" "the Codex override must carry the block byte for byte"
  pass "size reports comparable numbers and the Codex override round-trips the block"
}

# spawn_case <name> <harness> [list-edit]: drive the real fm-spawn against a
# fake pane for a project that declares rules. Sets S_HOME, S_WT, S_LOG, S_RC.
S_HOME=; S_WT=; S_LOG=; S_RC=; S_ERR=
spawn_case() {
  local name=$1 harness=$2 edit=${3:-} proj fakebin
  proj=$(new_copy "$name.proj")
  [ -z "$edit" ] || edit_list "$proj" "$edit"
  fm_git_add_origin "$proj" "$proj.origin.git"
  S_WT="$TMP/$name.wt"; S_HOME="$TMP/$name.home"; S_LOG="$TMP/$name.launch.log"; S_ERR="$TMP/$name.err"
  git -C "$proj" worktree add --quiet -b "wt-$name" "$S_WT"
  fakebin=$(fm_test_make_spawn_fakebin "$TMP/$name.fake")
  fm_test_spawn_home "$S_HOME" "$harness"
  fm_test_spawn_brief "$S_HOME" "rules-$name"
  : > "$S_LOG"; S_RC=0
  FM_FAKE_LAUNCH_LOG="$S_LOG" fm_test_run_spawn "$S_HOME" "$S_WT" "$fakebin" "rules-$name" "$proj" --scout >"$S_ERR" 2>&1 || S_RC=$?
}

test_spawn_delivers_the_block_to_claude() {
  local launch prompt
  spawn_case sclaude claude
  expect_code 0 "$S_RC" "a claude spawn into a project with declared rules ($(tail -3 "$S_ERR" | tr '\n' ' '))"
  launch=$(cat "$S_LOG")
  prompt="$S_HOME/state/rules-sclaude.project-rules.d/claude-prompt"
  assert_contains "$launch" "--append-system-prompt \"\$(cat '" "the claude launch must read its system prompt from the rendered file"
  assert_grep "You are a task worker launched by Firstmate" "$prompt" "the rendered prompt must keep the worker statement"
  assert_grep 'Web rule: `keep` the "frame" stable.' "$prompt" "the rendered prompt must carry the rules block"
  assert_grep "# Project rules gate" "$S_HOME/data/rules-sclaude/launch-brief.md" "the launch brief must carry the startup gate"
  assert_contains "$launch" "admission-brief.md" "the worker must be launched on the admission message, not on its task"
  assert_grep "Your task is not in this message." "$S_HOME/data/rules-sclaude/admission-brief.md" "the admission message must say the task comes later"
  assert_grep "# Current worker role contract" "$S_HOME/data/rules-sclaude/admission-brief.md" "the admission message must keep the worker role contract"
  assert_no_grep "brief for rules-sclaude" "$S_HOME/data/rules-sclaude/admission-brief.md" "the admission message must not contain the task"
  assert_grep "brief for rules-sclaude" "$S_HOME/data/rules-sclaude/launch-brief.md" "the task stays in the brief the helper hands over"
  assert_equals "1 1 4" "$(node -e 'const h=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")).hooks;console.log(h.SessionStart.length,h.PreToolUse.length,["UserPromptSubmit","Stop","StopFailure","SessionEnd"].filter(k=>h[k]).length)' "$S_WT/.claude/settings.local.json")" "the worker settings must hold the rules hooks beside the busy hooks"
  assert_equals start "$(node -e 'process.stdout.write(JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")).stage.name)' "$S_HOME/state/rules-sclaude.project-rules")" "admission must leave the start stage open"
  pass "fm-spawn gives a claude worker the block through its system prompt, the gate, and the rules hooks"
}

test_spawn_delivers_the_block_to_codex() {
  local launch tries=0
  spawn_case scodex codex
  expect_code 0 "$S_RC" "a codex spawn into a project with declared rules ($(tail -3 "$S_ERR" | tr '\n' ' '))"
  launch=$(cat "$S_LOG")
  assert_contains "$launch" "--disable hooks" "a codex worker must keep its hook layer off"
  assert_contains "$launch" "fm-project-rules.sh' emit '" "the codex launch must pass the block as developer instructions"
  assert_contains "$launch" "admission-brief.md" "a codex worker must be launched on the admission message, not on its task"
  until [ -s "$S_HOME/state/rules-scodex.project-rules.d/responder.pid" ]; do
    tries=$((tries + 1)); [ "$tries" -lt 50 ] || fail "fm-spawn must start the codex worker's responder with the worker"
    sleep 0.1
  done
  rm -f "$S_HOME/state/rules-scodex.project-rules"
  pass "fm-spawn gives a codex worker the block as developer instructions with hooks still off, and starts its responder"
}

test_spawn_refuses_when_admission_fails() {
  spawn_case srefuse claude 'L.prepare={argv:["sh","-c","exit 9"],timeout_s:5}'
  expect_code 1 "$S_RC" "a spawn whose project prepare step fails"
  assert_grep "prepare step failed (exit 9)" "$S_ERR" "the refusal must carry the prepare failure"
  assert_equals "" "$(cat "$S_LOG")" "no agent may be launched after a refused admission"
  spawn_case sother pi
  expect_code 1 "$S_RC" "a spawn of an unsupported tool into a project with declared rules"
  assert_equals "" "$(cat "$S_LOG")" "no unsupported tool may be launched into a project with declared rules"
  pass "fm-spawn launches nothing when admission is refused"
}

test_no_declared_list_changes_nothing
test_admission_refusals
test_prepare_runs_scrubbed
test_block_receipt_and_chained_reads
test_a_skipped_part_cannot_complete
test_claude_hook_verdicts
test_compaction_reopens_and_ages_answers
test_superseded_stage_keeps_obligations
test_receipt_table_exhaustion
test_claude_scan_evidence_and_witness
test_claude_scan_reports_changed_rules_and_stale_stage
test_codex_scan_start_and_mid_turn_compaction
test_codex_scan_between_turn_compaction_and_triggers
test_codex_scan_reads_every_command_shape
test_codex_read_then_run_in_one_call_is_in_order
test_codex_following_the_doorbell_is_not_a_violation
test_codex_helper_spelled_through_a_variable
test_codex_admission_then_work_in_one_call_is_in_order
test_codex_scan_is_idempotent_and_cursor_safe
test_codex_missing_evidence_and_witness
test_codex_responder_interrupts_a_compaction_and_a_skipped_trigger
test_codex_responder_leaves_everything_else_alone
test_codex_sub_agent_log_is_never_read_as_the_workers
test_a_slow_pattern_counts_as_a_match
test_drift_covers_skills_and_the_declaration
test_failures_inside_the_hook_and_scan_are_never_silent
test_no_node_is_needed_without_declared_rules
test_version_drift_launches_with_children_denied
test_relaunch_admits_a_copy_with_uncommitted_work
test_readiness_on_touched_paths
test_merge_settings_preserves_existing_content
test_size_and_emit
test_spawn_delivers_the_block_to_claude
test_spawn_delivers_the_block_to_codex
test_spawn_refuses_when_admission_fails

echo "# all fm-project-rules tests passed"
