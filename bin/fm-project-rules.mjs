#!/usr/bin/env node
// Engine behind bin/fm-project-rules.sh; that wrapper's header owns usage and
// docs/project-rules.md owns the contract. Invoked only through the wrapper,
// which resolves the task's Codex session log before calling in.

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { spawnSync } from "node:child_process";

const CAP = 79000;
const PART = 10000;
const ROWS = 64;
const STAGE_SECS = Number(process.env.FM_PROJECT_RULES_STAGE_SECS || 900);
const GRACE_SECS = Number(process.env.FM_PROJECT_RULES_GRACE_SECS ?? 60);
const CORRECT_SECS = Number(process.env.FM_PROJECT_RULES_CORRECT_SECS ?? 180);
const TOOLS = ["claude", "codex"];
const EDIT_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"];
const HELPER = process.env.FM_PROJECT_RULES_HELPER || "";
// The worker-facing Firstmate commands a worker may run while a stage is open.
const HELPERS = ["fm-project-rules.sh", "fm-task-heartbeat.sh", "fm-captain-hold.sh"].map((name) => path.join(path.dirname(HELPER), name));

// Claude treats only exit 2 from a tool hook as a refusal, so every failure
// inside that hook, including a locked or corrupt record, must exit 2.
let failCode = 1;
const die = (message, code = failCode) => {
  process.stderr.write(`fm-project-rules: ${message}\n`);
  process.exit(code);
};
const sha = (data) => crypto.createHash("sha256").update(data).digest("hex");
const now = () => Math.floor(Date.now() / 1000);
const q = (text) => `'${String(text).replace(/'/g, `'\\''`)}'`;
const recordPath = (state, id) => path.join(state, `${id}.project-rules`);
const dirPath = (state, id) => `${recordPath(state, id)}.d`;
const helperCommand = (R, verb, ...rest) => [q(HELPER), verb, q(R.state), q(R.task), ...rest].join(" ");

function sleep(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

// Serialize every read-modify-write of one task's record. The hook, the
// worker's own commands, and the watcher scan can all land at once.
let held = "";
process.on("exit", () => {
  if (held) fs.rmSync(held, { recursive: true, force: true });
});

function withRecord(state, id, change) {
  const file = recordPath(state, id);
  const lock = `${file}.lock`;
  for (let attempt = 0; ; attempt += 1) {
    try {
      fs.mkdirSync(lock);
      held = lock;
      break;
    } catch {
      const age = Date.now() - (fs.statSync(lock, { throwIfNoEntry: false })?.mtimeMs ?? Date.now());
      if (age > 30000) fs.rmSync(lock, { recursive: true, force: true });
      else if (attempt > 200) die(`could not lock ${file}`);
      else sleep(50);
    }
  }
  try {
    if (!fs.existsSync(file)) return change(null);
    const R = JSON.parse(fs.readFileSync(file, "utf8"));
    const result = change(R);
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, `${JSON.stringify(R)}\n`, { mode: 0o600 });
    fs.renameSync(tmp, file);
    return result;
  } finally {
    fs.rmSync(lock, { recursive: true, force: true });
    held = "";
  }
}

// ---- declared list -------------------------------------------------------

function loadList(copy) {
  const file = path.join(copy, ".agents", "project-rules.json");
  if (!fs.existsSync(file)) return null;
  const bad = (message) => die(`${file}: ${message}`);
  let L;
  try {
    L = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (error) {
    bad(`does not parse: ${error.message}`);
  }
  const only = (object, allowed, where) => {
    if (!object || typeof object !== "object" || Array.isArray(object)) bad(`${where} must be an object`);
    for (const key of Object.keys(object)) if (!allowed.includes(key)) bad(`unknown field ${where}.${key}`);
  };
  const strings = (value, where) => {
    if (!Array.isArray(value) || value.length === 0 || value.some((item) => typeof item !== "string" || !item)) {
      bad(`${where} must be a non-empty array of strings`);
    }
  };
  const source = (entry, where) => {
    if (("path" in entry) === ("resolve" in entry)) bad(`${where} needs exactly one of path or resolve`);
    if ("resolve" in entry) return strings(entry.resolve, `${where}.resolve`);
    const parts = String(entry.path).split("/");
    if (typeof entry.path !== "string" || !entry.path || path.isAbsolute(entry.path) || parts.includes("..")) {
      bad(`${where}.path must be a repo-relative path`);
    }
  };
  const subset = (value, of, where) => {
    strings(value, where);
    if (value.some((tool) => !of.includes(tool))) bad(`${where} may only name ${of.join(", ")}`);
  };
  only(L, ["version", "budget_bytes", "prepare", "rules", "skills", "dispatched_child_types"], "list");
  if (L.version !== 1) bad("version must be 1");
  if (L.prepare !== undefined) {
    only(L.prepare, ["argv", "timeout_s"], "prepare");
    strings(L.prepare.argv, "prepare.argv");
    if (!Number.isInteger(L.prepare.timeout_s) || L.prepare.timeout_s < 1 || L.prepare.timeout_s > 900) {
      bad("prepare.timeout_s must be a whole number from 1 to 900");
    }
  }
  if (!Array.isArray(L.rules) || L.rules.length === 0) bad("rules must be a non-empty array");
  const ids = new Set();
  L.rules.forEach((rule, index) => {
    const where = `rules[${index}]`;
    only(rule, ["id", "path", "resolve", "tools", "native"], where);
    if (typeof rule.id !== "string" || !/^[a-z0-9][a-z0-9-]*$/.test(rule.id) || ids.has(rule.id)) bad(`${where}.id must be unique and match [a-z0-9][a-z0-9-]*`);
    ids.add(rule.id);
    source(rule, where);
    if (rule.tools !== undefined) subset(rule.tools, TOOLS, `${where}.tools`);
    if (rule.native !== undefined) subset(rule.native, rule.tools ?? TOOLS, `${where}.native`);
  });
  L.skills ??= [];
  if (!Array.isArray(L.skills)) bad("skills must be an array");
  const names = new Set();
  L.skills.forEach((skill, index) => {
    const where = `skills[${index}]`;
    only(skill, ["name", "description", "invocation", "body", "references", "required"], where);
    if (typeof skill.name !== "string" || !/^[A-Za-z0-9][A-Za-z0-9:._-]*$/.test(skill.name) || names.has(skill.name) || skill.name === "brief") bad(`${where}.name must be unique`);
    names.add(skill.name);
    if (typeof skill.description !== "string" || /\n/.test(skill.description)) bad(`${where}.description must be one line`);
    if (!["model", "manual"].includes(skill.invocation)) bad(`${where}.invocation must be model or manual`);
    only(skill.body, ["path", "resolve"], `${where}.body`);
    source(skill.body, `${where}.body`);
    (skill.references ?? []).forEach((reference, at) => {
      only(reference, ["path", "resolve"], `${where}.references[${at}]`);
      source(reference, `${where}.references[${at}]`);
    });
    if (skill.required === undefined) return;
    only(skill.required, ["at", "before_commands", "on_paths"], `${where}.required`);
    if (Object.keys(skill.required).length !== 1) bad(`${where}.required needs exactly one trigger kind`);
    if ("at" in skill.required && skill.required.at !== "start") bad(`${where}.required.at must be start`);
    if ("before_commands" in skill.required) {
      strings(skill.required.before_commands, `${where}.required.before_commands`);
      for (const pattern of skill.required.before_commands) {
        if (pattern.length > 300) bad(`${where}.required.before_commands patterns may be at most 300 characters`);
        try {
          commandPattern(pattern);
        } catch (error) {
          bad(`${where}.required.before_commands pattern ${pattern} is not usable: ${error.message}`);
        }
      }
    }
    if ("on_paths" in skill.required) strings(skill.required.on_paths, `${where}.required.on_paths`);
  });
  if (L.dispatched_child_types !== undefined) strings(L.dispatched_child_types, "dispatched_child_types");
  return L;
}

// POSIX extended patterns compile as JavaScript once the bracket classes are
// spelled out; anything else that fails to compile refuses the list.
function commandPattern(pattern) {
  const classes = { space: "\\s", digit: "0-9", alpha: "A-Za-z", alnum: "A-Za-z0-9", upper: "A-Z", lower: "a-z" };
  return new RegExp(pattern.replace(/\[:(\w+):\]/g, (whole, name) => classes[name] ?? whole));
}

// One simple command split into its words, or null when it holds anything a
// shell would act on beyond plain words: operators, expansions, substitutions,
// redirections, or a line break. `extra` names further characters a caller is
// prepared to see inside a word.
function simpleWords(command, extra = "") {
  const word = new RegExp(`^(?:'[^']*'|"[^"$\`\\\\]*"|[A-Za-z0-9_@%+=:,./${extra}-]+)+`);
  const words = [];
  let rest = String(command).trim();
  while (rest) {
    const found = rest.match(word);
    if (!found) return null;
    words.push(found[0].replace(/'([^']*)'|"([^"]*)"/g, "$1$2"));
    rest = rest.slice(found[0].length);
    if (rest && !/^[ \t]/.test(rest)) return null;
    rest = rest.replace(/^[ \t]+/, "");
  }
  return words;
}

