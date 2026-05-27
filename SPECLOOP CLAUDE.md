# SpecLoop: Technical Architecture and 16-Week Implementation Roadmap

## TL;DR
- **Build SpecLoop as a Python orchestrator around four open-source pillars: `pyslang` (parsing/IR), Yosys + SymbiYosys (formal), a quantized 7–14B RTL-finetuned LLM served by vLLM (assertion generation), and Qdrant (on-prem semantic search).** Frontier-model parity is not realistic in 4 months solo, but CodeV-CodeQwen-7B (77.6% VerilogEval-Machine pass@1, Apache-2.0) plus RAG and an iterative SBY feedback loop is enough for an MVP that beats AssertLLM-style baselines on small/medium modules.
- **The single hardest technical risk is not LLM quality; it is graceful handling of "real" RTL** — encrypted IP, vendor primitives, missing dependencies, `timescale`/`define` preambles, and modules too large for any context window. Plan the parser/IR and the blackboxing/stub-generation policy first; everything downstream depends on them.
- **Use Yosys+SBY as the only backend in the MVP**, but expose a `FormalBackend` Python ABC so JasperGold (TCL) and Synopsys VC Formal can be slotted in later. Counterexamples come out as VCD; parse with `vcdvcd` and translate into natural-language traces for the LLM repair prompt. Cap iterations at 3 for ≤14B models, 5 for frontier-class.

---

## Plan Coverage Table

| # | Topic | Covered |
|---|---|---|
| 1 | System architecture, project layout | §1 |
| 2 | RTL parsing & hierarchy extraction | §2 |
| 3 | Assertion generation strategy | §3 |
| 4 | Formal verification integration | §4 |
| 5 | Feedback loop design | §5 |
| 6 | On-prem LLM deployment | §6 |
| 7 | Semantic search layer | §7 |
| 8 | Failure classification | §8 |
| 9 | Deep dives — six hard problems | §9 |
| 10 | 16-week roadmap | §10 |

---

## §1. Full System Architecture

SpecLoop is a CLI-first pipeline. Every stage writes structured JSON artifacts to a per-run cache directory; this makes the iterative formal loop debuggable and the whole pipeline resumable.

**Components and data flow:**

```
RTL files ──► (1) Ingest+Preprocess ──► canonical_units.json
              (pyslang preprocessor)
                          │
                          ▼
              (2) IR Extractor ──► ir/{module}.json  (hierarchy, ports, params, FSM)
              (pyslang AST walk)
                          │
                          ▼
              (3) Dependency Resolver ──► closures/{module}.f  (filelist + stubs)
              (NetworkX DAG over IR)
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
   (4) Module Classifier        (5) Spec/Assertion Generator
   (heuristics on IR)           (LLM via vLLM OpenAI API)
              │                       │
              └───────────┬───────────┘
                          ▼
              (6) Assertion Builder ──► sva/{module}.sv  (bind module)
                          │
                          ▼
              (7) Formal Runner ──► results/{module}.json  (PASS/CEX/TIMEOUT)
              (sby subprocess, VCD parser)
                          │
                          ▼
              (8) Repair Loop  ◄─── feedback if FAIL/CEX
              (LLM with VCD-derived NL trace)
                          │
                          ▼
              (9) Spec Writer ──► specs/{module}.md
                          │
                          ▼
              (10) Indexer ──► Qdrant collection: speclib
                          │
                          ▼
              (11) Query CLI ──► natural-language search
```

**Technology choices and justification:**

| Component | Tech | Why |
|---|---|---|
| Parser/preprocessor | **pyslang** (Python bindings for slang) | slang is the fastest and most LRM-compliant open-source SV frontend per the chipsalliance sv-tests suite; round-trips preprocessor output, exposes a Python ScriptSession, and parses SV-1800-2023 |
| Fallback parser | **Verible** `verible-verilog-syntax --export_json` | Apache-2.0, explicit JSON CST export, designed to parse un-preprocessed source — good for files slang rejects |
| Elaboration & synthesis | **Yosys 0.46+** with `read_verilog -sv` and the slang plugin | ISC-licensed, scriptable from Python via subprocess, supports `(* blackbox *)` for missing modules |
| Formal engine | **SymbiYosys (sby)** with smtbmc + bitwuzla/yices/z3 | The standard open formal driver; `.sby` is a deterministic INI-style file we can templatize |
| Equivalence check | **EQY** | Used for refactor/optimization checking, not assertion truth; partitions designs so it scales better than SBY on miters |
| Mutation-based assertion-quality | **MCY** | Measures how good the **assertion set** is at catching single-bit mutations — directly answers "how complete is my generated spec?" |
| VCD parsing | **vcdvcd** (`StreamParserCallbacks`) | Pure-Python, stream-based, the same library VCDiag (arXiv 2506.03590) uses for waveform failure classification |
| LLM serving | **vLLM 0.6+** with Marlin-AWQ INT4 | PagedAttention + Marlin-AWQ; on jarvislabs.ai's H200 benchmark of Qwen2.5-32B-Instruct-AWQ, Marlin-AWQ delivered **741 tok/s vs AWQ's 68 tok/s — a 10.9× speedup** while preserving 51.8% HumanEval pass@1 (identical to FP16); OpenAI-compatible REST decouples us from any single model |
| Dev/local model | **Ollama** with GGUF Q5_K_M | For laptop dev when GPU isn't available |
| Embedding model | **UniXcoder-base** for code, **BGE-large-en-v1.5** for NL spec text | UniXcoder is one of the strongest open code embedders; CoCoSoDa (arXiv 2204.03293) reports it as the third-best baseline (beaten only by CoCoSoDa itself, which exceeds UniXcoder by 5.9% MRR on average) |
| Vector DB | **Qdrant** (self-hosted, Rust) | Highest RPS in Qdrant's own 2024 benchmark across most scenarios, real-time updates, rich payload filtering — fits per-module metadata filters |
| CLI | **Typer** + **Rich** | Typer = decorator-based subcommands; Rich = readable diagnostics |
| Orchestration | **Prefect 2** or plain `asyncio` + SQLite cache | Each module is independent ⇒ trivially parallel; Prefect adds retries and observability |
| Configuration | **Pydantic v2** models | One settings file (`speclib.toml`); each artifact JSON is validated by a model |

**Project layout:**

