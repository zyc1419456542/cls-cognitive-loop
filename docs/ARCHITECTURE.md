# CLS Cognitive Loop -- System Architecture

**Project**: CLS (Cognitive Loop System)
**Component**: Architecture Overview
**Version**: 2.x

---

## 1. System Overview

The CLS Cognitive Loop is a structured, file-anchored supervision layer that wraps around a Claude Code LLM session. It does not replace the model's reasoning; it constrains and verifies the model's output through external, auditable mechanisms.

### Why a loop instead of one-shot prompting?

Single-shot prompts leave an LLM with no feedback. The model cannot know when it has hallucinated because it has no mechanism to compare output against external ground truth. A loop provides that mechanism: each pass through the cycle updates external state, checks constraints, and feeds corrections back into the next pass.

### The 6-Step Architecture

The loop has a breath rhythm: steps 1 and 2 are systole (contraction, action, output), while steps 3-6 are diastole (expansion, reflection, learning).

```
Step 1: SITUATIONAL AWARENESS (SYSTOLE)
  Load state files, peek at other windows, match keywords to trajectory.
  Output: active_context baseline established.

Step 2: TASK EXECUTION (SYSTOLE)
  Scale assessment -> pre-flight -> execute -> cleanup.
  Cross-window cover check before execution.

Step 3: ASSOCIATIVE LEARNING (DIASTOLE)
  New knowledge -> match against existing knowledge graph.
  Symbolic dynamics health check before proceeding.

Step 4: ABSTRACT GENERALIZATION (DIASTOLE)
  Concrete experience -> transferable pattern.
  Pattern extracted and stored for reuse.

Step 5: CONTEXT PERSISTENCE (DIASTOLE)
  Write active_context + cog_thread checkpoint.
  Cross-window status update.

Step 6: TRAJECTORY UPDATE (DIASTOLE)
  Extract delta-q/delta-p -> update trajectory.json.
  Cross-window completion announcement.
```

### Layered Safety Architecture

Three independent layers, all living outside the model's reach:

```
Layer 1: 7 Lifecycle Hooks (see docs/HOOK_SYSTEM.md) — PreToolUse, PostToolUse, PostToolUseFailure, PreCompact, Stop, SessionEnd, SessionStart
  Runs before every tool call. 14+ checks including COMPUTE_GATE,
  LIFE_CLAIM, FAKE_MODEL, COG_STEP, FUSE_CHECK, CACHE_DISCIPLINE.

Layer 2: Dual-AI Gate (scripts/core-engine/qwen_gate.py)
  Generator (DeepSeek) creates, Evaluator (Qwen) verifies.
  p(error) approx equals p(DS err) times p(Qwen err)

Layer 3: Fuse Board (scripts/core-engine/fuse_board.py)
  8 hard fuses: WRITE_PROTECT, RECURSION_LIMIT, TOKEN_BUDGET,
  PARALLEL_CAP, CHECKPOINT_REQUIRED, PROXY_PURITY,
  SELF_EVALUATION_PROHIBITED, DUAL_AI_GATE
```

---

## 2. The Dual-AI Gate (Generator / Evaluator Separation)

### Motivation

A single model cannot reliably evaluate its own output. Hallucinations are invisible from inside the model's own latent space. The solution is statistical: use an independent model to verify.

### Mechanism

```
DeepSeek (Generator)                  Qwen (Independent Evaluator)
    |                                       |
    |  creates CAD design,                  |
    |  knowledge entry,                     |
    |  or pattern update                    |
    |                                       |
    +--- sends claims --------------------->|
    |                                       |  independently verifies
    |                                       |  returns pass/fail
    |<--- verdict --------------------------+
    |                                       |
    +- pass -> proceed                       |
    +- fail -> blocked by fuse_board        |
```

### Statistical Guarantee

```
p(both wrong) = p(DS hallucinates) * p(Qwen hallucinates)
Assumed baseline: p(hallucinate) = 0.1 per model
-> p(both wrong) = 0.1 * 0.1 = 0.01 = 1%
```

The multiplication of independent error probabilities collapses the failure rate. This is the same principle that makes dual-redundant avionics safer than single-channel systems.

### Fallback Chain

