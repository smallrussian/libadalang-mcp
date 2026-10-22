# How well it works

Measured on 2026-10-21 with GNAT Pro / Libadalang 26.1, on
[gnatcoll-core](https://github.com/AdaCore/gnatcoll-core) (commit `ad1fefd`,
about 157 Ada sources). The project file is `core/gnatcoll_core.gpr`, used as is
with `-XGNATCOLL_OS=windows` or `unix`.

## 1. Name resolution vs. the GNAT compiler

Ground truth: the cross-references GNAT writes to `.ali` files when compiling
with `-gnatc`. For **every** reference GNAT recorded in the project's sources
(read, write, call, `with`), we run the same code as `goto_definition` and check
where it lands. For 400 randomly sampled entities, we compare `find_references`
with GNAT's complete list of references. Script: [`scripts/eval_xref.py`](../scripts/eval_xref.py).

| | Windows | Linux |
|---|---|---|
| references checked | 43,842 | 43,057 |
| **goto_definition: same entity as GNAT** | **99.87%** | **99.86%** |
| … same declaration exactly | 74.5% | 74.1% |
| … other view of the same entity ¹ | 25.4% | 25.7% |
| different entity | 0.10% | 0.10% |
| no answer (`unresolved` / `no declaration`) | 0.03% | 0.04% |
| goto_definition latency p50 / p95 | 0.6 / 1.0 ms | 0.3 / 0.5 ms |
| **find_references recall** (400 entities) | **100%** | **99.96%** |
| **find_references precision** | **99.91%** | **99.82%** |
| find_references latency p50 / p95 | 3.9 / 31 ms | 2.7 / 21 ms |
| project load (first call) | 0.7 s | 1.2 s |

¹ GNAT always reports an entity's first declaration. Libadalang returns the
view visible at the reference: inside a package body, the full declaration of a
private type; inside a subprogram body, the body's copy of a parameter.
`goto_definition` lists the entity's other parts too, so both are in the answer.
Matching is done with Libadalang's own part links (`p_all_parts`), not by name.

What the remaining 0.1% are, by inspection:

- **Dispatching calls.** E.g. `Next (Self)` where `Self` is an `INI_Parser`:
  GNAT names the overriding `INI_Parser.Next`, Libadalang the abstract
  `Config_Parser.Next` it overrides.
- **`/=` and `=` on predefined or container types.** Same line in the runtime
  spec, different column convention.
- **Unresolved (12–16 references).** Arguments of `pragma Import (C, ...)` and
  similar, `Interfaces.C` inside a pragma, and one generic actual (`"<" => Less.all`).
- **find_references extras.** The entity named in a `pragma Import`, which GNAT
  doesn't record as a reference.

Bugs this measurement found and fixed: `find_references` listing `end A.B;` of
dotted package names; failures on `A, B : T` declarations; reporting
attributes and predefined operators as `unresolved` instead of
`no declaration`.

## 2. Response size vs. the files it replaces

`scripts/smoke.py --gnatcoll <path>`. "files" is the size of the sources the
answer points into, i.e. what an agent would otherwise open.

| tool call | response chars | file chars | ratio |
|---|---|---|---|
| find_unit GNATCOLL.Strings_Impl | 148 | 121,031 | 818× |
| package_files GNATCOLL.OS (specs) | 1,257 | 103,061 | 82× |
| package_files GNATCOLL.JSON + deps | 1,531 | 122,154 | 80× |
| outline GNATCOLL.JSON (hits the 8k cap) | 7,995 | 26,792 | 3.4× |
| goto_definition | 256 | 48,055 | 188× |
| find_references Create_Object | 803 | 146,284 | 182× |

## 3. An agent with and without the tools

[`scripts/eval_agent.py`](../scripts/eval_agent.py) runs Claude Code headless
(`claude -p`, Sonnet) on 5 questions about gnatcoll-core
([questions and expected answers](../scripts/agent_questions_gnatcoll.json),
ground truth from GNAT). The two configurations:

- **with tools:** the `ada` MCP server plus Read/Grep/Glob;
- **no tools:** Read/Grep/Glob only.

Shell, edits and web access are off in both. Score: every expected `file:line`
present, and no wrong location in the relevant files.

| question | with tools | no tools |
|---|---|---|
| all call sites of `Create_Object` | ✔ 23 s, $0.22 | ✘ listed the body as a call · 19 s, $0.04 |
| call sites of the `Set_Field` overload taking a Boolean (9 overloads) | ✔ 23 s, $0.10 | ✔ 57 s, $0.20 (15 turns) |
| which `Create` overload a call uses (11 overloads) | ✔ 16 s, $0.08 | ✔ 25 s, $0.06 |
| which `Get` overload a call uses (18 overloads) | ✔ 13 s, $0.05 | ✔ 46 s, $0.16 (11 turns) |
| components of record `Parsing_Error` | ✔ 20 s, $0.08 | ✔ 12 s, $0.05 |
| **total** | **5/5 · 95 s · $0.53** | **4/5 · 160 s · $0.51** |

Reading it:

- With the tools, every answer was right. It took 40% less wall time overall,
  at about the same total cost.
- On the overload questions, which need name resolution, the tools made Claude
  2–3× faster and cheaper. Without them it grepped and read its way through
  10–15 turns, and still got there: Sonnet is good at this.
- On simple lookups (a record's components, a name that's unique),
  grep + read is a bit cheaper. Each turn with the tools carries their schemas
  and instructions in the context.
- The tools only see sources in the GPR project. Asked about call sites "in
  this repository", grep also found the calls in `testsuite/`, which isn't in
  `gnatcoll_core.gpr`.

Caveat: 5 questions, one run each, with Claude Sonnet. The server is designed
for Codex + GPT, so treat this as a sanity check that the tools help and
don't mislead, not as a benchmark. Re-run with `--runs 3`, or add questions.

## Reproducing

```bash
# 1. ground truth (writes .ali files outside the source tree)
gprbuild -P core/gnatcoll_core.gpr -XGNATCOLL_OS=unix --relocate-build-tree=/tmp/ali --root-dir=. \
    -c -k -q -j0 --restricted-to-languages=ada -cargs:ada -gnatc
# 2. resolution accuracy
python scripts/eval_xref.py -P core/gnatcoll_core.gpr -w . -XGNATCOLL_OS=unix --ali-dir /tmp/ali
# 3. response sizes
python scripts/smoke.py --gnatcoll /path/to/gnatcoll-core
# 4. agent A/B (uses Claude Code; costs about $1 per run of the 5 questions)
python scripts/eval_agent.py --questions scripts/agent_questions_gnatcoll.json \
    --cwd /path/to/gnatcoll-core --mcp-config ada-mcp.json --model sonnet
```

`eval_xref.py` works on any GPR project: point it at your own codebase's
project and `.ali` directory to get the same numbers for your code.
