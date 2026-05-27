# SpecLoop Technical Architecture and Implementation Roadmap

## System architecture and design principles

The product brief defines SpecLoop as a fully on-premises system with two layers: a verified spec-and-assertion generation pipeline over undocumented RTL, and a semantic search layer over the resulting documented codebase. It also makes the most important architectural decision explicit: **do not** treat RTL reconstruction plus equivalence checking as the primary verification method; instead, generate assertions and verify the original RTL directly. That is the right pivot. In practice, SpecLoop should use assertion-driven formal as the source of truth, and reserve equivalence checking for validating SpecLoop’s own preprocessing transforms, wrappers, or future RTL-refactoring features. fileciteturn0file0L3-L6 fileciteturn0file0L17-L18

For a solo developer working toward an MVP in four months, the right architecture is a **modular monolith with plugin interfaces**, not a microservice mesh. Put everything in one Python repository, with containerized adapters for heavyweight dependencies such as Surelog, Yosys/SymbiYosys, EQY, Verilator, and Jasper. Use stable internal contracts between stages so the implementation can later split into services without rewriting the core. This keeps the build simple enough for a CLI-first MVP while still matching the brief’s eventual-platform ambition. fileciteturn0file0L1-L6 fileciteturn0file0L28-L31

The end-to-end flow should be:

**repository or filelist input → build metadata resolution → preprocessing and elaboration → module IR and hierarchy graph → context packing → LLM generation of behaviors/specs/assertions → backend-specific assertion lowering → formal execution → counterexample summarization and repair loop → structured report store → semantic index and search API.** That flow directly implements the brief’s required stages: parsing, preamble preservation, dependency closure, LLM generation, formal verification, iterative refinement, and structured per-module output. fileciteturn0file0L9-L15

A practical repository layout is:

```text
specloop/
  apps/
    cli/              # Typer CLI
    api/              # FastAPI service
  core/
    models/           # Pydantic contracts
    ir/               # ModuleIR, graph, query models
    orchestration/    # pipeline DAG / job runner
    reports/          # report rendering and confidence scoring
  adapters/
    parsers/          # surelog, slang, verible, tree-sitter, verilator
    formal/           # sby, eqy, jasper
    llm/              # local model serving clients
    search/           # embeddings and Qdrant
  prompts/
    behavior/
    assertion/
    repair/
  workers/
    parse_worker/
    llm_worker/
    formal_worker/
  assets/
    assertion_patterns/
    vendor_primitive_models/
    example_manifests/
```

The core internal contracts should be explicit and versioned. At minimum, define five canonical objects:

```python
CompilationBundle   # normalized files, defines, include dirs, compilation units, tops
ModuleIR            # ports, params, packages, clocks/resets, instances, summaries, source spans
AssertionSet        # candidate behaviors, generated SVA, backend profile, bind wrapper
FormalResult        # pass/fail/unknown/error, trace refs, vacuity, timing, backend metadata
SearchDocument      # verified spec text, assertion text, protocol tags, graph features, proof scores
```

Recommended implementation choices for each major component are below.

| Component | Inputs | Outputs | Recommended technology | Why this choice |
|---|---|---|---|---|
| CLI and API | repo path, filelist, top modules, query text | jobs, reports, search results | **Python**, **Typer**, **FastAPI**, **Pydantic** | Python minimizes integration friction with EDA tooling and model stacks; Typer and FastAPI keep the CLI-first MVP and later platform aligned |
| Metadata store | job records, module reports, failure events | queryable structured state | **PostgreSQL** for platform, **SQLite** for local MVP | Start local, move to Postgres without changing the schema model |
| Artifact store | logs, UHDM, AST JSON, VCD/FST/FSDB refs, generated assertions | immutable artifacts | local filesystem for MVP, **MinIO/S3-compatible** later | EDA flows produce lots of files; object storage fits naturally |
| Pipeline runner | stage graph, retries, caching | stage execution records | simple in-process DAG runner first; later **Temporal** or **Dagster** | Do not overbuild the orchestration layer before the MVP works |
| Parser frontends | filelists, defines, include dirs, tops | UHDM, ASTs, diagnostics, hierarchy | **Surelog/UHDM** primary, **slang** secondary, **Verible / tree-sitter-systemverilog** fallback | This combination covers industrial elaboration, semantic introspection, and failure recovery |
| Formal backends | normalized bundle + assertions | proofs, counterexamples, statuses | **Yosys + SymbiYosys**, **EQY**, **Jasper** adapter | Open-source proof path for MVP, commercial path for customers |
| Model serving | prompt packets, retrieved patterns | structured JSON generations | **vLLM** default, **SGLang** where structured generation matters most, **TensorRT-LLM** optional for NVIDIA-only high-throughput deployments | These are the most practical on-prem serving stacks with current ecosystem support |
| Semantic search | verified docs, assertions, graph tags | top-k modules, composition suggestions | **Qdrant** | Qdrant supports dense retrieval, payload filtering, and production-friendly indexing/quantization features |
| Search embeddings | module summaries, assertions, queries | dense vectors and rerank scores | **BGE-M3** for hybrid NL retrieval, **nomic-embed-code** or a domain-tuned RTL encoder for code/RTL similarity | You need both text-semantic and code/behavioral signals, not one generic embedding alone |

The parser-stack recommendation is grounded in the current tool landscape. Surelog is a full SystemVerilog 2017 preprocessor, parser, and elaborator that emits UHDM for downstream tools; slang is designed as a production-quality SystemVerilog library with Python bindings and JSON/AST introspection; Verible explicitly targets parsing un-preprocessed source for developer tools; and tree-sitter-systemverilog is useful as a fast, maintained fallback grammar, whereas tree-sitter-verilog still has open parsing issues in areas such as generate constructs and multi-dimensional arrays. citeturn32view1turn32view0turn31view3turn32view2turn32view4turn32view5

