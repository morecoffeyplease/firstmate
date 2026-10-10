#!/usr/bin/env node
// Engine behind bin/fm-project-rules.sh; that wrapper's header owns usage and
// docs/project-rules.md owns the contract. Invoked only through the wrapper,
// which resolves the task's Codex session log before calling in.

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const CAP = 79000;
const PART = 6000;
const ROWS = 64;
const STAGE_SECS = Number(process.env.FM_PROJECT_RULES_STAGE_SECS || 900);
const GRACE_SECS = Number(process.env.FM_PROJECT_RULES_GRACE_SECS ?? 60);
const TOOLS = ["claude", "codex"];
const EDIT_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"];
const HELPER = process.env.FM_PROJECT_RULES_HELPER || "";
const BIN = path.dirname(HELPER);

const die = (message, code = 1) => {
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
    "- Load a required skill only through that helper; it says when one is required.",
    "- A child agent does not receive this block. Use only general-purpose children, and run required-skill commands such as product tests yourself, not in a child.",
  ];
  for (const rule of resolved.rules.filter((entry) => !entry.native)) {
    lines.push("", `## Rule file: ${rule.label} [${rule.id}]`, "", rule.data.toString("utf8").trimEnd());
  }
  lines.push("", "## Skill catalog", "");
  for (const skill of resolved.skills) {
    lines.push(`- ${skill.name} (${skill.invocation === "model" ? "auto" : "manual"}${trigger(skill.required)}): ${skill.description}`);
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

function refuseCodexConfig(copy, resolved) {
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
  const agents = resolved.rules.find((rule) => rule.native && rule.file === chain);
  return agents ? agents.id : null;
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
  if (L.prepare) {
    const result = runIn(copy, L.prepare.argv, L.prepare.timeout_s);
    if (result.status !== 0) {
      die(`project prepare step failed (exit ${result.status ?? "timeout"}): ${String(result.stderr || result.stdout || "").trim().slice(-600)}`);
    }
    const dirty = spawnSync("git", ["-C", copy, "status", "--porcelain"], { encoding: "utf8" }).stdout.trim();
    if (dirty) die(`project prepare step left the copy with uncommitted changes:\n${dirty.split("\n").slice(0, 10).join("\n")}`);
  }
  const resolved = resolveList(copy, L, tool);
  const R = { v: 1, task: id, state, copy, tool, version, backend, version_qualified: versionQualified };
  if (tool === "codex") refuseCodexConfig(copy, resolved);
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
    gen: option("--gen") || crypto.randomUUID(),
    brief: option("--brief"),
    head: spawnSync("git", ["-C", copy, "rev-parse", "HEAD"], { encoding: "utf8" }).stdout.trim(),
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
    hits: {},
    violations: [],
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

function owed(R) {
  const stage = R.stage;
  const steps = [];
  if (!stage.closed) {
    if (stage.row < 0) steps.push("The receipt table is used up; tell your supervisor this task needs a relaunch.");
    else if (!stage.acked) steps.push(`Quote receipt row ${String(stage.row).padStart(2, "0")} from the end of your project rules block: ${helperCommand(R, "ack")} <the 6-character code>`);
    if (!stage.brief) steps.push(`Read your launch brief again: ${helperCommand(R, "serve", "brief")}`);
    for (const skill of startSkills(R).filter((entry) => !answered(R, entry.name))) {
      steps.push(`Load required skill ${skill.name}: ${helperCommand(R, "serve", q(skill.name))}`);
    }
  }
  return steps;
}

function next([state, id]) {
  withRecord(state, id, (R) => {
    if (!R) return process.stdout.write("Project rules: nothing is declared for this task.\n");
    const steps = owed(R);
    if (steps.length) {
      process.stdout.write(`Project rules admission (stage ${R.stage.name}) is OPEN. Do these now, in order, before any other action:\n`);
      steps.forEach((step, index) => process.stdout.write(`${index + 1}. ${step}\n`));
      return;
    }
    process.stdout.write("Project rules: nothing is owed right now.\n");
    for (const skill of R.skills.filter((entry) => entry.required && !entry.required.at && !answered(R, entry.name))) {
      const when = skill.required.before_commands ? "running its trigger commands" : `editing ${skill.required.on_paths.join(", ")}`;
      process.stdout.write(`Before ${when}, load ${skill.name}: ${helperCommand(R, "serve", q(skill.name))}\n`);
    }
  });
}

function ack([state, id, code]) {
  withRecord(state, id, (R) => {
    if (!R) die("no project rules are declared for this task");
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
    const parts = partsOf(R, what);
    const code = (part) => crypto.createHmac("sha256", R.secret).update(`${what}|${R.compactions}|${part}`).digest("hex").slice(0, 8);
    let done = 0;
    if (given) {
      done = parts.findIndex((_, index) => code(index + 1) === given) + 1;
      if (!done) die(`that code does not belong to ${what} at this stage; start again with: ${helperCommand(R, "serve", q(what))}`);
    }
    if (done === parts.length) {
      if (what === "brief") R.stage.brief = true;
      else R.answers[what] = { gen: R.compactions, at: new Date().toISOString() };
      settle(R);
      return process.stdout.write(`${what}: all ${parts.length} part(s) delivered and recorded.${owed(R).length ? ` Next: ${helperCommand(R, "next")}` : ""}\n`);
    }
    const part = done + 1;
    process.stdout.write(`--- ${what} part ${part} of ${parts.length} ---\n${parts[part - 1]}--- end of part ${part} of ${parts.length}; continue with: ${helperCommand(R, "serve", q(what), code(part))} ---\n`);
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
  const deny = (message) => {
    process.stderr.write(`${message}\n`);
    process.exit(2);
  };
  const tool = payload.tool_name ?? "";
  const input = payload.tool_input ?? {};
  const command = tool === "Bash" ? String(input.command ?? "") : "";
  const ours = command.trim().replace(/^(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|"[^"]*"|\S*)\s+)*/, "").replace(/^['"]/, "").startsWith(`${BIN}/`);
  const verdict = withRecord(state, id, (R) => {
    if (!R) return "";
    if (!R.stage.closed && !ours && !["Read", "Glob", "Grep"].includes(tool)) {
      return `Project rules admission (stage ${R.stage.name}) is open, so this tool call is refused. Run: ${helperCommand(R, "next")}`;
    }
    if (["Agent", "Task"].includes(tool)) {
      const type = input.subagent_type || "general-purpose";
      if (!R.children.includes(type)) {
        const allowed = R.children.length ? `Use one of: ${R.children.join(", ")}.` : "No child type is qualified for this tool version, so do the work yourself.";
        return `Child agent type ${type} has not been shown to load this project's rules, so it is refused. ${allowed}`;
      }
    }
    for (const skill of R.skills.filter((entry) => entry.required && !entry.required.at)) {
      const rel = EDIT_TOOLS.includes(tool) ? path.relative(R.copy, path.resolve(R.copy, String(input.file_path ?? input.notebook_path ?? ""))) : "";
      const matched = skill.required.before_commands
        ? Boolean(command) && !ours && skill.required.before_commands.some((pattern) => commandPattern(pattern).test(command))
        : Boolean(rel) && skill.required.on_paths.some((glob) => pathPattern(glob).test(rel));
      if (!matched) continue;
      R.hits[skill.name] = true;
      if (payload.agent_id && skill.required.before_commands) return `This command requires skill ${skill.name}, which a child agent cannot be shown to hold. Run it from the main worker.`;
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

function violate(R, kind, detail) {
  if (!R.violations.some((entry) => entry.kind === kind && entry.detail === detail)) R.violations.push({ kind, detail, at: new Date().toISOString() });
}

function noteNative(R, text, gen, wanted) {
  for (const rule of R.rules.filter((entry) => entry.native && wanted(entry))) {
    R.log.native[`${gen}:${rule.id}`] = holds(text, rule.file);
  }
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
    } else if (entry.type === "response_item" && payload.type === "message") {
      const text = (Array.isArray(payload.content) ? payload.content : []).map((item) => item?.text ?? "").join("\n");
      if (payload.role === "developer" && text.includes(block)) R.log.evidence[gen] = true;
      if (payload.role === "user" && text.startsWith("# AGENTS.md instructions")) noteNative(R, text, gen, () => true);
    } else if (entry.type === "response_item" && /_call$/.test(payload.type ?? "")) {
      const raw = strings([payload.input, payload.arguments, payload.action]).join(" ");
      let command = raw;
      try {
        command = JSON.parse(raw.match(/cmd:\s*("(?:[^"\\]|\\.)*")/)?.[1] ?? "null") ?? raw;
      } catch {
        command = raw;
      }
      if (command.includes(`${BIN}/`)) continue;
      const at = Date.parse(entry.timestamp ?? "") || Date.now();
      const closed = Date.parse(R.closed?.[gen] ?? "") || Infinity;
      if (at < closed) {
        if (gen > 0 && R.log.turn === 0) R.same_turn_calls += 1;
        else violate(R, gen === 0 ? "worked-before-admission" : "worked-before-refresh", `stage ${gen === 0 ? "start" : `c${gen}`}`);
      }
      for (const skill of R.skills.filter((item) => item.required?.before_commands)) {
        if (!skill.required.before_commands.some((pattern) => commandPattern(pattern).test(command))) continue;
        R.hits[skill.name] = true;
        const answer = R.answers[skill.name];
        if (!(answer?.gen === gen && Date.parse(answer.at) <= at)) violate(R, "trigger-skipped", `${skill.name} c${gen}`);
      }
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

function scanOne(state, id, rollout, report = true) {
  const out = [];
  withRecord(state, id, (R) => {
    if (!R) return;
    const block = fs.readFileSync(path.join(dirPath(state, id), "block.txt"), "utf8").trimEnd();
    try {
      if (R.tool === "codex" && rollout) scanCodex(R, block, rollout);
      if (R.tool === "claude") scanClaude(R, block);
    } catch (error) {
      out.push(`scan-error ${String(error.message).slice(0, 160)}`);
    }
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
    for (const rule of R.rules) {
      const data = fs.existsSync(rule.file) ? fs.readFileSync(rule.file) : null;
      if (!data || sha(data) !== rule.sha) alarm(`changed:${rule.id}`, `rules-changed ${rule.id} differs on disk from what this session was given; relaunch needed`);
    }
    for (const entry of R.violations) alarm(`violation:${entry.kind}:${entry.detail}`, `${entry.kind} ${entry.detail} (${where})`);
    if (R.same_turn_calls && stage.closed) alarm(`same-turn:${stage.gen}`, `same-turn-calls ${R.same_turn_calls} tool call(s) ran between a mid-turn compaction and its refresh (${where})`);
  });
  for (const line of out) process.stdout.write(`project-rules: ${id} ${line}\n`);
}

// ---- readiness, size, status ---------------------------------------------

function ready([state, id]) {
  const reasons = withRecord(state, id, (R) => {
    if (!R) return [];
    const open = owed(R).map((step) => `owed: ${step}`);
    const changed = [
      ...spawnSync("git", ["-C", R.copy, "diff", "--name-only", R.head, "HEAD"], { encoding: "utf8" }).stdout.split("\n"),
      ...spawnSync("git", ["-C", R.copy, "status", "--porcelain", "--untracked-files=all"], { encoding: "utf8" }).stdout.split("\n").map((line) => line.slice(3)),
    ].filter(Boolean);
    for (const skill of R.skills.filter((entry) => entry.required && !entry.required.at)) {
      const touched = skill.required.on_paths?.some((glob) => changed.some((file) => pathPattern(glob).test(file)));
      if ((touched || R.hits[skill.name]) && !answered(R, skill.name)) open.push(`required skill ${skill.name} is not loaded for the current generation: ${helperCommand(R, "serve", q(skill.name))}`);
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
  admit, next, ack, serve, hook, ready, size, status, emit,
  "merge-settings": mergeSettings,
  scan: ([state, id, rollout]) => scanOne(state, id, rollout),
  detect: ([state, id, rollout]) => scanOne(state, id, rollout, false),
};
if (!verbs[verb]) die(`unknown verb ${verb ?? ""}`);
verbs[verb](args);
