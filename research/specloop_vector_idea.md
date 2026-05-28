# SpecLoop Research Directions: Vector Space Applications

## Background: What Are We Talking About?

### What is a module?
A hardware module is a self-contained unit of digital logic with defined inputs and outputs. Think of it like a function in software — it takes signals in, does something with them, and produces signals out. A counter module takes a clock, reset, and enable signal and outputs a count value. A FIFO module takes write data and a write enable and stores values you can read back later. Modules are the building blocks of chips.

### What is an assertion?
An assertion is a formal mathematical statement about what a module must always do. It's not a test — it's a proof obligation. "Whenever reset is low, count must be zero" is an assertion. "If the FIFO is full, the write pointer must not change" is an assertion. When SymbiYosys proves an assertion, it means that property holds for every possible input, for all time — not just the inputs you tested.

### What is a vector?
A vector is a list of numbers that represents something in a high-dimensional space. The key insight is that things with similar meaning end up near each other in this space, and things with opposite meaning end up far apart or in opposite directions. The classic example: in a word embedding space, `king - man + woman ≈ queen`. The vector for "king" minus the vector for "man" plus the vector for "woman" points almost exactly to the vector for "queen." The geometry of the space encodes meaning.

### How do we represent a module as a vector?
SpecLoop already does this. Each module gets a composite document built from its port signature, behavioral summary, module type, and its complete formally-verified assertion suite. This document gets passed through BGE-large-en-v1.5 (a text embedding model) which produces a 1024-dimensional vector. That vector is stored in Qdrant. When you search "find me an AXI adapter," your query is embedded as a vector and Qdrant finds the nearest module vectors by cosine similarity.

The key property we want: **two modules that do similar things should have similar vectors.** A 32-bit counter and a 16-bit counter should be closer to each other than either is to a FIFO. An AXI adapter and a Wishbone adapter should be close because both are bus protocol converters.

### How do we represent an assertion as a vector?
An assertion describes a behavioral property — "the write pointer increments by one when write-enable is high and the FIFO is not full." This is a sentence about behavior. You embed it the same way you embed any text — pass it through BGE and get a vector. Assertions about similar behaviors (two different "valid stays high until ready" properties from different AXI modules) will be near each other. Assertions about orthogonal behaviors (a reset assertion and a data-integrity assertion) will be far apart.

The assertion vector space represents the space of all possible behaviors a hardware module could exhibit. Each proven assertion is a point in that space. The collection of a module's assertion vectors is a cloud of points describing everything we've formally proven about that module.

---

## The Dual Vector Space Architecture

This is the core architectural idea that separates SpecLoop from any existing tool. Every module in the library lives simultaneously in **two separate vector spaces**:

### Space 1: Functional Vector Space
What the module **does** — its behavioral semantics. Built from port signatures, behavioral descriptions, and formally verified assertions. Searched using cosine similarity. Used to find modules that satisfy a functional requirement.

### Space 2: PPA Vector Space
How the module **performs** — its power, performance, and area characteristics. Built from structural features extracted during IR parsing plus lightweight ML prediction. Dimensions are:
- **Latency**: critical path delay in normalized units (minimize → 0)
- **Throughput**: data rate in normalized units (maximize → 1)
- **Area**: gate equivalents in normalized units (minimize → 0)
- **Power**: estimated switching power in normalized units (minimize → 0)

The target point in PPA space is **application-dependent**, not fixed. For an HFT FPGA: `[0, 1, *, *]` — minimize latency, maximize throughput, area and power less critical. For a low-power IoT sensor: `[*, *, 0, 0]` — minimize area and power, throughput just needs to meet spec.

### Why Two Separate Spaces?
Combining behavioral and performance dimensions into one space causes interference — a fast module and a slow module that do the same thing might embed far apart even though they're functionally equivalent. Keeping the spaces separate means functional search is uncontaminated by performance characteristics, and performance optimization is uncontaminated by behavioral semantics.

