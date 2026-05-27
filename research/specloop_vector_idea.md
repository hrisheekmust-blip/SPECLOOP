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

## Why This Is Novel

Applying vector space arithmetic to hardware module composition for formal verification is not in the literature as of mid-2026. The adjacent work includes:

- **HW2VEC** (2021) — embeds hardware circuits as graph vectors for Trojan detection, but not for composition or assertion generation
- **STELLAR** (2026) — uses structural similarity for SVA retrieval, but doesn't do vector arithmetic or gap detection
- **AssertionForge** (2025) — knowledge graph fusion of spec + RTL, different representation entirely

The specific combination of:
1. Formally-verified module behaviors as vectors
2. Vector arithmetic for composition search
3. Residual vectors for gap detection
4. Interaction terms for targeted assertion generation

...is unexplored. The foundation (embedding spaces, vector arithmetic) is proven at 10+ years of NLP research. The application to hardware formal verification is new.

---

## When To Build This

**Prerequisites:**
- Library size: 200+ formally verified modules minimum for the geometry to be meaningful
- Embedding quality validation: verify that similar modules actually cluster correctly (manual inspection of UMAP plot)
- Baseline compositional search working well (current approach)

**Suggested implementation order:**
1. Application 5 (assertion transfer, weighted RAG) — buildable now, low risk, immediate quality improvement
2. Application 2 (gap detection) — buildable at ~50 modules, high user value
3. Application 1 (vector arithmetic composition search) — buildable at ~200 modules
4. Application 3 (interaction term assertion generation) — builds on 1 and 2
5. Application 4 (coverage map) — nice to have, marketing/demo value

---

## One-Line Summary

**Represent every formally-verified module and every proven assertion as a point in a high-dimensional behavioral space, then use vector arithmetic in that space to search for compositions, detect library gaps, target assertion generation, and track coverage — turning SpecLoop's library from a database into a navigable map of verified hardware behavior.**
