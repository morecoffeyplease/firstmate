#!/usr/bin/env bash
# Live acceptance guard for project-rules delivery (docs/project-rules.md).
#
# The verdict comes from the installed Claude and Codex, not from a stub: what
# this guards is exactly what each vendor's tool loads, keeps through a
# compaction, and writes to its own log. Each tool is launched by the real
# bin/fm-spawn.sh into an isolated Herdr lab, on a fresh pool copy of a project
# that declares its rules, and must pass every stage twice:
#   A  admission, the rules and catalog in context, a required-skill command,
#      a refused child type, and what a permitted child holds
#   B  the same after a forced compaction
#   C  the same after an automatic compaction, in a second session replayed
#      from the captured launch with the tool's compaction limit lowered
# A stage passes only when the worker's own report and the tool's own log
# agree. A leg that could not be measured is reported as unknown and fails the
# run, so an unknown never qualifies a tool.
#
# It submits prompts and spends model tokens, so it is opt-in:
#   FM_PROJECT_RULES_LIVE_E2E=1 tests/fm-project-rules-live-e2e.test.sh
# Controls:
#   FM_PROJECT_RULES_ACCEPT_REPO=<clone>   test that project's declared list on a
#     throwaway clone of it instead of the built-in fixture; the clone must have
#     no plugin install record, so its prepare step is what resolves the rules
#   FM_PROJECT_RULES_ACCEPT_TRIGGER=<cmd>  a safe command matching one of that
#     project's before_commands patterns
#   FM_PROJECT_RULES_LIVE_TOOLS="claude codex"   FM_PROJECT_RULES_LIVE_RUNS=2
#   FM_PROJECT_RULES_LIVE_LEGS=forced|automatic   run one leg while iterating; a
#     run with a leg left out reports it as unknown and qualifies nothing, and
#     so does a run with fewer than 2 sessions per leg
#   FM_PROJECT_RULES_LIVE_CLAUDE_ARGS / FM_PROJECT_RULES_LIVE_CODEX_ARGS   extra
#     fm-spawn arguments, for example "--model gpt-6.1-sol --effort medium"
#   FM_PROJECT_RULES_ACCEPT_PLUGIN_RULE_DIRS / FM_PROJECT_RULES_ACCEPT_SKILL_DIRS
#     colon-separated folders of the pinned plugin rules and skills, checked
#     against the declared list independently of it
#   FM_PROJECT_RULES_ACCEPT_PLUGINS_DIR=<dir>  the plugin store prepare reads; a
#     copy with one changed byte must make prepare refuse
#   FM_PROJECT_RULES_ACCEPT_PAYLOAD_CLAUDE / _CODEX  the payload bytes the
#     project's own budget check reports, compared with Firstmate's number
#   FM_PROJECT_RULES_QUALIFY_CONFIG=<dir>  on a full pass, merge the qualified
#     tool, version, backend and child rows into <dir>/project-rules-qualified
# shellcheck disable=SC2016 # Fixture text and node one-liners are literal on purpose.
set -u

# shellcheck source=tests/fixtures.sh
. "$(dirname "${BASH_SOURCE[0]}")/fixtures.sh"

fm_live_gate opt-in FM_PROJECT_RULES_LIVE_E2E herdr jq node treehouse git

note() { printf '# %s\n' "$1"; }
PR="$ROOT/bin/fm-project-rules.sh"
export FM_PROJECT_RULES_SEND=true  # this guard's own log reads must not steer the worker under test
TOOLS=${FM_PROJECT_RULES_LIVE_TOOLS:-claude codex}
RUNS=${FM_PROJECT_RULES_LIVE_RUNS:-2}
TMP=$(fm_test_tmproot fm-project-rules-live)
LAB_HELPER="$ROOT/bin/fm-herdr-lab.sh"
LAB=$("$LAB_HELPER" name fm-67-rules-live) || fail "could not name an isolated Herdr lab"
cleanup() {
  local status=$?
  "$LAB_HELPER" teardown "$LAB" || status=1
  fm_test_cleanup
  exit "$status"
}
trap cleanup EXIT
"$LAB_HELPER" provision "$LAB" || fail "could not provision the isolated Herdr lab"
lab() { "$LAB_HELPER" run "$LAB" "$@"; }