### How PPA Vectors Are Built
When a module enters the library after formal verification, SpecLoop extracts structural features from its IR:
- Number of always blocks, flip-flops, combinational logic depth
- Port widths, parameter count, submodule count
- Module type (sequential/combinational/FSM/memory)

These features feed a lightweight predictor trained on modules in the library that have been synthesized. As the library grows, the predictor improves because it has more ground truth. No synthesis is required for every new module — prediction runs in milliseconds alongside the existing ingest pipeline.

### The Composition Search with Dual Spaces
When a user requests a design:
1. Embed the request as a functional vector F
2. Find all combinations of library modules whose functional vectors sum close to F (Application 1 below)
3. Among the functionally valid combinations, map each to a PPA vector by summing component PPA vectors plus interface overhead estimate
4. Find the combination whose PPA vector is closest to the user's performance target
5. That combination is the optimal assembly — not just functionally correct but performance-optimal for this specific application

**The key insight:** The same functionality can be implemented multiple ways with dramatically different PPA. A counter feeding a FIFO can be implemented with naive direct connection (functionally correct, hits timing at 100MHz) or with a registered interface and flow control (functionally correct, hits timing at 500MHz). Both pass the same assertions. The PPA vector space tells you which one to pick for your target.

This is something no LLM can do. An LLM generates one implementation. SpecLoop enumerates all proven combinations and selects the Pareto-optimal one for your performance requirements.

---

## Application 1: Compositional Search via Vector Arithmetic

### The current approach and its weakness
Right now `specloop compose "build me an AXI slave with a register file"` works by:
1. Calling the LLM to decompose the request into sub-functions
2. Searching for each sub-function independently
3. Hoping the LLM decomposed correctly and the searches found good matches

This is fragile. The LLM might decompose wrong. The searches might find irrelevant modules. The whole pipeline depends on language understanding at every step.

### The vector arithmetic approach
Embed the entire user request as a single vector R. This is the target point in behavioral space — where we want to end up.

For every pair of modules (A, B) in your library, compute `A + B` (vector addition, tip to tail). Find the pair whose sum is closest to R.

For every triple (A, B, C), compute `A + B + C`. Find the closest triple.

The combination of module vectors that sums closest to the request vector is the best composition — no LLM decomposition needed.

**Why this works:** If your library has an AXI adapter (vector A) and a register file (vector B), and their behavioral spaces together cover what "AXI slave with register file" means, then `A + B` will be geometrically close to R. The embedding space encodes the semantic relationship between behaviors, and addition in that space corresponds to behavioral union.

**Concrete example:**
```
R = embed("AXI slave with 32-bit register file, byte-strobe writes")
A = embed(picorv32_axi_adapter)  # vector from Qdrant
B = embed(picorv32_regs)         # vector from Qdrant

distance(A + B, R) = 0.12  # very close → good composition
distance(A + counter, R) = 0.71  # far → wrong combination
```

**Implementation path:**
- Retrieve all module vectors from Qdrant
- For compositions of size 2, 3, 4: enumerate combinations, compute vector sums, rank by distance to R
- Return top-k compositions with their component modules
- Use this as the candidate generation step instead of (or alongside) LLM decomposition

**Why the library size matters:** With 11 modules, the number of combinations is small and the geometry is sparse — there aren't enough points to make the space meaningful. At 200+ modules, the combinatorial search becomes interesting and the geometry becomes dense enough to be reliable. This is a month 3-4 feature.

---

## Application 2: Gap Detection — "What's Missing From Your Library"

### The core idea
When the best module vector combination still doesn't sum close to the request vector R, the residual vector `R - (A + B)` points in the direction of what's missing. That residual vector represents the behavioral gap — the functionality that exists in R but isn't covered by any module in your library.

You embed that residual vector back into natural language (by finding the nearest assertion descriptions or generating a behavioral summary from it) and tell the user: "Your library is missing a module that does X."