The formal-stack recommendation is also grounded in current public tooling. SymbiYosys is the Yosys-based front-end for bounded and unbounded safety proofs, cover, and liveness tasks; EQY is a Yosys-based front-end for equivalence checking; and Cadence Jasper FPV is still the commercial reference class for block-level property validation and industrial signoff-style formal usage. OpenTitan’s public formal flow is useful here because it demonstrates practical orchestration of Jasper and VC Formal through batch configuration, and reports proven, vacuous, covered, failing, and crash states per block. citeturn24search16turn29view2turn25search1turn28search0

## RTL parsing and hierarchy extraction

The most important implementation choice in SpecLoop is to treat **textual parsing** and **elaborated hierarchy reconstruction** as different problems. For real codebases, you cannot infer module dependencies, generate expansions, parameter overrides, and active package imports reliably from raw regex or single-file parses. The authoritative hierarchy should come from an elaborating frontend, and the source-preserving AST should come from a second frontend that is better for introspection and diagnostics. Surelog is the right primary elaborator because it explicitly supports preprocessor, parser, elaborator, libraries, configurations, separate-compilation semantics, parameter passing including `defparam`, and generate evaluation; its UHDM model contains only the active branch of an `if-generate`, which is exactly what SpecLoop needs for dependency closure. citeturn32view0turn32view1

The right parser architecture is therefore:

- **Primary elaboration path:** Surelog → UHDM → SpecLoop `ModuleIR`
- **Secondary semantic path:** slang → AST/JSON/symbols/diagnostics
- **Editor/recovery path:** Verible or tree-sitter-systemverilog
- **Preflight compile path:** Verilator and optionally Icarus for quick diagnostics

That stack gives you the best combination of industrial coverage, semantic analysis, and graceful degradation. Slang is especially valuable because it is designed as a reusable library, scales to commercial codebases, and exposes Python bindings that are ideal for solo-developer prototyping. Verible is intentionally good at un-preprocessed parsing for linting and formatting workflows, which makes it a strong recovery tool when the full build context is broken. Tree-sitter-systemverilog is the only tree-sitter grammar I would use in SpecLoop, because it is actively maintained and tested against sv-tests and real RTL codebases; the older tree-sitter-verilog project still shows open parse bugs for generate and multidimensional-array cases. citeturn31view3turn32view2turn32view4turn32view5

The exact ingestion algorithm should be:

```text
Discover build metadata
  → resolve top(s), source files, +incdir+, +define+, libraries, packages, tool options
  → canonicalize file order and compilation-unit semantics

Create a compilation context
  → fingerprint defines/includes/timescale/default_nettype/begin_keywords
  → store this as a PreambleCapsule object

Run Surelog elaboration
  → emit UHDM
  → extract elaborated instances, parameter bindings, package imports, active generates

Run slang on the same normalized input
  → collect AST spans, symbol tables, type info, warnings, source-preserving JSON

Fuse into ModuleIR
  → ports, params, clocks/resets, state regs, always blocks, FSMs, children, source spans

If elaboration fails
  → run Verible / tree-sitter-systemverilog recovery
  → emit PartialModuleIR + failure classification
```

The **PreambleCapsule** is not optional. The brief is correct that macros, `timescale`, defines, and related compile context must be preserved or downstream formal tooling will break. In practice, preserve at least these items per compilation unit and per extracted wrapper: `include` closure, `define` set, include directories, `timescale`, `default_nettype`, `celldefine`, `begin_keywords/end_keywords`, package imports, and tool-specific compatibility flags. A module should never be extracted or wrapped as a naked file fragment; always materialize it as **preamble capsule + package/import context + dependency closure + bind/assertion files**. fileciteturn0file0L9-L15 citeturn32view0turn34view0turn35view0

For **build metadata resolution**, SpecLoop should support four sources in priority order: explicit user filelists, FuseSoC cores, Bender manifests, and directory discovery. FuseSoC is a package manager and build system for HDL cores using CAPI2 core files; Bender is a dependency manager for hardware projects and can emit source lists; and lightweight Rust filelist parsers already exist for standard `.f`/`.flist` style manifests with include-dir and macro extraction. This lets SpecLoop interoperate with real projects instead of forcing a new manifest format. citeturn26search10turn26search4turn26search0turn26search12turn26search2

For **generate blocks and parameterized instances**, do not attempt text heuristics. Read the elaborated instance graph from UHDM, and record, for each instance edge: child module name, concrete parameter bindings, generate scope ancestry, and active conditional branch values. Surelog’s elaborator already supports generate evaluation and all flavors of parameter passing, including `defparam`, and expands the hierarchy tree accordingly. The child-closure algorithm can then recurse over the elaborated graph instead of guessing from raw source. citeturn32view0turn32view1

For **packages and compilation units**, you need to separate three cases. First, packages that are fully resolvable and imported before use can be carried normally. Second, packages with ordering dependencies should stay in their original compilation-unit order. Third, designs that depend on libraries/configurations or separate compilation-unit semantics should go through Surelog, because Yosys’ native frontend does not support `config` and library map files, while Surelog explicitly supports libraries and configurations. That difference matters in the legacy industrial codebases called out in the brief. citeturn35view0turn32view0

For **interfaces and modports**, use the richer parser frontends for extraction and treat backend support explicitly as a compatibility concern. Yosys’ native frontend only partially supports interfaces and requires named interface arguments; Verilator supports interfaces and modports, but not all patterns around generated modports or virtual interfaces; and yosys-slang provides a much better SystemVerilog frontend to Yosys than native `read_verilog -sv` for synthesizable subsets. In SpecLoop, that means interfaces should be represented in `ModuleIR` as first-class protocol bundles regardless of the proof backend, then lowered differently per backend. citeturn31view0turn31view1turn34view0turn31view2

For **encrypted blocks**, the rule should be simple: detect `pragma protect` or vendor-specific protection markers early, mark the module as opaque, and switch to a **black-box contract mode**. Verilator’s public docs are blunt here: open-source simulators cannot use encrypted RTL under IEEE P1735. That means SpecLoop cannot “solve” encrypted IP with clever parsing. The system should require one of three user-supplied options: a stub module with the same ports, a white-box behavioral model, or a preapproved commercial-tool flow that can decrypt inside the customer environment. The report should explicitly mark all assertions depending on opaque modules as lower confidence unless the user also provides functional contracts. citeturn34view0turn35view0