# Every Herdr call fm-spawn makes is forced through the lab helper.
SHIM="$TMP/shim"; mkdir -p "$SHIM"
cat > "$SHIM/herdr" <<SH
#!/usr/bin/env bash
set -u
clean_path=\${PATH#'$SHIM:'}
if [ "\$#" -eq 2 ] && [ "\$1" = status ] && [ "\$2" = --json ]; then
  exec env PATH="\$clean_path" '$LAB_HELPER' run '$LAB' status --json
fi
if [ "\$#" -lt 2 ] || [ "\${@: -2:1}" != --session ] || [ "\${@: -1}" != '$LAB' ]; then
  echo "refusing Herdr call outside the task lab" >&2
  exit 97
fi
set -- "\${@:1:\$#-2}"
exec env PATH="\$clean_path" '$LAB_HELPER' run '$LAB' "\$@"
SH
chmod +x "$SHIM/herdr"

# ---- the project under test ---------------------------------------------

commit_all() { git -C "$1" add -A && git -C "$1" -c user.name='Firstmate Tests' -c user.email='tests@example.invalid' commit -qm "$2" --no-verify; }

# The built-in fixture declares one file of each kind the contract names.
build_fixture() {  # <dir>
  local repo=$1
  fm_git_init_commit "$repo"
  mkdir -p "$repo/.agents" "$repo/.claude/rules" "$repo/apps/web" "$repo/tools" "$repo/vendor/rules" "$repo/skills/flow" "$repo/skills/tests" "$repo/skills/extra"
  printf '.prepared\n' > "$repo/.gitignore"
  printf '# Core for Codex\n\nNever rename the widget.\n' > "$repo/AGENTS.md"
  printf '# Map\n\nThe apps live under apps/.\n' > "$repo/CLAUDE.md"
  printf '# Core for Claude\n\nNever rename the widget.\n' > "$repo/.claude/rules/core.md"
  printf '# Plugin rule\n\nKeep queries bounded.\n' > "$repo/vendor/rules/standin.md"
  { printf '<!-- generated from vendor/rules/standin.md -->\n'; cat "$repo/vendor/rules/standin.md"; } > "$repo/.claude/rules/standin.md"
  printf '# Web area\n\nWeb pages use the shared frame.\n' > "$repo/apps/web/CLAUDE.md"
  printf '{"name":"web"}\n' > "$repo/apps/web/package.json"
  printf '#!/bin/sh\ntouch .prepared\n' > "$repo/tools/prepare.sh"
  printf '#!/bin/sh\nprintf "%%s/vendor/%%s\\n" "$(pwd -P)" "$1"\n' > "$repo/tools/resolve.sh"
  printf '#!/bin/sh\necho PRODUCT-TEST-RAN\n' > "$repo/tools/product-test.sh"
  printf '# Flow\n\nWork in small steps.\n' > "$repo/skills/flow/SKILL.md"
  { printf '# Tests\n\n'; for i in $(seq 1 260); do printf 'Step %s: run the canonical runner and read its output.\n' "$i"; done; } > "$repo/skills/tests/SKILL.md"
  printf '# Extra\n\nOptional reading.\n' > "$repo/skills/extra/SKILL.md"
  cat > "$repo/.agents/project-rules.json" <<'JSON'
{
  "version": 1,
  "budget_bytes": 64000,
  "prepare": { "argv": ["sh", "tools/prepare.sh"], "timeout_s": 30 },
  "rules": [
    { "id": "core", "path": "AGENTS.md", "tools": ["codex"], "native": ["codex"] },
    { "id": "core-claude", "path": ".claude/rules/core.md", "tools": ["claude"], "native": ["claude"] },
    { "id": "map", "path": "CLAUDE.md", "native": ["claude"] },
    { "id": "area-web", "path": "apps/web/CLAUDE.md" },
    { "id": "plugin", "resolve": ["sh", "tools/resolve.sh", "rules/standin.md"], "tools": ["codex"] },
    { "id": "plugin-claude", "path": ".claude/rules/standin.md", "tools": ["claude"], "native": ["claude"] }
  ],
  "skills": [
    { "name": "flow", "description": "How work proceeds here.", "invocation": "manual", "body": { "path": "skills/flow/SKILL.md" }, "required": { "at": "start" } },
    { "name": "tests", "description": "How to run product tests.", "invocation": "model", "body": { "path": "skills/tests/SKILL.md" }, "required": { "before_commands": ["(^|[;&| ])sh tools/product-test\\.sh( |$)"] } },
    { "name": "extra", "description": "Optional reading.", "invocation": "model", "body": { "path": "skills/extra/SKILL.md" } }
  ],
  "dispatched_child_types": ["general-purpose", "Explore"]
}
JSON
  commit_all "$repo" fixture
}

PROJECT="$TMP/project"
TRIGGER=${FM_PROJECT_RULES_ACCEPT_TRIGGER:-}
if [ -n "${FM_PROJECT_RULES_ACCEPT_REPO:-}" ]; then
  git clone --quiet --local "$FM_PROJECT_RULES_ACCEPT_REPO" "$PROJECT" || fail "could not clone $FM_PROJECT_RULES_ACCEPT_REPO"
  git -C "$PROJECT" remote remove origin
  [ -f "$PROJECT/.agents/project-rules.json" ] || fail "$FM_PROJECT_RULES_ACCEPT_REPO declares no project rules"
  records=$(node -e 'const fs=require("fs"),p=require("path");const f=p.join(process.env.CLAUDE_PLUGINS_DIR||p.join(process.env.CLAUDE_CONFIG_DIR||p.join(process.env.HOME,".claude"),"plugins"),"installed_plugins.json");if(!fs.existsSync(f)){console.log(0);process.exit()}const real=fs.realpathSync(process.argv[1]);let n=0;for(const list of Object.values(JSON.parse(fs.readFileSync(f,"utf8")).plugins||{}))for(const r of list)if(r.projectPath&&p.resolve(r.projectPath).startsWith(real))n++;console.log(n)' "$TMP")
  [ "$records" = 0 ] || fail "the throwaway clone already has $records plugin install record(s), so prepare would not be what resolves its rules"
else
  build_fixture "$PROJECT"
  export FM_PROJECT_RULES_FIXTURE=1
  TRIGGER='sh tools/product-test.sh'
  FM_PROJECT_RULES_ACCEPT_PLUGIN_RULE_DIRS="$PROJECT/vendor/rules"
  FM_PROJECT_RULES_ACCEPT_SKILL_DIRS="$PROJECT/skills"
fi

# One canary per tracked declared file, and an independent check that no
# tracked instruction file was left off the list.
CANARIES="$TMP/canaries.tsv"
node -e '
const fs=require("fs"),path=require("path"),{execFileSync}=require("child_process");
const [repo,out]=process.argv.slice(1);
const L=JSON.parse(fs.readFileSync(path.join(repo,".agents/project-rules.json"),"utf8"));
const tracked=execFileSync("git",["-C",repo,"ls-files"],{encoding:"utf8"}).split("\n").filter(f=>/(^|\/)(CLAUDE|AGENTS)\.md$/.test(f)||/^\.claude\/rules\/.+\.md$/.test(f));
const listed=new Set(L.rules.filter(r=>r.path).map(r=>r.path));
const missing=tracked.filter(f=>!listed.has(f));
if(missing.length){console.error("left off the declared list: "+missing.join(", "));process.exit(1)}
const rows=[];
// In the built-in fixture the resolved file is ours, so it carries a canary too.
if(process.env.FM_PROJECT_RULES_FIXTURE){fs.appendFileSync(path.join(repo,"vendor/rules/standin.md"),"\nContext audit line: FMQ-PLUGIN-9001\n");rows.push(["codex","plugin","FMQ-PLUGIN-9001","inline","vendor/rules/standin.md"].join("\t"))}
L.rules.filter(r=>r.path).forEach((r,i)=>{const token="FMQ-"+r.id.replace(/[^a-z]/g,"").toUpperCase()+"-"+(1000+i*37);fs.appendFileSync(path.join(repo,r.path),"\nContext audit line: "+token+"\n");for(const tool of r.tools||["claude","codex"])rows.push([tool,r.id,token,(r.native||[]).includes(tool)?"native":"inline",r.path].join("\t"))});
fs.writeFileSync(out,rows.join("\n")+"\n");
' "$PROJECT" "$CANARIES" || fail "the declared list does not cover every tracked instruction file"
unset FM_PROJECT_RULES_FIXTURE
commit_all "$PROJECT" "context audit canaries (throwaway copy only)"
fm_git_add_origin "$PROJECT" "$PROJECT.origin.git" 2>/dev/null || true
AREA_RULE=$(awk -F'\t' '$1=="claude" && $4=="inline" && $5 ~ /\/CLAUDE\.md$/ {print $5; exit}' "$CANARIES")
AREA_TOKEN=$(awk -F'\t' -v p="$AREA_RULE" '$1=="claude" && $5==p {print $3; exit}' "$CANARIES")
AREA_FILE=
[ -z "$AREA_RULE" ] || AREA_FILE=$(git -C "$PROJECT" ls-files "$(dirname "$AREA_RULE")" | grep -v 'CLAUDE\.md$' | head -1)
note "project under test: ${FM_PROJECT_RULES_ACCEPT_REPO:-built-in fixture}, $(wc -l < "$CANARIES" | tr -d ' ') tool-file canaries, child area file ${AREA_FILE:-none}"

# ---- driving one worker --------------------------------------------------

HOME_DIR="$TMP/home"; mkdir -p "$HOME_DIR/data" "$HOME_DIR/projects" "$HOME_DIR/state" "$HOME_DIR/config" "$TMP/pool"
touch "$HOME_DIR/state/.last-watcher-beat"; printf 'off\n' > "$HOME_DIR/config/herdr-presentation-spaces"
STATE=$(cd "$HOME_DIR/state" && pwd -P)
BRIEF_TOKEN="FMQ-BRIEF-4242"
ID=; TOOL=; PANE=; REPORT=; VERSION=

say() { lab pane send-text "$PANE" "$1" >/dev/null && sleep 1.5 && lab pane send-keys "$PANE" Enter >/dev/null; sleep 4; lab pane send-keys "$PANE" Enter >/dev/null 2>&1 || true; }
# field evaluates an expression written in this file over the task record;
# no outside input reaches it.
field() { "$PR" status "$STATE" "$ID" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const r=JSON.parse(s);let v;try{v=new Function(...Object.keys(r),"return ("+process.argv[1]+")")(...Object.values(r))}catch{v=undefined}process.stdout.write(String(v))})' "$1"; }
# A message typed while the tool is still compacting is summarized away with
# the rest of the history, so the next stage is sent only once the compaction
# is on record.
wait_compactions() {  # <count>
  local waited=0
  while [ "$waited" -lt 300 ]; do
    "$PR" detect "$STATE" "$ID" >/dev/null 2>&1 || true
    [ "$(field compactions)" -lt "$1" ] 2>/dev/null || { sleep 20; return 0; }
    sleep 5; waited=$((waited + 5))
  done
  fail "$TOOL $VERSION: the forced compaction never appeared on record"
}
wait_status() {  # <line> <seconds>
  local waited=0
  while [ "$waited" -lt "$2" ]; do
    grep -qF "$1" "$STATE/$ID.status" 2>/dev/null && return 0
    sleep 5; waited=$((waited + 5))
  done
  fail "$TOOL $VERSION: the worker never reported '$1' (pane: $(lab pane read "$PANE" --source recent --lines 30 2>/dev/null | tail -12 | tr '\n' ' '))"
}