// The words of a command only when it is one of the worker-facing Firstmate
// commands a worker may run while a stage is open: this helper's next, ack and
// serve, the heartbeat, and a decision event. The script is matched by exact
// path, and an environment prefix is accepted only when it names Firstmate's
// own root, home and state for this task, so it cannot point the script at
// other code.
function helperWords(command, R) {
  const words = simpleWords(command);
  if (!words) return null;
  const root = path.dirname(path.dirname(HELPER));
  const allowed = { FM_ROOT_OVERRIDE: [root], FM_HOME: [root, R ? path.dirname(R.state) : root], FM_STATE_OVERRIDE: [R ? R.state : path.join(root, "state")] };
  while (words.length && /^[A-Z_]+=/.test(words[0])) {
    const [name, value] = [words[0].split("=")[0], words[0].slice(words[0].indexOf("=") + 1)];
    if (!allowed[name]?.includes(value)) return null;
    words.shift();
  }
  const script = HELPERS.indexOf(words[0]);
  if (script < 0) return null;
  if (script === 0 && !["next", "ack", "serve"].includes(words[1])) return null;
  if (script === 2 && words[1] !== "decision-event") return null;
  return words;
}
const helperCall = (command, R) => helperWords(command, R) !== null;

// A project's pattern is matched under a time limit, and a pattern that runs
// out of time counts as a match, so a slow pattern cannot stall the hook or
// the watcher and cannot hide a command either. The command is also tried with
// its quoting and grouping characters blanked, so a wrapper such as
// sh -c "..." does not hide it.
function commandMatches(patterns, command) {
  const text = String(command).slice(0, 8000);
  const forms = [text, text.replace(/["'`(){}]/g, " ")];
  return patterns.some((pattern) => {
    try {
      return vm.runInNewContext("forms.some((form) => re.test(form))", { re: commandPattern(pattern), forms }, { timeout: 100 });
    } catch {
      return true;
    }
  });
}

function pathPattern(glob) {
  const body = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*\*\/?|\*|\?/g, (token) => {
    if (token === "**/") return "(?:.*/)?";
    if (token === "**") return ".*";
    return token === "*" ? "[^/]*" : "[^/]";
  });
  return new RegExp(`^${body}$`);
}

function scrubbed() {
  const keep = ["HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR", "CLAUDE_CONFIG_DIR", "CLAUDE_PLUGINS_DIR"];
  return Object.fromEntries(keep.filter((key) => process.env[key] !== undefined).map((key) => [key, process.env[key]]));
}

function runIn(copy, argv, seconds) {
  return spawnSync(argv[0], argv.slice(1), {
    cwd: copy,
    env: scrubbed(),
    timeout: seconds * 1000,
    killSignal: "SIGKILL",
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
}

// One declared source to a readable regular file with its bytes.
function locate(copy, entry, label) {
  let file;
  if (entry.path) file = path.join(copy, entry.path);
  else {
    const result = runIn(copy, entry.resolve, 30);
    file = String(result.stdout ?? "").trim();
    if (result.status !== 0 || !path.isAbsolute(file) || file.includes("\n")) {
      die(`${label}: resolve command did not print one absolute path (exit ${result.status ?? "timeout"}): ${String(result.stderr ?? "").trim().slice(0, 300)}`);
    }
  }
  let data;
  try {
    if (!fs.statSync(file).isFile()) throw new Error("not a regular file");
    data = fs.readFileSync(file);
  } catch (error) {
    die(`${label}: ${entry.path ?? file} is not a readable regular file (${error.message})`);
  }
  return { file, label: entry.path ?? path.basename(file), data, sha: sha(data), bytes: data.length };
}

// Resolve every declared source for one tool: the rules it needs and all skills.
function resolveList(copy, L, tool) {
  const rules = L.rules.filter((rule) => (rule.tools ?? TOOLS).includes(tool)).map((rule) => {
    const found = locate(copy, rule, `rule ${rule.id}`);
    const native = (rule.native ?? []).includes(tool);
    if (!native && /^[ \t]*@[\w.~/-]+[ \t]*$/m.test(found.data.toString("utf8"))) {
      die(`rule ${rule.id}: ${found.label} contains an @ import line, which an inlined file cannot expand`);
    }
    return { id: rule.id, native, ...found };
  });
  const skills = L.skills.map((skill) => ({
    name: skill.name,
    description: skill.description,
    invocation: skill.invocation,
    required: skill.required ?? null,
    files: [skill.body, ...(skill.references ?? [])].map((entry) => locate(copy, entry, `skill ${skill.name}`)),
  }));
  return { rules, skills };
}

function trigger(required) {
  if (!required) return "";
  if (required.at) return "; required at start";
  return required.before_commands ? "; required before certain commands" : "; required for certain paths";
}

function renderBlock(R, resolved, table) {
  const lines = [
    "# Project rules (delivered by Firstmate, binding for this task)",
    "",
    "The files below are this repository's own instructions, delivered in full because your tool does not load them by itself.",
    "They bind your work exactly as the files themselves do, and they stay here after any context compaction.",
    "",
    `- Before your first project command or edit, and again right after any context compaction, run: ${helperCommand(R, "next")}`,
    "  Do what it prints before anything else.",
    `- Your launch brief can be read again at any time with: ${helperCommand(R, "serve", "brief")}`,
    "- Load a required skill only through its command below, at the moment named.",
    "- A child agent does not receive this block. Use only general-purpose children, and run required-skill commands such as product tests yourself, not in a child.",
  ];
  for (const rule of resolved.rules.filter((entry) => !entry.native)) {
    lines.push("", `## Rule file: ${rule.label} [${rule.id}]`, "", rule.data.toString("utf8").trimEnd());
  }
  lines.push("", "## Skill catalog", "");
  for (const skill of resolved.skills) {
    lines.push(`- ${skill.name} (${skill.invocation === "model" ? "auto" : "manual"}${trigger(skill.required)}): ${skill.description}`);
  }
  const required = resolved.skills.filter((skill) => skill.required);
  if (required.length) {
    lines.push("", "## Required skills", "", "Load each of these through its command at the moment named, and again after any context compaction before the next such moment:", "");
    for (const skill of required) {
      let when = "at start, as part of admission";
      if (skill.required.before_commands) when = `before you run any command matching ${skill.required.before_commands.map((pattern) => `\`${pattern}\``).join(" or ")}`;
      if (skill.required.on_paths) when = `before you edit any path matching ${skill.required.on_paths.join(", ")}`;
      lines.push(`- ${skill.name}: ${when}. Load it with: ${helperCommand(R, "serve", q(skill.name))}`);
    }
  }
  lines.push("", "## Receipt rows (quote one only when the helper asks for it by number)", "");
  table.forEach((code, index) => lines.push(`row${String(index).padStart(2, "0")}=${code}`));
  return `${lines.join("\n")}\n`;
}

function payloadBytes(resolved) {
  const rules = resolved.rules.filter((rule) => !rule.native).reduce((sum, rule) => sum + rule.bytes, 0);
  return rules + resolved.skills.reduce((sum, skill) => sum + Buffer.byteLength(skill.name + skill.description), 0);
}

function newTable() {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  return Array.from({ length: ROWS }, () => Array.from({ length: 6 }, () => alphabet[crypto.randomInt(alphabet.length)]).join(""));
}

// ---- qualification -------------------------------------------------------

// config/project-rules-qualified is written by the live guard on a pass:
// "tool <tool> <version> <backend>" and "child <tool> <version> <type>".
function qualified(config) {
  const file = path.join(config, "project-rules-qualified");
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, "utf8").split("\n").map((line) => line.trim().split(/\s+/)).filter((row) => row.length === 4);
}

function toolVersion(tool) {
  const result = spawnSync(tool, ["--version"], { encoding: "utf8", timeout: 20000 });
  return (String(result.stdout ?? "").match(/\d+\.\d+\.\d+[\w.-]*/) ?? ["unknown"])[0];
}

// ---- admission -----------------------------------------------------------

function refuseCodexConfig(copy) {
  const home = process.env.CODEX_HOME || path.join(process.env.HOME ?? "", ".codex");
  let limit = 32768;
  for (const file of [path.join(home, "config.toml"), path.join(copy, ".codex", "config.toml")]) {
    if (!fs.existsSync(file)) continue;
    const top = fs.readFileSync(file, "utf8").split(/^\s*\[/m)[0];
    if (/^\s*developer_instructions\s*=/m.test(top)) die(`${file} already sets developer_instructions, which the rules block would replace`);
    const declared = top.match(/^\s*project_doc_max_bytes\s*=\s*(\d+)/m);
    if (declared) limit = Number(declared[1]);
  }
  const chain = ["AGENTS.override.md", "AGENTS.md"].map((name) => path.join(copy, name)).find((file) => fs.existsSync(file));
  const bytes = chain ? fs.statSync(chain).size : 0;
  if (bytes > limit) die(`${chain} is ${bytes} bytes, over Codex's ${limit}-byte project instruction limit; Codex would truncate it silently`);
}

function admit(args) {
  const [state, id, copy, tool] = args;
  const option = (name) => (args.includes(name) ? args[args.indexOf(name) + 1] : "");
  const L = loadList(copy);
  if (!L) {
    fs.rmSync(recordPath(state, id), { force: true });
    fs.rmSync(dirPath(state, id), { recursive: true, force: true });
    process.exit(3);
  }
  if (!TOOLS.includes(tool)) die(`this project declares rules that only Claude and Codex workers can be shown to hold; ${tool} is not supported`);
  const backend = option("--backend") || "tmux";
  const rows = qualified(option("--config"));
  const version = toolVersion(tool);
  if (!process.env.FM_PROJECT_RULES_QUALIFYING && !rows.some((row) => row[0] === "tool" && row[1] === tool && row[3] === backend)) {
    die(`${tool} on the ${backend} backend has never passed the project-rules live guard on this machine; run tests/fm-project-rules-live-e2e.test.sh first`);
  }
  const qualifying = Boolean(process.env.FM_PROJECT_RULES_QUALIFYING);
  const versionQualified = qualifying || rows.some((row) => row[0] === "tool" && row[1] === tool && row[2] === version && row[3] === backend);
  // A relaunch admits a copy that holds the worker's own uncommitted work, so
  // only a change these commands themselves make is refused.
  // Each changed or untracked path is compared by status and by content, so a
  // further change to a file the worker had already modified is still seen.
  const porcelain = () => spawnSync("git", ["-C", copy, "status", "--porcelain", "--untracked-files=all"], { encoding: "utf8" }).stdout.split("\n").filter(Boolean).map((line) => {
    const file = path.join(copy, line.slice(3).replace(/^.* -> /, ""));
    const stat = fs.statSync(file, { throwIfNoEntry: false });
    return `${line} ${stat?.isFile() ? sha(fs.readFileSync(file)) : "absent"}`;
  });
  const before = new Set(porcelain());
  if (L.prepare) {
    const result = runIn(copy, L.prepare.argv, L.prepare.timeout_s);
    if (result.status !== 0) {
      die(`project prepare step failed (exit ${result.status ?? "timeout"}): ${String(result.stderr || result.stdout || "").trim().slice(-600)}`);
    }
  }
  const resolved = resolveList(copy, L, tool);
  const dirty = porcelain().filter((line) => !before.has(line));
  if (dirty.length) die(`the project's prepare or resolve commands left the copy with uncommitted changes:\n${dirty.slice(0, 10).map((line) => line.replace(/ \S+$/, "")).join("\n")}`);
  const R = { v: 1, task: id, state, copy, tool, version, backend, version_qualified: versionQualified };
  if (tool === "codex") refuseCodexConfig(copy);
  let prior = null;
  try {
    prior = JSON.parse(fs.readFileSync(recordPath(state, id), "utf8"));
  } catch {
    prior = null;
  }
  const table = newTable();
  const block = renderBlock(R, resolved, table);
  const bytes = Buffer.byteLength(block);
  if (bytes > CAP) {
    const sizes = resolved.rules.filter((rule) => !rule.native).map((rule) => `${rule.label} ${rule.bytes}`).join(", ");
    die(`the rules block would be ${bytes} bytes, over the ${CAP}-byte size both tools were measured to hold: ${sizes}`);
  }
  const dir = dirPath(state, id);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(dir, "block.txt"), block, { mode: 0o600 });
  Object.assign(R, {
    brief: option("--brief"),
    // A relaunch keeps the first admission's base, so work committed before it is still checked at readiness.
    head: prior?.copy === copy && prior.head ? prior.head : spawnSync("git", ["-C", copy, "rev-parse", "HEAD"], { encoding: "utf8" }).stdout.trim(),
    dispatched: L.dispatched_child_types ?? [],
    list_sha: sha(fs.readFileSync(path.join(copy, ".agents", "project-rules.json"))),
    block_sha: sha(block),
    block_bytes: bytes,
    payload_bytes: payloadBytes(resolved),
    budget_bytes: L.budget_bytes ?? null,
    secret: crypto.randomBytes(16).toString("hex"),
    rules: resolved.rules.map(({ id: rule, native, file, label, sha: digest, bytes: size }) => ({ id: rule, native, file, label, sha: digest, bytes: size })),
    skills: resolved.skills.map((skill) => ({
      name: skill.name,
      required: skill.required,
      files: skill.files.map(({ file, label, sha: digest }) => ({ file, label, sha: digest })),
    })),
    children: qualifying ? ["general-purpose"] : rows.filter((row) => row[0] === "child" && row[1] === tool && row[2] === version).map((row) => row[3]),
    table,
    used: [],
    compactions: 0,
    answers: {},
    violations: [],
    triggered: {},
    drift: [],
    same_turn_calls: 0,
    alarms: {},
    log: { offset: 0, compactions: 0, events: 0, evidence: {}, native: {}, turn: 0 },
  });
  openStage(R, "start");
  fs.writeFileSync(recordPath(state, id), `${JSON.stringify(R)}\n`, { mode: 0o600 });
  const inline = R.rules.filter((rule) => !rule.native).length;
  process.stdout.write(`admitted ${id} tool=${tool} version=${version} block_bytes=${bytes} payload_bytes=${R.payload_bytes} inline=${inline} native=${R.rules.length - inline} skills=${R.skills.length}\n`);
  if (!versionQualified) process.stderr.write(`notice: ${tool} ${version} has not passed the project-rules live guard on this machine; children are denied until it has\n`);
}

// ---- stages, receipts, and the chained server ----------------------------

function openStage(R, name) {
  if (name !== "start") R.compactions += 1;
  const free = R.table.map((_, index) => index).filter((index) => !R.used.includes(index));
  const row = free.length ? free[crypto.randomInt(free.length)] : -1;
  if (row >= 0) R.used.push(row);
  R.stage = { name: name === "start" ? "start" : `c${R.compactions}`, gen: R.compactions, row, opened: now(), acked: false, brief: false, closed: null };
}

const startSkills = (R) => R.skills.filter((skill) => skill.required?.at === "start");
const answered = (R, name) => R.answers[name]?.gen === R.compactions;

function settle(R) {
  const stage = R.stage;
  if (stage.closed || !stage.acked || !stage.brief || !startSkills(R).every((skill) => answered(R, skill.name))) return;
  stage.closed = new Date().toISOString();
  R.closed ??= {};
  R.closed[stage.gen] = stage.closed;
}

// Each run of the worker's own helper is noted on the record, so the scan can
// tell a Codex call that ran the helper from one that did other work without
// having to read how the call spelled its command.
function noteHelper(R) {
  const at = new Date().toISOString();
  (R.first_helper ??= {})[R.compactions] ??= at;
  R.helper_runs = [...(R.helper_runs ?? []).slice(-200), at];
}

function owed(R) {
  const stage = R.stage;
  const steps = [];
  if (!stage.closed) {
    if (stage.row < 0) steps.push("The receipt table is used up; tell your supervisor this task needs a relaunch.");
    else if (!stage.acked) steps.push(`Quote receipt row ${String(stage.row).padStart(2, "0")} from the end of your project rules block: ${helperCommand(R, "ack")} <the 6-character code>`);
    for (const skill of startSkills(R).filter((entry) => !answered(R, entry.name))) {
      steps.push(`Load required skill ${skill.name}: ${helperCommand(R, "serve", q(skill.name))}`);
    }
    // The brief comes last and only once everything else is recorded, so a
    // worker that has not finished its reads has no task to start on.
    if (!steps.length && !stage.brief) steps.push(`${stage.gen ? "Read your task brief again" : "Everything required is recorded. Receive your task brief"}: ${helperCommand(R, "serve", "brief")}`);
  }
  return steps;
}

function next([state, id]) {
  withRecord(state, id, (R) => {
    if (!R) return process.stdout.write("Project rules: nothing is declared for this task.\n");
    noteHelper(R);
    const steps = owed(R);
    if (steps.length) {
      process.stdout.write(`Project rules admission (stage ${R.stage.name}) is OPEN. Do these now, in order, before any other action:\n`);
      steps.forEach((step, index) => process.stdout.write(`${index + 1}. ${step}\n`));
      process.stdout.write("Each read is finished only when the helper prints RECORDED. Admission stays OPEN until this command prints that nothing is owed, so run it again when you think you are done.\n");
      return;
    }
    process.stdout.write("Project rules: nothing is owed right now.\n");
    for (const skill of R.skills.filter((entry) => entry.required && !entry.required.at && !answered(R, entry.name))) {
      const when = skill.required.before_commands ? `running any command matching ${skill.required.before_commands.join(" or ")}` : `editing any path matching ${skill.required.on_paths.join(", ")}`;
      process.stdout.write(`Before ${when}, load ${skill.name}: ${helperCommand(R, "serve", q(skill.name))}\n`);
    }
  });
}

function ack([state, id, code]) {
  withRecord(state, id, (R) => {
    if (!R) die("no project rules are declared for this task");
    noteHelper(R);
    const stage = R.stage;
    if (stage.closed || stage.acked) return process.stdout.write("That receipt row is already recorded.\n");
    if (stage.row < 0 || String(code ?? "").trim().toUpperCase() !== R.table[stage.row]) {
      die(`that is not the code on row ${String(stage.row).padStart(2, "0")}; read it from the receipt rows at the end of your project rules block`);
    }
    stage.acked = true;
    settle(R);
    process.stdout.write(`Receipt row recorded.${owed(R).length ? ` Next: ${helperCommand(R, "next")}` : " Admission is complete."}\n`);
  });
}

function partsOf(R, what) {
  let text;
  if (what === "brief") {
    if (!R.brief || !fs.existsSync(R.brief)) die("this task's launch brief is not readable; tell your supervisor");
    text = fs.readFileSync(R.brief, "utf8");
  } else {
    const skill = R.skills.find((entry) => entry.name === what);
    if (!skill) die(`${what} is not a declared skill; the catalog in your project rules block lists them`);
    text = skill.files.map((entry) => {
      const data = fs.readFileSync(entry.file);
      if (sha(data) !== entry.sha) die(`${entry.label} changed on disk since this task was admitted; tell your supervisor this task needs a relaunch`);
      return `# ${what}: ${entry.label}\n\n${data.toString("utf8").trimEnd()}\n`;
    }).join("\n");
  }
  const parts = [""];
  for (const line of text.split("\n")) {
    if (parts[parts.length - 1] && Buffer.byteLength(parts[parts.length - 1]) + Buffer.byteLength(line) + 1 > PART) parts.push("");
    parts[parts.length - 1] += `${line}\n`;
  }
  return parts;
}

// Each part ends with the code that requests the next one, so no part can be
// skipped and a part cut short by a tool's output limit breaks the chain.
function serve([state, id, what, given]) {
  withRecord(state, id, (R) => {
    if (!R) die("no project rules are declared for this task");
    noteHelper(R);
    if (what === "brief" && !R.stage.closed && (!R.stage.acked || !startSkills(R).every((skill) => answered(R, skill.name)))) {
      die(`your task brief is handed over once the receipt row and every required skill are recorded; run: ${helperCommand(R, "next")}`);
    }
    const parts = partsOf(R, what);
    const code = (part) => crypto.createHmac("sha256", R.secret).update(`${what}|${R.compactions}|${part}`).digest("hex").slice(0, 8);
    let done = 0;
    if (given) {
      done = parts.findIndex((_, index) => code(index + 1) === given) + 1;
      if (!done) die(`that code does not belong to ${what} at this stage; start again with: ${helperCommand(R, "serve", q(what))}`);
    }
    if (done === parts.length) return process.stdout.write(`RECORDED: ${what} is already fully read (${parts.length} part(s)).\n`);
    const part = done + 1;
    // Serving the last part records the read, so there is no step after it
    // for a worker to miss; every earlier part still has to be asked for by
    // the code its predecessor ended with.
    if (part === parts.length) {
      if (what === "brief") R.stage.brief = true;
      else R.answers[what] = { gen: R.compactions, at: new Date().toISOString() };
      settle(R);
      return process.stdout.write(`=== ${what}: PART ${part} OF ${parts.length}, the last part. ===\n${parts[part - 1]}RECORDED: ${what} is fully read (${parts.length} part(s)).${owed(R).length ? ` Next, run: ${helperCommand(R, "next")}` : " Nothing more is owed."}\n`);
    }
    const run = helperCommand(R, "serve", q(what), code(part));
    process.stdout.write(`=== ${what}: PART ${part} OF ${parts.length}. NOT FINISHED until the helper prints RECORDED; the exact next command is on the last line. ===\n${parts[part - 1]}=== PART ${part} OF ${parts.length} - NOT FINISHED. Nothing is recorded yet. Run this exact command now: ${run} ===\n`);
  });
}

// ---- Claude hooks --------------------------------------------------------

function hook([state, id, event]) {
  let payload = {};
  try {
    payload = JSON.parse(fs.readFileSync(0, "utf8") || "{}");
  } catch {
    payload = {};
  }
  if (event === "session-start") {
    withRecord(state, id, (R) => {
      if (!R) return;
      if (payload.transcript_path) R.transcript = payload.transcript_path;
      if (payload.source === "compact") {
        R.hook_compactions = (R.hook_compactions ?? 0) + 1;
        if (R.hook_compactions > R.compactions) openStage(R, "compact");
      }
      if (owed(R).length) process.stdout.write(`Project rules admission (stage ${R.stage.name}) is open. Before any other action run: ${helperCommand(R, "next")}\n`);
    });
    return;
  }
  failCode = 2;
  const deny = (message) => {
    process.stderr.write(`${message}\n`);
    process.exit(2);
  };
  const tool = payload.tool_name ?? "";
  const input = payload.tool_input ?? {};
  const command = tool === "Bash" ? String(input.command ?? "") : "";
  let ours = false;
  const verdict = withRecord(state, id, (R) => {
    if (!R) return "";
    ours = helperCall(command, R);
    if (!R.stage.closed && !ours && !["Read", "Glob", "Grep"].includes(tool)) {
      return `Project rules admission (stage ${R.stage.name}) is open, so this tool call is refused. Run: ${helperCommand(R, "next")}`;
    }
    if (["Agent", "Task"].includes(tool)) {
      const type = input.subagent_type || "general-purpose";
      if (!R.children.includes(type)) {
        const allowed = R.children.length ? `Use one of: ${R.children.join(", ")}.` : "No child type is qualified for this tool version, so do the work yourself.";
        const asked = R.dispatched.includes(type) ? " This project's skills do dispatch it, so tell your supervisor it needs qualifying." : "";
        return `Child agent type ${type} has not been shown to load this project's rules, so it is refused. ${allowed}${asked}`;
      }
    }
    for (const skill of R.skills.filter((entry) => entry.required && !entry.required.at)) {
      const rel = EDIT_TOOLS.includes(tool) ? path.relative(R.copy, path.resolve(R.copy, String(input.file_path ?? input.notebook_path ?? ""))) : "";
      const matched = skill.required.before_commands
        ? Boolean(command) && !ours && commandMatches(skill.required.before_commands, command)
        : Boolean(rel) && skill.required.on_paths.some((glob) => pathPattern(glob).test(rel));
      if (!matched) continue;
      R.triggered[skill.name] = (R.triggered[skill.name] ?? 0) + 1;
      // A child never inherits the worker's own read of a skill.
      if (payload.agent_id) return `This requires skill ${skill.name}, which a child agent cannot be shown to hold. Do it from the main worker.`;
      if (!answered(R, skill.name)) return `This requires skill ${skill.name}, not yet loaded since the last compaction. Run: ${helperCommand(R, "serve", q(skill.name))}`;
    }
    return "";
  });
  if (verdict) deny(verdict);
}

// ---- scan: logs, cross-checks, alarms ------------------------------------

const real = (file) => {
  try {
    return fs.realpathSync(file);
  } catch {
    return path.resolve(String(file));
  }
};

function strings(value, out = []) {
  if (typeof value === "string") out.push(value);
  else if (value && typeof value === "object") for (const item of Object.values(value)) strings(item, out);
  return out;
}

// A native file is held when every significant line appears, in order.
function holds(text, file) {
  let at = 0;
  let frontmatter = false;
  const lines = fs.readFileSync(file, "utf8").split("\n");
  for (const [index, raw] of lines.entries()) {
    const line = raw.trim();
    if (index === 0 && line === "---") frontmatter = true;
    else if (frontmatter) frontmatter = line !== "---";
    else if (line && !line.startsWith("<!--")) {
      const found = text.indexOf(line, at);
      if (found < 0) return false;
      at = found + line.length;
    }
  }
  return true;
}

function newLines(R, file) {
  const stat = fs.statSync(file, { throwIfNoEntry: false });
  if (!stat) return [];
  if (R.log.file !== file || stat.size < R.log.offset) Object.assign(R.log, { file, offset: 0, compactions: 0, events: 0, evidence: {}, native: {}, turn: 0 });
  const handle = fs.openSync(file, "r");
  const buffer = Buffer.alloc(stat.size - R.log.offset);
  fs.readSync(handle, buffer, 0, buffer.length, R.log.offset);
  fs.closeSync(handle);
  const complete = buffer.lastIndexOf(10) + 1;
  R.log.offset += complete;
  return buffer.subarray(0, complete).toString("utf8").split("\n").filter(Boolean).flatMap((line) => {
    try {
      return [JSON.parse(line)];
    } catch {
      return [];
    }
  });
}

// A sub-agent writes its own session log in the same copy, and for a while it
// is the one written last. The task's log is the root session's, so a
// sub-agent's log is never adopted in its place.
function childLog(file) {
  let handle;
  try {
    handle = fs.openSync(file, "r");
    const buffer = Buffer.alloc(65536);
    let first = "";
    for (let at = 0; !first.includes("\n") && at < 4 << 20; at += buffer.length) {
      const got = fs.readSync(handle, buffer, 0, buffer.length, at);
      if (!got) break;
      first += buffer.subarray(0, got).toString("latin1");
    }
    const meta = JSON.parse(first.split("\n")[0]).payload ?? {};
    return meta.thread_source === "subagent" || Boolean(meta.source?.subagent);
  } catch {
    return false;
  } finally {
    if (handle !== undefined) fs.closeSync(handle);
  }
}

function violate(R, kind, detail) {
  let entry = R.violations.find((item) => item.kind === kind && item.detail === detail);
  if (!entry) R.violations.push((entry = { kind, detail, at: new Date().toISOString() }));
  return entry;
}

// A triggering command that started without its skill is a violation, and is
// also left for whoever acts on this task next to interrupt and correct.
function skip(R, name, gen, ran) {
  Object.assign(violate(R, "trigger-skipped", `${name} c${gen}`), { skill: name }).ran ??= ran;
  (R.nudges ??= {})[name] = gen;
}

// A skip the responder caught at its start is settled without an alarm once
// the skill is read and the command has been started again after it.
const corrected = (R, entry) => Boolean(entry.caught) && Date.parse(R.ran_after_read?.[entry.skill] ?? "") >= entry.ran;

// What the worker must be told right now. A compaction is dropped once the
// worker has started its refresh by itself, and a trigger once its skill is read.
function actions(R, caught) {
  const stage = R.stage;
  const say = [];
  if (R.compact_nudge === stage.gen && !stage.closed && !R.first_helper?.[stage.gen]) {
    say.push(`Your context was compacted (stage ${stage.name}). Before any other action run: ${helperCommand(R, "next")} It confirms your rules again and lists the required skills to load again. Then carry on with your task.`);
  }
  for (const [name, gen] of Object.entries(R.nudges ?? {})) {
    if (gen !== R.compactions || answered(R, name)) continue;
    const edit = R.skills.find((skill) => skill.name === name)?.required?.on_paths;
    say.push(`Required skill ${name} is not loaded in this stage, and what you just started needs it. Load it now with: ${helperCommand(R, "serve", q(name))} Then ${edit ? "check that edit against it" : "run the command again"}, and carry on with your task.`);
    if (caught) for (const entry of R.violations) if (entry.skill === name && entry.detail === `${name} c${gen}`) entry.caught ??= now();
  }
  delete R.compact_nudge;
  delete R.nudges;
  // A worker in the middle of a turn is stopped first, so the message is the
  // next thing it reads. The turn then counts as over until the log shows a
  // new one, so two actions close together interrupt once.
  const lines = say.map((text) => `fm-steer: ${text}`);
  if (lines.length && R.log.busy) lines.unshift("fm-interrupt");
  if (lines.length) R.log.busy = false;
  return lines;
}

function noteNative(R, text, gen, wanted) {
  for (const rule of R.rules.filter((entry) => entry.native && wanted(entry))) {
    R.log.native[`${gen}:${rule.id}`] = holds(text, rule.file);
  }
}

// Every shell command one Codex tool call carries: JSON arguments of a function
// call, each exec_command in a batched script, or a local shell action.
// `literals` are the commands that could be read. `bare` says a command site
// had no literal at all (a variable), and `raw` is the whole call text whenever
// anything in it was not fully readable, so that text is inspected too.
function codexCommands(payload) {
  const found = [];
  const add = (value) => {
    if (Array.isArray(value)) found.push(value.join(" "));
    else if (typeof value === "string" && value) found.push(value);
  };
  let args = payload.arguments;
  if (typeof args === "string") {
    try {
      args = JSON.parse(args);
    } catch {
      args = null;
    }
  }
  for (const source of [args, payload.action]) {
    if (source && typeof source === "object") for (const key of ["cmd", "command", "commands"]) add(source[key]);
  }
  for (const match of String(payload.input ?? "").matchAll(/\b(?:cmd|command)\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)/g)) {
    try {
      add(match[1].startsWith('"') ? JSON.parse(match[1]) : match[1].slice(1, -1));
    } catch {
      add(match[1]);
    }
  }
  // A command passed as a variable or built by an expression has no literal
  // to read, so whenever a call holds more command sites than literals the
  // whole call text is inspected as well.
  const input = String(payload.input ?? "");
  const sites = Math.max((input.match(/\b(?:cmd|command)\s*:/g) ?? []).length, (input.match(/\bexec_command\s*\(/g) ?? []).length);
  // A call that carries no command at all, such as a wait, is not work. One
  // that edits files without a shell command still is.
  const edits = /patch|edit|write/i.test(String(payload.name ?? ""));
  const bare = sites > found.length || (!found.length && edits);
  const raw = bare || found.some((command) => command.includes("${")) ? strings([payload.input, payload.arguments, payload.action]).join(" ") : "";
  // An edit names its files in the patch itself, wherever the call carries it.
  const patch = [...found, ...strings([args, payload.input, payload.action])].join("\n");
  const edited = [...patch.matchAll(/(?:^|\n)\*\*\* (?:Add File|Update File|Delete File|Move to): ([^\n]+)/g)].map((match) => match[1].trim());
  return { literals: found, bare, raw, sites, edited };
}

function scanCodex(R, block, file) {
  for (const entry of newLines(R, file)) {
    const payload = entry.payload && typeof entry.payload === "object" ? entry.payload : {};
    const gen = R.log.compactions;
    if (entry.type === "compacted") {
      R.log.compactions += 1;
      R.log.turn = 0;
      for (const text of strings(payload.replacement_history)) {
        if (text.includes(block)) R.log.evidence[R.log.compactions] = true;
        if (text.startsWith("# AGENTS.md instructions")) noteNative(R, text, R.log.compactions, () => true);
      }
    } else if (entry.type === "event_msg" && payload.type === "item_completed" && payload.item?.type === "ContextCompaction") {
      R.log.events += 1;
    } else if (entry.type === "event_msg" && payload.type === "task_started") {
      R.log.turn += 1;
      R.log.busy = true;
    } else if (entry.type === "event_msg" && ["task_complete", "turn_aborted"].includes(payload.type)) {
      R.log.busy = false;
    } else if (entry.type === "response_item" && payload.type === "message") {
      const text = (Array.isArray(payload.content) ? payload.content : []).map((item) => item?.text ?? "").join("\n");
      if (payload.role === "developer" && text.includes(block)) R.log.evidence[gen] = true;
      if (payload.role === "user" && text.startsWith("# AGENTS.md instructions")) noteNative(R, text, gen, () => true);
    } else if (entry.type === "response_item" && /_call$/.test(payload.type ?? "")) {
      const at = Date.parse(entry.timestamp ?? "") || Date.now();
      const closed = Date.parse(R.closed?.[gen] ?? "") || Infinity;
      // A helper call written as a template, with its arguments filled from a
      // loop, is still a helper call; anything else unreadable is work.
      const { literals, bare, raw, sites, edited } = codexCommands(payload);
      const read = (name) => R.answers[name]?.gen === gen && Date.parse(R.answers[name].at) <= at;
      // Its placeholders must be filled from plain words: any other string in
      // the call that holds a space or a shell character makes it ordinary work.
      const plain = !/\$\{/.test(raw) || [...raw.replace(/`[^`]*`/g, "").matchAll(/"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'/g)].every((match) => /^[\w:.-]*$/.test(match[1] ?? match[2] ?? ""));
      const helper = (command) => (command.includes("${") ? plain && helperCall(command.replace(/\$\{[^}]*\}/g, "x"), R) : helperCall(command, R));
      const all = raw ? [...literals, raw] : literals;
      const isWork = (command, index) => (index < literals.length ? !helper(command) : bare);
      const firstWork = all.findIndex(isWork);
      if (firstWork < 0) continue;
      // After a compaction a worker first follows Firstmate's doorbell, in
      // whatever shell it likes. Work before its refresh is counted from its
      // first helper run in that generation; a worker that never starts one is
      // caught by the unanswered stage and by readiness.
      const refreshing = gen === 0 || Date.parse(R.first_helper?.[gen] ?? "") <= at;
      // One call can finish a stage and then go on to work. That is in order
      // only when Firstmate's own commands come first in the call, so it is
      // judged when the call's output is logged.
      const pending = (R.log.pending ??= {});
      const defer = (item) => (pending[payload.call_id] ??= []).push(item);
      const early = () => {
        if (gen > 0 && R.log.turn === 0) R.same_turn_calls += 1;
        else violate(R, gen === 0 ? "worked-before-admission" : "worked-before-refresh", `stage ${gen === 0 ? "start" : `c${gen}`}`);
      };
      if (at < closed && refreshing) {
        // A command the scan could not read may be the helper itself, spelled
        // through a variable; that too is judged at the call's output, against
        // the helper runs the engine noted.
        // Only a call holding a single command can be explained by one helper run.
        const unread = sites === 1 && literals.every((command) => helper(command));
        if (payload.call_id && (unread || literals.slice(0, firstWork).some((command) => helper(command)))) defer({ gen, turn: R.log.turn, at, unread });
        else early();
      }
      for (const skill of R.skills.filter((item) => item.required?.before_commands)) {
        const hit = all.findIndex((command, index) => (index >= literals.length || !helper(command)) && commandMatches(skill.required.before_commands, command));
        if (hit < 0) continue;
        R.triggered[skill.name] = (R.triggered[skill.name] ?? 0) + 1;
        if (read(skill.name)) {
          (R.ran_after_read ??= {})[skill.name] = new Date(at).toISOString();
          continue;
        }
        // One call can finish reading the skill and then run the command. That
        // is in order only when the read comes first in the same call, so it
        // is judged when the call's output is logged.
        const readFirst = literals.slice(0, hit).some((command) => {
          const words = helperWords(command, R);
          return words !== null && words[1] === "serve" && words[4] === skill.name;
        });
        if (readFirst && payload.call_id) defer({ gen, skill: skill.name, at });
        else skip(R, skill.name, gen, at);
      }
      for (const skill of R.skills.filter((item) => item.required?.on_paths)) {
        const hit = edited.some((file) => skill.required.on_paths.some((glob) => pathPattern(glob).test(path.relative(R.copy, path.resolve(R.copy, file)))));
        if (!hit) continue;
        R.triggered[skill.name] = (R.triggered[skill.name] ?? 0) + 1;
        if (!read(skill.name)) (R.nudges ??= {})[skill.name] = gen;
      }
    } else if (entry.type === "response_item" && /_call_output$/.test(payload.type ?? "") && R.log.pending?.[payload.call_id]) {
      const done = Date.parse(entry.timestamp ?? "") || Date.now();
      for (const item of R.log.pending[payload.call_id]) {
        if (item.skill) {
          const answer = R.answers[item.skill];
          if (!(answer?.gen === item.gen && Date.parse(answer.at) <= done)) skip(R, item.skill, item.gen, item.at);
        } else if (item.unread && (R.helper_runs ?? []).some((run) => Date.parse(run) >= item.at - 1000 && Date.parse(run) <= done + 1000)) {
          continue;
        } else if (!(Date.parse(R.closed?.[item.gen] ?? "") <= done)) {
          if (item.gen > 0 && item.turn === 0) R.same_turn_calls += 1;
          else violate(R, item.gen === 0 ? "worked-before-admission" : "worked-before-refresh", `stage ${item.gen === 0 ? "start" : `c${item.gen}`}`);
        }
      }
      delete R.log.pending[payload.call_id];
    }
  }
  while (R.compactions < R.log.compactions) openStage(R, "compact");
}

function scanClaude(R, block) {
  if (!R.transcript) return;
  for (const entry of newLines(R, R.transcript)) {
    if (entry.type === "system" && entry.subtype === "compact_boundary") R.log.compactions += 1;
    const attachment = entry.attachment ?? {};
    if (attachment.type === "prompt_snapshot" && strings(attachment).some((text) => text.includes(block))) R.log.evidence[R.log.compactions] = true;
    if (attachment.type === "instructions") {
      for (const loaded of attachment.files ?? []) {
        noteNative(R, String(loaded.content ?? ""), R.log.compactions, (rule) => real(rule.file) === real(loaded.path ?? ""));
      }
    }
  }
  while (R.compactions < R.log.compactions) openStage(R, "compact");
}

// Every declared source is compared with what this session was admitted on:
// the declaration itself, each rule file, and each skill body and reference.
// A changed skill also loses its recorded read.
function noteDrift(R) {
  const same = (file, digest) => fs.existsSync(file) && sha(fs.readFileSync(file)) === digest;
  const drift = [];
  if (!same(path.join(R.copy, ".agents", "project-rules.json"), R.list_sha)) drift.push("declared-list");
  for (const rule of R.rules) if (!same(rule.file, rule.sha)) drift.push(rule.id);
  for (const skill of R.skills) {
    if (skill.files.every((entry) => same(entry.file, entry.sha))) continue;
    drift.push(`skill:${skill.name}`);
    delete R.answers[skill.name];
  }
  R.drift = drift;
}

function scanOne(state, id, rollout, report = true, act = "") {
  const out = [];
  let steps = [];
  withRecord(state, id, (R) => {
    if (!R) return;
    const before = R.compactions;
    const block = fs.readFileSync(path.join(dirPath(state, id), "block.txt"), "utf8").trimEnd();
    try {
      const log = rollout && childLog(rollout) ? R.log.file : rollout;
      if (R.tool === "codex" && log) scanCodex(R, block, log);
      if (R.tool === "claude") scanClaude(R, block);
      delete R.scan_error;
    } catch (error) {
      R.scan_error = String(error.message).slice(0, 160);
    }
    noteDrift(R);
    // Codex has no hook to tell the worker, so a newly detected compaction is
    // kept on the record until a caller that may act on the worker takes it.
    if (R.tool === "codex" && R.compactions > before) R.compact_nudge = R.compactions;
    if (act) steps = actions(R, act === "caught");
    if (!report) return;
    const alarm = (key, text) => {
      if (R.alarms[key]) return;
      R.alarms[key] = now();
      out.push(text);
    };
    const stage = R.stage;
    const where = `${R.tool} ${R.version}`;
    const age = now() - stage.opened;
    if (!stage.closed && age >= STAGE_SECS) alarm(`open:${stage.name}`, `stage-unanswered ${stage.name} open ${age}s (${where})`);
    if (stage.row < 0) alarm("exhausted", `receipt-table-exhausted relaunch needed (${where})`);
    if (!R.version_qualified) alarm("version", `unqualified-version ${where} has not passed the live guard; children denied`);
    for (let gen = 0; gen <= R.log.compactions; gen += 1) {
      const settled = gen < R.log.compactions || age >= STAGE_SECS || (stage.gen === gen && stage.closed);
      if (settled && !R.log.evidence[gen]) alarm(`evidence:${gen}`, `no-delivery-evidence generation ${gen}: the rules block is not in the tool's own log (${where})`);
      for (const rule of R.rules.filter((entry) => entry.native)) {
        if (R.log.native[`${gen}:${rule.id}`] === false || (settled && R.log.native[`${gen}:${rule.id}`] === undefined)) {
          alarm(`native:${gen}:${rule.id}`, `native-missing ${rule.id} generation ${gen}: declared native but not in the tool's own log (${where})`);
        }
      }
    }
    const other = R.tool === "codex" ? R.log.events : R.hook_compactions ?? 0;
    if (other === R.log.compactions) delete R.log.mismatch;
    else R.log.mismatch ??= now();
    if (R.log.mismatch && now() - R.log.mismatch >= GRACE_SECS) {
      alarm(`witness:${R.log.compactions}:${other}`, `witness-mismatch the log shows ${R.log.compactions} compaction(s), the ${R.tool === "codex" ? "compaction events" : "compaction hook"} ${other} (${where})`);
    }
    if (R.scan_error) alarm(`scan-error:${R.scan_error}`, `scan-error ${R.scan_error} (${where})`);
    for (const label of R.drift) alarm(`changed:${label}`, `rules-changed ${label} differs on disk from what this session was given; relaunch needed`);
    for (const entry of R.violations) {
      if (entry.caught && (corrected(R, entry) || now() - entry.caught < CORRECT_SECS)) continue;
      alarm(`violation:${entry.kind}:${entry.detail}`, `${entry.kind} ${entry.detail} (${where})`);
    }
    if (R.same_turn_calls && stage.closed) alarm(`same-turn:${stage.gen}`, `same-turn-calls ${R.same_turn_calls} tool call(s) ran between a mid-turn compaction and its refresh (${where})`);
  });
  // Written at once, since the responder's loop never yields to pending output.
  fs.writeSync(1, [...steps, ...out.map((line) => `project-rules: ${id} ${line}`)].map((line) => `${line}\n`).join(""));
}

// One Codex worker's responder: it reads the session log each time it grows
// and prints what is due at once. It returns after a short while so its caller
// resolves the log again, and at once when the task or this responder is replaced.
function respond([state, id, rollout, pidfile, pid]) {
  const mine = () => fs.existsSync(recordPath(state, id)) && fs.existsSync(pidfile) && fs.readFileSync(pidfile, "utf8").trim() === pid;
  const log = childLog(rollout) ? JSON.parse(fs.readFileSync(recordPath(state, id), "utf8")).log.file : rollout;
  let seen = -1;
  for (const until = Date.now() + 20000; Date.now() < until && mine(); sleep(250)) {
    const size = (log && fs.statSync(log, { throwIfNoEntry: false })?.size) ?? -1;
    if (size !== seen) scanOne(state, id, rollout, false, "caught");
    seen = size;
  }
}

// ---- readiness, size, status ---------------------------------------------

function ready([state, id]) {
  const reasons = withRecord(state, id, (R) => {
    if (!R) return [];
    const open = owed(R).map((step) => `owed: ${step}`);
    const stage = R.stage;
    if (R.scan_error) open.push(`the last read of the tool's own log failed (${R.scan_error})`);
    for (const label of R.drift) open.push(`${label} changed on disk after this session was given it; the task needs a relaunch`);
    if (stage.closed && !R.log.evidence[stage.gen]) open.push("the tool's own log does not show the rules block for the current generation");
    for (const rule of R.rules.filter((entry) => entry.native)) {
      if (stage.closed && R.log.native[`${stage.gen}:${rule.id}`] !== true) open.push(`the tool's own log does not show ${rule.label}, which the project declares it loads by itself`);
    }
    for (const entry of R.violations) {
      if (!R.alarms[`violation:${entry.kind}:${entry.detail}`] && !corrected(R, entry)) open.push(`${entry.kind} ${entry.detail} has not reached the supervisor yet; it is reported on the next scan`);
    }
    const changed = [
      ...spawnSync("git", ["-C", R.copy, "diff", "--name-only", R.head, "HEAD"], { encoding: "utf8" }).stdout.split("\n"),
      ...spawnSync("git", ["-C", R.copy, "status", "--porcelain", "--untracked-files=all"], { encoding: "utf8" }).stdout.split("\n").map((line) => line.slice(3)),
    ].filter(Boolean);
    for (const skill of R.skills.filter((entry) => entry.required && !entry.required.at)) {
      const answer = R.answers[skill.name];
      const touched = skill.required.on_paths?.some((glob) => changed.some((file) => pathPattern(glob).test(file)));
      // A command that ran without its skill in the current generation stays
      // owed until the skill is read and the command is run again after it;
      // being reported to the supervisor does not settle it.
      const rerun = Date.parse(R.ran_after_read?.[skill.name] ?? "");
      const skipped = R.violations.some((entry) => entry.kind === "trigger-skipped" && entry.detail === `${skill.name} c${R.compactions}` && !(answer && rerun >= Date.parse(answer.at)));
      if ((touched && !answered(R, skill.name)) || skipped) open.push(`required skill ${skill.name} was due and is not loaded: ${helperCommand(R, "serve", q(skill.name))}`);
    }
    return open;
  });
  if (!reasons.length) return;
  process.stderr.write(`fm-project-rules: ${id} is not ready:\n${reasons.map((reason) => `  - ${reason}`).join("\n")}\n`);
  process.exit(1);
}

function size([copy, tool]) {
  const L = loadList(copy);
  if (!L) die(`${copy} declares no project rules`, 3);
  if (!TOOLS.includes(tool)) die("size needs claude or codex");
  const resolved = resolveList(copy, L, tool);
  const R = { state: "<state>", task: "<task>" };
  const block = renderBlock(R, resolved, newTable());
  process.stdout.write(`payload_bytes=${payloadBytes(resolved)} block_bytes=${Buffer.byteLength(block)} budget_bytes=${L.budget_bytes ?? "none"} cap_bytes=${CAP}\n`);
}

function status([state, id]) {
  withRecord(state, id, (R) => {
    if (!R) die("no project rules record for this task", 3);
    const { table, secret, ...shown } = R;
    process.stdout.write(`${JSON.stringify({ ...shown, rows_used: R.used.length }, null, 2)}\n`);
  });
}

// Codex takes the block as one TOML string; a JSON string literal is valid TOML.
function emit([state, id]) {
  process.stdout.write(`developer_instructions=${JSON.stringify(fs.readFileSync(path.join(dirPath(state, id), "block.txt"), "utf8"))}`);
}

// Add Firstmate's Claude hooks to a settings file without dropping what is there.
function mergeSettings([file, state, id]) {
  let settings = {};
  if (fs.existsSync(file)) {
    try {
      settings = JSON.parse(fs.readFileSync(file, "utf8"));
    } catch (error) {
      die(`${file} exists and does not parse, so Firstmate's hooks cannot be merged into it: ${error.message}`);
    }
  }
  const added = JSON.parse(fs.readFileSync(0, "utf8")).hooks;
  if (fs.existsSync(recordPath(state, id))) {
    const command = (event) => ({ hooks: [{ type: "command", command: `${q(HELPER)} hook ${q(state)} ${q(id)} ${event}` }] });
    added.SessionStart = [...(added.SessionStart ?? []), command("session-start")];
    added.PreToolUse = [...(added.PreToolUse ?? []), command("pretool")];
  }
  settings.hooks ??= {};
  for (const [event, groups] of Object.entries(added)) settings.hooks[event] = [...(settings.hooks[event] ?? []), ...groups];
  fs.writeFileSync(file, `${JSON.stringify(settings)}\n`);
}

const [verb, ...args] = process.argv.slice(2);
const verbs = {
  admit, next, ack, serve, hook, ready, size, status, emit, respond,
  "merge-settings": mergeSettings,
  scan: ([state, id, rollout, act]) => scanOne(state, id, rollout, true, act),
  detect: ([state, id, rollout, act]) => scanOne(state, id, rollout, false, act),
};
if (!verbs[verb]) die(`unknown verb ${verb ?? ""}`);
if (verb === "hook" && args[2] !== "session-start") failCode = 2;
try {
  verbs[verb](args);
} catch (error) {
  die(`internal failure: ${error.message}`);
}