For **vendor primitives and library cells**, build and ship a `vendor_primitive_models/` library. In the Yosys flow, empty stub modules can be marked `(* blackbox *)`, behavioral library models can use `(* whitebox *)`, and if you know the exact port semantics but not the implementation, Yosys can even attach an SMT-LIB contract to a blackbox module. This is the correct place to encode common flops, clock gates, IO pads, SRAM wrappers, and synchronizer cells. For Jasper, the equivalent is to compile the same stubs or library models as part of the formal filelist. citeturn35view0

For **messy and partial codebases**, use parser ensembles plus recovery modes. Verilator has a strong SystemVerilog preprocessor and broad language support, which makes it a good preflight checker. Icarus’ `-i` option can ignore missing modules and no-top situations, which is useful when you want a diagnostic pass on incomplete designs rather than a hard failure. Slang’s compatibility options and high-quality diagnostics help a lot with old code styles. Run all of them as diagnostics before declaring the module unrecoverable. citeturn34view0turn27search10turn31view3

A final point: do not let the parsing layer emit only “modules.” It should emit a richer `ModuleIR` object with this minimum shape:

```json
{
  "module_name": "uart_rx",
  "source_files": ["rtl/uart_rx.sv"],
  "source_spans": [{"file":"rtl/uart_rx.sv","line_start":12,"line_end":233}],
  "ports": [{"name":"clk_i","dir":"input","width":1,"role":"clock"}],
  "parameters": [{"name":"CLKDIV","value":"16"}],
  "package_imports": ["uart_pkg::*"],
  "instances": [{"name":"u_fifo","module":"sync_fifo","params":{"DEPTH":"8"}}],
  "generate_contexts": [{"path":"gen_parity","active":true}],
  "always_blocks": [{"kind":"always_ff","signals":["state_q","bit_cnt_q"]}],
  "fsm_candidates": [{"state_reg":"state_q","encoding":"enum"}],
  "signal_roles": {"rst_ni":"reset","valid_o":"status","data_i":"payload"},
  "dependency_closure": ["sync_fifo","uart_pkg"],
  "parse_status": "ok"
}
```

That object becomes the single source of truth for later prompting, proof generation, search indexing, and failure reporting.

## Assertion generation strategy

SpecLoop should not ask one model to “read RTL and write final SVA” in a single shot. The public research trend in hardware and assertion generation is clear: better results come from **structured intermediate reasoning**, iterative refinement, and tool feedback. Papers such as Spec2Assertion, AssertLLM, SANGAM, AutoSVA, and CoverAssert all move away from naive one-pass generation and toward structured descriptions, chain-style reasoning, iterative repair, or coverage-guided loops. citeturn7search1turn7search9turn8search10turn8search22turn22academia20

The right generation pipeline is a **three-stage prompt stack**:

1. **Behavior extraction**
   - Input: `ModuleIR`, code slices, clocks/resets, port roles, child summaries
   - Output: normalized behavior objects

2. **Property synthesis**
   - Input: behavior objects + backend profile + assertion pattern library
   - Output: assertion candidates with classification as `assert`, `assume`, or `cover`

3. **Property hardening**
   - Input: candidates + compile diagnostics + backend compatibility checker
   - Output: final bind file or wrapper package

The model should always emit structured JSON before it emits code. That makes downstream validation much safer.

A useful behavior-extraction schema is:

```json
{
  "module_summary": "UART receiver with oversampling and optional parity check",
  "clocks": ["clk_i"],
  "resets": [{"signal":"rst_ni","active_low":true}],
  "behaviors": [
    {
      "id": "b_rx_idle_reset",
      "kind": "reset",
      "text": "After reset, the receiver returns to IDLE and valid_o is low.",
      "signals": ["state_q", "valid_o", "rst_ni"]
    },
    {
      "id": "b_start_to_valid_latency",
      "kind": "bounded_latency",
      "text": "A valid frame eventually produces valid_o within N bit intervals unless parity/framing error occurs.",
      "signals": ["rx_i", "valid_o", "parity_err_o", "frame_err_o"]
    }
  ]
}
```

Then, in the synthesis stage, require each behavior to become one or more candidates in this form:

```json
{
  "assertion_id": "a_start_to_valid_latency",
  "behavior_id": "b_start_to_valid_latency",
  "property_type": "latency",
  "classification": "assert",
  "clock": "clk_i",
  "disable_condition": "!rst_ni",
  "assumptions_needed": ["sample clock stable", "no X on rx_i during frame"],
  "backend_profile": "open_source_yosys",
  "sva_or_logic": "..."
}
```

The most important design choice here is **backend-specific assertion profiles**. Yosys’ open-source frontend supports only a limited subset of SystemVerilog assertions and formal constructs, mostly basic `assert property(<expression>)` forms plus `assume`, `restrict`, `cover`, and helper functions like `$past`, `$stable`, `$rose`, and `$fell`. It is not the place to freely generate rich SERE-heavy industrial SVA. Jasper, by contrast, is the backend where you should allow richer native concurrent SVA. So SpecLoop should expose two generation profiles:

- `open_source_yosys`
- `jasper_full_sva`

In the first profile, lower multi-cycle sequences into helper logic, counters, and clocked one-cycle assertions. In the second profile, emit idiomatic concurrent SVA directly. citeturn31view0turn31view1turn24search13turn25search1

The prompt should therefore explicitly carry the backend profile. A concrete prompt skeleton is:

```text
SYSTEM
You are a senior formal verification engineer.
Generate only JSON conforming to the provided schema.
Use only signals, params, and child summaries in the context.
Do not invent ports or clocks.
If a property needs environment assumptions, emit them explicitly.

USER
Backend profile: open_source_yosys
Goal: create high-value, non-trivial formal properties for this RTL module.

Context sections:
1. Module signature and roles
2. Clocks and resets
3. Parameter values and legal ranges
4. Active generate branches
5. Child-instance summaries
6. Always-block summaries
7. FSM candidates and state encodings
8. Retrieved assertion patterns from similar verified modules
9. Unsupported constructs for this backend

Tasks:
- Extract the important observable behaviors.
- Prefer interface contracts, state invariants, bounded latency, legality, safety, ordering.
- Reject trivial assertions such as signal == signal or permanently disabled antecedents.
- Classify each property as assert / assume / cover.
- Emit code in a bind-style wrapper using only this backend profile.
```