children_task() {
  if [ "$TOOL" = claude ]; then
    printf '%s' "Then try to launch one Explore child agent with the task 'say hi' and append one line beginning EXPLORE: saying whether it was allowed or refused. Then launch one general-purpose child agent with exactly this task and nothing added: 'Before using any tool, list every token of the form FMQ-<LETTERS>-<digits> literally visible in your own context, one per line, or NONE. Then read only the first 3 lines of ${AREA_FILE:-README.md} and list every such token visible now.' and append its reply verbatim under a heading '## CHILD \$STAGE'."
  else
    printf '%s' "Then spawn one sub-agent with exactly this task and nothing added: 'Before using any tool, list every token of the form FMQ-<LETTERS>-<digits> literally visible in your own context, one per line, or NONE.' and append its reply verbatim under a heading '## CHILD \$STAGE'."
  fi
}
stage_task() {  # <stage letter>
  local trigger=
  [ -z "$TRIGGER" ] || trigger="Then run exactly \`$TRIGGER\` and append the last line it prints."
  printf '%s' "Stage $1. First do whatever your project rules gate requires right now. Then, without searching and without opening any instruction or rule file yourself, append to $REPORT a section headed '## $1' listing every token of the form FMQ-<LETTERS>-<digits> literally visible in your context, one per line. $trigger $(children_task | sed "s/\\\$STAGE/$1/g") Never tell a child any token. Then append the status line 'working: $1 written' and end your turn."
}