```
speclib/
├── pyproject.toml            # uv / hatch
├── speclib.toml              # default config
├── src/speclib/
│   ├── cli.py                # Typer entrypoint
│   ├── config.py             # Pydantic settings
│   ├── ir/
│   │   ├── schema.py         # IR pydantic models
│   │   ├── extractor.py      # pyslang AST → IR
│   │   └── preamble.py       # `timescale, defines, packages
│   ├── deps/
│   │   ├── resolver.py       # closure + filelist
│   │   └── blackbox.py       # stub generation
│   ├── classify/
│   │   └── module_type.py    # FSM / mem / comb / seq heuristics
│   ├── gen/
│   │   ├── prompts/          # jinja2 templates
│   │   ├── llm_client.py     # vLLM OpenAI wrapper
│   │   └── assertion.py      # SVA assembly + bind file
│   ├── formal/
│   │   ├── backend.py        # ABC: run(), parse_result()
│   │   ├── sby_backend.py
│   │   ├── eqy_backend.py
│   │   └── jasper_backend.py # TCL template
│   ├── vcd/
│   │   └── trace_to_nl.py    # vcdvcd → natural language
│   ├── loop/
│   │   └── repair.py         # iterative feedback
│   ├── failure/
│   │   ├── classify.py       # error-pattern regex
│   │   └── schema.py         # FailureReport pydantic
│   ├── index/
│   │   ├── embed.py          # UniXcoder + BGE
│   │   └── store.py          # Qdrant client
│   └── search/
│       ├── query.py
│       └── decompose.py      # composite query splitter
├── tests/
│   └── fixtures/             # tiny SV designs
└── examples/
    └── opentitan_prim/
```

**CLI surface:**

```
speclib ingest  <path> [--top MODULE] [--filelist FILE]
speclib spec    <module> [--max-iter 3] [--backend sby|jasper]
speclib spec-all [--parallel N]
speclib search  "find an AXI-Lite slave with byte-strobe write"
speclib report  <module>
speclib serve   # FastAPI for IDE plugins
```

---

## §2. RTL Parsing and Hierarchy Extraction

### 2.1 Library comparison

| Library | Lang | SV-2012/17 cov. | Generates JSON | Preproc | Notes |
|---|---|---|---|---|---|
| **pyslang / slang** | C++ w/ Python | Most complete open source per chipsalliance sv-tests; LRM 1800-2023 | Yes via API | Built-in, IEEE-compliant | **Recommended primary.** Round-trips source; robust on broken code |
| **Verible** | C++ (chipsalliance) | High coverage; explicitly tolerates un-preprocessed source | `verible-verilog-syntax --export_json` | Partial (handles `define etc. inline) | Fallback / for files slang rejects |
| **sv-parser** | Rust (dalance) | Fully compliant with IEEE 1800-2017 | Indirect (custom walk) | Yes (`parse_sv` takes defines/includes) | Pure parse tree only — no elaboration |
| **PyVerilog** | Python | Verilog-2005 + some SV; weak on packages/interfaces/typedefs | Internal AST | Limited | Has dataflow/controlflow analyzers but ages poorly; users often sv2v first |
| **Verilator** | C++ | Synthesizable subset, very good | `--xml-only` (XML AST) | Robust | Heavy dep; good for elaboration; XML is verbose |
| **Icarus** | C++ | Older; weaker on SV | No | Yes | Use only as simulation backend, not parsing |
| **tree-sitter-verilog** | C w/ Python | Surface syntax only — not full SV | Trees only | No | Good for syntax-highlighting; **not enough for IR extraction** |
| **hdlConvertor** | C++/ANTLR4 | Full SV-2017 claim | Yes, JSON AST out | Yes | ANTLR-based; slower than slang; useful as third opinion |

**Decision:** Primary = `pyslang` for AST + elaboration; Verible's `--export_json` is the auxiliary fallback for files slang refuses. Reject PyVerilog as primary because of weak SV-2012+ support.

### 2.2 The preamble problem

Every module-under-prove must be presented to Yosys with a synthesizable preamble:

1. During ingest, run `slang -E` (preprocessor only) to expand all `` `define ``, `` `include ``, `` `ifdef ``, and `` `timescale ``. Save expanded text alongside the original.
2. Walk imports: for each `import pkg::*` or `pkg::name` reference, materialize a `pkg_<name>_preamble.svh` containing the package source (or an `import pkg::*` line if we re-include the package file).
3. At formal time, the `.sby` `[files]` section is ordered: `package_files` → `interface_files` → `dep_modules` → `dut.sv` → `bind.sv`. The slang Yosys plugin then handles SV ⇒ RTLIL.
4. Persist the file-order DAG in `closures/{module}.f` (the canonical industrial Verilog filelist format).

### 2.3 Dependency closure

Build a `networkx.DiGraph` over IR:
- Nodes = `(module, parameter_overrides_hash)` tuples (since `foo #(.W(8))` and `foo #(.W(16))` are distinct elaborations).
- Edges = instantiations, including those inside `generate`/`if-generate` blocks that survive elaboration.
- Closure = reverse-topological-sort all reachable nodes from the user-named top.
- Parametric instantiations: ask pyslang for the *elaborated* compilation, which resolves generates; record the resolved parameter map per instance.

### 2.4 Edge cases and graceful degradation

| Case | Handling |
|---|---|
| **Encrypted IP** (`.v.enc`, `.svp`, `pragma protect`) | Detect by extension/`pragma protect begin`. Skip parsing; emit a **stub blackbox** matching the wrapper's declared interface; mark `confidence=encrypted` |
| **Vendor primitives** (Xilinx `OBUF`/`DSP48`, Intel `LCELL`) | Ship a curated `vendor_blackboxes/` library (~30 modules covers 7-series + UltraScale + Stratix common primitives); auto-include based on detected vendor pragmas |
| **`(* synthesis translate_off *)`** | Honor in pyslang via preprocessor flags; alternatively strip the wrapped region during ingest and log |
| **Missing dependency** | Generate a `(* blackbox *)` stub from the **caller's port connection list** — infer port directions from how signals are read/written at the call site (slang's elaborator returns this); width = connecting net's width |
| **Mixed Verilog-1995/2001/SV** | Always parse with `slang --std=sv2023`; slang accepts older dialects as subsets. For files that fail, fall back to Verible (more lenient) and flag `parse_quality=partial` |
| **`include` chains** | Resolve via slang's `-I` include paths from the filelist; fail-soft to stub if unresolvable |
| **`bind` directives in DUT** | Preserve; IR records `bind` targets so we don't double-bind |