The **context provided to the model** should be much narrower than “all dependency-closed RTL.” Give it the module-under-test body, the PreambleCapsule, a machine-generated summary of child instances, the inferred clocks/resets, signal-role labels, likely FSM/state info, and a retrieved library of assertion patterns from similar already-verified modules. This is exactly the kind of structured intermediate representation that the recent assertion-generation literature keeps rediscovering: convert raw code or vague specs into normalized semantic descriptions before final property generation. citeturn7search9turn7search1turn22academia20

The **assertion pattern targets** should vary by module type. The table below is what I would hard-code into SpecLoop as the first pattern library.

| Module type | Primary patterns | Typical outputs |
|---|---|---|
| Combinational datapath | decode exclusivity, output legality, deterministic mapping, onehot selects | `assert`, `cover` |
| Sequential register block / queue | reset behavior, update relations, occupancy invariants, overflow/underflow safety, bounded response | `assert`, `assume`, `cover` |
| FSM | legal states, legal transitions, no dead state, eventual exit, sparse / onehot integrity | `assert`, `cover` |
| Protocol / bus / handshake wrapper | ready/valid, req/ack, no drop, no duplication, ordering, bounded latency, fairness assumptions | `assert`, `assume`, `cover` |
| Interface / adapter | width / mode consistency, protocol conversion invariants, no illegal simultaneous enables | `assert`, `assume` |

That pattern selection aligns well with both industrial assertion practice and the public work in AutoSVA and coverage-driven assertion generation. citeturn8search22turn22academia20turn24search12

Before any candidate reaches a formal backend, run four local checks:

1. **Name grounding:** every referenced signal, parameter, and instance must exist in `ModuleIR`.
2. **Backend compatibility:** reject unsupported operators or syntax for the current backend profile.
3. **Vacuity lint:** flag candidates whose antecedent appears constant false, permanently reset-disabled, or unreachable.
4. **Duplicate normalization:** hash normalized ASTs of assertions so semantically identical variants do not waste solver time.

That last step matters because vacuous or duplicate properties are one of the fastest ways to create a false sense of coverage. Public formal-debug guidance is very clear that antecedents can easily be over-constrained into never happening, which produces misleading “proofs.” citeturn24search15turn24search3

Confidence scoring for an assertion should not be just “proved or failed.” Use a composite score:

```text
confidence =
  0.35 * proof_status_score +
  0.20 * non_vacuity_score +
  0.20 * coverage_score +
  0.15 * context_integrity_score +
  0.10 * backend_agreement_score
```

Where:
- `proof_status_score` is highest for `proven`, lower for `bounded-only proven`, lowest for `unknown/error`
- `non_vacuity_score` penalizes vacuous or unreachable assertions
- `coverage_score` comes from behavior coverage and mutation score
- `context_integrity_score` penalizes missing children, encrypted blocks, or partial parses
- `backend_agreement_score` increases when multiple backends agree on the result

That is not a standard formula from literature; it is the engineering score I would use in the product.

## Formal integration and feedback loop

The formal subsystem should be exposed through a single adapter interface:

```python
class FormalBackend(Protocol):
    def prepare(self, bundle: CompilationBundle, assertion_set: AssertionSet) -> PreparedJob: ...
    def run(self, job: PreparedJob) -> FormalRunHandle: ...
    def collect(self, handle: FormalRunHandle) -> FormalResult: ...
```

That separation is important because the preparation step is where most backend-specific fragility lives: wrapper synthesis, filelist expansion, library models, black-box insertion, and backend-profile lowering.

SpecLoop should support three backend classes from the beginning:

- **Open-source proof backend:** Yosys + SymbiYosys
- **Open-source equivalence backend:** EQY
- **Commercial proof backend:** Jasper FPV

The open-source proof path is the fastest route to an MVP because SymbiYosys is already the standard front-end for Yosys-based formal verification, and it supports bounded and unbounded safety proofs, cover, and liveness tasks. The `.sby` format gives you standard sections for engines, scripts, and files; SBY supports engines such as `smtbmc`, `abc pdr`, `aiger`, and `btor`, and supports solvers including z3, bitwuzla, boolector, yices, cvc4, and cvc5. citeturn24search16turn29view0

A good default `.sby` template for SpecLoop is:

```text
[options]
mode prove
depth 25
timeout 300

[engines]
abc pdr
smtbmc bitwuzla

[script]
plugin -i slang
read_slang rtl_bundle.sv --top uart_rx_fpv
read_verilog -formal assert_bundle_open.sv
prep -top uart_rx_fpv

[files]
rtl_bundle.sv
assert_bundle_open.sv
```

Use `abc pdr` as the first proving engine for control-heavy safety properties, then `smtbmc bitwuzla` for a second engine with a different solving style. For cover-oriented discovery runs, instantiate a separate cover-mode task with a shorter timeout. The SBY reference and quickstart also make it easy to collect traces: failing runs produce engine directories with VCD traces, and the reference documents VCD/FST-generation options. citeturn29view0turn24search8turn24search0

EQY should **not** be the primary validator for “spec correctness,” because the brief correctly rejects reconstruction-based verification as the core architecture. Instead, use EQY for two narrow but valuable purposes:

- validating that SpecLoop’s **reduced or wrapped proof harness** is equivalent to the original module at the observable boundary
- validating future automated RTL refactors, simplifications, or stubbed reductions

That is where EQY shines. Its documentation exposes a rich partitioning flow, per-strategy statuses, and output artifacts such as matched IDs, partitions, and strategy logs. The `sby` strategy inside EQY is especially useful because it delegates partition proofs to SymbiYosys, with configurable engines, timeouts, and x-prop handling. fileciteturn0file0L17-L18 citeturn29view2turn29view1turn30search2