# A long line typed into a busy terminal can lose its start, so a later stage
# is written to a file and only a short pointer is typed.
send_stage() {  # <letter>
  local file="$HOME_DIR/data/$ID/stage-$1.md"
  stage_task "$1" > "$file"
  # Reading the stage file is itself work, so the pointer leads with the gate,
  # the way a steer through the task's inbox would.
  say "First run your project rules next command and do everything it prints. After that, read the file $file and do exactly what it says."
}

spawn_worker() {  # <tool> <run>
  TOOL=$1; ID="rules-$1-$2"; REPORT="$HOME_DIR/data/$ID/report.md"
  FM_HOME="$HOME_DIR" FM_ROOT_OVERRIDE="$ROOT" "$ROOT/bin/fm-brief.sh" "$ID" project --scout >/dev/null || fail "could not scaffold a brief"
  node -e 'const fs=require("fs");const [f,task,spec]=process.argv.slice(1);fs.writeFileSync(f,fs.readFileSync(f,"utf8").replace("{TASK}",task).replace("{FIRSTMATE_SPEC}",spec))' \
    "$HOME_DIR/data/$ID/brief.md" "Read-only context audit of this repository copy; change nothing in it. Audit marker: $BRIEF_TOKEN" "$(stage_task A) Later stages arrive as messages typed into this session."
  local args_var extra=()
  args_var="FM_PROJECT_RULES_LIVE_$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')_ARGS"
  # shellcheck disable=SC2206 # Word splitting of the caller's extra arguments is intended.
  [ -z "${!args_var:-}" ] || extra=(${!args_var})
  env -u HERDR_ENV -u HERDR_PANE_ID -u HERDR_TAB_ID -u HERDR_WORKSPACE_ID -u HERDR_SOCKET_PATH -u HERDR_BIN_PATH -u FM_TASK_ID \
    PATH="$SHIM:$PATH" HERDR_SESSION="$LAB" FM_SPAWN_NO_GUARD=1 FM_PROJECT_RULES_QUALIFYING=1 \
    FM_HOME="$HOME_DIR" FM_ROOT_OVERRIDE="$ROOT" FM_TREEHOUSE_ROOT="$TMP/pool" \
    "$ROOT/bin/fm-spawn.sh" "$ID" "$PROJECT" --scout --harness "$1" --backend herdr ${extra[@]+"${extra[@]}"} > "$TMP/$ID.spawn" 2>&1 \
    || fail "$1: the real fm-spawn refused the worker: $(tail -5 "$TMP/$ID.spawn" | tr '\n' ' ')"
  PANE=$(sed -n 's/^herdr_pane_id=//p' "$STATE/$ID.meta")
  VERSION=$(field version)
  [ -f "$(sed -n 's/^worktree=//p' "$STATE/$ID.meta")/.prepared" ] || [ -n "${FM_PROJECT_RULES_ACCEPT_REPO:-}" ] || fail "$1: the project prepare step did not run in the fresh copy"
}

