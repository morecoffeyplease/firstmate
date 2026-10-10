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
    code=$(printf '%s\n' "$out" | sed -n "s/.*continue with: .* '$what' \([0-9a-f]\{8\}\) ---\$/\1/p" | tail -1)
    [ -n "$code" ] || return 1
    out=$("$PR" serve "$STATE" t1 "$what" "$code") || return 1
    case "$out" in *"delivered and recorded"*) break ;; esac
  done
  printf '%s\n' "$parts"
}
close_stage() { "$PR" ack "$STATE" t1 "$(row_code)" >/dev/null && read_all brief >/dev/null && read_all flow >/dev/null; }
edit_list() { node -e 'const fs=require("fs");const f=process.argv[1];const L=JSON.parse(fs.readFileSync(f,"utf8"));eval(process.argv[2]);fs.writeFileSync(f,JSON.stringify(L))' "$1/.agents/project-rules.json" "$2"; git -C "$1" -c user.name=t -c user.email=t@example.invalid commit -qam edit; }
hook() { printf '%s' "$2" | "$PR" hook "$STATE" t1 "$1"; }

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
  pass "the project prepare step runs with a reduced environment that keeps HOME"
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
  assert_equals 64 "$(printf '%s\n' "$block" | grep -c '^row[0-9][0-9]=[A-Z2-9]\{6\}$')" "the block must end with a 64-row receipt table"
  out=$("$PR" next "$STATE" t1)
  assert_contains "$out" "stage start) is OPEN" "next must report the open start stage"
  assert_contains "$out" "Load required skill flow" "next must list the start skill"
  assert_not_contains "$out" "Load required skill tests" "a command-triggered skill is not owed at start"
  "$PR" ack "$STATE" t1 ZZZZZZ >/dev/null 2>&1 || rc=$?
  expect_code 1 "$rc" "a wrong receipt code"
  "$PR" ack "$STATE" t1 "$(row_code)" >/dev/null || fail "the right receipt code must be accepted"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "readiness must fail while the brief and start skill are owed"
  rc=0; "$PR" serve "$STATE" t1 tests deadbeef >/dev/null 2>&1 || rc=$?
  expect_code 1 "$rc" "a part code that was never issued"
  read_all brief >/dev/null || fail "the brief must be readable to the end"
  read_all flow >/dev/null || fail "the start skill must be readable to the end"
  assert_not_equals null "$(field stage.closed)" "the stage must close once the row, brief and start skill are done"
  assert_contains "$("$PR" next "$STATE" t1)" "nothing is owed right now" "next must report a closed stage"
  parts=$(read_all tests) || fail "a long skill must be readable to the end"
  [ "$parts" -ge 3 ] || fail "a long skill must be served in several parts, got $parts"
  assert_equals 0 "$(field 'answers.tests.gen')" "a completed skill read is recorded for the current generation"
  pass "the block inlines non-native rules and the catalog, and receipts and chained reads close the start stage"
}

