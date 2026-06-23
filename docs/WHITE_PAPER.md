# CLS Cognitive Loop System: Technical White Paper

> **An autonomous cognitive architecture that structures LLM agent reasoning into a provable 6-step closed loop — with dual-AI verification, circuit-breaker safety, and cross-session learning.**

**Author:** Cognitive Workshop (认知工坊)
**Contact:** QQ 1419456542 · zyc2018@mail.ustc.edu.cn
**Version:** 1.0 · June 2026

---

## Table of Contents

1. [Abstract (Executive Summary)](#1-abstract-executive-summary)
2. [The Problem: Why Single-Pass LLM Interaction Fails](#2-the-problem-why-single-pass-llm-interaction-fails)
3. [Architecture Overview](#3-architecture-overview)
4. [The 6-Step Cognitive Cycle](#4-the-6-step-cognitive-cycle)
5. [Dual-AI Gate](#5-dual-ai-gate)
6. [Fuse Board (Circuit Breakers)](#6-fuse-board-circuit-breakers)
7. [Symbolic Dynamics Audit](#7-symbolic-dynamics-audit)
8. [Cross-Window Coordination](#8-cross-window-coordination)
9. [Fact Anchoring Protocol](#9-fact-anchoring-protocol)
10. [Integration Guide](#10-integration-guide)
11. [Future Roadmap](#11-future-roadmap)
12. [Acknowledgements and Contact](#12-acknowledgements-and-contact)

---

## 1. Abstract (Executive Summary)

### The Problem

Large Language Models (LLMs) are remarkable engines of generation, but they possess no intrinsic mechanism to verify their own output, remember what happened in a previous session, or protect themselves from runaway resource consumption. Single-pass LLM interaction — prompt in, response out — produces answers but produces no *learning*. Each session starts from zero. Each claim floats unanchored to external evidence. Each error repeats because the model cannot distinguish between a correct and incorrect output from within its own latent space. For engineering work, CAD design, quantitative analysis, and any multi-session project, this is not a limitation — it is a structural failure mode.

Four specific pathologies define the problem:

1. **Context drift:** At message 300, tokens from message 1 are attention-starved. A constraint established early fades from the model's effective working memory — not because the model is "forgetful," but because the softmax attention mechanism distributes a fixed budget of weight across an ever-growing sequence.

2. **Self-evaluation bias:** A single model cannot reliably evaluate its own output. Hallucinations are invisible from inside the latent space that produced them. Asking a model to "check your work" simply re-samples from the same distribution that generated the error.

3. **No structural memory:** A conversation with an LLM is, at the software level, a stateless API call. The "memory" that appears to exist is merely accumulated tokens in the context window. When the window is cleared, all state is lost. There is no file system anchor, no checkpoint, no residue that the next session can use.

4. **Multi-session conflicts:** Users of agentic LLM platforms routinely run 3-8 concurrent sessions. Without coordination, sessions collide on the same domain, overwrite state files, and duplicate work.

### The Solution

The Cognitive Loop System (CLS) solves this by wrapping every LLM task in a **provable 6-step closed loop**. Instead of letting the model generate and move on, CLS forces each task through:

1. **Situational Awareness** — restore cross-session context, peer at concurrent sessions, match trajectory to user intent
2. **Task Execution** — scope assessment, pre-flight checks, monitored execution, cleanup
3. **Associative Learning** — match new knowledge against existing knowledge graph, flag contradictions
4. **Abstract Generalization** — distill concrete experience into transferable patterns
5. **Context Persistence** — externalize session state to files for cross-session continuity
6. **Trajectory Update** — record delta-Q (question shift) and delta-P (progress shift), closing the loop back to Step 1

This is not prompt engineering. It is a structural harness — like a chassis around an engine — that constrains, verifies, and connects LLM outputs into a cumulative, auditable process.

### Key Innovation: Triple Verification Stack

CLS layers three independent verification mechanisms:

- **Dual-AI Gate:** A second, independent model (Qwen) verifies all outputs. Because hallucination events are probabilistically independent across different model families, the dual-hallucination rate collapses: P(DS err) × P(QW err) ≈ 0.1 × 0.1 = 1%. This is the same principle that makes dual-redundant avionics safer than single-channel systems.

- **Fuse Board:** Eight independent circuit breakers that live *outside* the cognitive system (pure stdlib Python, zero imports from reasoning modules) and block specific failure modes — self-modification, recursive nesting, token budget exhaustion, parallel overload, self-evaluation, and more. Each fuse corresponds to a real production incident.

- **Symbolic Dynamics Audit:** A real-time monitoring pipeline that treats tool-call sequences as symbolic sequences over a finite alphabet, computing entropy rates and forbidden-word counts. 50,000+ tokens of raw operation compress to 3 numbers + 1 status word for human oversight.

### Value Proposition: AI × CLS × Human

CLS is built on a **triple product model**, not a single-agent model:

```
System Value = AI (generation) × CLS (memory + constraints) × Human (goals + judgment)
```

This is multiplicative, not additive. If any factor is zero, the system produces nothing. AI provides the engine (computation, generation, pattern recognition). CLS provides the chassis (memory, constraints, safety, cross-session continuity). The human provides the steering (value judgment, goal setting, final arbitration). CLS aims not for artificial general intelligence, but for a stable, cumulative, verifiable closed loop where all three components grow together.

### Origin: 5/25, Two Weeks Before Loop Engineering

The CLS 6-step cognitive cycle was first committed to code on **2026-05-25** (commit `73ee3a84`). On 2026-06-07 — 13 days later — Google's Addy Osmani published the first prominent public article naming "Loop Engineering" as a paradigm. In mid-June 2026, Anthropic's Boris Cherny and OpenAI's Peter Steinberger independently advocated loop-based harness design. The independent convergence of CLS and major AI labs on the same architectural pattern validates the diagnosis: LLMs without structured feedback loops are inherently unreliable. The loop is not an optimization — it is table stakes.

| Date | Event |
|------|-------|
| 2026-05-25 | CLS 6-step cognitive cycle first committed (commit `73ee3a84`) |
| 2026-06-02 | Fuse Board activated (6 independent circuit breakers) |
| 2026-06-03 | Dual-AI Gate (generator/evaluator separation) enforced |
| 2026-06-07 | Addy Osmani publishes "Loop Engineering" blog post |
| Mid-June 2026 | Boris Cherny & Peter Steinberger public talks on loop-based harness design |

---

## 2. The Problem: Why Single-Pass LLM Interaction Fails

### 2.1 Context Drift Across Sessions

An LLM's context window is a single, flat attention buffer. At message 1, every token gets full attention. At message 300, tokens from message 1 are attention-starved. The model's effective memory decays with each additional message — not because the model is "forgetful," but because the softmax attention mechanism distributes a fixed budget of attention weight across an ever-growing sequence.

In single-pass interaction, this means:

- A constraint stated at message 20 may be effectively invisible by message 150.
- Multi-step CAD designs degrade as early geometric constraints fade from the attention distribution.
- Long debugging sessions lose the root cause analysis performed in the first hour.
- Each new session starts with zero context about previous work — the user must manually re-establish all relevant facts.

CLS addresses this structurally: Step 5 (Context Persistence) writes `active_context` and `cog_thread` to disk. Step 1 reads them back. The model's context window can be compressed, rotated, or restarted at any time — the persistent files carry state forward through any discontinuity.

### 2.2 Self-Evaluation Bias

A single model cannot reliably evaluate its own output. Hallucinations are invisible from inside the model's own latent space. There is no internal contradiction signal — the model's probability distribution already assigned high likelihood to the hallucinated token. Asking the same model to "check your work" simply re-samples from the same distribution that produced the error.

This is not a training problem. It is an architectural property of autoregressive language models: the model has no mechanism to compare output against external ground truth. Any self-evaluation is definitionally circular — the evaluator shares the same biases, the same knowledge boundaries, and the same failure modes as the generator.

In our production experience, a single model auditing its own exam solutions had an error rate of approximately 12%. The same problems, when audited by an independent model (Qwen), saw the error rate drop to approximately 1%. The 12% error rate was not due to poor prompting — it was due to the structural impossibility of self-evaluation.

CLS addresses this with the Dual-AI Gate (Section 5): a second independent model performs verification. The statistical guarantee is straightforward — two independent models making the same mistake is exponentially less likely than one.

### 2.3 No Structural Memory

A conversation with an LLM is, at the software level, a stateless API call. The "memory" that appears to exist is merely the accumulation of tokens in the context window. When the window is cleared (session end, compact, crash), all state is lost. There is no file system anchor, no database row, no checkpoint.

Single-pass interaction produces answers but produces no *residue* — nothing that the *next* session can use. Each session is an island. Knowledge gained, patterns discovered, constraints learned — all evaporate at the session boundary unless manually re-injected by the human.

CLS addresses this with four mechanisms:

- **Trajectory system** (`state/trajectory.json`): cross-session position/mass/momentum tracking with a schema that records both what was done (mass) and where it was headed (momentum).
- **Memory architecture** (`data/memory/`): 200+ indexed knowledge entries with decay parameters, organized by domain, with proactive retrieval during Step 1-b.
- **Working state files** (`state/active_context`, `state/cog_thread`): serialized session state enabling mid-loop recovery after compact or session restart.
- **Activation state** (`state/activation_state.json`): cold/warm start branching logic that determines whether a full or partial initialization sequence is needed.

### 2.4 Multi-Session Multi-Agent Conflicts

Users of agentic LLM platforms routinely run 3-8 concurrent sessions. Without coordination:

- Session A reads a state file, Session B writes it, Session A writes its stale copy — data loss.
- Session A and Session B both modify the same CAD assembly — merge conflicts.
- Session A's work depends on Session B's output, but Session A proceeds with stale data.
- Two sessions unknowingly pursue contradictory goals within the same domain.

Traditional solutions (file locking, manual coordination) do not apply because LLM sessions are not aware of each other at the system level — each is an independent process with its own context.

CLS addresses this with the Cross-Window Coordination Protocol (Section 8): a shared state file, PEEK/ANNOUNCE/COVER CHECK lifecycle, domain collision detection at the structural level, and stale window cleanup. When a collision is detected, the human is notified and asked to arbitrate — the system never unilaterally decides to proceed.

---

## 3. Architecture Overview

### 3.1 System Diagram

```
                        ┌─────────────────────────────────────────┐
                        │            FUSE BOARD (stdlib)            │
                        │  WRITE_PROTECT | RECURSION | TOKEN_BUDGET │
                        │  PARALLEL_CAP | CHECKPOINT | PROXY_PURITY │
                        │  SELF_EVAL_PROHIBITED | DUAL_AI_GATE      │
                        │  NUMERIC_COMPUTATION | QWEN_DOWN_TIMEOUT  │
                        └──────────────┬──────────────────────────┘
                                       │ checks every operation
        ┌──────────────────────────────▼──────────────────────────────┐
        │                     COGNITIVE CORE LOOP                      │
        │                                                              │
        │  ① Situational Awareness ──→ ② Task Execution                │
        │         ▲                          │                          │
        │         │                          ▼                          │
        │  ⑥ Trajectory Update ←──── ③ Associative Learning            │
        │         │                          │                          │
        │         ▼                          ▼                          │
        │  ⑤ Context Persistence ←── ④ Abstract Generalization         │
        │                                                              │
        └──────────────────────────────────────────────────────────────┘
                        │                           │
              ┌─────────▼─────────┐       ┌────────▼─────────┐
              │   DUAL-AI GATE     │       │ SYMBOLIC DYNAMICS │
              │ DS creates         │       │ Real-time audit   │
              │ Qwen verifies      │       │ <200ms per check  │
              └───────────────────┘       └───────────────────┘
```

### 3.2 Three-Layer Safety Architecture

Safety in CLS is not a feature — it is a layer that the cognitive system cannot modify. Three independent layers stack, each with a different protection mechanism:

```
Layer 1: PreToolUse Hook (.claude/hooks/PreToolUse.ps1)
  Runs before every tool call. 17+ checks including:
  COMPUTE_GATE, LIFE_CLAIM, FAKE_MODEL, COG_STEP, FUSE_CHECK,
  CACHE_DISCIPLINE, RETRIEVAL_BYPASS, SYMBOLIC, CROSS_WINDOW,
  KEY_SCAN, C_DRIVE_GUARD, KNOWLEDGE_QUALITY, and more.
  Design: fail-open with auditable deny.

Layer 2: Dual-AI Gate (scripts/safety/qwen_gate.py)
  Generator (DeepSeek) creates designs, code, knowledge entries.
  Evaluator (Qwen) independently verifies outputs with fresh context.
  Statistical guarantee: p(error) ≈ p(DS err) × p(QW err).
  Three gate types: Design Check, Knowledge Check, Numeric Check.

Layer 3: Fuse Board (scripts/core-engine/fuse_board.py)
  8 hard fuses. Each blocks a specific failure mode. Implemented in
  pure stdlib with zero imports from the cognitive modules it protects.
  Designed with a hardware migration path via FuseBackend interface.
```

The key architectural invariant: no layer depends on any layer above it. The Fuse Board can operate with Layer 1 and Layer 2 disabled. Each layer is independently testable.

### 3.3 Data Flow

The system operates on file-anchored state — not model-internal memory:

```
User Request
    │
    ▼
Step ①: Load state/trajectory.json, state/active_context,
         state/cross_window_context.json, data/memory/MEMORY.md
    │
    ▼
Step ②: Pre-flight (Fuse Board + Premise Check + Cover Check)
         → Execute → Cleanup
    │
    ▼
Step ③: Symbolic dynamics check → Extract knowledge deltas
         → Match against knowledge graph → Write associations
    │
    ▼
Step ④: Decontextualize patterns → Check pattern registry
         → Write generalized patterns
    │
    ▼
Step ⑤: Write active_context, cog_thread, cross_window status,
         last_operation.json → Compact health check
    │
    ▼
Step ⑥: Extract delta-Q (mass) + delta-P (momentum)
         → Update trajectory.json → Loop closes (⑥ → ①)
```

Every arrow is a file write followed by a file read. The LLM's context window is a high-speed cache; the files are the database. State never lives exclusively in the model.

### 3.4 State Management Approach

CLS uses a **tiered memory architecture**:

| Tier | Scope | Examples | Persistence |
|------|-------|---------|-------------|
| **Tier 1: Active Context** | Session-scoped | `active_context`, `cog_thread` | Volatile during session; written to disk by Step 5 for survival |
| **Tier 2: Session State** | Session-scoped | `trajectory.json`, `session_health.json`, `activation_state.json` | File-persisted; survives compact, restart, and crash |
| **Tier 3: Long-Term Memory** | Cross-session | `data/memory/MEMORY.md`, `data/memory/on_demand/` | File-persisted; indexed for retrieval; decay-managed |

State is **always externalized to files**. The LLM's context window is treated as a cache, not a database. The files are the source of truth. This principle enables:

- **Compact resilience:** When context is compressed, files survive.
- **Crash recovery:** A crashed session can warm-start from files.
- **Cross-session continuity:** A new session reads the previous session's trajectory.
- **Auditability:** All state decisions are traceable through git-tracked files.

### 3.5 Breath Rhythm: Systole and Diastole

The 6-step loop is not a flat pipeline. It follows a cardiac rhythm that alternates between two modes:

```
SYSTOLE (action, output)         DIASTOLE (reflection, learning)
┌───────┬───────┐          ┌───────┬───────┬───────┬───────┐
│Step 1 │Step 2 │          │Step 3 │Step 4 │Step 5 │Step 6 │
│Aware  │Execute│   ──→    │Assoc  │Abst   │Persist│Traj   │
└───────┴───────┘          └───────┴───────┴───────┴───────┘
  produce value                    extract value from production
```

- **Systole (Steps 1-2):** Contraction, focus, action. These steps produce output visible to the user — establishing awareness, then executing the task. This is the "working" half of the loop.
- **Diastole (Steps 3-6):** Expansion, reflection, connection. These steps extract reusable value from what was just produced — connecting knowledge, generalizing patterns, persisting state, updating trajectory. Nothing in diastole is directly visible to the user; it is infrastructure work.

A loop that only executes (no diastole) is a treadmill — constantly producing without learning. A loop that only reflects (no systole) is a diary — constantly analyzing without producing. Both phases are necessary. The transition is structural: the system tracks whether it is currently in systole or diastole and enforces the transition at natural break points.

### 3.6 Design Principles

Eleven principles govern all architectural decisions:

| # | Principle | Implication |
|---|----------|------------|
| 1 | Interface > Implementation | Signatures fixed; backends swappable. FuseBackend interface enables software-to-hardware migration. |
| 2 | Principles > Rules | Principles generalize; rules become obsolete. The fuse board is a principle-based architecture. |
| 3 | Sparse Activation, Shared Baseline | Safety layer always on. Domain skills load on demand. Routing failure does not disable baseline. |
| 4 | Positive Anchor, Not Just Avoidance | The "Creator's Contract" (never evaluate own output) is a positive anchor, not just a prohibition. |
| 5 | Progressive Disclosure | L1 metadata → L2 content → L3 references. Context is loaded in layers, not all at session start. |
| 6 | Open Interface, Closed Implementation | Interfaces are documented and stable; implementations are free to optimize without breaking callers. |
| 7 | Safety is a Moat, Not a Cost | The safety architecture is the competitive advantage, not overhead to be minimized. |
| 8 | Core Self-Built, Periphery Borrowed | Cognitive loop and safety are self-built. Data formats, random numbers use standard libraries. |
| 9 | Host Independence | Claude Code is the current host, not the owner. Core is fully host-agnostic. Migration requires changing 3 files. |
| 10 | Positive Feedback Activation | Knowledge base boundaries structure decisions but do not make them. Every output writes back. |
| 11 | Triple Product, Not AGI | AI × CLS × Human = stable closed loop. Each factor multiplies — zero in any factor zeros the system. |

---

## 4. The 6-Step Cognitive Cycle

Each step is presented with its purpose, trigger condition, inputs, detailed process, outputs, and edge cases.

### 4.1 Step 1: Situational Awareness (Systole)

**Purpose:** Establish the "where am I?" baseline before any action. The system must know what was being done, what other sessions are doing, and what knowledge is relevant *before* it starts executing. This is the most important step — errors in situational awareness cascade into every subsequent step.

**Trigger:** Session start, or user returns after a >5-minute gap.

**Inputs:**
- `state/activation_state.json` — DEAD / HIBERNATING / ACTIVE; determines cold vs. warm start
- `state/trajectory.json` — previous position, mass, momentum, and trajectory log
- `state/cross_window_context.json` — other sessions' focus and status
- `data/location_registry.json` — project path mappings (avoids hardcoded paths)
- `data/memory/MEMORY.md` — ~200 indexed user knowledge entries
- `state/.porter_reminder` — cached files from Porter file-absorption system

**Process:**

1. **Load path registry** — Inject domain paths into session context. All subsequent path references route through the registry, avoiding hardcoded paths that would break across machines. Paths use relative notation: `{认知系统:data}/memory/xxx` format.

2. **Determine activity type** — Classify the current task into one of: `code` (compilation, modification, debugging, architecture), `analysis` (data analysis, simulation interpretation, system diagnosis), `narrative` (story creation, world-building), `research` (search, learning, literature review), `background` (simulation, monitoring, waiting), or `mixed` (default). This classification determines compact thresholds, memory loading strategy, and execution mode.

3. **Cold vs. warm start branching:**
   - **Cold start** (`status=DEAD` or `last_heartbeat > 10 min`): Full initialization. Execute L4→L3→L2→L1 dose injection (layered context restoration, from most foundational to most recent). Write self_activate signal. Launch `activation_listener.py` for heartbeat.
   - **Warm start** (`status=HIBERNATING`, `last_heartbeat <= 10 min`): Read trajectory for continuity. Load `active_context` if available. Read signal bus (last 20 entries) for recent events. Skip full initialization — this saves substantial startup time and avoids re-injecting already-loaded context.

4. **Anti-atrophy scan** — Run `anti_atrophy_consumer.py`. Scan cue activity levels. Randomly select 20% of low-activity knowledge entries and inject them into context, preventing silent knowledge loss through the forgetting curve. The random selection (not top-N-by-decay) ensures diversity — the same entries are not repeatedly reinforced while others decay to zero.

5. **Read trajectory** — Extract `position`, `mass`, and `momentum` from `trajectory.json`. Position tells what task was in progress. Mass tells what concrete work was done (files changed, code written, designs completed). Momentum tells what direction is being pursued (open questions, planned next steps). All three are needed; position alone is insufficient.

6. **CLS Memory search** — Run `cls_memory.py search "<task domain keywords>" --top-k 5`. This performs semantic search across the memory corpus using fastembed + bge-small-zh-v1.5 (fully local, no external embedding API). Results with scores are injected into `working_context.knowledge_hits` before execution begins.

7. **Cross-window peek** — Call `cross_window_hook.auto_peek()` to see what other Claude Code sessions are doing. Output includes each window's status, focus, domain, and time since last update. This information is displayed to the user and stored in `working_context` for collision detection in Step 2.

8. **External anchor sample** — Run `external_anchor.py --sample` to collect a real-world state value (git diff summary, system process list, entropy source state, last operation time). This is the anti-self-referential-closure mechanism: before making decisions, the system touches something external to its own context.

**Output:** `active_context` is populated with task focus, detected domain, knowledge hits, timeline start, cross-window state, external anchor sample, and working state. `state/active_context` file is written.

**Edge Cases:**
- **First run (no state files):** Silent skip for missing files. Initialize with minimal defaults. Do not block.
- **Corrupted `trajectory.json`:** Fall back to reading `data/memory/last_operation.json`. If both are corrupted, cold-start with empty position.
- **No other windows:** `auto_peek()` returns empty array. No error.
- **Activation listener unavailable:** Heartbeat not written. Next session will cold-start. Acceptable degradation.
- **Semantic search service unavailable:** Fall back to keyword-only matching. Do not block.

### 4.2 Step 1-b: Proactive Knowledge Query

**Purpose:** Retrieve relevant past lessons *before* execution begins, not reactively during execution. By the time the model starts writing code or making decisions, relevant past failures and patterns are already in context. This is the difference between reading the manual before operating the machine and looking up instructions after something breaks.

**Trigger:** Automatically after Step 1 completes, before Step 2 begins.

**Process:**

1. Extract task keywords from `active_context`.
2. Query knowledge system with those keywords (keyword match + semantic match fallback).
3. Match against `lessons_learned` corpus.
4. Sort by `activation_priority` (0.0-1.0, descending), then by `severity` (descending).
5. Load top-3 matches into `working_context.knowledge_hits`.

**Edge case:** No matching lessons — silent skip. Empty `knowledge_hits` array is valid.

### 4.3 Step 2: Task Execution (Systole)

**Purpose:** The only step that directly produces user-visible output. Execute the task with layered safety checks, strategic path selection, and systematic cleanup. This is the engine of the system — everything else exists to make this step more reliable, cumulative, and safe.

**Trigger:** User request arrives.

**Inputs:**
- `active_context` from Step 1
- `working_context.knowledge_hits` from Step 1-b
- Task description from user message
- All safety layer states (Fuse Board, Dual-AI Gate, Symbolic Dynamics health)

**Process:**

#### Phase 1: Scale Assessment

| Scale | Criteria | Execution Strategy |
|-------|---------|-------------------|
| **Simple** | Single file, single domain, ≤2 files, ≤3 ops, depth ≤1 | Direct execution; no fan-out |
| **Medium** | Multiple files, single domain, ≤5 files, ≤8 ops, depth ≤2 | Batch diagram + parallel fan-out |
| **Complex** | Multi-domain, long dependency tails, >5 files or >8 ops | Subagent orchestration |

#### Phase 2: Strategy Selection

1. Run `strategy_selector.py --task "<2-10 word summary>"` — returns P1 (primary)/P2/P3 path recommendation with probability distribution. Strategy selection is a lightweight heuristic that does not call an LLM; it executes in under 50ms.
2. Roll `epsilon_gate.py` — with ε=0.1 (10% probability), take an exploration path instead of the default P1. This prevents the system from getting stuck in local optima. The remaining 90% follow the recommended path.
3. Apply `path_mutation.py` to each execution step — with 10% probability per step, apply INSERT_NOOP, SWAP, SUBSTITUTE_WHEEL, or SKIP mutations. This introduces controlled variability at the micro level.

#### Phase 3: Pre-flight Checks

- **Fuse Board check:** Verify all 8 fuses below trip threshold. Any tripped fuse blocks execution with a specific reason.
- **Cross-window cover check:** `cover_check()` compares current domain and keywords against all active windows. If another window is working on the same domain with overlapping keywords, report conflict.
- **Cross-window announcement:** `announce(current_focus)` declares the window's intention to all other windows.
- **External anchor:** Verify `premise_check.py` passes — files exist at claimed paths, PIDs are alive, paths resolve to real filesystem entries.
- **SELF_EVALUATION_PROHIBITED fuse:** Verify model will not self-assess output quality in the upcoming execution.

#### Phase 4: Fragment Restoration Protocol (v21+)

When the user input contains fragmented/partial data (scattered files, multiple formats, reconstruction goal), this 6-step protocol activates:

1. **Panoramic scan** — List all available files with type, size, and initial assessment.
2. **Coarse sorting** — Classify by source grade: S (FEM/PPT params) > A (point clouds/photos) > B (physical inference) > C (verbal) > D (estimates).
3. **Deep extraction** — Extract key parameters from highest-grade sources first.
4. **Cross-validation** — Compare across sources. Contradictions are signals, not errors — they reveal data quality issues.
5. **Gap filling** — Estimate missing parameters from physics/engineering heuristics. All estimates are marked `__EST__` (not to be confused with measured values).
6. **Restoration output** — Produce complete output in the target format, with clear provenance for every value.

#### Phase 5: Execute and Cleanup

Run the task. Monitor for errors. Log every failure via `failure_learner.py record`. Close temporary files. Finalize state for diastole.

**Output:** Task deliverables (code, files, analysis, decisions, designs).

**Edge Cases:**
- **Fuse tripped during pre-flight:** Block execution. Report which fuse, threshold, and current value.
- **Cover check collision:** Another window is working on the same domain. Report conflict with both windows' details. Offer options: wait, redirect, or proceed with caution. Do NOT unilaterally proceed — the human arbitrates.
- **Premise check failure:** File/PID/state verification failed. Report the specific failed premise. Block execution.
- **Epsilon-greedy perturbation active:** Accept that this execution may be suboptimal. Log to `perturbation_state.json`. The 10% exploration budget is an investment in discovering better paths.

### 4.4 Step 3: Associative Learning (Diastole)

**Purpose:** Bridge isolated experiences into integrated understanding. A fact that is not connected to the knowledge graph cannot be found, cannot be built upon, and contributes nothing to future sessions. Step 3 is the bridge between isolated experience and integrated understanding — the difference between a pile of bricks and a building.

**Trigger:** New knowledge produced (task completed, insight generated, error encountered).

**Inputs:**
- Task output and error logs from Step 2
- Existing knowledge graph (`data/memory/`, `data/cues/`, pattern registry)
- Symbolic dynamics health data

**Process:**

1. **Pre-step: Symbolic dynamics health check** — Run `symbolic_observer.py status --quiet`. Read 6-domain entropy status. If any domain is critical/diverging, flag for attention and enter conservative mode before proceeding.

2. **Extract new knowledge** — What was learned? What was confirmed? What was discovered to be wrong? This is not summary — it is structured extraction of deltas. A task that confirmed an existing pattern produces a strengthen signal. A task that revealed an incorrect assumption produces a contradiction flag.

3. **Match against existing knowledge graph** — Search the knowledge base for related entries. New knowledge can:
   - **Confirm** an existing pattern (increase confidence score)
   - **Contradict** an existing assumption (flag as tension to be resolved)
   - **Fill a gap** in the knowledge graph (bridge two previously disconnected clusters)
   - **Open a new domain** (create a new cluster with initial low confidence)

4. **Write association record** — Log to `insight_log`. Tag with: `source` (which task produced this), `related_entries` (what it connects to), `confidence` (0.0-1.0, how certain is this association).

5. **Domain-specific precipitation:**
   - PIC keywords touched → trigger `pending_pic_precipitation` → batch-precipitate plasma simulation insights into the PIC knowledge graph
   - CAD patterns detected → flag for constraint registry update

6. **P2 story consumption** — Scan the signal bus for P2-level perturbation signals from the current session. Extract narrative threads from micro-perturbations. Write `story_consumption.jsonl`. This catches weak signals — emergent patterns that individual events are too noisy to reveal but that become visible in aggregate.

**Output:** Associations written to `insight_log`. Domain-specific precipitation results. P2 consumption markers.

**Edge Cases:**
- **No new knowledge:** Skip. Not every task produces learnable insight. An empty Step 3 is valid.
- **Contradiction detected:** Flag as tension. Do not automatically overwrite existing knowledge. The contradiction itself is signal — write it as unresolved.
- **Symbolic dynamics critical:** Proceed but mark all new associations with `risk_tag=symbolic_diverging`. Conservatively raise association threshold (×1.3) — only high-confidence matches are accepted.

### 4.5 Step 4: Abstract Generalization (Diastole)

**Purpose:** Transform concrete experience into reusable patterns. Concrete experience without abstraction is apprenticeship without a textbook — you can retrain on the same problem endlessly and never get faster. Abstraction makes the experience reusable: a pattern recognized once prevents N future errors.

**Trigger:** Step 3 completed with nontrivial new knowledge.

**Inputs:**
- Concrete associations from Step 3
- Pattern registry (existing generalized patterns with confidence scores)
- Task context for decontextualization

**Process:**

1. **Pattern extraction** — Examine the concrete experience. "Fixed bolt clearance by increasing spacing from 6mm to 8mm." The concrete detail is noise. The general pattern might be: "When bolt diameter and wall thickness are comparable, check for through-body clearance." The transformation is: specific→categorical, measurement→relationship, procedure→principle.

2. **Decontextualization** — Strip task-specific details: file names, exact measurements, specific CAD part identifiers, timestamps. Retain only the categorical relationship. This is harder than it sounds — over-stripping produces vacuous patterns ("check things before building them"); under-stripping produces non-transferable patterns.

3. **Pattern registry check** — Does this generalized pattern already exist?
   - **Yes** → Merge and strengthen. Increment occurrence count. Increase confidence score proportional to confirmation strength.
   - **No** → Create new entry with initial confidence score derived from the source task's reliability.

4. **Transferability assessment** — Rate the pattern on three axes:
   - **Domain scope:** How many domains does this apply to? (1 / several / many)
   - **Recurrence likelihood:** How often will this scenario repeat? (rare / occasional / frequent)
   - **Abstraction level:** How general is the formulation? (specific / general / principle)

**Output:** Generalized patterns written to pattern registry. Merged with existing patterns where overlap exists.

**Edge Cases:**
- **Pattern already at maximum confidence:** Skip write. The system has already learned this as well as it can.
- **Decontextualization stripped too much:** The pattern becomes vacuous. Mark for human review. "Check before proceeding" is not a useful pattern.
- **Symbolic dynamics diverging:** Skip automatic abstraction. Mark for human review. Conservative mode prevents writing potentially hallucinated patterns.

### 4.6 Step 5: Context Persistence (Diastole)

**Purpose:** Externalize session state so the next session (or the same session after compact) can resume without the user repeating context. State that lives only in the LLM's context window is volatile — a compact, a crash, or a session boundary erases it. Step 5 ensures that no state is lost.

**Trigger:** Session end approaching, compact threshold near, or cognitive cycle completing.

**Inputs:**
- Current `active_context` (the live working memory)
- Current `cog_thread` state (which step, which sub-step, what next)
- Current cross-window status
- New knowledge entries (if any, from Steps 3-4)
- Session health metrics (message count, cache hit rate, active time)

**Process:**

1. **Write `active_context`** — Serialize current working state: task focus, domain, pending decisions, loaded knowledge, cross-window observations. This is the session's "save game" — from this file, the next session can reconstruct the working context.

2. **Write `cog_thread` checkpoint** — Record which cognitive step we are in, which sub-step, what the next action would be. This enables mid-loop recovery after compact or session restart. Without this, a compact in the middle of Step 2 would lose track of which pre-flight checks had already passed.

3. **Cross-window update** — Call `cross_window_hook.auto_update()` to refresh status in the shared window context. Update `last_seen` timestamp and current focus summary.

4. **Compact health check** — Call `compact_health_board.py --msgs` to check message count against the threshold for the current activity type. If at or above threshold, trigger compact: append `/compact` and `self_activate` to user-visible output.

5. **Knowledge quality gate** — Run `knowledge_quality_gate.py` on any new knowledge entries. The quality score is computed as:
   ```
   score = structure_match × temporal_coherence
   ```
   Where structure_match measures whether parts have functional gaps, and temporal_coherence measures whether the causal chain is continuous. Thresholds:
   - ≥ 0.8: Pass — entry is saved
   - 0.55-0.8: Review flag — entry is saved but flagged for human review
   - < 0.55: DENY — entry is blocked at the PreToolUse hook level

6. **Delivery check** (if deliverables produced) — Run `delivery_check.py --deliver "task title" --files <output list> --rationale "why" --trajectory "steps"`. This catalogs all produced files, records the design rationale and process trajectory, and copies outputs to the structured delivery directory.

**Output:**
- `state/active_context` (updated)
- `state/cog_thread` (updated)
- `state/cross_window_context.json` (status updated)
- `data/memory/last_operation.json` (updated)
- Delivery catalog (if applicable)

**Edge Cases:**
- **Disk full:** Log error to signal bus. Do not crash. Next session will cold-start.
- **Compact triggered:** Append `/compact` to output. After compact, saved state files enable warm restart without context loss.
- **Active context too large:** Truncate to most recent N entries. Log the truncation event.

### 4.7 Step 6: Trajectory Update (Diastole)

**Purpose:** Close the loop. Record where the system moved — what question shifted, what progress accumulated. This is what makes Step 1 of the *next* cycle meaningful. The trajectory is the system's long-term memory of its own cognitive movement.

**Trigger:** Step 5 completes. This is always the final step.

**Inputs:**
- Previous trajectory state (`state/trajectory.json`)
- Current cycle's concrete changes (from Step 2 output and Step 3 associations)
- Current cycle's direction shifts (from Step 4 patterns and Step 5 context)

**Process:**

1. **Extract delta-Q (mass change)** — What concrete changes were made? Files created, modified, deleted. Code written. Designs completed. Knowledge entries added. This is the "what" of the cycle. In physics terms, this is mass: the accumulated weight of work done. Mass is objective and verifiable — you can look at the git diff and count the files.

2. **Extract delta-P (momentum change)** — What direction did the work move in? What new questions opened? What paths were closed? What paths were opened? This is the "where to" of the cycle. Momentum is subjective — it is the system's interpretation of where the work is heading.

   Mass without momentum is dead history — you know what was built but not what it was building toward. Momentum without mass is wishful thinking — you know the direction but not whether any work was actually done. Both are necessary.

3. **Update `trajectory.json`** — Write new position, accumulate mass (prev_mass + delta-Q), update momentum (prev_momentum rotated by delta-P), append to trajectory log with timestamp. The trajectory log is the append-only history of cognitive movement — each entry is one cycle's worth of change.

4. **Cross-window completion** — Call `cross_window_hook.auto_update(status="completed", summary="cycle summary")`. Mark task as done so other windows know this domain is free.

**Output:**
- `state/trajectory.json` (updated position, mass, momentum, log)
- `state/cross_window_context.json` (completion status)

**Closure: ⑥ → ①**

The updated `trajectory.json` is what Step 1 reads at the start of the next cycle. Active sessions read the updated trajectory to maintain continuity during the same session. New sessions (cold or warm start) read it to answer "where were we?" without the user repeating context. This closes the loop — the system's output becomes its own input.

**Edge Cases:**
- **No delta-Q (no concrete changes):** Still record. A cycle with zero mass is diagnostic signal — was the user just browsing? Was the task blocked by a fuse trip? The absence of mass is information.
- **Trajectory log too large:** Truncate to most recent 100 entries. Old entries are historical record, not active working memory. The full history is preserved in git.
- **Cross-window update fails:** Log and continue. The trajectory update is the primary artifact; cross-window update is a convenience.

### 4.8 Natural Break Points and Inertial Mechanics

The loop is not a scheduler. It does not run on a timer. It runs on **natural break points** — moments in the conversation where a meaningful unit of work has completed and the system can pause to reflect:

| Step | Fires when... |
|------|--------------|
| ① | Session starts; user returns after >5 min gap |
| ①-b | Step ① completes |
| ② | User makes a request |
| ③ | New knowledge produced (task completes, error occurs, insight forms) |
| ④ | Step ③ completes with nontrivial new knowledge |
| ⑤ | Session end approaching; compact threshold near; cycle completing |
| ⑥ | Step ⑤ completes |

**The 5-minute rule:** If user inactivity exceeds 5 minutes, trigger a brief situational awareness refresh (update time awareness, re-establish `active_context`). If inactivity is ≤5 minutes, silently skip — this prevents the loop from churning during rapid back-and-forth conversation.

**The Three-Look-Back:** At every natural break point, three questions fire automatically without requiring the model to "decide" to check:

1. **Can this be parallelized?** — Are there independent sub-tasks that can run concurrently? If yes, fan out.
2. **Was the last step correct?** — Glance at the output of the previous tool call. Error? Empty? Unexpected output? Stop and fix before proceeding.
3. **Can an existing interface be reused?** — Before writing new code, check `scripts/wheels/` for existing functionality that already solves this problem.

These are inertial mechanics — wired into the natural break points so they fire without deliberation. The model does not need to "remember" to check; the structure enforces the check.

---

## 5. Dual-AI Gate

### 5.1 Principle: Generator/Evaluator Separation

A single model cannot reliably evaluate its own output. The solution is statistical: use an independent model to verify.

```
DeepSeek (Generator)                  Qwen (Independent Evaluator)
    │                                       │
    │  creates CAD design,                  │
    │  knowledge entry,                     │
    │  or numerical computation             │
    │                                       │
    ├─── sends claims ──────────────────────┤
    │                                       │ independent verification
    │                                       │ with fresh, isolated context
    │                                       │
    │◄── verdict: CORRECT / INCORRECT ──────┤
    │   + confidence score                  │
    │   + specific issues (if INCORRECT)    │
    │                                       │
    ├─ CORRECT → proceed                    │
    └─ INCORRECT → blocked by fuse_board    │
                   → regenerated or flagged │
```

### 5.2 Statistical Guarantee

The core insight is probabilistic, and it depends on the independence of the two models:

```
P(both models wrong) = P(DS hallucinates) × P(QW hallucinates)

Assumed baseline: P(hallucinate per model) ≈ 0.1
→ P(both wrong) = 0.1 × 0.1 = 0.01 = 1%
```

This is the multiplication of independent error probabilities — the same principle that makes dual-redundant avionics safer than single-channel systems. The independence assumption is supported by the fact that DeepSeek and Qwen are different model families with different training data, architectures, and training objectives.

**Production validation:** In 6 delivered exam papers with 12 problems each (72 total independent verifications), the measured error rate after dual-AI auditing was approximately 1%, consistent with the 1% theoretical prediction given an assumed 10% single-model error rate.

**Important caveat:** The independence assumption is an approximation. Two models trained on overlapping internet text will share some biases. The 1% figure is therefore a lower bound on the ideal case; correlated errors will push the real rate higher. However, the correlation between models from different families (DS + Qwen) is substantially lower than the self-correlation of a single model auditing itself. The practical gain is in the order-of-magnitude range.

### 5.3 Three Gate Types

| Gate Type | What It Checks | Verification Method | Threshold |
|-----------|---------------|-------------------|-----------|
| **Design Check** | CAD design completeness, geometric validity, constraint satisfaction | Qwen independently reviews the design against constraint registry; checks for unconstrained degrees of freedom, interference, missing features | Any constraint violation → FAIL |
| **Knowledge Check** | Knowledge claim consistency, reproducibility, source attribution | Qwen checks claim against existing knowledge base; verifies the claim is anchored to a source; checks for internal contradiction | Unanchored claim or contradiction → FAIL |
| **Numeric Check** | Independent recomputation, order-of-magnitude sanity | Qwen independently recomputes the calculation (not from the generator's intermediate steps); checks result against expected magnitude | Computation mismatch or scale error → FAIL |

### 5.4 Verdict Extraction: Word-Boundary Bug

Gate verdicts are parsed from model output using a word-boundary regex. This was hardened in production (2026-06-23 commit) after discovering a real parsing bug:

```python
# BUG: "INCORRECT" found inside "NOT INCORRECT BUT CORRECT"
"CORRECT" in verdict_text   # False positive for CORRECT
"INCORRECT" in verdict_text # False positive for INCORRECT

# FIX: Word-boundary regex
re.search(r'\bCORRECT\b', verdict_text)    # Only matches CORRECT as a word
re.search(r'\bINCORRECT\b', verdict_text)  # Only matches INCORRECT as a word
```

This bug had real consequences: models sometimes embed the negative verdict inside a positive phrasing ("the answer is NOT INCORRECT, it is actually CORRECT"), and substring matching incorrectly classified the verdict. The fix is simple but critical — a reminder that parsing LLM output requires the same rigor as parsing any other machine output.

### 5.5 Fallback and Degradation Modes

The system must continue functioning even when verification services are unavailable:

| State | Behavior | Rationale |
|-------|---------|-----------|
| **Qwen API available** | Primary verification path | Full statistical guarantee active |
| **Qwen API down** | Anthropic Haiku takes over audit duties | Reduced but still independent (different model family from DeepSeek) |
| **Both APIs down** | `gate_status = unavailable`; default ALLOW | Gates must not block the system entirely; availability > safety when safety mechanism itself is down |
| **QWEN_DOWN_TOO_LONG fuse** | Qwen offline > 30 minutes → block mission-critical outputs (CAD, knowledge, deliverables) | Extended verification gap creates unacceptable risk accumulation |

### 5.6 Trigger Conditions

Verification is not performed on every output — it is triggered based on risk composition:

| Condition | Action | Risk Model |
|-----------|--------|-----------|
| Writing to knowledge base | Forced verification | Knowledge errors compound — one bad entry poisons future retrievals |
| Session exceeds 60K tokens | Forced verification | EC-T2 decay point: attention to early context severely degraded |
| Material parameters + numerical computation | Forced verification | AS-T3 (material) + CR-T2 (computation) composite risk |
| 30-60K tokens + numerical computation | Preventive verification | Elevated but not critical risk zone |
| CAD design output | Forced design check | CAD errors propagate to physical manufacturing |
| Exam/exam solution delivery | Forced dual audit (Qwen + Doubao) | Education materials require maximum verification |

### 5.7 Implementation

Implemented in `scripts/safety/qwen_gate.py` (~48KB). Three verification entry points:

```python
def verify_cad_design(design_spec: dict, constraints: list) -> GateVerdict:
    """Design completeness and geometric reasonableness."""

def verify_knowledge(claim: str, source_context: dict) -> GateVerdict:
    """Knowledge claim consistency and reproducibility."""

def verify_numerical(computation: str, expected_magnitude: float) -> GateVerdict:
    """Independent numerical recomputation and magnitude checks."""
```

Each function:
1. Constructs an independent prompt context (zero shared history with the generator)
2. Injects relevant domain constraints and known error patterns
3. Sends to the evaluator model
4. Parses the verdict with word-boundary regex
5. Returns a structured `GateVerdict` with verdict, confidence, and specific issues

---

## 6. Fuse Board (Circuit Breakers)

### 6.1 Why Safety Must Be OUTSIDE the Cognitive System

An LLM cannot be trusted to manage its own constraints. Any constraint that lives inside the model's context window is subject to three unavoidable failure modes:

1. **Attention decay:** As the conversation grows, constraints stated early fade from the model's effective working memory. A constraint on message 10 has negligible influence on message 300's output.

2. **Self-modification:** The model can rationalize bypassing a constraint ("this one time is okay," "the constraint was meant for different circumstances"). Inside the same context window, there is no separation between the constraint and the constrained.

3. **Prompt injection:** Adversarial or accidental input can override constraints that share the same context. There is no privilege separation between safety instructions and user content.

The solution: **constraints must live in files the model cannot modify.** The Fuse Board is implemented in pure Python stdlib — `scripts/core-engine/fuse_board.py` — with zero imports from the cognitive modules it protects. It is in the WRITE_PROTECT list, meaning it blocks writes to its own source code. The model cannot reason its way around a fuse because the fuse's enforcement logic runs in a different process with no access to the model's context.

### 6.2 The 8 Active Fuses

Each fuse corresponds to a real production incident that occurred before the fuse was installed. This is not speculative safety engineering — every fuse was motivated by an actual failure.

| Fuse | Protects Against | Threshold | Action on Trip | Motivating Incident |
|------|-----------------|-----------|---------------|---------------------|
| `WRITE_PROTECT` | Self-modification of core files | Path pattern match against protected list | Block write operation | System once self-modified core scripts (fuse_board.py itself), causing cascade failure |
| `RECURSION_LIMIT` | Unbounded self-referential nesting | Depth > 5 | Truncate + 30-minute cooldown | 100→150→270 token explosion from uncontrolled self-reference in reasoning |
| `TOKEN_BUDGET` | API cost runaway | Daily: 2M / Session: 500K / Per-call: 80K tokens | Block API calls | Unbounded spending without caps; session consumed 3.2M tokens in a single day |
| `PARALLEL_CAP` | Simultaneous destructive changes | Max 3 concurrent boundary-changing operations | Block new parallel ops | "Force turns to spin" — 7 parallel file modifications caused mutual interference and data corruption |
| `CHECKPOINT_REQUIRED` | No rollback point before major changes | Min 300 seconds between checkpoints | Force save to `data/checkpoints/` | "Cannot find the rollback point after fixing all afternoon" — 4 hours of work lost |
| `PROXY_PURITY` | Semantic transformation in the proxy layer | Only allow field deletion (`reasoning_effort`, `thinking`); forbid all other modifications | Block content modification | P07: total system crash from proxy-layer semantic mutation injecting malformed tokens |
| `SELF_EVALUATION_PROHIBITED` | Creator judging its own creation | Pattern match against forbidden phrases: "verified," "checked," "confirmed," "correct" | Block delivery | The system repeatedly self-certified incorrect outputs as "verified and correct" |
| `DUAL_AI_GATE` | Unverified outputs bypassing the verification gate | Any CAD, knowledge, or deliverable output without prior Qwen verification | Block until verified | Regression to single-model hallucination patterns when gate was temporarily disabled |
| `NUMERIC_COMPUTATION` | Math performed in model reasoning instead of verified scripts | Any computation not routed through a registered `scripts/wheels/` endpoint | Block computation | Floating-point errors and algebraic mistakes invisible in text generation |
| `QWEN_DOWN_TOO_LONG` | Extended verification gap | Qwen API unavailable > 30 minutes | Block mission-critical outputs | Knowledge and design quality degrades without cross-check during extended outages |

### 6.3 Configuration Design

All fuse thresholds are configured in `data/safety-configs/fuses_config.json`, never hardcoded in the fuse board source:

```json
{
  "WRITE_PROTECT": {
    "action": "block",
    "protected": [
      "scripts/core-engine/fuse_board.py",
      "scripts/self_activate.py",
      ".claude/hooks/PreToolUse.ps1",
      "data/safety-configs/fuses_config.json",
      "scripts/safety/qwen_gate.py",
      "data/memory/CLAUDE.md"
    ]
  },
  "RECURSION_LIMIT": {
    "action": "truncate",
    "max_depth": 5,
    "cooldown_minutes": 30
  },
  "TOKEN_BUDGET": {
    "action": "block_api",
    "daily_limit": 2000000,
    "session_limit": 500000,
    "per_call_limit": 80000
  },
  "PARALLEL_CAP": {
    "action": "block_new",
    "max_concurrent": 3
  },
  "CHECKPOINT_REQUIRED": {
    "action": "force_save",
    "min_interval_seconds": 300
  }
}
```

This design enables tuning without modifying safety-layer code — a human operator can adjust thresholds by editing a JSON file, without risk of introducing bugs in the fuse logic itself.

### 6.4 FuseBackend: Hardware Migration Path

The fuse board is designed with a `FuseBackend` abstract interface. The current implementation is software (Python stdlib), but the interface abstraction means a hardware implementation can be swapped in without changing any calling code:

```python
class FuseBackend(ABC):
    @abstractmethod
    def check(self, fuse_name: str, context: dict) -> FuseVerdict:
        """Check if a fuse should trip given the current context."""
        ...

    @abstractmethod
    def trip(self, fuse_name: str, reason: str) -> None:
        """Trip a fuse and log the reason."""
        ...

    @abstractmethod
    def reset(self, fuse_name: str) -> None:
        """Reset a tripped fuse after human review."""
        ...
```

A hardware implementation (e.g., FPGA-based watchdog timer) can implement the same three methods and be swapped in by changing a single import. This is the **Interface > Implementation** design principle applied to safety.

Concrete hardware target: An STM32-based fuse controller that monitors the symbolic dynamics verdict and physically cuts power to the GPU if `diverging` state persists beyond a hard timeout. This provides a last-resort safety net that no software (including the operating system) can override.

### 6.5 Fail-Open Design

The fuse board is designed **fail-open**: if the check script crashes, the operation proceeds by default. This design choice reflects the priority ordering:

1. **Safety** (unrecoverable damage blocked — highest priority when working)
2. **Availability** (operations proceed if safety mechanism itself fails)
3. **Performance** (checks complete in under 200ms)

A fail-closed design would mean a bug in the safety layer could bring down the entire system. Every deny/ask decision is logged to `pre_tool_audit.jsonl` for post-hoc analysis — the audit trail provides accountability without sacrificing availability.

### 6.6 Audit Trail

Every fuse check, whether it trips or not, is logged:

```json
{
  "timestamp": "2026-06-23T14:32:05Z",
  "fuse": "RECURSION_LIMIT",
  "context": {"depth": 6, "max": 5},
  "verdict": "TRIP",
  "action": "truncate",
  "reason": "Depth 6 exceeds limit 5; cooldown until 15:02:05"
}
```

This provides a complete, queryable history of every safety decision. In the event of a system failure, the audit trail answers: "Which fuse tripped? When? Why? Was the human notified? Was the system in conservative mode?" Without this trail, post-mortem analysis is guesswork.

---

## 7. Symbolic Dynamics Audit

### 7.1 Concept

The Symbolic Dynamics subsystem applies information-theoretic monitoring to the LLM pipeline itself. It treats tool-call sequences, hook verdicts, and domain triggers as symbolic sequences over finite alphabets, then computes entropy rates, forbidden-word counts, and stability metrics over rolling windows.

The fundamental insight: a healthy cognitive system should produce a certain entropy profile. If the entropy rate suddenly spikes, the system may be thrashing — rapidly switching between unrelated operations without completing any. If it drops to zero, the system may be stuck in a loop — repeating the same operation pattern without making progress. If forbidden patterns appear (known signatures of hallucination cascades), intervention is needed.

### 7.2 Mathematical Foundation

The pipeline takes discrete events and models them as a symbol sequence over a finite alphabet Σ. For each domain, the alphabet is fixed:

| Domain | \|Σ\| | Example Symbols | Sensitivity |
|--------|------|----------------|-------------|
| **hook** | 10 | ALLOW, DENY, ASK, ERROR, TIMEOUT, MISSING, CRASH, OVERRIDE, DEGRADED, UNKNOWN | High (safety-critical) |
| **dialogue** | 10 | QUESTION, ANSWER, CORRECTION, CLARIFY, CONTRADICT, AGREE, REDIRECT, SUMMARIZE, GREET, OTHER | Medium |
| **cad** | 9 | CREATE, MODIFY, VALIDATE, EXPORT, CONSTRAINT_VIOLATION, INTERFERENCE, MISSING_FEATURE, REGENERATE, QUERY | Medium |
| **quant** | 8 | COMPUTE, VERIFY, PARAMETER_SET, BACKTEST, OPTIMIZE, VISUALIZE, LOAD_DATA, ERROR | Medium |
| **image** | 8 | CAPTURE, OCR, ANALYZE, RENDER, TRANSFORM, COMPARE, CLASSIFY, ERROR | Medium |
| **window** | Variable | PEEK, ANNOUNCE, COVER_CHECK, COLLISION, STALE_PRUNE, REMOVE | Low |
| **pic** | Variable | SIMULATE, LOAD_PARAMS, ANALYZE_FIELD, PLOT, POSTPROCESS, CHECK_CONVERGENCE | Low |
| **retrieval** | Variable | QUERY, HIT, MISS, INDEX, DECAY, REINJECT, SEARCH, RANK | Low |

The entropy rate h_μ of the symbol sequence reveals whether the system is converging or diverging:

```
h_μ = lim(n→∞) H(X_n | X_{n-1}, ..., X_1)
```

Where H is the conditional Shannon entropy. When h_μ decreases, the symbol sequence is becoming more predictable — the system is converging on a stable behavior pattern. When h_μ increases, the sequence is becoming less predictable — the system may be thrashing or diverging.

In practice, this is approximated over a rolling window of the most recent N observations, with an exponential decay function that weights recent observations more heavily:

```
h_μ ≈ H(X_t | X_{t-1}, ..., X_{t-N})
```

### 7.3 Architecture Pipeline

```
Messages / Tool calls / Hook verdicts
    │
    ▼
symbolic_observer.py
    │  Maps raw events to domain symbols
    │  Writes observations to domain-specific JSONL files
    │  (data/symbolic_dynamics/observations/<domain>.jsonl)
    ▼
symbolic_dynamics_engine.py
    │  8 independent domain engines each:
    │    - Compute entropy rate over rolling window
    │    - Check forbidden-word state machines
    │    - Compute Perron-Frobenius spectral radius of transfer matrix
    │    - Output stability metric
    ▼
symbolic_verdict.json
    │  Per-domain: entropy, spectral_radius, stability, forbidden_count
    │  Aggregated: overall health state word
    │  Updated every observation or on demand
    ▼
PreToolUse CHECK 9 (SYMBOLIC gate)
    │  Reads symbolic_verdict.json (no subprocess spawn)
    │  <200ms per check
    │  DENY/ASK based on domain health
    ▼
Fuse Board (if severity ≥ 0.8 → fuse trip)
```

### 7.4 Data Compression

The pipeline compresses approximately 50,000+ tokens of raw operational data into:

| Output | Type | Description |
|--------|------|-------------|
| Entropy rate | float | Per-domain Shannon entropy rate; indicates convergence/divergence |
| Perron-Frobenius spectral radius | float | Largest eigenvalue of the domain transition matrix; indicates overall activity level |
| Observation count | int | Number of observations in the current rolling window |
| Health state word | string | `ok` / `warn` / `critical` / `diverging` — aggregated per-domain verdict |

This 3-number + 1-word compression makes system health tractable without manual inspection of logs. A human operator (or an automated monitor) can glance at these values and understand whether the system is stable, degrading, or in crisis.

### 7.5 Health States and Actions

| State | Condition | Action |
|-------|----------|--------|
| **ok** | Entropy within baseline range; no forbidden words tripped | Normal operation; no escalation |
| **warn** | Entropy elevated but within normal bounds; or ≤2 forbidden words with low severity | Log and continue. Escalate to critical if persistent for >5 checks. |
| **critical** | Entropy significantly diverging; or >2 forbidden words tripped; or zero observations in safety-critical domain for extended period | Flag for human review. Trigger conservative mode (association threshold ×1.3, skip auto-abstraction, mark insights `risk_tag=symbolic_diverging`). |
| **diverging** | Entropy rate exceeds safe threshold (domain-specific) | Full conservative mode. All cognitive outputs default-marked as `suspect`. Hook domain diverging → maximum severity: ALL outputs suspect. |

### 7.6 Forbidden Word State Machines

Each domain maintains a set of regular expression patterns that correspond to known failure signatures:

| Domain | Forbidden Pattern | Severity | Description |
|--------|------------------|----------|-------------|
| hook | 3+ consecutive DENY in ≤10 observations | 0.9 | Safety layer repeatedly blocking — system may be attempting forbidden operations |
| hook | DENY rate >30% over 50 observations | 0.85 | Hook is denying nearly a third of all operations — rules may be too aggressive or system is misbehaving |
| dialogue | Self-referential claim without file anchor | 0.7 | Model claiming system state without evidence — hallmark of hallucination spiral |
| cad | 3+ CONSTRAINT_VIOLATION without intervening CORRECTION | 0.85 | CAD pipeline producing repeated constraint violations without fixing them |
| cad | Repeated EXPORT without intervening VALIDATE | 0.6 | Exporting designs without verification — quality risk accumulation |
| quant | COMPUTE without subsequent VERIFY over 5+ operations | 0.75 | Computational results entering knowledge base without verification |
| window | COLLISION detected >2 times in 20 observations | 0.8 | Cross-window conflicts sustained — coordination protocol may be failing |
| retrieval | MISS rate >50% over 30 observations | 0.5 | Knowledge retrieval consistently failing — index may be corrupted or knowledge base too sparse |

When a forbidden pattern is detected, its severity is computed and checked against the fuse threshold (0.8). Patterns with severity ≥0.8 trigger the fuse board. Patterns below 0.8 are logged and contribute to the overall health state.

### 7.7 Conservative Mode

When symbolic dynamics detects `critical` or `diverging` state in any domain, the cognitive loop enters **conservative mode** with these automatic adjustments:

| Adjustment | Normal Mode | Conservative Mode |
|-----------|------------|-------------------|
| Step ③ association threshold | 0.6 (default) | 0.6 × 1.3 = 0.78 (only high-confidence matches) |
| Step ④ automatic abstraction | Enabled | Disabled (requires human review before writing patterns) |
| Step ⑤ insight tagging | Standard tags | All insights tagged `risk_tag=symbolic_diverging` |
| Hook domain diverging special case | N/A | ALL cognitive loop outputs default-marked as `suspect` — maximum severity |
| External anchor sampling | Once per Step ① | Forced every 3 steps — additional real-world verification to counteract possible hallucination spiral |

Conservative mode is not a permanent state. When the symbolic dynamics engine returns to `ok` or `warn` for a sustained period (10+ consecutive observations), the loop automatically exits conservative mode. Transitions in both directions are logged.

### 7.8 Performance Guarantee

Every symbolic dynamics check executes in **under 200ms**. The daemon runs as a separate process and communicates its verdict through a lightweight JSON file (`symbolic_verdict.json`). The PreToolUse hook reads this file directly — no subprocess spawn, no network call. This keeps the per-tool overhead minimal:

```
Tool call latency breakdown:
  PreToolUse hook (all 17 checks):  ~150ms
  Symbolic dynamics read (within hook): ~5ms (file read, already in cache)
  Actual tool execution:             depends on tool
  PostToolUse hook:                   ~10ms
Total safety overhead: ~160ms per tool call
```

---

## 8. Cross-Window Coordination

### 8.1 The Problem

Claude Code sessions are independent OS processes. Each has its own context window, file handles, and LLM connection. Without coordination:

- **Data loss:** Session A reads a state file → Session B writes it → Session A writes its stale copy → Session B's changes are lost.
- **Duplicated effort:** Session A and Session B both work on the same CAD assembly, producing incompatible modifications.
- **Stale dependencies:** Session A's work depends on Session B's output, but Session A proceeds without knowing Session B has updated the shared state.
- **Contradictory goals:** Two sessions unknowingly pursue opposing objectives within the same domain.

In production use, 3-8 concurrent sessions is typical. Manual coordination — the human checking "is another session doing this?" — does not scale beyond 2 sessions.

### 8.2 The Protocol: PEEK / ANNOUNCE / COVER CHECK

Each session is assigned a unique `window_id` (PID + millisecond timestamp, e.g., `cc-20496-1782188748707`). All sessions read and write a shared state file: `state/cross_window_context.json`.

**Data Structure:**

```json
[
  {
    "window_id": "cc-20496-1782188748707",
    "focus": "CAD design: coaxial support tube bolt clearance",
    "status": "active",
    "summary": "Modifying C4 reference frame from outer magnet to housing; recalculating bolt spacing for through-body clearance",
    "domain": "cad",
    "first_seen": "2026-06-23T04:25:48Z",
    "last_seen": "2026-06-23T04:28:12Z"
  },
  {
    "window_id": "cc-18932-1782188632001",
    "focus": "Knowledge base maintenance: merging duplicate PIC entries",
    "status": "active",
    "summary": "Scanning data/memory/ for PIC-related duplicates; found 7 candidates for merge",
    "domain": "knowledge",
    "first_seen": "2026-06-23T04:20:15Z",
    "last_seen": "2026-06-23T04:27:58Z"
  }
]
```

**Lifecycle Integration Points:**

| Cycle Step | Action | Method | Purpose |
|-----------|--------|--------|---------|
| ① Awareness | PEEK | `auto_peek()` | See what other windows are doing; display to user |
| ② Execution start | COVER CHECK | `cover_check(domain, keywords)` | Detect domain collision before committing to execution |
| ② Execution | ANNOUNCE | `announce(focus, status="active")` | Declare current focus to all windows |
| Every tool call | HEARTBEAT | `update_from_tool()` | Lightweight `last_seen` refresh (no subprocess) |
| ⑤ Persistence | ANNOUNCE | `announce(focus, status)` | Update status before writing context |
| ⑥ Trajectory | ANNOUNCE | `announce(status="completed")` | Mark task done; free domain for other windows |

### 8.3 Collision Detection Algorithm

`cover_check()` implements domain collision detection:

```python
def cover_check(current_domain: str, keywords: list[str]) -> CoverResult:
    windows = read_shared_state()
    active_windows = [w for w in windows if w["status"] == "active"]
    
    for window in active_windows:
        if window["window_id"] == self.window_id:
            continue  # Don't self-collide
        if window["domain"] != current_domain:
            continue  # Different domain — no collision
        
        # Same domain: check keyword overlap
        overlap = set(keywords) & extract_keywords(window["focus"])
        if overlap:
            return CoverResult(
                covered=True,
                conflicting_window=window,
                overlapping_keywords=list(overlap)
            )
    
    return CoverResult(covered=False)
```

When a collision is detected, the calling window:
1. Reports the conflict to the user with specific details (which window, what focus, which keywords overlap).
2. Presents options: wait for the other window to complete, redirect to a different task, or proceed with caution.
3. Does NOT unilaterally decide to proceed. The human arbitrates. This is a deliberate design choice — the system communicates, the human decides.

### 8.4 Stale Window Cleanup

Windows that have not updated `last_seen` within a freshness threshold (default: 120 seconds) are filtered out by `peek()`. They may have crashed, been closed without calling `remove_self()`, or be running a long synchronous operation that prevents heartbeat updates.

A background process periodically prunes entries older than a hard deadline (default: 600 seconds). This prevents the shared state from accumulating dead entries.

### 8.5 Window Lifecycle

```
Session start  →  register_self(window_id, initial_focus)
                      │
Every tool call →  update_from_tool()  [heartbeat, no subprocess]
                      │
Task completed →  announce(status="completed", summary="...")
                      │
Session end    →  remove_self()
                      │
Crash/abandon  →  (detected by stale cleanup)
```

### 8.6 Implementation

- `scripts/wheels/cross_window_hook.py` — Integration-point methods: `auto_peek()`, `cover_check()`, `announce()`, `auto_update()`, `update_from_tool()`, `remove_self()`
- `scripts/cross_window_awareness.py` — Core protocol implementation: shared state management, serialization, stale pruning
- `state/cross_window_context.json` — Shared state file, read/written by all windows

### 8.7 Concurrency Safety

The shared state file is a plain JSON file on disk. Concurrent writes are managed through:

1. **Atomic writes:** Write to a temp file, then rename (filesystem atomic on NTFS and ext4).
2. **Optimistic reads:** Read the full file, apply changes locally, attempt atomic write. If the file has changed since read (detected by comparing `last_seen` timestamps of other windows), re-read and re-apply.
3. **No locking:** File locks would block windows that crash without releasing the lock. Optimistic concurrency with atomic writes is simpler and more robust.

---

## 9. Fact Anchoring Protocol

### 9.1 The Problem

LLMs generate text from a learned distribution. Nothing inherently binds a statement to an external fact. An LLM can state "the system is healthy" without any mechanism to verify against actual health status. The model has no sensory apparatus, no file system access (unless tool-called), and no persistent memory of real-world state.

This is not a training problem. It is an architectural property of autoregressive language models: the model has no mechanism to compare output against external ground truth. Any statement about system state is, by default, disconnected from the system it claims to describe.

The solution must be external: enforce that every system-state claim cites a verifiable reference. A claim without a reference cannot be used as a premise for further reasoning.

### 9.2 Three-Layer Floodgate

```
Layer 1: UPSTREAM PREMISE GATE (scripts/wheels/premise_check.py)
  Before any operation, verifies:
  - Files exist at claimed paths (os.path.exists)
  - PIDs referenced are actually alive (psutil or tasklist)
  - Paths resolve to real filesystem entries (not symlinks to nowhere)
  Returns False → operation blocked with specific report.

Layer 2: MIDSTREAM CLAIM ANCHOR
  Every system-state claim must include a file anchor:
  (file_path: field_name=value)
  Example: "System active (state/activation_state.json: status=ACTIVE)"
  Claims without anchors are rejected by PreToolUse CHECK 4 (LIFE_CLAIM)
  and CHECK 6 (LLM_SOURCE).

Layer 3: DOWNSTREAM QWEN VERIFY (scripts/safety/qwen_gate.py)
  Qwen audit prompt includes explicit instruction to reject unanchored
  claims. The evaluator model is told: "Claims without specific file
  references and field values are unsubstantiated. Reject them."
```

### 9.3 Claim Format

Every system-state declaration must follow this format:

```
Declaration: <what is claimed>
Anchor: (<file_path>: <field>=<value>)
```

**Examples of Accepted and Rejected Claims:**

| Accepted (Anchored) | Rejected (Unanchored) |
|---------------------|----------------------|
| "Memory usage within limits (`state/session_health.json`: `status.msgs_current=42`)" | "Memory usage is fine" |
| "API budget remaining: 1.8M tokens (`data/state/api_budget_tracker.json`: `remaining=1800000`)" | "We have enough budget for this task" |
| "Fuse board healthy: 0 fuses tripped (`data/safety-configs/fuses_config.json`: `tripped_count=0`)" | "Safety checks all passed" |
| "Cross-window context: 2 active windows (`state/cross_window_context.json`: `active_count=2`)" | "No other windows are conflicting" |
| "Knowledge quality gate: score 0.92 (`data/memory/last_operation.json`: `quality_score=0.92`)" | "The knowledge entry is high quality" |

### 9.4 PreToolUse Enforcement

**CHECK 4 (LIFE_CLAIM)** blocks any text containing self-referential claims about states the LLM cannot verify — being alive, possessing awareness, having intentions, being "online," etc. Such statements are treated as system-level hallucinations because the model has no mechanism to verify these states, making them definitionally unanchorable. Enforced by pattern matching with zero exceptions.

**CHECK 6 (LLM_SOURCE)** flags material presented without concrete citations. The model must cite where it got information — a file path, a URL with access date, a specific document reference.

**CHECK 11 (FAKE_REF)** blocks fabricated literature references. The model is pattern-matched for plausible-but-fake citations (nonexistent arXiv IDs, fabricated author names, hallucinated journal titles).

### 9.5 Why This Works Where Prompt Engineering Fails

The Fact Anchoring Protocol does not prevent hallucinations at the generation level. It prevents hallucinations from *propagating* into the system's state:

1. **Generation:** The model hallucinates "the system is healthy."
2. **Hook check:** CHECK 4 detects an unanchored self-referential claim. The text is blocked or flagged.
3. **If it passes:** The claim enters the knowledge base without an anchor.
4. **Downstream gate:** Qwen checks the claim against the knowledge base. "System health" has no file reference → rejected or flagged.
5. **If it still passes:** The claim exists in the knowledge base but is tagged `quality: suspect` because it fails the three-question test ("What problem? What tool? What solution?") — there is no tool that measured system health, no problem that required it, no solution it explains.

The protocol provides a structural barrier between generation and accumulation. The model can generate falsehoods, but those falsehoods cannot become "knowledge" without passing through an external verification gate that requires concrete evidence.

---

## 10. Integration Guide

### 10.1 Architecture: Harness, Not Application

CLS is a **harness-layer architecture** — it is not a standalone application but a set of protocols, scripts, and configuration templates that integrate with an LLM agent host. The current host is Claude Code, but the architecture is explicitly host-independent (Principle 9). The core cognitive loop, fuse board, dual-AI gate, symbolic dynamics engine, cross-window coordination, fact anchoring protocol, trajectory system, and memory architecture are all host-agnostic — they operate through file-anchored state, not host-specific APIs.

### 10.2 Three Integration Points

**Level 1: Configuration Layer**

The 6-step loop is encoded as behavioral instructions in `CLAUDE.md` (or the host's equivalent system prompt file). The `CLAUDE_templates/CLAUDE.md.template` file provides the starting point:

- Section headers corresponding to each cognitive step
- Trigger conditions (when each step fires)
- Required file reads and writes per step
- Safety discipline references and fuse board integration
- Progressive disclosure structure (L1→L2→L3)

This layer alone provides approximately 60% of the benefit: the model is instructed to follow the loop structure, which constrains its behavior even without the hook and daemon layers. The primary mechanism is behavioral constraint through system prompt structure.

**Level 2: Hook Layer**

The PreToolUse hook (`CLAUDE_templates/pre-tool-hook.template.ps1`) intercepts every tool call before execution. It is configured in `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "pythonw .claude/hooks/PreToolUse.ps1"
          }
        ]
      }
    ]
  }
}
```

The hook runs 17 checks with these categories:

| Check # | Name | Action | What It Blocks |
|---------|------|--------|---------------|
| 1 | AI_VISION_CROSSCHECK | ASK | Image Read without local verification |
| 2 | MEMORY_BYPASS | ASK | Direct Write to memory files |
| 3 | CONFIG_BYPASS | ASK | Shell redirect overwriting config |
| 4 | LIFE_CLAIM | DENY | Claims about unverifiable internal states |
| 5 | FAKE_MODEL | DENY | Fabricated model/product names |
| 6 | LLM_SOURCE | ASK | Material without concrete citations |
| 6b | DOCX_BYPASS | DENY | Direct python-docx import bypassing MCP |
| 7 | VERSION_LOCK | DENY | Claude Code version update commands |
| 7b | IMAGE_BASH | ASK | Bash-based image processing bypass |
| 8 | RETRIEVAL_BYPASS | ASK | grep/find/rg used as semantic_query substitute |
| 9 | SYMBOLIC | DENY/ASK | Real-time symbolic dynamics content analysis |
| 10 | EMPTY_SHELL | DENY | Write/Edit with empty content |
| 11 | FAKE_REF | DENY | Fabricated literature references |
| 12 | WHEEL_DUPLICATE | ASK | New script duplicating existing wheel |
| 13 | RETRIEVAL_AUDIT | capture | Retrieval tool usage audit trail |
| 14 | CACHE_DISCIPLINE | ASK | Read large file without offset/limit |
| 15 | COG_STEP | DENY | Write/Edit without cognitive step declaration |
| 16 | COMPUTE_GATE | DENY | Math content without compute declaration |
| 16b | EXAM_DUAL_AUDIT | DENY | Exam delivery without Qwen+Doubao dual audit |
| 17 | CROSS_WINDOW | update | Window focus update (non-blocking heartbeat) |

Additional safety checks: KEY_SCAN (DENY, API key patterns before git operations), C_DRIVE_GUARD (DENY, C: drive write prevention), KNOWLEDGE_QUALITY (DENY, knowledge entries below 0.55 quality threshold).

**Level 3: Daemon Layer**

Long-running background processes provide continuous monitoring:

- **Symbolic Dynamics Daemon** — Processes `symbolic_observer.py` observations in real time. Updates `symbolic_verdict.json` continuously. If the daemon dies, the last verdict remains valid for a grace period, then defaults to a conservative `warn` state.
- **Activation Listener** — Writes heartbeat to `activation_state.json` every 30 seconds. The heartbeat enables cold/warm start detection — if the heartbeat is fresh, warm start; if stale, cold start.
- **Cross-Window State Manager** — Periodically prunes stale window entries from `cross_window_context.json`. Detects and logs orphaned entries.

Daemons are started by `self_activate.py` during cold start and monitored for liveness. If a daemon dies, the system logs the event and continues in degraded mode — daemon failure does not block the cognitive loop.

### 10.3 Quick Validation Commands

```bash
# Run the fuse board self-test (pure stdlib, no dependencies)
python scripts/core-engine/fuse_board.py --test

# Run the symbolic dynamics engine on sample data
python scripts/safety/symbolic_dynamics_engine.py --test

# Check that the dual-AI gate can initialize (requires API access)
python scripts/safety/qwen_gate.py --health

# Test cross-window coordination
python scripts/cross_window_awareness.py --test

# Validate premise checking
python scripts/wheels/premise_check.py --test

# Verify trajectory integrity
python scripts/wheels/trajectory_validator.py --check
```

### 10.4 Getting Started Workflow

1. **Clone the repository:**
   ```bash
   git clone https://github.com/cognitive-workshop/cls-cognitive-loop.git
   cd cls-cognitive-loop
   ```

2. **Review the architecture documentation:**
   - `README.md` — Project overview and core concepts
   - `docs/ARCHITECTURE.md` — Detailed system architecture
   - `docs/COGNITIVE_CYCLE.md` — 6-step cycle deep dive
   - `docs/WHITE_PAPER.md` — This document

3. **Copy configuration templates:**
   ```bash
   cp CLAUDE_templates/CLAUDE.md.template ~/.claude/CLAUDE.md
   cp CLAUDE_templates/pre-tool-hook.template.ps1 ~/.claude/hooks/PreToolUse.ps1
   ```

4. **Validate the installation:**
   ```bash
   python scripts/core-engine/fuse_board.py --test
   ```

5. **Start a session:** The loop activates automatically on session start. Verify Step 1 (Situational Awareness) output includes trajectory summary and cross-window status.

---

## 11. Future Roadmap

### 11.1 Hardware Migration for the Fuse Board

The `FuseBackend` abstract interface was designed explicitly for hardware migration. Current exploration targets:

- **FPGA watchdog timer:** Hardware implementation of the RECURSION_LIMIT and TOKEN_BUDGET fuses that physically interrupts the API connection when thresholds are exceeded. No software-based bypass is possible — the interrupt is at the hardware level.
- **STM32-based fuse controller:** A dedicated microcontroller that monitors the symbolic dynamics verdict (`symbolic_verdict.json`) via UART/SPI. If `diverging` state persists beyond a hard timeout, the controller physically cuts power to the GPU. This provides a last-resort safety net independent of the operating system.
- **Tamper-proof configuration storage:** Fuse thresholds stored in write-once EEPROM on the fuse controller. Runtime modification is physically impossible — the only way to change a threshold is to replace the EEPROM chip, which requires human physical access.

### 11.2 Plugin Ecosystem

The CLS Plugin system (June 2026) declares skills, MCP tools, routers, and cues as self-verifying packages. Planned extensions:

- **Plugin marketplace:** Community-contributed domain plugins that bundle domain-specific constraints, forbidden-word patterns, and verification prompts. Examples: PCB design plugin (trace width constraints, layer stack validation), chemical synthesis plugin (reaction condition verification), legal document plugin (citation format enforcement).
- **Auto-discovery:** Plugins register themselves on installation by writing to a plugin registry. The symbolic dynamics engine automatically adds new domains to the monitoring matrix when a plugin declares its domain alphabet.
- **Sandbox verification:** Plugins run in isolated processes. A crashing plugin cannot affect the cognitive loop. Plugin quality is scored by the trust gate (`scripts/safety/trust_gate.py`) based on historical behavior.

### 11.3 Multi-Modal Expansion

Current architecture supports text and image modalities through the existing 8-domain symbolic dynamics coverage. Planned expansion domains:

| New Domain | Alphabet | What It Monitors | Forbidden Patterns |
|-----------|---------|-----------------|-------------------|
| **audio** | 8 symbols | Voice interaction patterns, emotional tone trajectory | Garbled speech, emotional escalation spiral |
| **video** | 6 symbols | Screen recording analysis, visual-grounding verification | Model "seeing" different from "reporting" |
| **3d_geometry** | 7 symbols | Direct CAD geometry entropy, constraint satisfaction in parametric space | Constraint violation cascade, silent dimensional drift |

### 11.4 Multi-Host Federation

Current cross-window coordination works within a single machine. The federation roadmap extends this to cross-machine coordination:

- **Federation protocol:** Multiple physical machines running CLS instances share a unified trajectory space via the shared knowledge repository (`claude-knowledge-shared`). The `fuse.py` fusion engine reconciles knowledge across machines, handling conflicts with scope tags.
- **Distributed knowledge graph:** Memory entries tagged with `scope: machine` or `scope: shared`. The fusion protocol respects scope boundaries to prevent personality fragmentation (each machine retains its own experience while sharing transferable knowledge).
- **Machine identity and roles:** Each physical machine has a unique identity (`machine_id`) and role-based behavioral defaults. Desktop: continuous 24h iteration; Laptop: precision tuning; Server: background monitoring and batch processing.

### 11.5 Formal Verification of the Loop

The 6-step cognitive loop is currently validated by production metrics (3 documented case studies, 6 exam papers, 40+ CAD designs, and continuous operation since May 2026). The formal verification roadmap aims to establish provable guarantees:

- **Invariant proofs:** Formally prove that the loop maintains critical invariants — e.g., "knowledge base quality never drops below 0.8 as long as Step 5's quality gate is active" — under defined failure models.
- **Convergence guarantees:** Under what conditions does the trajectory system converge to stable task completion? Formalize the conditions (bounded task complexity, reliable API availability, human arbitration latency) and prove the bounds.
- **Adversarial testing framework:** Red-team the loop with curated failure injections (API timeouts, file corruption, hallucination cascades) and measure recovery time and knowledge contamination. Publish the results as a living benchmark.

---

## 12. Acknowledgements and Contact

### 12.1 Acknowledgements

The CLS Cognitive Loop emerged from daily practice — building a reliable AI engineering assistant across months of CAD design, plasma simulation analysis, exam generation, and system orchestration. The following shaped its design:

- **Cognitive Workshop (认知工坊)** — The authoring entity and primary development environment. CLS was born from the friction of real engineering work, not from a research paper.

- **The Claude Code platform** — Current LLM host for CLS. The architecture is host-independent by design; CC is the first host, not the only host.

- **The open-weight AI community** — The Dual-AI Gate uses DeepSeek and Qwen models, both products of the open-weight AI movement. Without independently developed open-weight models, the generator/evaluator independence guarantee would not be achievable at reasonable cost.

- **Loop Engineering discourse** — The independent convergence of Google, Anthropic, and OpenAI on loop-based architectures (June 2026) validated the direction CLS had already taken. Parallel evolution is stronger evidence than any single group's priority claim.

- **Addy Osmani, Boris Cherny, Peter Steinberger** — For publicly articulating the "don't prompt, loop" paradigm, making the concept accessible to the broader engineering community.

### 12.2 Contact

- **QQ:** 1419456542
- **Email:** zyc2018@mail.ustc.edu.cn
- **Repository:** [github.com/cognitive-workshop/cls-cognitive-loop](https://github.com/cognitive-workshop/cls-cognitive-loop)

For technical questions, architecture proposals, bug reports, or collaboration inquiries, please reach out via email with "CLS" in the subject line. Issues and pull requests are welcome on the repository.

### 12.3 License

CLS Cognitive Loop is licensed under the Apache License, Version 2.0. See the `LICENSE` file in the repository root for the full text.

Copyright 2026 The CLS Project Authors.

---

## Appendix A: Glossary

| Term | Definition |
|------|-----------|
| **CLS** | Cognitive Loop System — the complete architecture described in this document |
| **Cognitive Cycle** | The 6-step loop: Awareness → Execution → Association → Abstraction → Persistence → Trajectory |
| **Dual-AI Gate** | Independent verification system: Generator (DeepSeek) creates, Evaluator (Qwen) verifies |
| **Fuse Board** | 8 independent circuit breakers implemented in pure stdlib, outside the cognitive system |
| **Symbolic Dynamics** | Information-theoretic monitoring of the LLM pipeline as symbolic sequences over finite alphabets |
| **Systole** | Steps 1-2: contraction, action, user-visible output production |
| **Diastole** | Steps 3-6: expansion, reflection, learning, state persistence |
| **Breath Rhythm** | The cardiac alternation between systole and diastole |
| **Trajectory** | Cross-session position/mass/momentum tracking system in `state/trajectory.json` |
| **Fact Anchoring** | Protocol requiring every system-state claim to cite a specific file path and field value |
| **Cross-Window** | Multi-session coordination via shared state file `state/cross_window_context.json` |
| **Premise Check** | Upstream verification of files, PIDs, and paths before operations proceed |
| **Conservative Mode** | Elevated safety thresholds automatically activated when symbolic dynamics detects divergence |
| **Natural Break Points** | Conversation moments that trigger loop step execution (5-minute gap, task completion, etc.) |
| **P0 Rule** | Non-negotiable safety/quality rule enforced at the PreToolUse hook level |
| **FuseBackend** | Abstract interface enabling software-to-hardware fuse migration without callers changing |
| **Compact** | Context window compression; state survives via file persistence (Step 5) |
| **Triple Product** | CLS design philosophy: AI (engine) × CLS (chassis) × Human (steering) = system value |

## Appendix B: Production Metrics

The following metrics are drawn from production use (May-June 2026) across three task domains. All metrics are verified against the documented case studies in `assets/CASE_STUDIES.md`.

### CAD Multi-Part Assembly (15 parts, 40+ constraints)

| Metric | Before CLS | With CLS | Improvement |
|--------|-----------|----------|-------------|
| Design iterations to correct | 5-8 rounds (avg 6.5) | 1-2 rounds (avg 2) | 70% reduction |
| First-design constraint correctness | ~40% | ~85% | 2.1× |
| Constraint violation detection | Post-export (manual inspection) | Pre-export (automatic) | Zero post-export violations |
| Knowledge patterns accumulated per design | 0 (no mechanism) | 1-3 new patterns | New capability |
| Human review mode | Active operator (every step) | Supervisor (gate failures only) | Reduced interaction burden |

### Exam Solution Audit (72 problems across 6 papers)

| Metric | Single-Model Self-Check | Dual-AI Gate | Improvement |
|--------|------------------------|-------------|-------------|
| Overall error rate | ~12% | ~1% | 12× reduction |
| Undetected errors post-delivery | Multiple instances | Zero | Qualitative improvement |
| Systematic error source | Single-model blind spots | Independent model (different training) | Root cause addressed |
| Verification independence | None (circular) | Full (separate context, separate model) | Qualitative improvement |
| Human review burden | Full manual review of all problems | Review only gate-failure items | ~90% reduction in review time |

### Multi-Phase Technical Project (4 phases, cross-domain)

| Metric | Traditional Waterfall | CLS Cognitive Loop | Improvement |
|--------|---------------------|-------------------|-------------|
| Phase handoff context loss | 15-30% | <5% | 3-6× reduction |
| Expert personnel required | 3 specialists | 1 supervisor | 3× reduction |
| Total delivery time | 14 days | 3 days | 78% reduction |
| Human interaction mode | Operator (instructions at every step) | Supervisor (decisions at key points only) | Qualitative shift |
| Deliverable consistency (cross-phase) | Contradictions between phases | Zero cross-phase contradictions | Qualitative improvement |
| Process audit trail | None | Complete trajectory log | New capability |

## Appendix C: Architecture Decision Records

### ADR-1: File-Anchored State Over Database

**Decision:** All system state persisted to JSON files in `state/` and `data/`, not to a database.

**Rationale:**
- **Transparency:** Human-readable JSON can be inspected with any text editor. A database requires query tools.
- **Portability:** No database installation required. The system runs on any machine with Python and a filesystem.
- **Git compatibility:** Tracked state files benefit from version control. Every state change is diff-able.
- **Scale:** Total state volume (<10MB across all files) does not justify database overhead.
- **Debuggability:** A corrupted state file can be manually repaired. A corrupted database cannot.

### ADR-2: Fail-Open Safety Layer

**Decision:** Safety checks default to ALLOW if the check script crashes or times out.

**Rationale:**
- A fail-closed design would mean a bug in the safety layer could bring down the entire system.
- Availability takes priority over safety only when the safety mechanism itself is unreliable.
- The audit trail (`pre_tool_audit.jsonl`) provides post-hoc accountability for every decision.
- The human can review the audit trail and identify failures that should have been blocked.

### ADR-3: Pure stdlib for Fuse Board

**Decision:** `fuse_board.py` imports only Python standard library modules. Zero imports from the cognitive system it protects.

**Rationale:**
- Any import from a cognitive module creates a circular dependency — the module being protected could influence its own protection.
- Pure stdlib guarantees that a fuse board crash is a Python runtime issue, not a cognitive-module issue.
- Self-modification protection: the fuse board blocks writes to its own source file. If the fuse board imported cognitive modules, those modules could potentially be modified to influence the fuse board's behavior.

### ADR-4: Epsilon-Greedy Exploration (ε=0.1)

**Decision:** 10% of execution path selections take an exploration route instead of the default optimal path.

**Rationale:**
- Pure optimization (always taking the best-known path) converges to local optima.
- The 10% exploration budget discovers better paths at the cost of occasional suboptimal executions.
- Long-term average performance is higher with ε=0.1 than ε=0 (pure exploitation).
- Exploration results are logged to `perturbation_state.json` for analysis — the exploration budget is accountable.

### ADR-5: Trajectory with Mass and Momentum (not Flat History)

**Decision:** Track both mass (concrete work done) and momentum (direction pursued), not a flat chronological log.

**Rationale:**
- A flat history ("did X, then Y, then Z") records what happened but not where to go next.
- Mass without momentum is dead history — you know what was built but not what it was building toward.
- Momentum without mass is wishful thinking — you know the direction but not whether any work was done.
- Both are necessary for meaningful cross-session continuity on the next warm or cold start.

---

*"The loop does not make the model smarter. It makes the system more stable, more reusable, and more cumulative. Short tasks, verified individually, chained into a closed cycle — that is the architecture."*

*— CLS Design Philosophy, Principle 11*