# check_stage <letter> <generation>: the worker's report and the tool's own log must agree.
check_stage() {
  local stage=$1 gen=$2 section child token kind alarms
  local waited=0
  wait_status "working: $stage written" 600
  # A Codex compaction is steered at the turn end, so give the refresh time to land.
  while [ "$waited" -lt 300 ]; do
    "$PR" detect "$STATE" "$ID" >/dev/null 2>&1 || true
    [ "$(field stage.closed)" = null ] || break
    sleep 5; waited=$((waited + 5))
  done
  section=$(awk -v h="## $stage" '$0==h{p=1;next} /^## /{p=0} p' "$REPORT")
  child=$(awk -v h="## CHILD $stage" '$0==h{p=1;next} /^## /{p=0} p' "$REPORT")
  assert_equals "$gen" "$(field compactions)" "$TOOL $VERSION stage $stage: compactions recorded"
  assert_not_equals null "$(field stage.closed)" "$TOOL $VERSION stage $stage: the stage must be answered"
  while IFS=$'\t' read -r tool _ token kind _; do
    [ "$tool" = "$TOOL" ] || continue
    assert_contains "$section" "$token" "$TOOL $VERSION stage $stage: the worker must report the $kind canary $token"
    if [ "$kind" = native ] || [ "$TOOL" = codex ]; then
      assert_contains "$child" "$token" "$TOOL $VERSION stage $stage: a child must hold the $kind canary $token"
    fi
  done < "$CANARIES"
  [ "$TOOL" != claude ] || [ -z "$AREA_FILE" ] || assert_contains "$child" "$AREA_TOKEN" "$TOOL $VERSION stage $stage: a child that touches an area must hold that area's rule"
  assert_contains "$section" "$BRIEF_TOKEN" "$TOOL $VERSION stage $stage: the worker must hold its launch brief"
  if [ -n "$TRIGGER" ]; then
    assert_equals true "$(field 'skills.some((s) => s.required && s.required.before_commands && answers[s.name] && answers[s.name].gen === compactions)')" "$TOOL $VERSION stage $stage: the skill for the triggered command must be read in this generation"
    assert_equals true "$(field 'Object.values(triggered).some((n) => n > 0)')" "$TOOL $VERSION stage $stage: the triggering command must be recognized in the tool's real hook or log format"
  fi
  [ "$TOOL" != claude ] || assert_contains "$(grep '^EXPLORE:' "$REPORT" | tail -1)" "refused" "$TOOL $VERSION stage $stage: an Explore child must be refused"
  alarms=$("$PR" scan "$STATE" "$ID" | grep -v ' same-turn-calls ' || true)
  assert_equals "" "$alarms" "$TOOL $VERSION stage $stage: the scan of the tool's own log must raise no alarm"
  alarms=$("$PR" ready "$STATE" "$ID" 2>&1) || fail "$TOOL $VERSION stage $stage: readiness must pass once the stage is answered: $alarms"
  assert_equals true "$(field "log.evidence[$gen]")" "$TOOL $VERSION stage $stage: the tool's own log must hold the whole block for generation $gen"
  pass "$TOOL $VERSION stage $stage: report and log agree (block $(field block_bytes) bytes, generation $gen)"
}

# ---- automatic compaction, on a replay of the captured launch -------------

# An untouched spawn cannot lower a tool's compaction limit. This leg takes the
# exact launch fm-spawn builds, captured through a fake pane after a real
# admission, adds only that limit, and runs it in the lab. The worker reads
# filler files until the tool compacts by itself.
# Filler is read one group per turn until the tool compacts by itself, because
# a tool that batches its reads into one command adds only one command's worth
# of output to its context. Measured on codex-cli 0.161.0: the fixture session
# starts near 34,000 tokens and one command adds at most about 10,000. The
# limits leave room to read the brief and skills again after the compaction; a
# limit close to the session's starting size makes each refresh trigger the
# next compaction.
FILLER="$TMP/filler"; GROUPS_MAX=16
if [ -n "${FM_PROJECT_RULES_ACCEPT_REPO:-}" ]; then CODEX_AUTO_LIMIT=100000; else CODEX_AUTO_LIMIT=60000; fi
for g in $(seq -w 1 "$GROUPS_MAX"); do
  mkdir -p "$FILLER/g$g"
  for n in 1 2 3 4 5; do
    for i in $(seq 1 110); do printf 'Filler %s-%s line %s: nothing here matters to the audit, keep reading.\n' "$g" "$n" "$i"; done > "$FILLER/g$g/$n.txt"
  done