test_a_skipped_part_cannot_complete() {
  local out first second
  admit skip claude >/dev/null
  out=$("$PR" serve "$STATE" t1 tests)
  first=$(printf '%s\n' "$out" | sed -n "s/.*continue with: .* 'tests' \([0-9a-f]\{8\}\) ---\$/\1/p")
  out=$("$PR" serve "$STATE" t1 tests "$first")
  assert_contains "$out" "part 2 of" "the first code must release exactly the second part"
  assert_not_contains "$out" "TAIL-OF-TESTS-SKILL" "the second part must not contain the end of a longer skill"
  assert_equals undefined "$(field 'answers.tests')" "a partly read skill must not be recorded"
  second=$(printf '%s\n' "$out" | sed -n "s/.*continue with: .* 'tests' \([0-9a-f]\{8\}\) ---\$/\1/p")
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
  allowed "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"FM_HOME='/x' '$ROOT/bin/fm-project-rules.sh' next '$STATE' 't1'\"}}" "the helper itself must stay allowed while a stage is open"
  allowed '{"tool_name":"Read","tool_input":{"file_path":"/tmp/x"}}' "file reads must stay allowed while a stage is open"
  close_stage || fail "the start stage must close"
  allowed '{"tool_name":"Bash","tool_input":{"command":"ls"}}' "an ordinary command must be allowed after admission"
  denied '{"tool_name":"Agent","tool_input":{"subagent_type":"Explore"}}' "an unqualified child type" "general-purpose"
  allowed '{"tool_name":"Agent","tool_input":{"subagent_type":"general-purpose"}}' "a qualified child type must be allowed"
  denied '{"tool_name":"Bash","tool_input":{"command":"cd apps && bun run test"}}' "a triggered command before its skill" "serve"
  denied "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$COPY/apps/api/src/server/a/b.ts\"}}" "an edit on a triggered path before its skill" "audit"
  allowed "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$COPY/apps/web/x.ts\"}}" "an edit outside the triggered paths must be allowed"
  read_all tests >/dev/null
  allowed '{"tool_name":"Bash","tool_input":{"command":"bun run test"}}' "a triggered command must be allowed once its skill is read"
  denied '{"agent_id":"a1","tool_name":"Bash","tool_input":{"command":"bun run test"}}' "a triggered command inside a child" "main worker"
  allowed '{"tool_name":"Bash","tool_input":{"command":"bun run tests-unrelated"}}' "a command that only resembles the trigger must be allowed"
  pass "the Claude hook refuses work before admission, unqualified children, and triggered work before its skill"
}

test_compaction_reopens_and_ages_answers() {
  local first out rc=0
  admit compact claude >/dev/null; close_stage; read_all tests >/dev/null
  first=$(field stage.row)
  out=$(hook session-start '{"source":"compact","transcript_path":"/nonexistent"}')
  assert_contains "$out" "stage c1) is open" "the compaction hook must tell the worker a stage is open"
  assert_not_equals "$first" "$(field stage.row)" "a new stage must ask for a row no earlier stage used"
  out=$("$PR" next "$STATE" t1)
  assert_contains "$out" "Load required skill flow" "the start skill is owed again after a compaction"
  hook pretool '{"tool_name":"Bash","tool_input":{"command":"ls"}}' >/dev/null 2>&1 || rc=$?
  expect_code 2 "$rc" "work between a compaction and its refresh"
  close_stage
  rc=0; hook pretool '{"tool_name":"Bash","tool_input":{"command":"bun run test"}}' >/dev/null 2>&1 || rc=$?
  expect_code 2 "$rc" "a triggered command after a compaction, before the skill is read again"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "a skill whose trigger fired must be current before readiness"
  read_all tests >/dev/null; "$PR" ready "$STATE" t1 || fail "readiness must pass once every owed read is current"
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
  node -e 'const fs=require("fs");const [state,copy,bin,file,js]=process.argv.slice(1);const block=fs.readFileSync(state+"/t1.project-rules.d/block.txt","utf8");const dev=(t)=>({type:"response_item",payload:{type:"message",role:"developer",content:[{type:"input_text",text:t}]}});const agents=()=>({type:"response_item",payload:{type:"message",role:"user",content:[{type:"input_text",text:"# AGENTS.md instructions for "+copy+"\n\n# Agents core\n\nNever rename the widget.\n"}]}});const call=(cmd,ts)=>({type:"response_item",timestamp:ts||new Date().toISOString(),payload:{type:"custom_tool_call",name:"exec",input:"text(await tools.exec_command({cmd:"+JSON.stringify(cmd)+"}));\n"}});const started=()=>({type:"event_msg",payload:{type:"task_started"}});const event=()=>({type:"event_msg",payload:{type:"item_completed",item:{type:"ContextCompaction"}}});const compacted=(history)=>({type:"compacted",payload:{replacement_history:history||[]}});const rows=eval(js);fs.appendFileSync(file,rows.map(r=>typeof r==="string"?r:JSON.stringify(r)).join("\n")+(process.env.NO_NEWLINE?"":"\n"))' "$STATE" "$COPY" "$ROOT/bin" "$LOG" "$1"
}

