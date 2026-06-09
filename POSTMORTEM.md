# POSTMORTEM

SpecLoop was built between May and June 2026 as an on-premises tool for AI-assisted formal verification and composition of RTL. It was closed deliberately in June 2026, at a sound technical milestone, for strategic reasons. This is the short, honest account.

## What was built

- **A per-module spec engine:** RTL → typed IR (pyslang) → LLM-generated, categorized SVA → SymbiYosys proof → counterexample-guided repair loop → indexed, confidence-scored spec. Exercised on PicoRV32, lowRISC Ibex, and OpenTitan IP as well as the AXIS corpus.
- **A soundly-proven block library:** 13 single-clock AXI-Stream modules, 267 generated assertions re-proven under a gated harness — 231 genuine holds, 32 discarded with real counterexamples, every module passing three mandatory anti-vacuity gates.
- **A compositional proof layer:** an assume-guarantee proof of a four-stage pipeline over contract stubs (Chain A, 7/7 cross-boundary properties), and a closed-harness bounded proof that a COBS encode → register → decode chain round-trips every frame up to 8 bytes byte-exactly (Chain B).
- **A four-stage e2e pipeline:** natural-language request → catalog-constrained LLM planning → contract-derived retrieval → deterministic composition → AG proof, with honest failure at every stage and a synthesizable, proof-backed wrapper as output.

## What the soundness crisis taught us

Mid-project we discovered that the open-source Yosys front end used by sby silently ignores SystemVerilog `bind` — it parses the statement and never instantiates the spec. Every bind-attached proof to that point, the entire library and the composition results built on it, had been vacuous: `assert(1'b0)` passed. The full story is in [README.md](README.md#the-soundness-story-read-this-first); the lessons are worth stating on their own:

1. **A PASS is meaningless until you have made the same harness FAIL.** Anti-vacuity probes (`assert(1'b0)`, corrupted references, reachability covers) are not debugging aids; they are part of the definition of "proven." After the rebuild, they were mandatory gates, and the e2e pipeline prints its anti-vacuity verdict next to every proof.
2. **OSS formal toolchains can accept-and-ignore language constructs without a diagnostic.** Front-end behavior is part of your trusted computing base; pin it, and test the *toolchain's semantics* in your suite (this repo re-verifies the bind behavior on every test run).
3. **Suspiciously cheap results are results to attack first.** A 109-assertion composition proof in one second at confidence 1.0 was celebrated before it was questioned. It should have been questioned because it was celebratable.
4. **The crisis improved the product.** The rebuilt gated harness found 32 genuinely wrong LLM assertions that the vacuous era had blessed — the discard list is itself a contribution, a taxonomy of how plausible-looking generated specs are wrong.

## Why it was closed

Industry conversations in spring 2026 were consistent: RTL verification tooling — LLM-generated SVA in particular — is a crowded space, with multiple funded startups, EDA-vendor programs, and a fast-moving academic pipeline converging on it. The same conversations pointed at where the pain is less served: **physical design, and analog/mixed-signal**, where the data structures are harder, the tooling older, and the AI-assistance story far less developed.

SpecLoop was closed on that signal — after the soundness rebuild and the end-to-end milestone, not before. The choice was deliberate: ship a sound, reproducible, honestly-documented artifact and redirect, rather than ride into a crowded market on momentum. The composition thesis — proven blocks, contract-derived retrieval, proof-gated assembly — is documented in [DESIGN.md](DESIGN.md) and remains, in our view, the right architecture for whoever builds it at scale; the verification discipline it forced (gates, falsification-before-belief, toolchain-semantics tests) transfers directly to whatever comes next.