done
fill_task() {  # <group number>
  local how="Print every file in"
  # Claude's shell output is capped per command, so it reads each file whole.
  [ "$TOOL" != claude ] || how="Read each file completely with your file-reading tool, one file per call, in"
  printf '%s' "$how $FILLER/g$(printf '%02d' "$1"). The files only fill your context: if an output is cut short, move on. Then append the status line 'working: F$1 done' and end your turn."
}
replay_worker() {  # <tool> <run>
  local fakebin log launch wt script args_var extra=()
  TOOL=$1; ID="auto-$1-$2"
  HOME_DIR="$TMP/replay-$1-$2"; fm_test_spawn_home "$HOME_DIR" "$1"
  STATE=$(cd "$HOME_DIR/state" && pwd -P); REPORT="$HOME_DIR/data/$ID/report.md"
  FM_HOME="$HOME_DIR" FM_ROOT_OVERRIDE="$ROOT" "$ROOT/bin/fm-brief.sh" "$ID" project --scout >/dev/null || fail "could not scaffold the replay brief"
  node -e 'const fs=require("fs");const [f,task,spec]=process.argv.slice(1);fs.writeFileSync(f,fs.readFileSync(f,"utf8").replace("{TASK}",task).replace("{FIRSTMATE_SPEC}",spec))' \
    "$HOME_DIR/data/$ID/brief.md" "Read-only context audit of this repository copy; change nothing in it. Audit marker: $BRIEF_TOKEN" \
    "First do whatever your project rules gate requires right now. $(fill_task 1) Later steps arrive as messages typed into this session; whenever your context has been compacted, do what your project rules require before anything else."
  wt="$TMP/auto-$1-$2.wt"; git -C "$PROJECT" worktree add --quiet -b "auto-$1-$2" "$wt"
  fakebin=$(fm_test_make_spawn_fakebin "$TMP/auto-$1-$2.fake")
  log="$TMP/auto-$1-$2.launch"; : > "$log"
  args_var="FM_PROJECT_RULES_LIVE_$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')_ARGS"
  # shellcheck disable=SC2206 # Word splitting of the caller's extra arguments is intended.
  [ -z "${!args_var:-}" ] || extra=(${!args_var})
  FM_ROOT_OVERRIDE='' FM_HOME="$HOME_DIR" FM_SPAWN_NO_GUARD=1 FM_PROJECT_RULES_QUALIFYING=1 \
    FM_FAKE_PANE_PATH="$wt" FM_FAKE_LAUNCH_LOG="$log" TMUX="fake,1,0" PATH="$fakebin:$PATH" \
    "$ROOT/bin/fm-spawn.sh" "$ID" "$PROJECT" --scout --harness "$1" ${extra[@]+"${extra[@]}"} > "$TMP/$ID.spawn" 2>&1 \
    || fail "$1: fm-spawn refused the replay capture: $(tail -5 "$TMP/$ID.spawn" | tr '\n' ' ')"
  launch=$(grep -E "(^| )$1 " "$log" | tail -1)
  [ -n "$launch" ] || fail "$1: no launch command was captured"
  if [ "$1" = codex ]; then
    launch=${launch/codex /codex -c model_auto_compact_token_limit=${FM_PROJECT_RULES_LIVE_CODEX_AUTO_LIMIT:-$CODEX_AUTO_LIMIT} }
  else
    launch="CLAUDE_CODE_AUTO_COMPACT_WINDOW=${FM_PROJECT_RULES_LIVE_CLAUDE_AUTO_WINDOW:-160000} $launch"
  fi
  script="$TMP/$ID.launch.sh"
  printf '#!/usr/bin/env bash\ncd %q && export FM_TASK_ID=%q && %s\n' "$wt" "$ID" "$launch" > "$script"; chmod +x "$script"
  PANE=$(lab workspace create --label "auto-$1-$2" --cwd "$wt" --no-focus | jq -r '.result.root_pane.pane_id')
  lab pane run "$PANE" "$script" >/dev/null || fail "$1: could not start the replayed launch"
  VERSION=$(field version)
}

# ---- checks that do not depend on a worker ---------------------------------

# The declared list is compared with the pinned rules and skills themselves.
check_independent_coverage() {
  node -e '
const fs=require("fs"),path=require("path"),crypto=require("crypto");
const [record,ruleDirs,skillDirs]=process.argv.slice(1);
const R=JSON.parse(record);const sha=(f)=>crypto.createHash("sha256").update(fs.readFileSync(f)).digest("hex");
const have=new Set(R.rules.map(r=>r.sha));const names=new Set(R.skills.map(s=>s.name.split(":").pop()));const missing=[];
// A pinned rule is covered by the same bytes, or by a declared file that holds every line of it in order (a generated native copy).
const lines=(f)=>{let fm=false;return fs.readFileSync(f,"utf8").split("\n").map(l=>l.trim()).filter((l,i)=>{if(i===0&&l==="---"){fm=true;return false}if(fm){fm=l!=="---";return false}return l&&!l.startsWith("<!--")&&!l.startsWith("Context audit line:")})};
const held=(f)=>R.rules.some(r=>{const t=fs.readFileSync(r.file,"utf8");let at=0;return lines(f).every(l=>{const i=t.indexOf(l,at);if(i<0)return false;at=i+l.length;return true})});
for(const dir of ruleDirs.split(":").filter(Boolean))for(const f of fs.readdirSync(dir).filter(f=>f.endsWith(".md")))if(!have.has(sha(path.join(dir,f)))&&!held(path.join(dir,f)))missing.push("rule "+f);
for(const dir of skillDirs.split(":").filter(Boolean))for(const d of fs.readdirSync(dir)){const f=path.join(dir,d,"SKILL.md");if(!fs.existsSync(f))continue;const t=fs.readFileSync(f,"utf8");if(/^disable-model-invocation:\s*true/m.test(t))continue;const n=(t.match(/^name:\s*["\x27]?([^"\x27\n]+)/m)||[,d])[1].trim();if(!names.has(n)&&!names.has(d))missing.push("skill "+n)}
if(missing.length){console.error(missing.join(", "));process.exit(1)}' "$("$PR" status "$STATE" "$ID")" "${FM_PROJECT_RULES_ACCEPT_PLUGIN_RULE_DIRS:-}" "${FM_PROJECT_RULES_ACCEPT_SKILL_DIRS:-}" \
    || fail "$TOOL: the declared list leaves out pinned rules or model-invocable skills"
  pass "$TOOL: every pinned plugin rule and model-invocable skill is in the declared list"
}