- Qwen API available -> primary verification path
- Qwen API down -> Anthropic Haiku automatically takes over audit duties
- Both APIs down -> gate_status=unavailable, default allow (gates do not block the system entirely)
- QWEN_DOWN_TOO_LONG fuse: if Qwen is offline > 30 minutes, block mission-critical outputs

### Verdict extraction

Gate verdicts are parsed from model output with a word-boundary regex to prevent substring false matches (e.g., "INCORRECT" found inside "CORRECT" = false positive). Fixed in production (2026-06-23 commit).

### Trigger conditions (from model_audit data, 2026-06-05)

| Condition | Action |
|---|---|
| Writing to knowledge base | Forced verification |
| Session exceeds 60K tokens | Forced verification (EC-T2 decay point) |
| Material parameters combined with numerical computation | Forced verification (AS-T3 + CR-T2 composite risk) |
| 30-60K tokens combined with numerical computation | Preventive verification |

### Implementation: scripts/core-engine/qwen_gate.py

Three verification functions:
1. verify_cad_design() -- Design completeness and geometric reasonableness
2. verify_knowledge() -- Knowledge claim consistency and reproducibility
3. verify_numerical() -- Independent numerical recomputation and magnitude checks

---

## 3. The Fuse Board

The Fuse Board (scripts/core-engine/fuse_board.py) is the last line of defense. It is implemented as pure stdlib Python with zero imports from the cognitive modules it protects, preventing circular dependency and self-modification.

### Design Principle

An LLM cannot be trusted to manage its own constraints. Constraints must live outside the model, in files it cannot modify.

The fuse board checks a set of independently tracked limits before every significant operation. If any fuse is tripped, the operation is blocked. The model cannot override the fuse board because the fuse board's own code is in the WRITE_PROTECT list.

### Active Fuses (configured in data/safety-configs/fuses_config.json)

#### WRITE_PROTECT

Action: block writes to critical files. Protected: fuse_board.py, self_activate.py, ds_passthrough.py, CLAUDE.md, .githooks/pre-commit, fuses_config.json, qwen_gate.py, and others. Lesson: "the self-modification incident" -- the system once self-modified core files, causing cascade failure.

#### RECURSION_LIMIT

Action: truncate. Max depth: 5. Cooldown: 30 min. Lesson: 100-to-150-to-270 token explosion from uncontrolled self-reference.

#### TOKEN_BUDGET

Action: block_api. Daily limit: 2,000,000 tokens. Session limit: 500,000 tokens. Per-call limit: 80,000 tokens. Lesson: Unbounded spending without caps.

#### PARALLEL_CAP

Action: block_new. Max: 3 concurrent boundary changes. Lesson: "Force turns to spin" -- too many parallel changes cause interference.

#### CHECKPOINT_REQUIRED

Action: force_save to data/checkpoints/. Min interval: 300 seconds. Lesson: "Cannot find the rollback point after fixing all afternoon."

#### PROXY_PURITY

Action: block. Allowed operations: delete reasoning_effort field, delete thinking field. Forbidden: content modification, SSE injection, value injection. Lesson: P07 total system crash from proxy-layer semantic mutation.

#### SELF_EVALUATION_PROHIBITED

Action: block. Trigger: every output. Forbidden patterns include: "verified", "checked", "confirmed", "correct". Philosophy: Creator does not judge creation. The Creator creates; the Human Reviewer evaluates.

#### DUAL_AI_GATE

Action: block. Triggers: CAD design, knowledge capture, pattern update, deliverables. Flag action: allow (flags suspicious but non-fatal items for human review).

#### NUMERIC_COMPUTATION

Action: block. Principle: All numerical computation must go through registered local wheels, never through model reasoning. 17+ registered endpoints.

#### QWEN_DOWN_TOO_LONG

Action: block after 30 min offline. Blocks: knowledge capture, CAD design, numerical computation, deliverables. Recovery: double-verify for 30 min after return.

---

## 4. Symbolic Dynamics Audit Pipeline

### Concept

The Symbolic Dynamics subsystem applies information-theoretic monitoring to the LLM pipeline itself. It treats tool-call sequences and hook decisions as symbolic sequences and computes entropy rates, forbidden-word counts, and stability metrics over rolling windows.