A representative `.eqy` file for validating a reduced wrapper would look like:

```text
[options]
splitnets on

[gold]
read_slang original_bundle.sv --top uart_rx
prep -top uart_rx

[gate]
read_slang reduced_bundle.sv --top uart_rx
prep -top uart_rx

[strategy quick]
use sat
depth 8

[strategy deep]
use sby
engine abc pdr
timeout 300
apply *
```

Use `sat` first for small partitions, and the `sby` strategy for everything else. The EQY docs explicitly note that the built-in `sat` strategy is fast on small, simple partitions but does not handle memories well, while the `sby` strategy is the more versatile default. citeturn30search2turn29view1

For **Jasper integration**, the public documentation is less syntax-specific than the open-source Yosys stack, so the cleanest architecture is to follow the same pattern OpenTitan uses: emit a batch config plus filelists and let the tool-specific script consume them. OpenTitan’s public formal flow shows JasperGold and VC Formal jobs being driven from HJSON-based batch configs, with results reported as proven, disproven, unreachable, covered, vacuous, or crash states. Jasper’s FPV product page confirms the intended use case: exhaustive property validation at block level. citeturn28search0turn25search1

A Jasper job packet should therefore contain:

```json
{
  "name": "uart_rx_fpv",
  "dut": "uart_rx",
  "tool": "jasper",
  "filelist": "out/filelist.f",
  "defines": ["FORMAL", "SPECLOOP"],
  "include_dirs": ["rtl/include", "formal/include"],
  "clock_map": [{"name":"clk_i","kind":"posedge"}],
  "reset_map": [{"name":"rst_ni","active_low":true}],
  "assertion_files": ["out/assert_bundle_jasper.sv"],
  "library_models": ["formal/vendor_prims.sv"],
  "timeout_s": 600
}
```

Under the hood, the Jasper adapter should:

1. render a tool-native filelist and macro list,
2. analyze RTL plus assertion/bind files,
3. elaborate the DUT wrapper,
4. declare or infer clocks and resets,
5. run proof on all assertions,
6. export a machine-readable summary report,
7. export counterexample metadata or waveform references for failures.

Where the installed Jasper version allows it, collect waveform or counterexample database references; where it does not, parse textual reports and the property-result table instead. Public Jasper references and case studies both confirm the normal formal outcomes: proved, failed with counterexample, and debug via a visualize or waveform environment. citeturn25search8turn25search6turn25search5

The **feedback loop** should be explicit and deterministic, not open-ended agent behavior. A single iteration should look like this:

```text
generate behaviors
  → generate assertions
  → compile/lint assertions
  → prove assertions
  → summarize failures or vacuity
  → repair only failed/weak assertions
  → re-run proof
```

The counterexample packet passed back to the LLM should be compact and highly structured:

```json
{
  "assertion_id": "a_req_ack_bounded",
  "backend": "sby",
  "status": "fail",
  "first_fail_cycle": 7,
  "antecedent_true_cycles": [4],
  "consequent_expected_window": [5, 6, 7],
  "relevant_signals": {
    "req_i": ["0","0","1","1","1","0"],
    "ack_o": ["0","0","0","0","0","0"],
    "state_q": ["IDLE","IDLE","BUSY","BUSY","STALL","STALL"]
  },
  "cex_summary": "Request observed at cycle 4, but acknowledgment never asserted within 3 cycles.",
  "repair_constraints": [
    "Do not invent new signals",
    "Keep backend profile open_source_yosys",
    "Preserve reset disable iff"
  ]
}
```

That packet should be generated by machine code, not by the LLM. The LLM’s job is to repair properties, not to interpret raw VCDs from scratch. Use cone-of-influence ranking to choose the signals in the packet: property signals, changed state bits near the fail point, and the immediate control dependencies. For bigger traces, add a “first divergence window” only, not the full waveform.

Termination conditions should be conservative:

- stop when all non-waived assertions are `proven`
- stop when the remaining failures are all classified as environmental or dependency-related
- stop when the same normalized failure signature repeats twice
- stop when coverage or confidence no longer improves
- hard-cap the loop at **five** repair rounds per module

That cap is my engineering recommendation, not a figure endorsed by a single paper. The basis is that iterative repair frameworks in RTL and assertion generation show real gains from successive tool-feedback loops, but runtime compounds quickly with each additional cycle. For an MVP, a small fixed cap is the right tradeoff. citeturn27search14turn22academia20turn7search3

## On-prem LLM deployment

The brief is also right about the deployment constraint: customer RTL cannot leave the customer environment. Model selection, tuning, and serving therefore have to be designed for fully local use, with any frontier-model distillation or weak-label generation happening **only on public datasets outside customer data paths**, if you use it at all. fileciteturn0file0L19-L20

The strongest open-source strategy is **not** to search for one magical model that does everything. Use a **task-specialized local model stack**:

- **RTL generation and reasoning model:** a CodeV-R1- or VeriRL-class model
- **RTL summarization / understanding teacher or reranker:** DeepRTL-class model
- **Assertion-repair specialist:** AssertSolver-class model
- **Embedding model:** DeepRTL2-style domain encoder, or a practical fallback stack using BGE-M3 plus a code embedding model

That recommendation follows the recent published specialization trend in RTL LLMs. CodeV introduced an RL-enhanced Verilog generation recipe and reports strong results on VerilogEval and RTLLM benchmarks. CodeV-R1 pushes that further with synthetic data, round-trip validation, and reasoning-style training, reporting open-source results competitive with or better than some larger general models. VeriRL shows further gains from reinforcement learning on 7B-class code backbones. DeepRTL and DeepRTL2 focus more on RTL understanding, generation, and especially embedding-style tasks. AssertSolver is explicitly targeted at using assertion-failure signals to debug RTL design models. citeturn3search5turn20search5turn20search0turn17search5turn8search3

For **practical model selection**, I would do this:

- **Generation model for the MVP:** start with the best reproducible open Verilog-specialized model or recipe you can run locally. If CodeV-R1 weights are available and acceptable for your deployment, use them. If not, reproduce the recipe on a strong open code backbone using their public data and tuning approach. citeturn20search5turn20search0
- **Repair model:** use an assertion-focused specialist or a smaller fine-tuned copy of the generation model trained on counterexample-to-repair pairs. AssertSolver is the best public conceptual starting point. citeturn8search3
- **Summarizer/model critic:** use a DeepRTL-style teacher to produce high-quality module summaries and to rerank candidate behaviors. DeepRTL is explicitly built around RTL understanding and multi-level descriptions. citeturn20search6turn17search6

The data pipeline for fine-tuning should combine **public instruction-style RTL data**, **raw open RTL corpora**, and **tool-generated supervision**. At minimum, I would use:

- **VeriThoughts** for reasoning-augmented RTL examples and formal-equivalence-oriented supervision
- **OpenRTLSet** for large-scale open RTL corpora
- **MG-Verilog** and **VerilogDB** for multi-granularity and high-quality synthesizable modules
- **OpenCores / FreeCores / HDLBits-derived corpora** for breadth
- **DeepCircuitX** or other repo-level corpora for dependency-closure and repository-context training

That directly addresses the brief’s quality-gap concern with public-data-only domain adaptation. fileciteturn0file0L19-L27 citeturn18search8turn18search5turn18search0turn19search1turn19search0turn18search3

The right fine-tuning curriculum is:

1. **Stage one: domain SFT**
   - module summarization
   - behavior extraction
   - SVA generation
   - assertion classification (`assert/assume/cover`)
   - counterexample summarization

2. **Stage two: tool-grounded preference or reward tuning**
   - reward passing compile/proof outcomes
   - penalize undefined signals, unsupported syntax, vacuity, and trivial properties

3. **Stage three: retrieval-conditioned tuning**
   - train with similar-module and assertion-pattern retrieval in the prompt

This is the same broad pattern seen in recent hardware-LLM work: many failures come from missing domain knowledge and poor problem framing rather than purely raw reasoning weakness, so retrieval and domain-specific fine-tuning close a lot of the practical gap. citeturn27search1turn27search9turn27search14

For **serving infrastructure**, use **vLLM as the default**. It is the safest production choice for a local generation service because it supports high-throughput serving with tensor parallelism and is widely used in the ecosystem. Use **SGLang** where you most need fast structured generation, grammars, or complex decoding flows. Use **TensorRT-LLM** as an optional optimization path on NVIDIA-only deployments where throughput matters more than portability. Keep **llama.cpp** around for development laptops, debugging, and small quantized side models. citeturn6search4turn6search5turn6search2turn6search9

A practical deployment profile looks like this:

- **Developer mode:** Docker Compose, one quantized generation model, one embedding model, local Qdrant, local file-backed artifact store
- **Team mode:** shared GPU host, vLLM API for generation, Qdrant on persistent volume, Postgres metadata store
- **Customer production mode:** air-gapped or internal-cluster deployment with mounted EDA tools and customer licenses, model weights mirrored internally, no outbound network

To close the gap with frontier models without violating the on-prem rule, use five levers together:

- public-data-only domain SFT
- tool-grounded repair loops
- structured prompts and backend profiles
- retrieval of similar verified modules and assertion patterns
- task specialization instead of one giant model

That combination is much more realistic than hoping a single generic open model will suddenly match frontier proprietary models on RTL reasoning. fileciteturn0file0L20-L27 citeturn20search5turn20search0turn27search1

## Semantic search layer

The second layer of SpecLoop only becomes trustworthy if it indexes **verified knowledge**, not raw guesses. So the search corpus should be built from:

- verified natural-language module summaries
- verified assertion texts and labels
- protocol and interface tags
- structural metadata from `ModuleIR`
- child-instance summaries and dependency graph features
- proof metadata such as `proven`, `covered`, `vacuous`, `unknown`

That is what turns the search index into a behavior index instead of a source-code grep. fileciteturn0file0L5-L6 fileciteturn0file0L27-L31

The vector database should be **Qdrant**. Public Qdrant documentation makes it a good fit because it supports HNSW indexing, payload filtering, and storage/quantization features that are useful in production retrieval systems. More importantly for SpecLoop, the hybrid dense-plus-filtered retrieval style fits the problem much better than plain dense search alone. citeturn4search7turn5search15

The embedding layer should be **hybrid by design**:

- **Text-semantic channel:** `bge-m3`
- **Code/RTL channel:** `nomic-embed-code` or your own domain-tuned encoder
- **RTL-specialized teacher or target architecture:** DeepRTL2-style encoder
- **Reranker:** cross-encoder or LLM critic over the top 20 results

Why hybrid? Because the query “find a module that does X” is not the same problem as “find code text similar to this code chunk.” BGE-M3 is useful because it supports dense retrieval, lexical matching, and multi-vector style retrieval in one model family. Nomic’s code embedding model is useful because it is much more code-sensitive than a generic sentence embedder. DeepRTL2 is especially relevant because it is one of the few published efforts aimed directly at RTL embedding and retrieval tasks rather than just code generation. citeturn5search15turn5search1turn17search5

To capture **behavioral similarity, not just syntax**, each search document should be multi-field:

```json
{
  "module_name": "sync_fifo",
  "verified_summary": "...",
  "behavior_tags": ["fifo", "bounded_queue", "ready_valid", "overflow_protected"],
  "protocol_tags": ["ready_valid"],
  "assertion_snippets": ["no_underflow", "occupancy_bounds", "push_pop_ordering"],
  "interface_signature": ["clk", "rst_n", "w_valid", "w_ready", "r_valid", "r_ready"],
  "graph_features": {
    "has_fsm": false,
    "state_regs": 1,
    "child_modules": ["ram_1r1w"]
  },
  "proof_scores": {
    "confidence": 0.91,
    "non_vacuous_ratio": 0.88
  }
}
```

At query time, run a **two-stage retrieval**:

1. **Retrieve**
   - dense text search over `verified_summary`
   - lexical search over tags, assertion names, interface names
   - payload filtering on protocol, widths, clocking, proof status