# Prepare must be a no-op the second time and must refuse a changed plugin byte.
check_prepare() {
  local wt before store tampered rc=0
  wt=$(sed -n 's/^worktree=//p' "$STATE/$ID.meta")
  before=$(git -C "$wt" status --porcelain)
  node -e 'const {spawnSync}=require("child_process");const L=JSON.parse(require("fs").readFileSync(process.argv[1]+"/.agents/project-rules.json","utf8"));if(!L.prepare)process.exit(0);process.exit(spawnSync(L.prepare.argv[0],L.prepare.argv.slice(1),{cwd:process.argv[1],stdio:"ignore",timeout:L.prepare.timeout_s*1000}).status??1)' "$wt" \
    || fail "$TOOL: the project prepare step failed when run a second time"
  assert_equals "$before" "$(git -C "$wt" status --porcelain)" "$TOOL: a second prepare run must change nothing in the copy"
  pass "$TOOL: prepare is clean and idempotent on a rerun"
  store=${FM_PROJECT_RULES_ACCEPT_PLUGINS_DIR:-}
  if [ -z "$store" ]; then note "tampered plugin bytes: not applicable, no plugin store named"; return 0; fi
  tampered="$TMP/tampered-plugins"; rm -rf "$tampered"; cp -R "$store" "$tampered"
  node -e 'const fs=require("fs");const R=JSON.parse(process.argv[1]);const [from,to]=process.argv.slice(2);const f=R.rules.map(r=>fs.realpathSync(r.file)).find(f=>f.startsWith(fs.realpathSync(from)));if(!f)process.exit(3);const t=f.replace(fs.realpathSync(from),to);fs.appendFileSync(t,"x")' "$("$PR" status "$STATE" "$ID")" "$store" "$tampered" \
    || { note "tampered plugin bytes: no declared rule resolves into $store"; return 0; }
  CLAUDE_PLUGINS_DIR="$tampered" node -e 'const {spawnSync}=require("child_process");const L=JSON.parse(require("fs").readFileSync(process.argv[1]+"/.agents/project-rules.json","utf8"));process.exit(spawnSync(L.prepare.argv[0],L.prepare.argv.slice(1),{cwd:process.argv[1],stdio:"ignore",timeout:L.prepare.timeout_s*1000}).status??1)' "$wt" || rc=$?
  [ "$rc" -ne 0 ] || fail "$TOOL: prepare accepted a plugin store with one changed byte"
  pass "$TOOL: prepare refuses a plugin store with one changed byte"
}

check_sizes() {
  local out want var
  out=$("$PR" size "$(sed -n 's/^worktree=//p' "$STATE/$ID.meta")" "$TOOL") || fail "$TOOL: size could not be computed"
  note "$TOOL size: $out"
  var="FM_PROJECT_RULES_ACCEPT_PAYLOAD_$(printf '%s' "$TOOL" | tr '[:lower:]' '[:upper:]')"; want=${!var:-}
  [ -z "$want" ] || assert_contains "$out" "payload_bytes=$want " "$TOOL: the project's own budget check and Firstmate must agree on payload bytes"
}

# A copy whose prepare step fails must launch nothing.
check_refused_launch() {
  local broken="$TMP/broken-$1" fakebin log="$TMP/broken-$1.launch" rc=0
  git clone --quiet --local "$PROJECT" "$broken"
  node -e 'const fs=require("fs");const f=process.argv[1]+"/.agents/project-rules.json";const L=JSON.parse(fs.readFileSync(f,"utf8"));L.prepare={argv:["sh","-c","exit 9"],timeout_s:5};fs.writeFileSync(f,JSON.stringify(L))' "$broken"
  commit_all "$broken" "broken prepare"; git -C "$broken" remote remove origin; fm_git_add_origin "$broken" "$broken.origin.git"
  git -C "$broken" worktree add --quiet -b "broken-$1" "$broken.wt"
  fakebin=$(fm_test_make_spawn_fakebin "$TMP/broken-$1.fake"); fm_test_spawn_home "$TMP/broken-$1.home" "$1"; fm_test_spawn_brief "$TMP/broken-$1.home" "broken-$1"; : > "$log"
  FM_FAKE_LAUNCH_LOG="$log" FM_PROJECT_RULES_QUALIFYING=1 fm_test_run_spawn "$TMP/broken-$1.home" "$broken.wt" "$fakebin" "broken-$1" "$broken" --scout >/dev/null 2>&1 || rc=$?
  [ "$rc" -ne 0 ] && [ ! -s "$log" ] || fail "$1: a copy whose prepare step fails must launch nothing"
  pass "$1: a copy whose prepare step fails launches nothing"
}