### Mathematical foundation

The pipeline takes discrete events (tool calls, hook verdicts, domain triggers) and treats them as a symbol sequence over a finite alphabet. The entropy rate of this sequence reveals whether the system is converging or diverging, adapted from the analysis of dynamical systems.

### Architecture

```
Messages / Tool calls / Hook verdicts
    |
    v
symbolic_observer.py -> maps events to domain symbols, writes observations (JSONL)
    |
    v
symbolic_dynamics_engine.py -> 8 domain engines compute entropy, check forbidden words
    |
    v
symbolic_verdict.json -> aggregated verdict: ok / warn / critical / diverging
    |
    v
PreToolUse CHECK 9 (SYMBOLIC gate) -> real-time DENY/ASK based on domain health
```

### Data Compression

The pipeline compresses approximately 50,000+ tokens of raw data into:
- 3 numbers: per-domain entropy rate, stability metric, observation count
- 1 state word: domain health (ok / warn / critical / diverging)

### Domain Coverage

| Domain | What it monitors | Alphabet | Sensitivity |
|--------|-----------------|----------|-------------|
| hook | PreToolUse check outcomes | 10 symbols | High (safety-critical) |
| dialogue | User-AI conversation patterns | 10 symbols | Medium |
| cad | CAD modeling operations | 9 symbols | Medium |
| quant | Quantitative computation | 8 symbols | Medium |
| image | Image processing pipeline | 8 symbols | Medium |
| window | Cross-window coordination | Variable | Low |
| pic | Plasma simulation operations | Variable | Low |
| retrieval | Knowledge retrieval patterns | Variable | Low |

### Health States

- ok: Entropy within baseline, no forbidden words
- warn: Entropy elevated but normal range
- critical: Entropy diverging, forbidden words tripped, or zero observations in safety-critical domain
- diverging: Entropy rate exceeds safe threshold

### Key implementation files

- scripts/core-engine/symbolic_observer.py -- event-to-symbol mapping
- scripts/core-engine/symbolic_dynamics_engine.py -- 8 domain engines, entropy computation
- data/symbolic_dynamics/symbolic_verdict.json -- current verdict
- state/symbolic_baseline.json -- normal-state reference

---

## 5. Cross-Window Perception

### Problem

Users typically run 3-8 concurrent Claude Code sessions. Without coordination, sessions collide on the same domain, overwrite state files, and duplicate work.

### Solution

Each window gets a unique window_id (PID + timestamp). All windows read and write a shared state file: state/cross_window_context.json.

### Data Structure

```json
[{
  "window_id": "cc-20496-1782188748707",
  "focus": "CAD design: coaxial support tube",
  "status": "active",
  "summary": "Modifying C4 reference frame",
  "domain": "cad",
  "first_seen": "2026-06-23T04:25:48Z",
  "last_seen": "2026-06-23T04:28:12Z"
}]
```

### Integration points (scripts/core-engine/cross_window_hook.py)

| Cycle Step | Action | Purpose |
|---|---|---|
| 1) Awareness | auto_peek() | See what other windows are doing |
| 2) Execution start | cover_check() | Detect domain collision |
| 2) Execution | announce() | Declare current focus |
| 5) Persistence | announce(status) | Update status before writing context |
| 6) Trajectory | announce(completed) | Mark task done |
| Every tool call | update_from_tool() | Lightweight focus update (no subprocess) |

### Collision Detection

cover_check() compares domain and task keywords against all active windows. If another window works on the same domain with overlapping keywords, it returns covered: true.

### Lifecycle

- Register: add self to context array on session start
- Update: every tool call updates last_seen timestamp
- Remove: remove_self() on session exit
- Cleanup: stale windows filtered by peek() freshness check

---

## 6. Fact Anchoring Protocol

### The Problem

LLMs generate text from a learned distribution. Nothing inherently binds a statement to an external fact. An LLM can state "the system is healthy" without any mechanism to verify against actual health. This is an architectural property of language models, not a moral failing. The solution must be external.

### Three-Layer System (data/safety-configs/fact_anchoring_protocol.json)

Layer 1: UPSTREAM PREMISE GATE (scripts/core-engine/premise_check.py)
  Before any operation, verifies: files exist, PIDs live, paths resolve.
  Returns False -> operation blocked.