### Why this is powerful
Right now SpecLoop either finds a match or fails silently. This turns failures into actionable information. Instead of "no good match found for sub-function 'write controller'," you get "your library is missing a module that implements write-enable flow control based on a full flag — consider building and indexing one."

### The gap vector also guides generation
When a block doesn't exist in the library, the gap vector tells you what to generate. Instead of asking an LLM "write me a write controller" from scratch, you compute the projections of existing modules onto the gap vector's axes and use those projections as constraints. Existing modules that have strong projection onto the gap vector's axes contribute their behavioral patterns to the generation prompt. The LLM is generating a module that fills a mathematically defined behavioral gap, not hallucinating from scratch.

### Concrete example
```
R = embed("RISC-V CPU with AXI bus and hardware multiplier")
A = picorv32 (base CPU)
B = picorv32_axi (AXI wrapper)

residual = R - (A + B)
nearest_assertions_to_residual = ["multiplication result is correct", 
                                   "multiply latency bounded",
                                   "PCPI handshake completes"]

→ "Your library is missing a hardware multiplier coprocessor module"
```

The system can then suggest: "Run `specloop spec picorv32_pcpi_mul && specloop index picorv32_pcpi_mul` to add this to your library."

### Implementation path
- After compositional search, compute residual = R - best_sum
- Find top-k assertions in Qdrant whose vectors are closest to the residual
- Use those assertions as context to generate a natural-language description of the missing module
- Surface to user as a library gap report

---

## Application 3: Automatic Assertion Gap Detection for Compositions

### The problem it solves
When you compose two modules, you get their individual assertion sets for free — both modules are already proven. But there's a third category of assertions that neither module's proof covers: the **interaction assertions** — properties that only emerge when the modules are connected together.

"The counter value that was written to the FIFO is the same value that comes out when you read" is an interaction property. Neither the counter proof nor the FIFO proof covers it — the counter doesn't know about the FIFO, and the FIFO doesn't know about the counter.

Right now SpecLoop's composition assertion generator guesses at these interaction properties using the LLM's intuition. That's better than nothing but it's not principled.

### The vector approach
The behavioral space of a composition should equal the union of its components' behavioral spaces plus the interaction terms.

In vector terms:
```
composition_space = A_assertions + B_assertions + interaction_terms
interaction_terms = composition_request - (A_assertions + B_assertions)
```

The interaction term vector points toward the behavioral region that neither module's assertions cover. Generate assertions specifically targeting that region.

**Concretely:**
1. Compute the sum of all assertion vectors from module A and module B
2. Embed the composition's full behavioral description (from the user request + wrapper structure)
3. Compute the gap: `gap = composition_vector - (sum_A + sum_B)`
4. Find the k nearest assertion examples in your library to the gap vector
5. Use those as few-shot examples when prompting the LLM to generate composition assertions
6. The LLM is now guided toward the specific behavioral region that needs coverage

**Why this matters for quality:** Today's composition assertions are generic. With this approach, they're targeted at precisely the gaps that matter — the properties that formal verification of the composition would otherwise miss.

---

## Application 4: Library Evolution and Coverage Tracking

### The idea
As your library grows, you can track the coverage of your assertion space as a geometric measure. Plot all your module vectors in 2D (using UMAP or t-SNE for dimensionality reduction). Dense regions mean you have many modules with similar behavior — you're over-indexed there. Sparse regions are gaps in your library's behavioral coverage.

This gives you a **coverage map of verified hardware behaviors.** An enterprise customer can look at their library's coverage map and immediately see: "We have great coverage of AXI protocol modules but nothing in the clock domain crossing region."

### Concrete implementation
- Periodically compute UMAP projection of all module vectors in Qdrant
- Identify sparse regions by computing local density at each point
- Label sparse regions by finding the nearest assertion descriptions
- Generate a coverage report: "Your library covers these behavioral domains well: [list]. These domains have sparse coverage: [list]."