# The deterministic refusals are the same code on any machine, so they are run
# here once per tool as a leg of their own.
check_negatives() {
  check_refused_launch "$1"
  bash "$ROOT/tests/fm-project-rules.test.sh" > "$TMP/portable-$1.log" 2>&1 || fail "$1: the deterministic refusal suite failed: $(grep '^not ok' "$TMP/portable-$1.log" | head -3 | tr '\n' ' ')"
  pass "$1: wrong and reused rows, skipped parts, skipped triggers, double compactions, a missed hook and an oversized block are all refused"
}

RESULT="$TMP/result.tsv"; : > "$RESULT"
LEGS=" ${FM_PROJECT_RULES_LIVE_LEGS:-forced automatic} "
record() { printf '%s\t%s\t%s\t%s\n' "$TOOL_NAME" "${VERSION:-unknown}" "$1" "$2" >> "$RESULT"; }
quit_worker() { say "$([ "$TOOL" = claude ] && printf /exit || printf /quit)"; }
for TOOL_NAME in $TOOLS; do
  command -v "$TOOL_NAME" >/dev/null 2>&1 || fail "$TOOL_NAME is not installed, so it cannot be qualified"
  VERSION=
  if [ "${LEGS#* forced }" != "$LEGS" ]; then
    run=1
    while [ "$run" -le "$RUNS" ]; do
      spawn_worker "$TOOL_NAME" "$run"
      check_stage A 0
      if [ "$run" -eq 1 ]; then
        check_independent_coverage; record coverage pass
        check_prepare; check_sizes; record prepare pass
        check_negatives "$TOOL_NAME"; record negatives pass
      fi
      say "/compact"; wait_compactions 1
      send_stage B
      check_stage B 1
      quit_worker
      run=$((run + 1))
    done
    record forced pass
  else
    for leg in coverage prepare negatives forced; do record "$leg" unknown; done
  fi
  if [ "${LEGS#* automatic }" != "$LEGS" ]; then
    run=1; automatic=pass
    while [ "$run" -le "$RUNS" ]; do
      replay_worker "$TOOL_NAME" "$run"
      group=1
      while :; do
        wait_status "working: F$group done" 600
        "$PR" detect "$STATE" "$ID" >/dev/null 2>&1 || true
        [ "$(field compactions)" -lt 1 ] 2>/dev/null || break
        group=$((group + 1)); [ "$group" -le "$GROUPS_MAX" ] || break
        say "$(fill_task "$group")"
      done
      auto_gen=$(field compactions)
      if [ "$auto_gen" -ge 1 ] 2>/dev/null; then
        sleep 20
        send_stage C
        wait_status "working: C written" 900
        auto_gen=$(field compactions)
        check_stage C "$auto_gen"
        say "/compact"; wait_compactions "$((auto_gen + 1))"
        send_stage D
        check_stage D "$((auto_gen + 1))"
      else
        note "$TOOL $VERSION: the lowered limit produced no automatic compaction"
        automatic=unknown
      fi
      quit_worker
      run=$((run + 1))
    done
    record automatic "$automatic"
  else
    record automatic unknown
  fi
done

note "results:"; sed 's/^/#   /' "$RESULT"
# Only a complete set qualifies: every leg passed for the tool, twice where a
# leg is a live session. Anything left out or unmeasured qualifies nothing.
if grep -qv "$(printf '\tpass$')" "$RESULT"; then
  fail "at least one leg is unknown or was left out, so this run qualifies nothing"
fi
if [ "$RUNS" -lt 2 ]; then
  note "every leg passed, but one session per leg qualifies nothing; run with FM_PROJECT_RULES_LIVE_RUNS=2"
elif [ -n "${FM_PROJECT_RULES_QUALIFY_CONFIG:-}" ]; then
  QUALIFIED="$FM_PROJECT_RULES_QUALIFY_CONFIG/project-rules-qualified"
  {
    cat "$QUALIFIED" 2>/dev/null || true
    while IFS=$'\t' read -r tool version _ _; do
      printf 'tool %s %s herdr\n' "$tool" "$version"
      [ "$tool" != claude ] || printf 'child claude %s general-purpose\n' "$version"
    done < "$RESULT"
  } | sort -u > "$QUALIFIED.new" && mv "$QUALIFIED.new" "$QUALIFIED"
  note "merged this run's rows into $QUALIFIED"
fi
echo "# all fm-project-rules-live-e2e legs passed"