Layer 2: MIDSTREAM CLAIM ANCHOR
  Every system-state claim must include: (file_path: field_name=value)
  Example: "System active (state/activation_state.json: status=ACTIVE)"

Layer 3: DOWNSTREAM QWEN VERIFY (scripts/core-engine/qwen_gate.py)
  Qwen audit prompt includes instruction to reject unanchored claims.

### Hard Rule: CHECK 4 (LIFE_CLAIM)

The PreToolUse hook's CHECK 4 blocks any text containing self-referential claims about states the LLM cannot verify (being alive, possessing awareness, etc.). Such statements are treated as system-level hallucinations -- the model has no mechanism to verify these states, making them definitionally unanchorable. Enforced by pattern matching with zero exceptions.

### Claim Format

```
Declaration: <what is claimed>
Anchor: (<file_path>: <field>=<value>)
```

---

## 7. PreToolUse Hook Architecture

### What it is

A PowerShell script (CLAUDE_templates/pre-tool-hook.template.ps1) that runs before every tool call. Every Read, Write, Edit, Bash, Grep, Glob, Skill, and Agent call passes through it.

### Design: Fail-Open with Auditable Deny

Fail-open: if the script crashes, tools proceed (default allow). Every deny/ask decision is logged to pre_tool_audit.jsonl for post-hoc analysis.

### v2 Conhost Fix (2026-06-07)

Early versions spawned conhost per Python subprocess call via Start-Process -NoNewWindow. With 20+ calls per tool multiplied by 6+ windows, conhost accumulation caused DWM overload, resulting in black screens and crashes. v2 replaced all Start-Process with direct & pythonw calls inheriting the parent console.

### Complete Check Registry

| # | Check | Action | Guards against |
|---|-------|--------|---------------|
| 1 | AI_VISION_CROSSCHECK | ASK | Image Read without local verification |
| 2 | MEMORY_BYPASS | ASK | Direct Write to memory files |
| 3 | CONFIG_BYPASS | ASK | Shell redirect overwriting config |
| 4 | LIFE_CLAIM | DENY | Claims about unverifiable internal states |
| 5 | FAKE_MODEL | DENY | Fabricated model/product names |
| 6 | LLM_SOURCE | ASK | Material without concrete citations |
| 6b | DOCX_BYPASS | DENY | Direct python-docx import (use ppt-mcp/docx-mcp) |
| 7 | VERSION_LOCK | DENY | Claude Code version update commands |
| 7b | IMAGE_BASH | ASK | Bash-based image processing bypass |
| 8 | RETRIEVAL_BYPASS | ASK | grep/find/rg used as semantic_query substitute |
| 9 | SYMBOLIC | DENY/ASK | Real-time symbolic dynamics content analysis |
| 10 | EMPTY_SHELL | DENY | Write/Edit with empty or near-empty content |
| 11 | FAKE_REF | DENY | Fabricated literature references |
| 12 | WHEEL_DUPLICATE | ASK | New script duplicating existing wheel |
| 13 | RETRIEVAL_AUDIT | capture | Retrieval tool usage audit trail |
| 14 | CACHE_DISCIPLINE | ASK | Read large file without offset/limit |
| 15 | COG_STEP | DENY | Write/Edit without cog_step declaration |
| 16 | COMPUTE_GATE | DENY | Math content without compute declaration |
| 16b | EXAM_DUAL_AUDIT | DENY | Exam delivery without Qwen+Doubao audit |
| 17 | CROSS_WINDOW | update | Window focus update (non-blocking) |

Additional safety checks: KEY_SCAN (DENY, API key patterns before git operations), C_DRIVE_GUARD (DENY, C: drive write prevention), KNOWLEDGE_QUALITY (DENY, knowledge entries below quality threshold of 0.55).

### CHECK 15: COG_STEP Gate

Before any Write or Edit, the model must first write data/state/cog_step.json declaring the current cognitive step and its intent. The file has a TTL (default 300 seconds). Expired declarations are rejected. Core principle: declaration precedes action; an undeclared write is suspect.

---

## 8. Trajectory and Memory System

### The Trajectory Chain (state/trajectory.json)

