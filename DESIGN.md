# DESIGN — the unimplemented vision

> **Status: design work, not shipped features.** Everything in this document is architecture that was designed, partially prototyped, or validated only in isolation. The shipped, reproducible system is described in [README.md § What is proven today](README.md#what-is-proven-today). Where a claim below *was* validated experimentally, the experiment is cited; where a reference came from project notes rather than verified reading, it is flagged.

SpecLoop's end state was meant to be: a natural-language request comes in; the system retrieves formally-proven blocks whose *contracts* satisfy the request's decomposed obligations; composition candidates are accepted by proof, not by similarity score; and among the formally-accepted candidates, the one closest to the user's power/performance/area target ships. This document records the design for the parts that were not built, and the evidence that motivated them.

## 1. Why vector similarity is the wrong primitive for hardware composition

The shipped system started where every retrieval system starts: embed a composite document per module (ports, behavioral summary, proven assertions) with BGE-large-en-v1.5, store in Qdrant, rank by cosine. It failed in two instructive ways.

**Failure 1 — protocol pollution (measured).** The query "AXI-Stream arbiter two inputs round robin" ranked `axis_pipeline_register` #1 and the actual `arbiter` #10. Root cause, isolated by probing: the library's AXI modules repeat tvalid/tready/tdata-stability phrasing dozens of times, so shared protocol vocabulary dominates the embedding on *both* sides of the cosine. Removing the protocol terms from the query ("arbiter round robin grant request") ranked `arbiter` #1 by a 0.13 margin. Because cosine is symmetric, no document-side representation fixes a polluted query vector — assertion-centric centroids and per-category vectors were tested and made it *worse* (each module's centroid is itself dominated by protocol-stability assertions). A query-side protocol-strip blend shipped as a stopgap; it treats the symptom.

**Failure 2 — structural fingerprints don't carry function (measured).** A 32-dimensional structural fingerprint (port counts, widths, FF counts, …) placed `axis_srl_register` at distance 0.268 from `axis_srl_fifo` — closer than to any other register; 11 of 13 nearest-neighbor pairs crossed functional categories. Structure describes *how big* a block is, not *what it does*.

**The structural argument.** Both failures are instances of one mismatch: composition needs an **exact, asymmetric** decision — *does this block satisfy this obligation?* — and similarity is graded, symmetric, and direction-blind. A FIFO and a skid register are extremely *near* in any reasonable embedding space and are **not** substitutable: one reorders timing behavior the other guarantees not to. Conversely, a block may satisfy an obligation while looking textually nothing like it. In software retrieval, a near-miss is a mostly-useful document; in hardware composition, a near-miss is a wrong netlist. There is no partial credit.

What shipped instead points at the answer: stage-2 retrieval derives each block's behavioral signature from its **proven contracts** (the surviving assertions in `work/<block>.bind.sv`) and matches roles to blocks by classification over that signature — no hand labels (scrambling stored labels changes nothing; a test enforces this), no embeddings in the trusted path. The contract signal cleanly separates exactly the pair the fingerprint confused: `axis_srl_register` proves data-stability with no occupancy/pointer contracts; `axis_srl_fifo` proves emptiness + occupancy + pointer increment/decrement with no data-stability contract. The design below generalizes this from a hand-built concept vocabulary to a principled mechanism.

## 2. Assertion-centric embedding with formal contract extraction

Embed what is *proven*, not what is written.

- **Contract extraction (the shipped seed, generalized).** `parse_proven_assertions` already extracts (label, guard chain, asserted expression) triples from proven specs, guard-aware and decode-wire-aware. The design adds normalization: rewrite signal references to bundle-role form (`s_axis_tdata` → `slave.data`, internal pointers → `state.*`) so contracts are comparable across modules, and tag each with its category (reset, handshake, data-integrity, occupancy, timing/latency).
- **Per-assertion vectors, set-valued modules.** Each normalized contract embeds individually. A module is not one vector but a *structured set* of contract vectors grouped by category. A role's requirement is likewise a small set of required-behavior vectors.
- **Set-to-set matching.** Score = per-requirement max-similarity against the candidate's contract set (direction: requirement → contract), aggregated across requirements. Protocol boilerplate stops dominating because nothing is pooled into a single centroid — a tvalid-stability contract can only answer a tvalid-stability requirement.
- **Role of the embedding: recall, never acceptance.** The embedding's only job is to shortlist candidates cheaply. Acceptance is §3's job. This division is the direct lesson of Failure 1: similarity may *propose*, only proof may *decide*.

## 3. Exact asymmetric contract satisfaction: SymbiYosys subsumption checking

The acceptance test that replaces similarity:

> Candidate block **B** fills role **R** iff B's proven contract set **entails** each of R's required contracts — checked by SymbiYosys, per (candidate, requirement) pair.

Mechanically this is the assume-guarantee stub pattern that Chain A's composition proof used by hand (`work/recheck_axis/ag/chainA_ag_closed.sv`), turned into a retrieval primitive:

1. Emit a stub module with B's interface whose outputs are free (`anyseq`) **assumed** to obey B's proven contracts — nothing else. No RTL internals, so the check's cost is independent of block size (the Chain A stub proofs ran in seconds).
2. **Assert** the role's required contract φ_R over the stub.
3. `sby` proves or refutes. Pass ⇒ B's guarantees subsume the obligation; B is accepted for this requirement.
4. Standard anti-vacuity gates apply (assumptions satisfiable; `assert(1'b0)` must fail) — non-negotiable after this project's history.

Properties of this check that similarity cannot have:

- **Asymmetric by construction.** B may guarantee strictly more than R needs — subsumption, not equivalence. A full-featured FIFO can fill a plain buffer role; a plain buffer cannot fill a FIFO-with-occupancy role.
- **Exact.** The verdict is a proof or a counterexample, not a score above a threshold.
- **Gap-closing as a by-product.** When entailment fails, the failing obligation *is* the missing contract, stated formally. The Chain A proof hit this twice ("gap → closed" in `AG_RESULT.md`): the needed property was then proven directly on the block's real RTL inline and added to its contract set. Retrieval failures grow the library instead of just failing.

The full retrieval pipeline becomes: assertion-centric ANN shortlist (§2, cheap, tuned for recall) → SBY subsumption check per shortlisted candidate (sound) → only formally-accepted candidates reach composition, which then proves cross-boundary properties over the same stubs.

> **Citation gap (author to fill):** the contract/refinement literature this should be situated in — assume-guarantee contract theory and refinement/subsumption checking — is not covered by the repo's notes. No citation is offered here rather than an invented one.

## 4. The dual vector space architecture: functional + PPA

(From `research/specloop_vector_idea.md`, where this is developed in full.)

Every library block lives in **two separate spaces**:

- **Functional space** — what the block does: the assertion-centric contract vectors of §2. Used only for candidate generation.
- **PPA space** — how it performs: normalized latency / throughput / area / power dimensions. The shipped system already lands *real* Yosys synthesis statistics (`synth -flatten` cell/FF counts via `ppa/synth.py`) into the index at ingest time, with a heuristic fallback when synthesis fails — so this space has ground truth, not just prediction.

They are kept separate because combining them causes interference: a fast and a slow implementation of the same function must be *near* functionally and *far* in PPA; one space cannot encode both relations. The target point in PPA space is application-dependent — an HFT pipeline wants `[latency→0, throughput→1, *, *]`, an IoT sensor wants `[*, *, area→0, power→0]` — so selection is: **among the formally-accepted candidates (§3), rank by distance to the user's PPA target; when nothing dominates, return the Pareto frontier.** PPA never gates correctness; it chooses among proven options. The same functionality composed two ways (naive wiring vs. registered interface) passes the same contracts with very different PPA — exactly the choice no single-answer LLM can surface.

Further out, the research note develops vector-arithmetic composition search (component vectors summing toward a request vector), residual vectors as *gap detection* ("your library is missing a block that does X"), and PPA-residual-guided **equivalence-preserving rewriting** (pipelining/retiming/resource-sharing rules à la ROVER/ASPEN, selected by the residual, feeding verified variants back into the library). All of it is gated on library scale — the note itself estimates 200+ blocks before the geometry is meaningful, and the proven library at close was 13. That honesty is part of the design.

## 5. The dual formal backend: OSS Yosys + Tabby CAD

The soundness incident (README § The soundness story) is the motivation: **front ends differ semantically, and they differ silently.** The open-source `read_verilog` front end parses SystemVerilog `bind` and ignores it; synlig (the open-source slang-based front end) honors `bind` correctly — verified by the `assert(1'b0)` probe — but hit internal errors on the composition RTL and was reverted (the two-pass architecture was documented for later in the revert commit). A single front end is a single point of silent failure.

The plan of record:

- **OSS leg (Yosys + sby, pinned build):** the reproducibility leg. Runs the gated per-module re-proofs, the AG composition proofs, and the anti-vacuity gates. Everything in this repo runs on this leg alone, with inline-only property attachment as a hard rule.
- **Commercial leg (Tabby CAD Yosys / Verific front end):** full SystemVerilog + SVA semantics, including `bind`, for industrial RTL that the OSS parsers reject — with a JasperGold adapter as the eventual customer-site backend (per the original planning docs).
- **Cross-check discipline, mechanized:** the same property set goes through both legs, and the pipeline *diffs the number of assertion cells in the elaborated netlists* before trusting either. "Count the checks in the netlist" graduates from a debugging trick to a mandatory pipeline stage — it is the check that would have caught the `bind` bug on day one.

## 6. Prior art the design drew on

Pulled from the repo's research notes (`research/specloop_vector_idea.md`) and planning documents. **These citations were carried in project notes and should be verified against the actual papers before external use.**

| Work | Relevance |
|---|---|
| ROVER (Intel + Imperial, TCAD 2024) | RTL optimization as e-graph rewriting with formally verified equivalence-preserving rules — basis for §4's rewriting loop |
| ASPEN (Cornell, MLCAD 2025) | LLM-proposed rewrite rules, formally verified before entering the pool, with real PPA feedback |
| FastPASE (ISQED 2024) | PPA prediction from RTL dataflow graphs, 16–155× faster than synthesis (~13% error) |
| MasterRTL (TCAD 2024) | Pre-synthesis module-level PPA estimation from operator graphs |
| DeepRTL (ICLR 2025) / DeepRTL2 (ACL 2025) | RTL understanding/embedding models; DeepRTL2 unifies generation + embedding and regresses PPA from embeddings |
| STELLAR (2026) | AST structural fingerprints retrieving (RTL, SVA) pairs to guide assertion generation — nearest neighbor to SpecLoop's RAG; structural, not contract-grounded |
| AssertLLM / AssertionForge / SANGAM | LLM assertion generation from human-written specs; none build a self-expanding proven library |
| AutoSVA (Princeton) | Automatic SVA testbench generation for module interactions (deadlock/livelock focus); not compositional |
| VERT dataset (2025) | Large-scale SVA fine-tuning data — the case for the training flywheel in the original plan |
| HW2VEC (2021) | Circuit-as-graph embeddings (Trojan detection) — early evidence circuits embed meaningfully |
| Embedding-model candidates from the planning docs | UniXcoder + BGE-large (one plan), BGE-M3 + nomic-embed-code + DeepRTL2 (the other) — superseded by §2's per-contract design |

Known gaps for the author to fill: assume-guarantee contract theory / refinement-checking citations for §3, and the Verific/Tabby CAD capability claims in §5.