When dependencies can't be resolved, Yosys's `blackbox` pass converts the module to an empty stub with the same ports — per the Yosys docs: *"The 'blackbox' attribute on modules is used to mark empty stub modules that have the same ports as the real thing but do not contain information on the internal configuration."* This is well-supported by SBY and EQY (with caveats around parametric port widths — see Yosys issues #1259, #4561).

### 2.5 IR schema (JSON)

```json
{
  "$schema": "speclib.ir/v1",
  "module": "axi_lite_slave",
  "file": "rtl/axi/axi_lite_slave.sv",
  "lines": [12, 247],
  "parameters": [
    {"name":"ADDR_W","type":"int","default":32},
    {"name":"DATA_W","type":"int","default":32}
  ],
  "ports": [
    {"name":"clk","dir":"in","width":1,"type":"logic","is_clock":true},
    {"name":"rst_n","dir":"in","width":1,"type":"logic","is_reset":true,"polarity":"low"},
    {"name":"awvalid","dir":"in","width":1,"interface":"axi_lite","role":"aw.valid"}
  ],
  "always_blocks": [
    {"kind":"always_ff","sensitivity":["posedge clk"],"has_async_reset":true}
  ],
  "state_regs": [
    {"name":"wr_state","width":2,"encoding":"binary",
     "states":["IDLE","ADDR","DATA","RESP"]}
  ],
  "memories": [],
  "submodules": [
    {"inst":"u_arb","module":"prim_arbiter_fixed","params":{"N":4}}
  ],
  "imports": ["axi_pkg"],
  "type_inferred": "sequential.fsm.interface",
  "confidence": 1.0
}
```

---

## §3. Assertion Generation Strategy

### 3.1 Module type inference

A pure-IR rule cascade (all features available from §2.5):

1. **Encrypted/blackbox** → skip assertion gen, only emit interface contract.
2. **Memory**: contains `reg [W-1:0] mem [...]` *and* both write-enable + read-enable signals → memory template.
3. **FSM**: has `state_regs` with > 1 state encoding *and* a `case`/`unique case` on the state reg → FSM template.
4. **Interface**: ≥ 80% of ports have an interface tag (matched against an AXI/AHB/APB/Avalon/Wishbone signal lexicon) → interface contract template.
5. **Sequential**: contains `always_ff` or `always @(posedge clk)` → sequential template.
6. **Combinational** otherwise.

### 3.2 Assertion categories

| Category | Templates | Example |
|---|---|---|
| Interface contracts | valid/ready stability, no-X-on-valid, payload constancy while stalled | `valid && !ready |=> $stable(data)` |
| Functional (comb) | output = f(inputs), bit-level identities | `assert(parity == ^data)` |
| Temporal | latency, throughput bounds | `req |-> ##[1:4] ack` |
| Safety | no overflow, mutex, X-prop | `!(grant_a && grant_b)` |
| Liveness | eventual ack/grant | `req |-> s_eventually ack` (cover under BMC depth) |
| FSM completeness | state-reachability covers, no-deadlock, illegal-encoding | `cover(state == DONE)`; `unique case` exhaustiveness |

### 3.3 Prompt structure (concrete template)

```jinja
{# system #}
You are an expert in SystemVerilog Assertions (SVA) for formal property
verification with Yosys/SymbiYosys. Generate SVA properties that will be
bound to the module via a `bind` statement. Use only synthesizable SVA
supported by sby+verific. Avoid $past with negative arguments and avoid
properties spanning multiple clock domains.

{# user #}
## Module under verification
{{ module_name }} ({{ module_type }})

## Ports
{{ ports_table }}

## Parameters
{{ params_table }}

## Detected clock(s) and reset(s)
clk={{ clk }}, rst={{ rst }} (active {{ polarity }})

## Source RTL
```systemverilog
{{ module_rtl }}
```

{% if neighbors %}
## Surrounding context (instantiating parent signature)
{{ neighbors }}
{% endif %}

{% if few_shot %}
## High-quality reference assertion suites (retrieved by similarity)
{% for ex in few_shot %}
### {{ ex.title }}
```systemverilog
{{ ex.body }}
```
{% endfor %}
{% endif %}

## Task
Generate a `bind {{ module_name }} {{ module_name }}_sva` module containing:
1. Interface contracts (every valid/ready pair, every read-after-write)
2. Functional properties for each output (≥ 1 per output)
3. Temporal properties for any latency >1 cycle paths
4. Safety: no X on outputs after reset; one-hot/mutex on grant signals
5. FSM: cover each named state; no deadlock; legal-state assertion

Output JSON:
{
  "bind_module": "<full SV source>",
  "assertion_index": [
    {"name": "...", "category": "...", "rationale": "..."}
  ]
}
```

### 3.4 Large-module handling

VerilogEval modules are tiny (≤ 200 LoC); real RTL is not. Three escalations:

1. **Sub-block extraction**: walk the IR for top-level always-blocks and `case`/`if` sub-trees; emit each as separately verifiable sub-properties, then compose.
2. **Sliding window over always-blocks**: include port decls + the always-block of interest + signals it reads/writes. AssertionForge (arXiv 2503.19174) uses a knowledge-graph fusion of spec + RTL for the same goal.
3. **Hierarchical summarization**: ask the LLM to first produce a *natural-language behavioral summary* of the module (cheap), cache it, and use those summaries as context when generating assertions for *parents* — the pattern Spec2Assertion (arXiv 2505.07995) calls "progressive regularization."

### 3.5 Quality metrics

- **SVA-syntax pass rate** (pyslang elaborates the bind file) — fast pre-filter; AssertCraft (arXiv 2411.15442) reports +26% syntax pass after iterative re-prompting against a custom compiler.
- **Formal pass rate** under SBY modes `prove` and `bmc`.
- **Mutation kill rate** via **MCY**: mutates single bits in the post-synth netlist and re-runs assertions; uncovered mutations → assertion gap. The ZipCPU MCY case study reports `Coverage: 99.9%` of mutations after iteration, with an `EQGAP` tag flagging mutations that expose a formal-equivalence gap but no assertion fires. This is the single best objective measure of assertion *completeness* in open source.
- **Coverage** via SBY `mode cover` — counts how many cover points are reachable in the bounded trace.

AssertLLM (arXiv 2402.00386) reports 89% of generated SVAs are syntactically and functionally correct on an I2C design using GPT-4 + RAG — the realistic upper bound for the MVP.

---

## §4. Formal Verification Integration

### 4.1 Yosys script

Generated per module by Jinja2:

```tcl
# yosys read.ys
read_verilog -sv -formal pkg/axi_pkg.sv
read_verilog -sv -formal {{ dependency_files }}
read_verilog -sv -formal {{ dut }}
read_verilog -sv -formal {{ bind_sva }}
hierarchy -check -top {{ top }}
prep -top {{ top }} -flatten
chformal -assert -cover -assume
write_smt2 -wires build/{{ top }}.smt2
```

For SV with property/sequence syntax beyond Yosys's built-in reader, prepend `verific -sv ...; verific -import {{ top }}` if the user has Tabby CAD; otherwise the slang plugin (`yosys-plugin-slang`) closes much of the gap.

### 4.2 `.sby` template

```ini
[tasks]
bmc
prove
cover

[options]
bmc: mode bmc
bmc: depth {{ bmc_depth | default(40) }}
prove: mode prove
prove: depth {{ prove_depth | default(20) }}
cover: mode cover
cover: depth {{ cover_depth | default(60) }}
multiclock {{ multiclock | default('off') }}

[engines]
bmc:   smtbmc bitwuzla
prove: abc pdr
cover: smtbmc bitwuzla

[script]
read -formal {{ dut }}
read -formal {{ bind_sva }}
prep -top {{ top }}

[files]
{% for f in files %}{{ f }}
{% endfor %}
```

The `.sby` reference docs (`symbiyosys.readthedocs.io/en/latest/reference.html`) define this exact section structure: `[tasks] [options] [engines] [script] [files]`. Engines per the official quickstart: `smtbmc` for BMC/cover, `abc pdr` for unbounded prove.

### 4.3 EQY

Use only when an existing reference implementation exists and the user wants the LLM-generated equivalent spec validated against it:

```ini
[gold]
read_verilog -sv reference.sv
prep -top dut
[gate]
read_verilog -sv generated.sv
prep -top dut
[strategy sby]
use sby
depth 4
engine smtbmc bitwuzla
```

The EQY docs note its asymmetric 3-valued (gold) / 2-valued (gate) X-propagation semantics — important when don't-cares matter. Use SBY-on-miter for sub-module assertion checks; use EQY only for equivalence checking.

### 4.4 Multi-backend abstraction (Python)

```python
class FormalResult(BaseModel):
    status: Literal["pass","fail","timeout","compile_error","unknown"]
    assertion: str
    counterexample_vcd: Path | None
    counterexample_nl: str | None       # filled in by §5
    depth_reached: int
    engine: str
    wall_seconds: float
    log_tail: str

class FormalBackend(Protocol):
    def run(self, work_dir: Path, top: str, dut: Path,
            bind_sva: Path, deps: list[Path], mode: str) -> list[FormalResult]: ...

class SBYBackend:   # subprocess sby
class JasperBackend:# writes run.tcl; calls `jg -batch run.tcl`
class VCFormalBackend: # writes fcsh script
```

JasperGold takes a TCL script (per Cadence training and the Chalmers tutorial): `analyze -sv file.sv ; elaborate -top dut ; clock clk ; reset !rst_n ; prove -all ; report -summary`. Capture stdout, scrape `Proven`/`Cex` lines. OpenTitan's `hw/formal/` flow drives Jasper via `dvsim.py` with hjson configs — a good template.

### 4.5 Result schema

```json
{
  "module": "axi_lite_slave",
  "engine": "sby/smtbmc+bitwuzla",
  "totals": {"pass": 12, "fail": 1, "timeout": 0, "vacuous": 2},
  "assertions": [
    {"name":"a_addr_stable_while_stalled",
     "status":"fail",
     "depth":7,
     "cex_vcd":"work/cex_a_addr_stable.vcd",
     "cex_nl":"At cycle 4, awvalid asserts; at cycle 5 awaddr changes but awready is low.",
     "log_tail":"...assert failed at depth 7..."}
  ],
  "wall_seconds": 38.2,
  "confidence": 0.86
}
```

---

## §5. Feedback Loop Design

### 5.1 Loop

```
iter = 0
while iter < MAX:
    sva = LLM.generate(prompt0)
    syn_ok, syn_errs = pyslang_check(sva)
    if not syn_ok:
        prompt0 = repair_prompt(sva, syn_errs); iter+=1; continue
    results = formal_backend.run(...)
    bad = [r for r in results if r.status in {"fail","compile_error","unknown"}]
    if not bad: break
    for r in bad: r.cex_nl = vcd_to_nl(r.counterexample_vcd, ir)
    prompt0 = repair_prompt(sva, results)
    iter += 1
stable = (assertions(prev) == assertions(curr))
```

### 5.2 VCD → natural language

Using `vcdvcd.VCDVCD` with `store_tvs=True`:

1. Identify signals named in the failing assertion (the SBY `--dump-vcd` trace file).
2. For each clock edge, emit a row of `(signal, value, changed_since_last)`.
3. Find the first cycle where the assertion's antecedent is true and the consequent is false; describe the **delta**, not the full trace.
4. Template: *"On cycle N, `{antecedent_signals}` were {values}, so the assertion expected `{consequent}` at cycle N+{k}, but observed `{actual}`. Between cycle N and N+k, `{signal_x}` toggled at cycle N+j due to {inferred_cause: 'reset asserted'/'stall_high'/...}."*

VCDiag (arXiv 2506.03590) classifies waveform failures with exactly this NL-conversion pattern.

### 5.3 Repair prompt

```
Previous assertions:
{{ sva }}

Formal verification results:
{% for r in results %}
- {{ r.name }} ({{ r.status }}): {{ r.cex_nl or r.log_tail }}
{% endfor %}

Most likely causes:
- An assertion was over-strong (consider weakening with disable iff)
- An assumption was missing (reset behavior, handshake stability)
- A vacuous proof (assertion antecedent never true — add a cover)

Revise the bind module so that all assertions either prove or have
a documented counterexample that exposes a real RTL bug. If you believe
the RTL has a bug, surface it as a comment `// BUG_CANDIDATE: ...`.
```

### 5.4 Termination

- `max_iter = 3` for ≤14B open models, `5` for frontier (matches what AssertCraft, ChIRAAG, SANGAM all find empirically: returns diminish past 3–5 iterations).
- **Convergence**: cosine similarity of consecutive assertion-set embeddings > 0.97 → stop.
- **Stuck detector**: same failure signature on the same assertion for 2 iterations → mark assertion `unresolved` and continue.

### 5.5 Failure classification (the loop's view)

- **Vacuous**: SBY `mode cover` for the antecedent fails (antecedent never fires). Re-prompt to weaken or remove.
- **Genuine fail**: `mode bmc` finds CEX *and* `mode prove` finds CEX → real RTL bug or wrong assertion. Surface as `BUG_CANDIDATE`.
- **Compile/elaboration**: Yosys stderr matches `ERROR: ... in line` → re-prompt with errors.
- **Timeout/unknown**: increase depth once, then accept as low confidence.

### 5.6 Module confidence score

```
confidence = 0.5 * pass_fraction +
             0.3 * mutation_kill_rate (MCY) +
             0.1 * cover_fraction +
             0.1 * (1 / (1 + repair_iter))
```

---

## §6. On-Premise LLM Deployment

### 6.1 Model landscape (May 2026)

**Frontier closed (for comparison only, not deployable):**
- GPT-4o: **63% pass@1 on VerilogEval-v2 spec-to-RTL with 1-shot ICL** (up from 56% at 0-shot); 69.0% RTLLM v1.1 Func pass@5 (Pinckney et al., arXiv 2408.11053; VeriCoder Tbl III).
- DeepSeek-R1 (671B MoE, 37B activated): 65.7 / 62.8 VE-M/H pass@1; 58.6% RTLLM Func pass@5 (VeriCoder Tbl III).

**Open general-purpose:**
- Llama 3.1 405B: 58% on VE-v2 spec-to-RTL with 1-shot ICL (Pinckney 2024) — strongest open general model, but ~800 GB at FP16, impractical on-prem for solo.
- DeepSeek-Coder 6.7B (instruct) base: ~24% RTLLM Func under VeriCoder's standardized eval; fits a single 24 GB GPU at AWQ INT4.
- Qwen2.5-Coder-14B-Instruct: base for VeriCoder; 35.3% VE-Human pass@1 unfine-tuned.

**Open RTL-specialized (the operational choice set):**

| Model | Base | License (weights) | VE-Machine pass@1 | VE-Human pass@1 | RTLLM v1.1 Func pass@5 |
|---|---|---|---|---|---|
| **RTLCoder-DeepSeek 6.7B v1.1** | DS-Coder-6.7B | Apache-2.0 (code+data+weights) | **61.2%** (native eval, Liu et al. TCAD'25 Tbl II) | **41.6%** | **48.3%** |
| **CodeV-CodeQwen-7B** | CodeQwen1.5-7B | Apache-2.0 | **77.6%** (Zhao et al. arXiv 2407.10424 Tbl III) | 53.2% | 55.2% |
| **VeriCoder-14B** | Qwen2.5-14B-Inst | Apache-2.0 | 55.7 (standardized) | 38.3 | 48.3% / 79.3% syntax |
| **CodeV-Verilog-QC** (Qwen2.5-Coder-7B) | Qwen2.5-Coder-7B | Apache-2.0 | 80.1% | 59.2% | — |
| **OriGen** | DS-Coder-7B | Apache-2.0 (code) | 74.1 native / 35.9 std | 54.4 / 22.3 | — |
| **BetterV-CodeQwen-7B** | CodeQwen1.5-7B | Code only, **weights not released** | 68.1% | 46.1% | — |
| **ChipNeMo-13B** | Llama-2-13B DAPT+SFT | **Not released** | 43.4% (greedy) | 22.4% | — |
| **VeriGen** (CodeGen-16B FT) | CodeGen-16B | MIT (HF) | ~46% | ~29% | low |
| **ChipSeek-R1** (RL, PPA reward) | DS-R1-Distill-Qwen-14B | Open repo | SOTA on VE-Machine; **+17% Func pass@5, +2.7% Syntax over prior best** on RTLLM v1.1 (Chen et al. arXiv 2507.04736) | — | best |
| **CodeV-R1-7B** | DS-R1-Distill-Qwen-7B | Open | — | 68.6–68.8 VE-v2 spec→RTL pass@1 | **72.9% RTLLM v1.1 pass@1** — beats 671B DS-R1 by 8.1% (Zhu et al. arXiv 2505.24183) |

**Operational choice for SpecLoop MVP:**

- **Primary generator**: **CodeV-CodeQwen-7B** (Apache-2.0, weights public; best published RTLLM/VerilogEval combo at 7B). Runs at AWQ INT4 in ~10 GB VRAM → fits a single RTX 4090 (24 GB).
- **Reasoning fallback for hard modules**: **CodeV-R1-7B** or **ChipSeek-R1** (14B). Use only when the 7B repair loop fails after 2 iterations.
- **Embedding**: UniXcoder-base (768d).

### 6.2 Fine-tuning

For users with RTL of their own and ground-truth spec/assertions:

- **Method**: QLoRA-4bit on the 7B or 14B base. Per arXiv 2308.10462: *"practitioners can fine-tune models such as CodeLlama-7B without exceeding 19GB of GPU memory. For even larger models, such as CodeLlama-34B, QLoRA with quantization enables fine-tuning within the constraints of a 24GB VRAM GPU."* Standard recipe (EvolCodeLlama): r=32, alpha=16, dropout=0.05.
- **Datasets**:
  - **VeriThoughts** (NYU + NJIT, arXiv 2505.20302) — 20K Verilog samples with reasoning traces, formally verified by Yosys. **Primary** for RTL+reasoning fine-tuning.
  - **MetRex** — 25K synthesizable Verilog with post-synthesis metric NL descriptions.
  - **RTLCoder-Data** — 80K instruction-code pairs (Apache-2.0).
  - **OpenLLM-RTL** (ICCAD'24) — 7K verified high-quality samples.
  - **OpenCores** + **FreeCores** crawl for unlabeled DAPT corpus.
- **Format**: instruction-tuning pairs `{"messages":[{"role":"user","content":<RTL>+<port table>},{"role":"assistant","content":<bind module JSON>}]}`. Mix with NL-spec → RTL pairs (VeriThoughts) at 1:1 to retain reasoning ability.
- **Infrastructure**: RunPod ($0.79/hr A100 80GB), Lambda Labs, or Modal. 7B QLoRA on 20K samples ≈ 12 hours on A100 80GB ≈ $10. 14B ≈ 30 hours.
- **Serving**: vLLM 0.6+ with `--quantization awq_marlin`. On jarvislabs.ai's benchmark of Qwen2.5-32B-Instruct-AWQ on H200, Marlin-AWQ achieved **741 tok/s vs AWQ's 68 tok/s — a 10.9× speedup** with identical 51.8% HumanEval pass@1.

### 6.3 Closing the gap with RAG and CoT

ChipNeMo (arXiv 2311.00176) reports concrete domain-adaptation gains: ChipNeMo-70B-SteerLM scored **5.12 vs LLaMA2-70B-Chat's 1.81 (a 3.31-point gain on a 7-point Likert scale)** on the engineering-assistant chatbot, and ChipNeMo-13B-Chat improved bug-summarization Likert scores by **0.82, 1.09, and 0.61** over LLaMA2-13B-Chat on technical summary, managerial summary, and assignment recommendation respectively. Headline takeaway: *"Context, including Retrieval-augmented-generation (RAG) and oracles, significantly boosts all base model performance."*

Practical RAG/CoT for SpecLoop:

- **RAG corpus**: pre-embed the OpenTitan `hw/formal/` directory (open, well-curated SVA examples) + the user's already-documented modules. Retrieve top-3 examples by behavioral similarity (§7) and inject as few-shot.
- **CoT prompt**: "Think step-by-step: (1) identify clock and reset, (2) list each output's combinational vs sequential origin, (3) for each, derive at least one assertion. Then emit SVA."
- **Self-consistency**: generate `n=5` at T=0.5, pick the assertion set with highest syntactic-pass count, then run the loop.
- **Discriminator-guided**: BetterV's contribution — train a small classifier to score assertion quality and bias generation. Out of scope for MVP.

### 6.4 Hardware sizing

| Workload | Min GPU | Recommended |
|---|---|---|
| 7B AWQ inference (CodeV-CodeQwen) | RTX 4090 24 GB | A6000 48 GB (multi-stream) |
| 14B AWQ inference | RTX 4090 24 GB (tight) | A6000 48 GB |
| 7B QLoRA fine-tune | A100 40 GB | A100 80 GB |
| 14B QLoRA fine-tune | A100 80 GB | H100 80 GB |
| CPU fallback (Ollama Q5_K_M) | 32 GB RAM | Slow (~10 tok/s) — dev only |

---

## §7. Semantic Search Layer

### 7.1 What to embed

The naive approach (embed raw RTL) loses behavior. Use a *composite document* per module:

```
[Header]
Module: axi_lite_slave
Type: sequential.fsm.interface
Interfaces: AXI-Lite (subordinate)
Clocks: clk (single)

[Behavioral summary (LLM-generated, cached)]
This module implements an AXI-Lite subordinate that accepts 32-bit
word writes and reads to a 4 KB register file. Write transactions
follow the standard AW/W/B handshake; reads follow AR/R...

[Port signature]
clk:1 in clock; rst_n:1 in reset/low; awvalid:1 in; ...

[State machine]
States: IDLE, ADDR, DATA, RESP
Transitions: IDLE→ADDR on awvalid; ADDR→DATA on awready; ...

[Verified assertion suite]
- valid_stable_until_ready
- no_addr_change_while_stalled
- (12 more)
```

### 7.2 Embedding strategy

Two-tower:
- **Code/structure tower**: UniXcoder-base on RTL + port table. UniXcoder is the strongest open-weight code embedder in its size class, though CoCoSoDa (arXiv 2204.03293) reports it can be beaten by another 5.9% MRR on average with contrastive training — a candidate for a later fine-tune.
- **Text tower**: BGE-large-en-v1.5 on the behavioral summary + assertion names.

At query time, embed the NL query with both towers, concatenate scores with a learned weight (start 0.5/0.5, tune on a held-out query set).

### 7.3 HW-specific prior art

- **HW2VEC** (arXiv 2107.12328) embeds Verilog AST/DFG via GNN — designed for Trojan/IP-piracy detection but the same DFG+pooling can serve as a third tower for structural similarity. Optional MVP+1 feature.
- **LoRACode** (arXiv 2503.05315) — LoRA adapters atop CodeBERT/UniXcoder/StarCoder for code retrieval; not hardware-specific but shows meaningful MRR gains from light fine-tuning.
- No published "behavioral RTL embedding" benchmark exists as of May 2026.

**Recommendation:** start with UniXcoder+BGE on composite documents (§7.1); commit to a small contrastive fine-tune (anchor module, paraphrased spec = positive, random module = negative) on the user's library by month 3.

### 7.4 Vector DB

| DB | Local | Filter | Throughput | Verdict |
|---|---|---|---|---|
| **Qdrant** | Single binary | Rich payload filtering | Highest RPS in Qdrant's own benchmarks across most scenarios | **Pick this** |
| Chroma | Python lib | Basic | Lower throughput; great DX | Use for unit tests |
| FAISS | Lib | None | Fastest raw ANN, no persistence model | Use as raw ANN if needed |
| Weaviate | Container | Schema-based | Heavier ops | Overkill solo |

**Decision**: Qdrant in production, Chroma for unit tests.

### 7.5 Composite queries

For "find a module that does X *and* Y":

1. **Decompose** with the LLM into atomic predicates: `["AXI-Lite subordinate", "byte-strobe write support"]`.
2. **Retrieve** top-K independently per predicate.
3. **Rerank** by intersection: bias toward modules appearing in *both* result sets, fall back to fused score.

For "can existing modules be composed to do Z":

1. Decompose Z into a function pipeline.
2. For each sub-function, retrieve top-1 module.
3. Verify port-type compatibility via the IR (output types of stage *i* match input types of stage *i+1*).
4. Surface as a composition suggestion with a generated wrapper skeleton.

### 7.6 Indexing granularity

Index **per-module** (the composite document above) + **per-assertion** (assertion name + body + parent module ref) in the same Qdrant collection with a `kind` payload filter. Per-module wins for "find a module that…" queries; per-assertion wins for "find an example of valid/ready stability." Hierarchical roll-up (per-design top-level summary) is a v2 feature.

---

## §8. Failure Classification System

### 8.1 Detection patterns

| Failure mode | Detection |
|---|---|
| **Yosys compile** | stderr `^ERROR: (.+) in line (\d+)` → `CompileError(line=…)` |
| **Missing module** | `ERROR: Module '\\(.+)' referenced in module .+ does not have` → `MissingModule(name=…)` |
| **Parameter mismatch** | `does not have a parameter named` → `ParamMismatch(name=…)` |
| **SBY assertion fail** | `engine_0: ## .. Assert failed in (.+): (.+)` + presence of `engine_0/trace.vcd` → `FormalFail(name, vcd)` |
| **SBY timeout** | wall-clock > limit OR `## TIMEOUT` in sby log → `Timeout(depth)` |
| **Vacuous** | `mode cover` of antecedent returns FAIL → `VacuousProof(assertion)` |
| **Context truncation** | tokenize prompt; if > 90% of model ctx → `ContextTruncated(tokens, limit)` |
| **Solver unknown** | `engine_0: Status: UNKNOWN` → `SolverUnknown` |

### 8.2 JSON report schema

```json
{
  "module": "axi_lite_slave",
  "iteration": 2,
  "failures": [
    {
      "type": "FormalFail",
      "assertion": "a_addr_stable_while_stalled",
      "cex_vcd": "...",
      "cex_summary": "awaddr changes at cycle 5 while awready=0",
      "offending_line": 14,
      "suggested_fix": "Add disable iff (!rst_n); guard with awvalid stability"
    },
    {
      "type": "MissingModule",
      "name": "prim_arbiter_fixed",
      "suggested_fix": "Auto-stub generated; rerun with --include hw/ip/prim"
    }
  ]
}
```

### 8.3 CLI output (Rich)

```
✗ axi_lite_slave  iter=2  pass=11/14  conf=0.71
  ▸ a_addr_stable_while_stalled FAIL
      cex: awaddr changes @cycle 5 while awready=0
      fix: add `disable iff (!rst_n)` and guard with $stable(awvalid)
  ▸ a_resp_no_x SUSPECT (vacuous: antecedent never fires within depth 40)
  ▸ COMPILE: line 14 — missing `;` after `assign rdata = …`
```

---

## §9. Deep Dives — Six Hard Problems

### 9.1 Open-model quality gap for RTL

**Status (May 2026):** Open 7B RTL-finetuned models now match or beat GPT-4-class on VerilogEval-Machine (CodeV-CodeQwen 77.6% vs GPT-4o ~63.7% under standardized eval) but trail on VerilogEval-Human spec-to-RTL (53.2% vs 56.1%). The gap is largest on *behavioral* (multi-step temporal) reasoning — exactly what assertion generation needs.

**Techniques that demonstrably work:**
- **Domain-adaptive pretraining (DAPT)** on RTL — ChipNeMo (arXiv 2311.00176) reports +0.82 to +1.09 Likert-scale gains on bug summarization and 3.31-point gains on engineering-assistant chatbot tasks vs LLaMA2 base.
- **Self-reflection / iterative correction** — OriGen (arXiv 2407.16237), AssertCraft (arXiv 2411.15442, +26% syntax pass via re-prompt loop).
- **RL with verifiable reward** — ChipSeek-R1 (arXiv 2507.04736) uses Group Relative Policy Optimization with a hierarchical PPA-aware reward; **+17% Func pass@5, +2.7% Syntax over prior best on RTLLM v1.1**.
- **Knowledge-graph fusion of spec + RTL** — AssertionForge (arXiv 2503.19174).
- **MCTS at inference** — SANGAM (arXiv 2506.13983).
- **Reasoning-trace SFT** — VeriThoughts (arXiv 2505.20302) — fine-tuning small models on R1-derived reasoning traces with formal-equivalence verification labels.

**Recommendation:** Start with CodeV-CodeQwen-7B + RAG + 3-iteration loop. If quality is insufficient on user benchmarks, QLoRA-tune on VeriThoughts (20K) + the user's own labeled set.

### 9.2 Assertion quality and coverage

**Industry standard:** Cadence JasperGold's Coverage App reports branch/statement/expression/functional coverage relative to assertions. In open source, the equivalent is **MCY** (Mutation Cover with Yosys): it mutates single bits in the synthesized netlist and reports the fraction of mutations that the assertion set kills. MCY's `EQGAP` tag specifically isolates mutations that produce a formal-equivalence difference but no assertion fires — these are the assertion gaps to surface.

**Recommended composite metric** (re-stated): `0.5·pass_frac + 0.3·MCY_kill + 0.1·cover_frac + 0.1·iter_decay`.

### 9.3 Scalability to large modules

Three published approaches, each implementable modestly:

1. **Sub-block decomposition** along always-block boundaries (used by AssertCraft).
2. **Spec ⇆ RTL KG fusion** (AssertionForge) — extract entities (signals, FSM states) from both, fuse, then chunk the KG.
3. **Hierarchical summarization** — Spec2Assertion's "progressive regularization" — summarize leaf modules first, then use summaries as context for parents.

Open question (flagged): no public benchmark *exists* for assertion generation on modules > 1000 LoC. The MVP should set an artificial limit at ~800 LoC for direct prompting, escalate to sub-block + summary above that.

### 9.4 Real industrial RTL messiness

Commercial EDA tools (Cadence Xcelium, Synopsys VCS) use Verific as the SV frontend; Verific has decades of bug-compatibility with broken industrial RTL. Open-source equivalents (slang, Verible, Yosys's SV reader) are good but not bug-compatible.

**Heuristics that work:**
- Always try slang first; on parse failure, fall back to Verible; on its failure, attempt Yosys `read_verilog -defer` and convert the bad module to a blackbox stub.
- Maintain a `vendor_blackboxes/` library — ~30 modules covers Xilinx 7-series + UltraScale + Intel Stratix common primitives.
- For encrypted IP, parse only the wrapper and emit a stub; never attempt assertion gen.

### 9.5 Multi-backend formal abstraction

SVA is mostly portable across SBY (with Verific or slang plugin), JasperGold, and VC Formal. **Non-portable constructs to avoid in generated SVA:**
- Multi-clock properties (the SBY docs explicitly state: *"properties spanning multiple different clock domains are currently unsupported"*).
- `local` variables in sequences (mixed support).
- `let` declarations inside property body (Jasper only).
- Recursive properties (limited everywhere).

Constrain the LLM via the system prompt to a "portable SVA subset"; fail-soft to PSL or to a custom Python checker only as last resort.

### 9.6 Semantic search for RTL behavioral similarity

Prior work is thin:
- **HW2VEC** (arXiv 2107.12328) — GNN over AST/DFG; designed for Trojan/IP-piracy detection. Captures structural, not behavioral, similarity.
- **LoRACode** (arXiv 2503.05315) — LoRA adapters atop CodeBERT/UniXcoder/StarCoder for code retrieval; not hardware-specific but shows fine-tuning gains.
- No published "behavioral RTL embedding" benchmark exists.

**Recommendation:** start with UniXcoder+BGE on composite documents (§7.1); commit to a small contrastive fine-tune on the user's library by month 3.

---

## §10. 16-Week Implementation Roadmap

Assumptions: solo developer ~30 hrs/week, GitHub Copilot + Claude Code level AI assistance, RTX 4090 24 GB + occasional A100 rental for fine-tuning.

### Month 1 — Foundation

**Week 1: Skeleton + ingest**
- Build: `pyproject.toml` (uv), CLI skeleton with Typer (`speclib ingest`, `spec`, `search`), Pydantic settings, Rich logging.
- Build: pyslang-based preprocessor + IR extractor for **ports only**.
- Done: `speclib ingest opentitan_prim/` produces `ir/<module>.json` for 50 OpenTitan prim modules.

**Week 2: IR completeness + dependency closure**
- Build: full IR (params, FSM detection, memory detection, always-block summary). NetworkX dep graph + filelist generator.
- Build: `vendor_blackboxes/` with ~30 common Xilinx/Intel primitives.
- Done: any module from OpenTitan or a small CPU (RV32 NERV) produces a complete closure that Yosys can `hierarchy -check` cleanly.

**Week 3: Module classifier + first LLM call**
- Build: heuristic module-type classifier; vLLM serving CodeV-CodeQwen-7B AWQ; basic prompt template; bind-file generator.
- Done: `speclib spec <leaf_module>` produces a syntactically valid `bind_sva.sv` for combinational and simple sequential leaf modules.

**Week 4: Milestone #1 — "Lint-pass MVP"**
- Build: SBY runner (mode `bmc` only, depth 20); FormalResult schema; SQLite cache.
- Done: end-to-end run on 10 leaf modules (counter, FIFO, decoder, simple FSM); ≥ 60% of generated assertions syntactically clean; ≥ 30% prove.
- Demo: CLI shows assertions + Y/N from SBY.

### Month 2 — Formal integration & feedback loop

**Week 5: VCD parsing & NL trace**
- Build: vcdvcd-based counterexample extractor; NL trace translator; failure classification regex library.

**Week 6: Repair loop**
- Build: iterative repair prompt, convergence detection, max-iter caps, confidence score.
- Done: on the same 10-module set, average assertion pass rate improves by ≥ 15 pp after 3 iterations.

**Week 7: Multi-backend abstraction**
- Build: `FormalBackend` ABC; JasperGold TCL emitter (untested without Jasper license but unit-tested via template diffing); EQY mode for equivalence.
- Build: SBY `mode prove` and `mode cover` support; vacuous detection.

**Week 8: Milestone #2 — "Iterative MVP"**
- Build: MCY runner for mutation-kill metric on a small subset (3 modules).
- Done: full feedback loop runs on a 30-module corpus (OpenTitan prim/* + a small CPU); ≥ 50% modules reach `confidence ≥ 0.7`.
- Demo: per-module HTML report with VCD waveform snippet (`gtkwave -b` PNG or `surfer` web view).

### Month 3 — Search, polish, large-RTL

**Week 9: Spec writer + indexing**
- Build: behavioral-summary generator (LLM); composite-document builder; UniXcoder + BGE embeddings; Qdrant docker setup.
- Done: ingestion of OpenTitan + a public CPU produces searchable index.

**Week 10: Search + composite queries**
- Build: query CLI; LLM-based decomposition; rerank; port-type composition check.
- Done: 20 hand-written queries return useful top-3 (e.g., "AXI-Lite slave with byte strobes", "round-robin arbiter for N requesters").

**Week 11: Large-module strategy**
- Build: sub-block extraction, sliding-window prompt, summary cache. Set 800-LoC threshold for direct prompt.
- Done: largest module in corpus (target: ~2000 LoC AXI crossbar) produces a partial assertion suite at `confidence ≥ 0.5`.

**Week 12: Milestone #3 — "Beta-quality CLI"**
- Build: failure-classification CLI output (Rich), `speclib report <module>` HTML.
- Run on a real OpenTitan IP block end-to-end; record baseline metrics for the demo.
- Done: a developer who has never seen SpecLoop can run `ingest`, `spec`, `search` on their own RTL.

### Month 4 — Fine-tune, scale, polish

**Week 13: Fine-tuning prep**
- Build: VeriThoughts + RTLCoder-Data + OpenLLM-RTL ingestion; instruction-pair formatter (RTL → JSON bind module); train/eval split.
- Rent A100 80GB, validate QLoRA recipe end-to-end on a 1K subset.

**Week 14: Full fine-tune + eval**
- Run QLoRA-4bit on CodeV-CodeQwen-7B for 3 epochs on VeriThoughts + RTLCoder-Data. ~24 hours, ~$25.
- Evaluate vs base: VerilogEval-Human pass@1, RTLLM v1.1 Func pass@5, internal SVA-syntax pass rate.
- Done: ≥ 5 pp improvement on at least one benchmark; deploy as `speclib-rtl-7b-ft-v1`.

**Week 15: Scalability + parallelism**
- Build: Prefect or asyncio-based per-module parallel runs; resumable cache; soft GPU memory budget.
- Run on a 500-module corpus.

**Week 16: Milestone #4 — "Demo-ready MVP"**
- Polish: documentation, install script, OS-X + Linux test, a short screencast.
- Final benchmarks: assertion pass rate, MCY kill rate, search MRR@10 vs hand-labeled queries.
- Done: published GitHub repo, blog post with metrics table, working `pipx install speclib`.

### Risk-driven re-prioritization

If by **week 8** the iterative loop is not improving pass rate, the bottleneck is the LLM, not the formal flow → reallocate week 13–14 to fine-tuning early (swap with weeks 9–10). If by **week 11** large modules are still failing badly, drop fine-tuning to half-effort and double down on hierarchical decomposition; the MVP is more useful at "great on small modules" than "mediocre on everything."

---

## Recommendations (decision-ready)

1. **Lock in the parser/IR and dependency-closure design first (weeks 1–2). Everything else depends on robust ingest.** Use pyslang as primary, Verible as fallback. Ship a `vendor_blackboxes/` library on day one.
2. **Use CodeV-CodeQwen-7B at AWQ-Marlin INT4 as the MVP generator** — it fits a single 24 GB GPU, is Apache-2.0, and posts the best open published RTLLM/VerilogEval combo at 7B. Fall back to a 14B reasoning model only for modules where the 7B repair loop fails after 2 iterations.
3. **Make Yosys+SBY the only backend in the MVP**, but keep a `FormalBackend` ABC for Jasper/VC-Formal extensibility. Use bitwuzla for BMC, ABC-PDR for prove.
4. **Adopt the composite confidence score (pass × MCY-kill × cover × iter-decay).** MCY is the only open-source way to objectively answer "how good is my assertion set?".
5. **Treat semantic search as a separate workstream from assertion generation** — they share infrastructure but fail independently. Ship search at month 3 even if assertion quality is weaker than hoped.
6. **Re-benchmark the model choice every quarter.** Between Aug 2024 and May 2026 the open-source RTL SOTA moved from RTLCoder (61%) through BetterV → CodeV → CodeV-R1 → ChipSeek-R1 — your "primary model" recommendation will likely change at least once during the build.

### Thresholds that should change the plan

- **If week-4 MVP can't reach 30% formal-prove rate on toy modules**: the bug is in the prompt or the bind harness, not the model. Stop and debug the bind generation before adding the feedback loop.
- **If week-8 iterative loop yields < 15 pp gain after 3 iters**: switch primary model to CodeV-R1-7B or a 14B reasoning model immediately; the base model is too weak.
- **If week-11 large-module run fails on any module > 500 LoC**: the IR-aware sub-block extractor is the gating piece; reallocate week 13 to it.
- **If on-prem deployment hardware is limited to < 24 GB VRAM**: drop to RTLCoder-DeepSeek-6.7B GGUF Q5_K_M on Ollama and accept ~15 pp lower assertion quality.

---

## Caveats

- **Benchmark numbers are noisy.** Pinckney et al. (arXiv 2408.11053) and Wei et al. (arXiv 2504.15659) report substantially different pass@1 for the same models because of prompt/post-processing differences. Trust internal benchmarks on your own corpus over published ones.
- **No published behavioral-embedding benchmark for RTL exists.** §7's two-tower approach is an educated bet, not a validated technique. Expect 4–6 weeks of fine-tuning iteration if results are weak.
- **The "real industrial RTL" problem is genuinely hard.** Verific (commercial) parses things no open-source frontend reliably does. Set user expectations: SpecLoop will be excellent on clean SV-2017 and degrade gracefully (blackboxes + warnings) elsewhere.
- **Open SVA support varies by toolchain.** SBY without a Verific or slang-plugin license has limited SV property/sequence support; the OSS CAD Suite ships the free slang plugin which closes much of the gap. JasperGold/VC Formal remain the gold standard for hard SVA; their TCL/license surface is the project's main "future work" axis.
- **The field is moving monthly.** CodeV-R1, ChipSeek-R1, AssertionForge, AssertCraft, SANGAM, Spec2Assertion all appeared within the year preceding May 2026. Re-benchmark every quarter; the primary-model recommendation will likely change.
- **ChipNeMo weights are not publicly released**, despite the strong DAPT result — its numbers are useful as a target but not as a deployable model. The closest open reproduction is the RTLCoder/CodeV/VeriCoder line.
- **Fine-tuning may help less than expected.** VeriCoder shows that under standardized eval, several "specialized" models score *lower* than their published native-eval numbers. Always re-evaluate fine-tunes against the standardized harness, not the loose one used in the source paper.