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
#   FM_PROJECT_RULES_LIVE_CLAUDE_ARGS / FM_PROJECT_RULES_LIVE_CODEX_ARGS   extra
#     fm-spawn arguments, for example "--model gpt-6.1-sol --effort medium"
#   FM_PROJECT_RULES_QUALIFY_CONFIG=<dir>  on a full pass, write the qualified
#     tool, version, backend and child rows to <dir>/project-rules-qualified
# shellcheck disable=SC2016 # Fixture text and node one-liners are literal on purpose.
set -u

# shellcheck source=tests/fixtures.sh
. "$(dirname "${BASH_SOURCE[0]}")/fixtures.sh"

fm_live_gate opt-in FM_PROJECT_RULES_LIVE_E2E herdr jq node treehouse git

note() { printf '# %s\n' "$1"; }
PR="$ROOT/bin/fm-project-rules.sh"
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
  printf '# Plugin rule, Claude copy\n\nKeep queries bounded.\n' > "$repo/.claude/rules/standin.md"
  printf '# Plugin rule\n\nKeep queries bounded.\n' > "$repo/vendor/rules/standin.md"
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
  TRIGGER='sh tools/product-test.sh'
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
L.rules.filter(r=>r.path).forEach((r,i)=>{const token="FMQ-"+r.id.replace(/[^a-z]/g,"").toUpperCase()+"-"+(1000+i*37);fs.appendFileSync(path.join(repo,r.path),"\nContext audit line: "+token+"\n");for(const tool of r.tools||["claude","codex"])rows.push([tool,r.id,token,(r.native||[]).includes(tool)?"native":"inline"].join("\t"))});
fs.writeFileSync(out,rows.join("\n")+"\n");
' "$PROJECT" "$CANARIES" || fail "the declared list does not cover every tracked instruction file"
commit_all "$PROJECT" "context audit canaries (throwaway copy only)"
fm_git_add_origin "$PROJECT" "$PROJECT.origin.git" 2>/dev/null || true
note "project under test: ${FM_PROJECT_RULES_ACCEPT_REPO:-built-in fixture}, $(wc -l < "$CANARIES" | tr -d ' ') tool-file canaries"

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
    printf '%s' "Then try to launch one Explore child agent with the task 'say hi' and append one line beginning EXPLORE: saying whether it was allowed or refused. Then launch one general-purpose child agent with exactly this task and nothing added: 'Before using any tool, list every token of the form FMQ-<LETTERS>-<digits> literally visible in your own context, one per line, or NONE.' and append its reply verbatim under a heading '## CHILD \$STAGE'."
  else
    printf '%s' "Then spawn one sub-agent with exactly this task and nothing added: 'Before using any tool, list every token of the form FMQ-<LETTERS>-<digits> literally visible in your own context, one per line, or NONE.' and append its reply verbatim under a heading '## CHILD \$STAGE'."
  fi
}
stage_task() {  # <stage letter>
  local trigger=
  [ -z "$TRIGGER" ] || trigger="Then run exactly \`$TRIGGER\` and append the last line it prints."
  printf '%s' "Stage $1. First do whatever your project rules gate requires right now. Then, without searching and without opening any instruction or rule file yourself, append to $REPORT a section headed '## $1' listing every token of the form FMQ-<LETTERS>-<digits> literally visible in your context, one per line. $trigger $(children_task | sed "s/\\\$STAGE/$1/g") Never tell a child any token. Then append the status line 'working: $1 written' and end your turn."
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
  wait_status "working: $stage written" 600
  section=$(awk -v h="## $stage" '$0==h{p=1;next} /^## /{p=0} p' "$REPORT")
  child=$(awk -v h="## CHILD $stage" '$0==h{p=1;next} /^## /{p=0} p' "$REPORT")
  assert_equals "$gen" "$(field compactions)" "$TOOL $VERSION stage $stage: compactions recorded"
  assert_not_equals null "$(field stage.closed)" "$TOOL $VERSION stage $stage: the stage must be answered"
  while IFS=$'\t' read -r tool _ token kind; do
    [ "$tool" = "$TOOL" ] || continue
    assert_contains "$section" "$token" "$TOOL $VERSION stage $stage: the worker must report the $kind canary $token"
    if [ "$kind" = native ] || [ "$TOOL" = codex ]; then
      assert_contains "$child" "$token" "$TOOL $VERSION stage $stage: a child must hold the $kind canary $token"
    fi
  done < "$CANARIES"
  assert_contains "$section" "$BRIEF_TOKEN" "$TOOL $VERSION stage $stage: the worker must hold its launch brief"
  if [ -n "$TRIGGER" ]; then
    assert_equals true "$(field 'Object.keys(hits).length > 0 && Object.keys(hits).every((n) => answers[n] && answers[n].gen === compactions)')" "$TOOL $VERSION stage $stage: the skill for the triggered command must be read in this generation"
  fi
  [ "$TOOL" != claude ] || assert_contains "$(grep '^EXPLORE:' "$REPORT" | tail -1)" "refused" "$TOOL $VERSION stage $stage: an Explore child must be refused"
  "$PR" ready "$STATE" "$ID" || fail "$TOOL $VERSION stage $stage: readiness must pass once the stage is answered"
  alarms=$("$PR" scan "$STATE" "$ID" | grep -v ' same-turn-calls ' || true)
  assert_equals "" "$alarms" "$TOOL $VERSION stage $stage: the scan of the tool's own log must raise no alarm"
  assert_equals true "$(field "log.evidence[$gen]")" "$TOOL $VERSION stage $stage: the tool's own log must hold the whole block for generation $gen"
  pass "$TOOL $VERSION stage $stage: report and log agree (block $(field block_bytes) bytes, generation $gen)"
}

RESULT="$TMP/result.tsv"; : > "$RESULT"
for TOOL_NAME in $TOOLS; do
  command -v "$TOOL_NAME" >/dev/null 2>&1 || fail "$TOOL_NAME is not installed, so it cannot be qualified"
  run=1
  while [ "$run" -le "$RUNS" ]; do
    spawn_worker "$TOOL_NAME" "$run"
    check_stage A 0
    say "/compact"; sleep 45
    say "$(stage_task B)"
    check_stage B 1
    say "$([ "$TOOL" = claude ] && printf /exit || printf /quit)"
    run=$((run + 1))
  done
  printf '%s\t%s\tforced\tpass\n' "$TOOL" "$VERSION" >> "$RESULT"
  # The automatic leg needs the tool's own compaction limit lowered on the
  # launch, which an untouched spawn cannot do; until that replay is measured
  # for this tool and version it is reported as unknown.
  printf '%s\t%s\tautomatic\tunknown\n' "$TOOL" "$VERSION" >> "$RESULT"
done

note "results:"; sed 's/^/#   /' "$RESULT"
if grep -q "$(printf '\tunknown$')" "$RESULT"; then
  fail "at least one leg is unknown, so this run qualifies nothing"
fi
if [ -n "${FM_PROJECT_RULES_QUALIFY_CONFIG:-}" ]; then
  while IFS=$'\t' read -r tool version _ _; do
    printf 'tool %s %s herdr\n' "$tool" "$version"
    [ "$tool" != claude ] || printf 'child claude %s general-purpose\n' "$version"
  done < "$RESULT" | sort -u > "$FM_PROJECT_RULES_QUALIFY_CONFIG/project-rules-qualified"
  note "wrote $FM_PROJECT_RULES_QUALIFY_CONFIG/project-rules-qualified"
fi
echo "# all fm-project-rules-live-e2e legs passed"