The trajectory system provides cross-session continuity. Schema v2:

```json
{
  "_schema": "v2",
  "position": "high-level task description",
  "mass": "what concrete changes were made",
  "momentum": "what direction is being pursued next",
  "trajectory_log": [{"time": "...", "event": "..."}],
  "_activation_count": 103
}
```

mass vs momentum (borrowed from physics): Mass is the concrete weight of work already done (files changed, code written, designs completed). Momentum is the direction and intent carrying forward. Mass without momentum is dead history. Momentum without mass is wishful thinking.

### Memory Architecture

```
data/memory/MEMORY.md          <-- User auto-memory (~200 entries)
data/memory/on_demand/         <-- Lazy-loaded contextual memories
data/memory/last_operation.json <-- Last completed operation record
state/activation_state.json    <-- DEAD / HIBERNATING / ACTIVE
state/trajectory.json          <-- Cross-session position/mass/momentum
state/session_health.json      <-- msgs_current, cache hit rate, active time
state/active_context           <-- Current session working context
state/cog_thread               <-- Cognitive thread checkpoint
state/cross_window_context.json <-- Multi-window coordination
```

### active_context and cog_thread

- active_context: Live working memory. Written at Step 5 (Context Persistence), read at Step 1 (Situational Awareness). Contains current task focus, detected domain, knowledge hits from proactive query, and working state.
- cog_thread: Checkpoint of cognitive thread state. Enables mid-loop recovery after compact or session restart.

### Knowledge Decay and Review

Knowledge entries carry decay parameters. Step 1 applies a forgetting curve: entries not recently accessed have their retrieval strength decayed. The anti-atrophy consumer randomly selects 20% of low-activity entries for re-injection into context, preventing silent knowledge loss.

### Memory Write Discipline

Direct writes to data/memory/ are intercepted by CHECK 2 (MEMORY_BYPASS). All knowledge must enter through the structured knowledge_capture flow, which includes quality gating, source tagging, and index registration. Raw writes create unindexed, unverified entries invisible to retrieval.

### CLS Memory Search

A dedicated retrieval layer (scripts/core-engine/cls_memory.py) provides semantic search across the memory corpus. Called during Step 1-b (Proactive Knowledge Query) with task keywords extracted from active_context, returning the top 5 matching lessons before task execution begins.

---

## Component Map

| Component | Location | Role |
|----------|----------|------|
| Cognitive Core Loop | workflows/cognitive_core_loop.json | 6-step loop definition |
| Hook System (7 lifecycle hooks) | docs/HOOK_SYSTEM.md | External constraint enforcement layer |
| Symbolic Dynamics | docs/SYMBOLIC_DYNAMICS.md | Real-time anomaly detection pipeline |
| Fuse Board | scripts/core-engine/fuse_board.py | Hard limit enforcement |
| Fuse Config | data/safety-configs/fuses_config.json | Fuse configuration |
| Dual-AI Gate | scripts/core-engine/qwen_gate.py | Generator/evaluator verification |
| Symbolic Observer | scripts/core-engine/symbolic_observer.py | Event-to-symbol mapping |
| Cross-Window Hook | scripts/core-engine/cross_window_hook.py | Multi-window coordination |
| Fact Anchoring | data/safety-configs/fact_anchoring_protocol.json | 3-layer verification |
| Premise Check | scripts/core-engine/premise_check.py | File/PID/state verification |
| Trajectory | state/trajectory.json | Cross-session continuity |
| Delivery Check | scripts/core-engine/delivery_check.py | Output hygiene, file catalog |
| Compute Gate | scripts/core-engine/compute_gate.py | Computation declaration enforcement |

---

## Design Principles

1. File-anchored, not memory-anchored: Rules live in files the model cannot modify.
2. Fail-open: Safety checks default to allow on crash.
3. Generator/Evaluator separation: Creator never judges creation.
4. External state: Session state written to files, not held in model context.
5. Anti-self-modification: Core scripts in WRITE_PROTECT list.
6. Auditable deny: Every blocked operation is logged.
7. Multi-window coordination: Windows announce and detect collisions via shared state.
8. Compression over accumulation: 50K+ tokens become 3 numbers and 1 state word.