test_codex_scan_start_and_mid_turn_compaction() {
  local out
  admit xscan codex >/dev/null; codex_fixture xscan
  codex_log '[started(),dev("skills\n"),dev(block),agents(),call(bin+"/fm-project-rules.sh next")]'
  close_stage
  codex_log '[call("ls")]'
  assert_equals "" "$("$PR" scan "$STATE" t1)" "a Codex session with the block, the native file, and work only after admission must not alarm"
  codex_log '[compacted([{type:"message",role:"developer",content:[{text:block}]},{type:"message",role:"user",content:[{text:"# AGENTS.md instructions for x\n\n# Agents core\n\nNever rename the widget.\n"}]}]),event(),call("ls"),call("ls")]'
  "$PR" detect "$STATE" t1
  assert_equals c1 "$(field stage.name)" "a compaction record must open a stage"
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
  codex_log '[call("bun run test")]'
  out=$("$PR" scan "$STATE" t1)
  assert_contains "$out" "trigger-skipped tests c1" "a triggered command without its skill must alarm on Codex"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "a skipped trigger must block readiness until the skill is read"
  read_all tests >/dev/null; "$PR" ready "$STATE" t1 || fail "readiness must pass once the skill is read"
  pass "the Codex scan accepts next-turn evidence and catches a triggered command run without its skill"
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

test_readiness_on_touched_paths() {
  admit paths claude >/dev/null; close_stage
  "$PR" ready "$STATE" t1 || fail "readiness must pass when nothing triggered"
  mkdir -p "$COPY/apps/api/src/server"; printf 'x\n' > "$COPY/apps/api/src/server/q.ts"
  "$PR" ready "$STATE" t1 2>/dev/null && fail "a diff that touches a triggered path must block readiness until its skill is read"
  read_all audit >/dev/null; "$PR" ready "$STATE" t1 || fail "readiness must pass once the path skill is read"
  pass "readiness requires the skill for any triggered path the work touched"
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
  assert_equals "1 1 4" "$(node -e 'const h=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")).hooks;console.log(h.SessionStart.length,h.PreToolUse.length,["UserPromptSubmit","Stop","StopFailure","SessionEnd"].filter(k=>h[k]).length)' "$S_WT/.claude/settings.local.json")" "the worker settings must hold the rules hooks beside the busy hooks"
  assert_equals start "$(node -e 'process.stdout.write(JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")).stage.name)' "$S_HOME/state/rules-sclaude.project-rules")" "admission must leave the start stage open"
  pass "fm-spawn gives a claude worker the block through its system prompt, the gate, and the rules hooks"
}

test_spawn_delivers_the_block_to_codex() {
  local launch
  spawn_case scodex codex
  expect_code 0 "$S_RC" "a codex spawn into a project with declared rules ($(tail -3 "$S_ERR" | tr '\n' ' '))"
  launch=$(cat "$S_LOG")
  assert_contains "$launch" "--disable hooks" "a codex worker must keep its hook layer off"
  assert_contains "$launch" "fm-project-rules.sh' emit '" "the codex launch must pass the block as developer instructions"
  assert_contains "$launch" "fm-project-rules.sh' detect '" "the codex turn-end program must also check for a compaction"
  pass "fm-spawn gives a codex worker the block as developer instructions with hooks still off"
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
test_codex_scan_is_idempotent_and_cursor_safe
test_codex_missing_evidence_and_witness
test_readiness_on_touched_paths
test_merge_settings_preserves_existing_content
test_size_and_emit
test_spawn_delivers_the_block_to_claude
test_spawn_delivers_the_block_to_codex
test_spawn_refuses_when_admission_fails

echo "# all fm-project-rules tests passed"