2. **Rerank**
   - compare the query to the top-k module summaries plus assertion sets
   - boost modules whose verified assertions mention the requested behavior
   - down-rank low-confidence or highly vacuous modules

This is where general code-representation research is helpful. GraphCodeBERT explicitly adds data-flow information to improve code representations; UniXcoder improves cross-modal and code-retrieval behavior. In SpecLoop, the analogous move is to use **verified behaviors and structural metadata** as first-class retrieval features, not just source tokens. citeturn4search0turn4search1turn17search5

For the query **“find a module that does X”**, the response pipeline should be:

```text
NL query
  → behavior extraction from query
  → dense + sparse retrieval
  → protocol/width/reset filters
  → rerank on verified summaries + assertions
  → return modules + evidence
```

The evidence in the final answer should cite the verified module summary and the top supporting assertions, not just the module name. That is how you make the search trustworthy for hardware engineers.

For the query **“can existing modules be combined to do Y”**, do not try to have the LLM hallucinate an architecture graph from scratch. Instead:

1. decompose Y into sub-functions,
2. retrieve candidate modules per sub-function,
3. build a compatibility graph using interface and protocol metadata,
4. run beam search or A* over compositions,
5. ask the LLM only to explain the best compositions found.

The compatibility graph should use typed edges such as:

- protocol-compatible
- width-adaptable
- clock-domain-compatible
- reset-compatible
- control/data-path role-compatible

For the MVP, keep composition conservative: return “likely composition candidates” plus required adapters; do not auto-generate glue logic yet.

## Failure classification and hard problems

The brief explicitly asks for per-module failure classification, including compile errors, mismatches, timeouts, dependency issues, truncation, and more. That should be implemented as a **two-layer classifier**: first, deterministic rules over parser/formal logs; second, a small model-based classifier only for unresolved or ambiguous cases. NVIDIA’s VerilogEval tooling already includes classification of common compile and runtime failures, and more recent RTL-LLM error-analysis work shows the value of separating syntax, domain-knowledge, ambiguity, and reasoning failures instead of lumping everything into “model bad.” fileciteturn0file0L13-L15 citeturn27search0turn27search2turn27search1

The output format should look like this:

```json
{
  "module": "uart_rx",
  "parse_status": "partial",
  "verification_status": "failed",
  "failure": {
    "class": "formal_mismatch",
    "subclass": "counterexample_found",
    "backend": "sby",
    "tool_phase": "prove",
    "message": "Request observed but ack missing within bound",
    "evidence": {
      "property_id": "a_req_ack_bounded",
      "first_fail_cycle": 7,
      "trace_ref": "artifacts/uart_rx/cex/trace.vcd",
      "diagnostic_refs": ["artifacts/uart_rx/logs/sby.log"]
    },
    "repairable": true
  },
  "confidence": 0.63
}
```

Use this top-level taxonomy:

- `compile_error`
  - `syntax_error`
  - `unsupported_construct`
  - `undefined_symbol`
  - `package_or_include_missing`
  - `backend_profile_violation`

- `dependency_issue`
  - `missing_module`
  - `missing_encrypted_stub`
  - `library_resolution_failure`
  - `macro_context_mismatch`

- `formal_mismatch`
  - `counterexample_found`
  - `vacuous_proof`
  - `overconstraint_suspected`
  - `assumption_conflict`

- `timeout`
  - `solver_timeout`
  - `partition_timeout`
  - `jasper_license_wait`
  - `resource_exhaustion`

- `truncation`
  - `incomplete_llm_output`
  - `unterminated_module`
  - `malformed_json`

- `reconstruction_failure`
  - `eqy_non_equivalent_wrapper`
  - `legacy_rtl_rebuild_failure`

That last class should exist only for wrapper/refactor validation and legacy experiments, **not** as the primary product path, because the brief’s architectural pivot away from reconstruction is correct. fileciteturn0file0L17-L18

The six hard problems in the brief deserve separate treatment.

**Open-source model quality gap.** This is real. The research and benchmark trend shows that generic open code models underperform strong proprietary models on RTL generation and reasoning, but that gap narrows sharply when the open models are trained on domain-specific data and corrected with tool feedback. CodeV, CodeV-R1, VeriRL, and the broader “understanding and mitigating RTL errors” line of work all converge on the same answer: curated domain data, tool-grounded self-correction, and retrieval matter more than naïvely scaling an untuned model. SpecLoop should therefore invest first in public-data domain SFT and repair loops, not in trying to host the biggest generic code model it can afford. fileciteturn0file0L20-L23 citeturn20search5turn20search0turn27search1

**Assertion quality and coverage.** “All assertions proved” does not mean “important behaviors covered.” The solution is a composite coverage metric: behavior-to-assertion mapping, non-vacuity checks, mutation score, and structural span. Use machine-extracted behavior points from the behavior-extraction stage as the denominator; require at least one non-vacuous assertion or coverpoint per major behavior; add mutation testing by inserting bounded behavioral mutants; and record the fraction of high-centrality control and state signals touched by non-vacuous properties. CoverAssert’s coverage-guided approach and standard vacuity guidance both support this direction. fileciteturn0file0L23-L24 citeturn22academia20turn24search15turn24search3

**Scalability to large modules.** Do not feed one giant module to the model as raw text. Instead, implement **structured hierarchical slicing**: split by always blocks, FSM regions, interface logic, and high-centrality cones of influence; summarize each slice; then recompose a module-level behavior set from slice summaries plus the instance graph. Repo-level hardware datasets such as DeepCircuitX and recent work on repository-context benchmarks are useful evidence that repository context and hierarchy matter. In SpecLoop, the concrete solution is a context packer that chooses the top-N slices by relevance for each behavior family. fileciteturn0file0L24-L25 citeturn18search3turn19search5

**Real industrial RTL messiness.** This is primarily a frontend-engineering problem, not an LLM problem. The way through it is parser pluralism plus explicit trust boundaries: Surelog for elaboration, slang for semantics, Verible/tree-sitter for recovery, vendor primitive libraries, encrypted-block blackboxing, and log-driven failure categories. The product should never pretend to have “understood” encrypted or partial blocks it could not actually elaborate. Report those as opaque dependencies, lower confidence, and keep going on the rest of the hierarchy. fileciteturn0file0L25-L26 citeturn32view1turn31view3turn32view2turn34view0

