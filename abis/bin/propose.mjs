/**
 * Propose a build configuration, on the runner, from the checked-out tree.
 *
 * WHY THIS MOVED. Until 2026-09-20 the proposing agent ran on the server and
 * PLAN_REMOTE_RUN D9 said it must: a `gh aw` workflow inside GitHub was refused
 * because in shape B the customer's source may not leave their perimeter, and
 * because the server-side agent reads a snapshot already proved to rebuild the
 * commit's git tree. The first half of that stopped applying when Siemens stood
 * up their own GitHub instance — the perimeter now contains the runner. The
 * second half is answered differently rather than abandoned: this reads a
 * checkout at a PINNED SHA, which is the commit by git's own integrity, not a
 * tree someone assembled.
 *
 * NOT `gh aw`, DELIBERATELY. Build_Matrix §14.3 item 4: *"`gh-aw` and the
 * Copilot engine behind it are cloud features"* and are unlikely to run on
 * GHES at all — which is exactly where this now has to work. A plain step that
 * talks to the configured gateway runs anywhere, including an instance with no
 * marketplace and no Copilot.
 *
 * NO DEPENDENCIES, ON PURPOSE. Ships in the run ref beside the zipapps and runs
 * on whatever Node the runner already has. `fetch` is global from Node 18. A
 * step that had to `npm install` on an air-gapped GHES instance would not run
 * at all (§14.3 item 2).
 *
 * EVERY TURN IS EVIDENCE. PLAN_EVIDENCE_INGEST §2.4: an agentic step's output
 * is worthless unless it arrives as evidence — the prompt, the model identity,
 * the transcript and the token counts. All of it is written next to the
 * proposal, in the same bundle, and the server records the result as
 * `ai_assisted` exactly as it does for its own calls.
 */
import { readFile, readdir, writeFile, stat } from "node:fs/promises";
import { join, relative, sep } from "node:path";

const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  args.set(process.argv[i].replace(/^--/, ""), process.argv[i + 1]);
}
const REPO = args.get("repo");
const OUT = args.get("out") ?? "out";
const MODEL = args.get("model") ?? "gpt-5";
const BASE = (args.get("base-url") ?? "https://api.openai.com/v1").replace(/\/$/, "");
const MAX_TURNS = Number(args.get("max-turns") ?? 16);
const KEY = process.env.ABIS_AI_API_KEY ?? "";

/** Files this never opens, and directories it never walks into. */
const SKIP_DIR = new Set([".git", "node_modules", ".venv", "__pycache__"]);
/** One file's worth of prompt. A 4 MB generated header helps nobody. */
const MAX_BYTES = 40_000;
/** The whole listing. Beyond this the tree is described, not enumerated. */
const MAX_ENTRIES = 4_000;

/**
 * Every path under the checkout, relative and forward-slashed.
 *
 * Walked once and cached: the agent asks for listings repeatedly and the tree
 * does not change under it — this runs before the build.
 */
let cachedTree = null;
async function tree() {
  if (cachedTree) return cachedTree;
  const out = [];
  const walk = async (dir) => {
    if (out.length > MAX_ENTRIES) return;
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      if (e.isDirectory()) {
        if (SKIP_DIR.has(e.name)) continue;
        await walk(join(dir, e.name));
      } else if (e.isFile()) {
        out.push(relative(REPO, join(dir, e.name)).split(sep).join("/"));
      }
    }
  };
  await walk(REPO);
  cachedTree = out.sort();
  return cachedTree;
}

/**
 * Read one file, and refuse to leave the checkout.
 *
 * The path comes from a model, which is the definition of untrusted input. A
 * `..` here would read the runner's environment into a prompt that is about to
 * be uploaded as evidence.
 */