---

## Application 5: Assertion Transfer Learning

### The idea
When you add a new module to SpecLoop for the first time, the LLM generates assertions from scratch. But if the embedding space is well-structured, new modules that are close to existing modules in vector space should have similar assertion sets.

**Vector-guided assertion generation:**
1. Embed the new module's IR as a vector
2. Find the k nearest modules in Qdrant
3. Retrieve their assertion sets
4. Use those assertions as few-shot examples in the generation prompt, weighted by vector distance

This is already partially done via the RAG approach, but doing it with explicit vector distance weighting — closer modules contribute more strongly — would improve assertion quality on new modules.

**The key benefit:** When you encounter a new AXI module you've never seen before, the system draws on the assertions from all the AXI modules already in your library. The more AXI modules you've verified, the better the first-pass assertion generation for new AXI modules becomes. The library compounds in value.

---

## Application 6: PPA-Optimal Composition Selection

### The core idea
For any given functional target, multiple combinations of library modules may satisfy it. The naive approach picks the first functional match. The PPA-optimal approach enumerates all functional matches, maps each to its PPA vector, and selects the one closest to the user's performance target.

This is the critical insight: **the most direct implementation is rarely the most efficient one.** A counter feeding a FIFO can be wired naively (works at 100MHz) or with a registered pipeline stage (works at 500MHz). Both are functionally identical. Only PPA-aware composition selection finds the 500MHz version automatically.

### The performance target vector
The user specifies a performance intent when making a composition request:

```
specloop compose "build me a streaming data processor" --target latency=min throughput=max
```

This translates to a target PPA vector: `[0, 1, *, *]` — minimize latency, maximize throughput, area and power unconstrained.

For a different application:
```
specloop compose "build me a sensor aggregator" --target power=min area=min
```

Target PPA vector: `[*, *, 0, 0]` — minimize area and power, latency just needs to meet spec.

### The Pareto frontier output
When no single combination hits the target exactly, SpecLoop returns the Pareto frontier — the set of compositions where you can't improve one dimension without worsening another. The user sees:

```
Composition Option A: latency=0.3ns, throughput=0.9, area=450GE  ← best latency
Composition Option B: latency=0.5ns, throughput=0.95, area=380GE ← best area
Composition Option C: latency=0.4ns, throughput=0.92, area=410GE ← balanced
```

This is a conversation no LLM can have. An LLM generates one answer with no awareness of the tradeoff space.

---

## Application 7: Equivalence-Preserving Rewriting for PPA-Targeted Variant Generation

### The problem with the current PPA residual approach

Application 2 describes using the gap vector to guide LLM generation of missing modules. The same residual idea applies to PPA space — when a composition's PPA vector doesn't hit the user's target, you compute the PPA residual and use it to constrain the LLM to generate a faster/smaller variant of an existing block.

The problem is that the LLM is generating a new module from scratch. Even with constraints derived from the PPA residual, the output is an LLM hallucination that needs a full SBY formal verification run to confirm it's still functionally correct. That's expensive, slow, and the repair loop often fails.

### The better approach: rewrite rules

Instead of generating a new variant from scratch, you apply mathematically proven transformation rules to the existing verified block. These rules are **equivalence-preserving by construction** — they change the structure of the RTL without changing its behavior.

Examples of rewrite rules:
- **Pipelining**: insert register stages along the critical path to increase clock frequency at the cost of latency cycles
- **Retiming**: move existing registers across combinational logic boundaries to balance pipeline stages without adding new registers
- **Resource sharing**: identify operations that don't occur in the same cycle and map them to a single shared resource, reducing area
- **Loop unrolling**: replicate logic to process multiple data items per cycle, trading area for throughput

Each of these rules has a formal proof that the output is behaviorally equivalent to the input. You don't need to re-run SBY on the core logic — equivalence is guaranteed by the rule. You only need to verify the composition wrapper, which is minimal.