**Formal tool integration.** The solution is a normalized adapter API plus backend profiles. Yosys/SBY is the portable open-source proof engine. EQY is the normalization and transform-checking engine. Jasper is the industrial full-SVA engine. The common denominator should be a `FormalResult` schema, not a common source syntax forced across all tools. That is what allows backend-specific lowering while giving the rest of SpecLoop a stable interface. fileciteturn0file0L26-L27 citeturn24search16turn29view2turn25search1turn28search0

**Semantic search embedding.** The solution is not a single “best” generic code embedding. Use a hybrid text-plus-RTL retrieval stack and fine-tune an RTL-aware encoder over verified summaries, assertion text, module pairs, and contrastive behavioral labels. DeepRTL2 is the best direct signal here; GraphCodeBERT and UniXcoder provide useful architectural precedents for adding structure-aware signals to code retrieval. SpecLoop should eventually train its own encoder on public RTL pairs and use verified assertions as extra supervision. fileciteturn0file0L27-L31 citeturn17search5turn4search0turn4search1

Open questions remain. Public documentation for Jasper is not as operationally explicit as the open-source Yosys stack, so the exact emitted TCL should be validated against the customer’s installed version and license bundle. Also, some of the strongest RTL-specialized models are newly published enough that their weight-release and licensing status may vary; the recipes and reported results are solid, but the specific deployable artifact you choose will depend on what is actually available inside your environment. citeturn25search1turn20search5turn20search0

## Implementation roadmap

The brief says the MVP should run on a real open RTL codebase such as PicoRV32, generate verified assertions per module, emit a structured verification report with confidence, and demonstrate semantic search by behavioral description. The roadmap below is tuned exactly to that outcome. fileciteturn0file0L28-L31

| Week | What to build | Exit criteria | Demo at end of week |
|---|---|---|---|
| Week one | Repo skeleton, Pydantic contracts, Typer CLI, artifact directory layout, local SQLite metadata DB | `specloop init`, `specloop ingest`, and artifact directories work | CLI ingests a repo and creates a job record |
| Week two | Filelist and manifest resolution for explicit `.f`, FuseSoC, and Bender; PreambleCapsule model | Normalized source bundle emitted for PicoRV32 | Show resolved files, defines, includes, tops |
| Week three | Surelog adapter and UHDM extraction; parse diagnostics persisted | Elaboration succeeds on PicoRV32 or comparable design | Print hierarchy tree and instance graph |
| Week four | Slang adapter, AST fusion, `ModuleIR` emitter, clock/reset and port-role inference | ModuleIR generated for every reachable module | Per-module JSON report with ports, params, children, clocks, resets |
| Week five | Recovery path with Verible/tree-sitter-systemverilog and compile preflight with Verilator | Broken files yield `PartialModuleIR` instead of hard crash | Demo graceful failure reporting on an intentionally damaged repo |
| Week six | Context packer and module-type classifier; first behavior-extraction prompt; local vLLM serving | Behavior JSON generated for small modules | Show generated behavior objects for 3–5 PicoRV32 modules |
| Week seven | Assertion pattern library and backend profiles (`open_source_yosys`, `jasper_full_sva`) | Candidate assertions compile locally | Show generated bind/assert files before proof |
| Week eight | SymbiYosys adapter with `.sby` generation, open-source proof execution, result normalization | At least one non-trivial module has passing and failing assertion examples | Demo proof results plus VCD trace reference |
| Week nine | Counterexample summarizer and single-property repair loop | Failed properties can be repaired in one automated loop on at least one module | Show “fail → repair → prove” on a FIFO or FSM block |
| Week ten | Coverage and confidence scoring: non-vacuity, behavior mapping, duplicate removal, structural span | Report includes confidence and vacuity-aware status | Show module report ranking strong vs weak assertion suites |
| Week eleven | EQY adapter for reduced-wrapper equivalence checking; blackbox/whitebox vendor primitive library | Wrapper or reduced bundle can be checked against original | Demo EQY proving wrapper equivalence on a reduced block |
| Week twelve | Search document generation, Qdrant indexing, dense + sparse retrieval over verified summaries | Natural-language search returns correct modules on PicoRV32-class repo | Query: “find a module that counts outstanding transfers” |
| Week thirteen | Composition search prototype using interface/protocol compatibility graph | System returns plausible multi-module compositions with evidence | Query: “can existing modules be combined to build a buffered UART path?” |
| Week fourteen | Jasper adapter skeleton: job packet renderer, filelist export, report parser, versioned tool shim | Can submit a Jasper batch job in a licensed environment and parse summary output | Demo on local mock if no license; real run if license exists |
| Week fifteen | End-to-end regression harness over a second codebase such as Ibex sub-blocks or OpenTitan primitives | Two different repos run end-to-end with structured reports | Benchmark dashboard with parse/proof/search success rates |
| Week sixteen | Polish the MVP: HTML/Markdown reports, search API, packaged CLI, install docs, example configs | One-command demo works from clean checkout | Full demo: ingest → verify → report → search |

The milestone boundaries should look like this:

- **End of month one:** robust parsing and hierarchy extraction on a real codebase, including preamble preservation and dependency closure. fileciteturn0file0L9-L11
- **End of month two:** assertion generation and open-source formal proof loop working on small and medium modules. fileciteturn0file0L12-L15
- **End of month three:** confidence scoring, failure taxonomy, and semantic search over verified documentation. fileciteturn0file0L15-L15 fileciteturn0file0L27-L31
- **End of month four:** polished end-to-end MVP, Jasper adapter skeleton, and a demoable workflow on PicoRV32 plus one additional open RTL codebase. fileciteturn0file0L28-L31

If you follow this order, the first credible demo appears by **week eight** and the first product-shaped demo appears by **week twelve**. That is the right shape for a four-month solo build: get the parser and proof loop real first, then layer in search and commercial-tool integration after the core artifact pipeline is already trustworthy.