async function readCited(p) {
  const rel = String(p).replace(/\\/g, "/").replace(/^\.\//, "");
  if (!rel || rel.startsWith("/") || rel.split("/").includes("..")) {
    return { error: "refused: a path must be relative and stay inside the checkout" };
  }
  const abs = join(REPO, rel);
  if (!abs.startsWith(REPO + sep)) {
    return { error: "refused: resolves outside the checkout" };
  }
  try {
    const s = await stat(abs);
    if (!s.isFile()) return { error: "not a file" };
    const buf = await readFile(abs);
    return {
      text: buf.subarray(0, MAX_BYTES).toString("utf-8"),
      truncated: buf.length > MAX_BYTES,
      bytes: buf.length,
    };
  } catch (err) {
    return { error: String(err && err.message ? err.message : err) };
  }
}

const PROMPT = (paths) => `
You are writing the build configuration for ONE repository, so that a build of
it can be run under tracing and then rebuilt from a reduced source bundle. The
complete source tree is checked out beside you at a pinned commit.

The tree holds ${paths.length} files${paths.length >= MAX_ENTRIES ? " (listing truncated)" : ""}.

Reply with EXACTLY ONE of these, and nothing else:

READ
<one path, relative to the repository root>

or

LIST
<a substring; every path containing it is returned>

or

CONFIG
<a JSON object>

The CONFIG object:

  {"adapter":"meson|cc|msvc|python|typescript-electron",
   "package":"<the importable or shipped name>",
   "build_cmd":"<the command that builds this project from a clean tree>",
   "test_cmd":"<the command that runs its unit tests>",
   "clean_cmd":"<the command that removes build output, or \\"\\">",
   "build_dir":"<meson only>",
   "install_dir":"<meson only>",
   "requirements":"<path to the build requirements file, or null>",
   "project_license":"<SPDX id from the LICENSE file>",
   "component_prefixes":[["<top-level dir>","<component name>",
                          {"origin":"first-party|third-party"}]],
   "keep":["<glob the reduction must not delete>"],
   "hash_only_reason":"<only if there is no test suite: why not, in one sentence>",
   "citations":{"build_cmd":"<path>","test_cmd":"<path>",
                "project_license":"<path>","component_prefixes":"<path>",
                "keep":"<the file that shows the keep is needed>"}}

RULES, enforced after you answer, so violating them loses the field:
- You MUST READ at least one file before sending CONFIG. A configuration
  written without opening anything is a guess that happens to name real files.
- EVERY COMMAND MUST BE SELF-CONTAINED. It runs on a bare runner that has this
  repository, a toolchain and nothing the project uses for development. A
  command naming a tool the project documents but does not ship must install it
  first, in the same command.
- Every value in "citations" MUST be a path in this tree. Each one is re-read.
- Cite the file that STATES the thing, not a README that mentions it.
- "keep" IS WHAT MAKES THE SECOND BUILD WORK, and leaving it empty is the
  single most common way a cell fails. After the traced build the tree is
  reduced to the files the tracer OBSERVED being read, then rebuilt from that
  reduced set. Anything the build system only touches at CONFIGURE time is
  invisible to a tracer and gets deleted, so the rebuild cannot configure and
  the cell fails after a 30-minute build. Measured on numpy twice:
  "File meson_cpu/x86/test_x86_v2.c does not exist", then
  "Missing the highway git submodule".
  List globs for: feature-probe sources compiled only to test whether they
  compile; files a build script checks the EXISTENCE of; submodule directories
  the build refuses to run without; generators and templates read while
  configuring. Cite the file that SHOWS it is needed.
  Keeping a file is a DECLARATION, not a measurement: kept files are reported
  in their own group, "build infrastructure (declared, not measured)", so a
  keep can never inflate what this run claims shipped.
- "test_cmd" MUST be a real command, OR "hash_only_reason" must say why there
  is none. The reduced bundle is rebuilt AND tested, and an empty test command
  silently downgrades that to "it still compiles" — the weaker half of the
  proof disappearing without anyone deciding to drop it.
  DO NOT GO LOOKING FOR SOMETHING ELSE IN THE TREE THAT HAPPENS TO BE
  TESTABLE. Measured on torvalds/linux, for a cell saying arch=hexagon,
  defconfig=comet_defconfig: the answer was
  "make -C tools/testing/selftests TARGETS=kselftest_harness" — the kernel's
  selftest harness, which is not the kernel and not that configuration. A
  configuration that builds the wrong thing is worse than one that admits it
  cannot be tested.
  So: if THIS configuration has a test suite, name the command. If it does not,
  set "hash_only_reason" to one sentence saying why, leave "test_cmd" empty,
  and the cell is recorded as hash-comparison-only — it still builds, reduces,
  rebuilds and compares artifact hashes, and the page says out loud that no
  test ran. Never send both.
- Do not invent a command that "should" work. If the tree does not say how it
  is built, say so in one sentence instead of guessing.
`;

async function call(messages) {
  const r = await fetch(`${BASE}/chat/completions`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${KEY}`,
    },
    body: JSON.stringify({ model: MODEL, messages }),
  });
  const body = await r.json().catch(() => null);
  if (!r.ok) {
    throw new Error(`gateway ${r.status}: ${body?.error?.message ?? "no body"}`);
  }
  return {
    text: body?.choices?.[0]?.message?.content ?? "",
    inputTokens: body?.usage?.prompt_tokens ?? 0,
    outputTokens: body?.usage?.completion_tokens ?? 0,
  };
}

const strip = (s) => s.replace(/^```[a-z]*\n?/i, "").replace(/```$/, "").trim();

async function main() {
  const paths = await tree();
  const transcript = [];
  const messages = [{ role: "user", content: PROMPT(paths) }];
  let reads = 0, inTok = 0, outTok = 0, proposal = null, refusal = null;

  for (let turn = 0; turn < MAX_TURNS; turn++) {
    let reply;
    try {
      reply = await call(messages);
    } catch (err) {
      refusal = `turn ${turn + 1}: ${err.message}`;
      break;
    }
    inTok += reply.inputTokens;
    outTok += reply.outputTokens;
    const lines = reply.text.trim().split("\n");
    const verb = (lines[0] ?? "").trim().toUpperCase();
    const body = lines.slice(1).join("\n").trim();
    transcript.push({ turn: turn + 1, verb, body: body.slice(0, 4000) });
    messages.push({ role: "assistant", content: reply.text });

    if (verb === "READ") {
      const got = await readCited(strip(body));
      reads++;
      messages.push({ role: "user", content:
        `--- ${body} ---\n${got.error ? "ERROR: " + got.error : got.text}`
        + (got.truncated ? `\n(truncated at ${MAX_BYTES} bytes)` : "") });
      continue;
    }
    if (verb === "LIST") {
      const needle = strip(body);
      const hits = paths.filter((p) => p.includes(needle)).slice(0, 200);
      messages.push({ role: "user", content:
        `--- paths containing ${JSON.stringify(needle)} ---\n${hits.join("\n")}` });
      continue;
    }
    if (verb === "CONFIG") {
      if (!reads) {
        messages.push({ role: "user", content:
          "--- refused ---\nYou have not READ a single file. Open what you "
          + "intend to cite first, then send CONFIG." });
        continue;
      }
      try {
        proposal = JSON.parse(strip(body));
      } catch (err) {
        messages.push({ role: "user", content:
          `--- that was not valid JSON ---\n${err.message}\nSend CONFIG again.` });
        continue;
      }
      break;
    }
    messages.push({ role: "user", content:
      "--- unrecognised reply ---\nAnswer with READ, LIST or CONFIG." });
  }

  // EVERY CITATION RE-READ HERE, on the runner, before the server ever sees it.
  // The floor is the same one `prepare.assemble` enforces: a field whose
  // citation does not resolve is reported unresolved and the server drops it.
  const citations = [];
  for (const [field, path] of Object.entries(proposal?.citations ?? {})) {
    const got = await readCited(path);
    citations.push({ field, path, resolved: !got.error });
  }

  await writeFile(join(OUT, "proposal.json"), JSON.stringify({
    proposal,
    citations,
    refusal,
    reads,
    turns: transcript.length,
    model: MODEL,
    base_url: BASE,
    input_tokens: inTok,
    output_tokens: outTok,
    tree_files: paths.length,
    tree_truncated: paths.length >= MAX_ENTRIES,
  }, null, 2) + "\n");
  await writeFile(join(OUT, "agent-transcript.json"),
    JSON.stringify({ model: MODEL, base_url: BASE, transcript }, null, 2) + "\n");

  console.log(proposal
    ? `proposed adapter=${proposal.adapter} after ${transcript.length} turn(s), `
      + `${reads} read(s), ${inTok}+${outTok} tokens`
    : `no configuration: ${refusal ?? "the agent never sent one"}`);
}

await main();