### How it connects to the PPA vector

When the PPA residual says "this composition needs to be faster in the latency dimension," SpecLoop selects the rewrite rules that target latency — pipelining and retiming. It applies them to the bottleneck block (the one contributing most latency to the composition's PPA vector), generates the transformed variant, adds it to the library with its new PPA vector, and re-runs composition search.

The gap shrinks. The library grows. The new variant is available for future compositions too.

### Why this beats asking the LLM

The LLM generating a variant has no formal guarantee of equivalence. It might introduce a subtle bug that SBY catches on the 3rd repair iteration, or doesn't catch at all if the assertion suite has gaps.

Rewrite rules give you:
- **Correctness by construction** — no SBY re-run on core logic
- **Predictable PPA impact** — pipelining a module with N combinational stages increases fmax by approximately N× and latency by N cycles, deterministically
- **A growing rule library** — every new rule you add increases the variant generation capability across all modules in the library

### Related work
- **ROVER** (Intel + Imperial College London, TCAD 2024) — formulates RTL optimization as e-graph rewriting, develops mixed-precision rewrite rules inspired by Intel engineers, formally verifies each rule preserves equivalence. Proves the approach works at industrial scale.
- **ASPEN** (Cornell, MLCAD 2025) — combines LLM-driven rule generation with e-graph rewriting and real PPA feedback. Uses LLMs to *propose* new rewrite rules which are then formally verified before being added to the rule pool.

### What's novel about SpecLoop's version

ROVER and ASPEN apply rewriting to optimize a single module in isolation. Neither has a verified module library, vector spaces, or compositional search underneath. SpecLoop's version uses the PPA residual vector to *select which rewrite rules to apply and to which block* — the vector space guides the rewriting, not just a general "make it faster" heuristic. The result goes back into the library and enriches future composition searches.

That specific combination — vector-guided equivalence-preserving rewriting feeding back into a formally verified compositional library — is unexplored in the literature.

---

## Related Work and What Makes This Different

### What exists in the literature

**PPA Prediction from RTL Embeddings:**
- *FastPASE* (ISQED 2024) — encodes RTL netlists as dataflow graphs, predicts PPA 16-155x faster than synthesis with ~13% error using graph convolutional networks. Proves PPA prediction from structural features is accurate enough to be useful.
- *MasterRTL / Transferable Pre-synthesis PPA Estimation* (TCAD 2024) — module-level PPA prediction using simple operator graphs. 98% accuracy on timing, 90% on power across 147 designs.
- *DeepRTL2* (ACL 2025) — first model unifying generation and embedding tasks for RTL. Uses XGBoost on RTL embeddings to predict area and delay. Demonstrates that RTL code embeddings carry enough structural signal for PPA regression.

**RTL Embeddings for Search:**
- *DeepRTL* (ICLR 2025) — outperforms GPT-4 on Verilog understanding using curriculum learning. Uses embedding similarity for code search and functionality equivalence checking.
- *STELLAR* (2026) — represents RTL blocks as AST structural fingerprints, retrieves structurally similar (RTL, SVA) pairs to guide assertion generation. Closest to SpecLoop's RAG approach but uses structural similarity not behavioral embedding arithmetic.

**Formal Verification + LLMs:**
- *AssertLLM, AssertionForge, SANGAM* — LLM-based assertion generation from spec documents. All require human-written specifications as input. None build a self-expanding library.
- *AutoSVA* (Princeton) — automatically generates SVA testbenches for module interactions, focused on deadlock/livelock detection. Not compositional.
- *VERT dataset* (2025) — large-scale SVA dataset for fine-tuning LLMs on hardware verification. Demonstrates that fine-tuning dramatically improves assertion quality — the same insight behind SpecLoop's training flywheel.

**Hardware Circuit Embeddings:**
- *HW2VEC* (2021) — embeds hardware circuits as graph vectors for Hardware Trojan detection. Proves that circuit structure can be meaningfully embedded in vector space. Does not use embeddings for composition or PPA optimization.

**Equivalence-Preserving RTL Rewriting:**
- *ROVER* (Intel + Imperial College London, TCAD 2024) — formulates RTL datapath optimization as e-graph rewriting with formally verified mixed-precision rewrite rules. Proves equivalence-preserving rewriting works at industrial scale.
- *ASPEN* (Cornell, MLCAD 2025) — extends ROVER with LLM-driven rule proposal and real PPA feedback from EDA tools. Rules are formally verified before entering the pool.

### What nobody has done

The specific combination that is novel:

1. **Formally verified module behaviors as vectors** — not just code similarity, but behaviorally grounded by mathematical proof
2. **Dual vector spaces** — separate functional and PPA spaces that don't interfere with each other
3. **Vector arithmetic for composition search** — tip-to-tail addition to find optimal module combinations
4. **Residual vectors for gap detection** — identifying what's missing and using gap projections to guide generation of the missing module
5. **PPA-aware composition selection** — among all functionally valid combinations, selecting the one optimal for a user-specified performance target
6. **Self-expanding library** — every verified module enriches future searches, assertions, and PPA predictions
7. **Vector-guided equivalence-preserving rewriting** — using PPA residual vectors to select which rewrite rules to apply to which block, feeding new variants back into the library. ROVER and ASPEN rewrite in isolation; SpecLoop rewrites in service of compositional search.

The foundational techniques (embedding spaces, vector arithmetic, PPA prediction, e-graph rewriting) are each proven independently over 10+ years. The application to formally verified hardware composition is unexplored.

---

## Why This Beats a Well-Trained RTL LLM

A state-of-the-art RTL LLM trained on millions of lines of Verilog (ChipSeek, CodeV, DeepRTL) generates one implementation per request. It has no awareness of:
- Whether its output is formally correct
- What the output's timing, area, or power will be
- Whether a different implementation of the same functionality would perform better
- What the tradeoff space looks like

SpecLoop with dual vector spaces:
- Generates only proven-correct implementations (formally verified)
- Knows the PPA of every component before generating a line of wrapper code
- Enumerates all valid compositions and selects the performance-optimal one
- Shows the user the tradeoff frontier when there's no perfect answer

The output is not just correct — it is the best possible correct implementation for your specific performance target, assembled from proven components, with mathematical guarantees.

---

## When To Build This

**Prerequisites:**
- Library size: 200+ formally verified modules minimum for the geometry to be meaningful
- Embedding quality validation: verify that similar modules actually cluster correctly (manual inspection of UMAP plot)
- PPA characterization: synthesize a representative subset of library modules to train the PPA predictor

**Suggested implementation order:**
1. Application 5 (assertion transfer, weighted RAG) — buildable now, low risk, immediate quality improvement
2. Application 2 (gap detection) — buildable at ~50 modules, high user value
3. PPA vector space + Application 6 (PPA-optimal composition) — buildable at ~100 modules with synthesis data
4. Application 1 (vector arithmetic composition search) — buildable at ~200 modules
5. Application 3 (interaction term assertion generation) — builds on 1 and 2
6. Application 7 (equivalence-preserving rewriting) — builds on PPA vector space, requires a rewrite rule library; start with 3-4 rules (pipelining, retiming, resource sharing, loop unrolling)
7. Application 4 (coverage map) — nice to have, marketing/demo value

---

## One-Line Summary

**Represent every formally-verified module in two vector spaces — one for what it does, one for how fast and efficient it is — then use vector arithmetic to find the combination of proven components that is both functionally correct and performance-optimal for your specific application target, and when no combination is optimal, use the PPA residual vector to select equivalence-preserving rewrite rules that generate a targeted variant with guaranteed correctness, turning SpecLoop's library into a self-improving, navigable map of verified hardware behavior with built-in performance intelligence.**
