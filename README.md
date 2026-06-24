# CLS Cognitive Loop

[![DOI](https://zenodo.org/badge/1278055077.svg)](https://doi.org/10.5281/zenodo.20830497)

> An LLM agent runtime framework that structures reasoning into a verifiable 6-step cognitive cycle — with dual-model cross-verification, stdlib-only circuit breakers, and cross-session state persistence.

---

## Problem

LLM agents drift on long tasks. Each step appears reasonable in isolation, but the overall trajectory diverges because the model lacks a stable external state system. `prompt + history` is a text sequence that expands, self-references, and forgets early constraints.

CLS enforces a 6-step cycle (awareness → execution → learning → generalization → persistence → trajectory update) on every task. State is checkpointed between steps and recoverable across sessions.

First commit on 2026-05-25, predating the "Loop Engineering" concept in public discourse (2026-06-07).

---

## Comparison

| Approach | What it does | What CLS adds |
|----------|-------------|--------------|
| Raw LLM API | One-shot, stateless | 6-step cycle with state checkpoints, safety gates, and cross-step verification |
| LangChain / LlamaIndex | Workflow orchestration + RAG | Cognitive cycle (not pipeline); dual-model verification (not self-review); stdlib-only circuit breakers |
| AutoGPT / BabyAGI | Open-ended task decomposition | Fixed 6-step cycle — no unbounded sub-task spawning |
| Loop Engineering (concept) | Theoretical framework | Working implementation predating the concept; dual-AI gate and fuse board as unique mechanisms |
| Claude Code / Cursor | Developer tools with agent features | Host-independent architecture, integrable into any LLM runtime |

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│ ① Situational Awareness → ② Task Execution           │
│        ↑                                ↓             │
│ ⑥ Trajectory Update  ←  ③ Associative Learning       │
│        ↓                                ↓             │
│ ⑤ Context Persistence ← ④ Abstract Generalization    │
└──────────────────────────┬───────────────────────────┘
                           │
           ┌───────────────▼───────────────┐
           │ Fuse Board (6 stdlib fuses)   │
           │ Dual-AI Gate (gen/verify)     │
           │ Symbolic Dynamics Audit       │
           └───────────────────────────────┘
```

**The 6 steps:**

| Step | Name | Function |
|------|------|----------|
| ① | Situational Awareness | Read state files, detect parallel sessions, establish context |
| ② | Task Execution | Scope assessment → pre-flight checks → execute → verify |
| ③ | Associative Learning | Match new knowledge against existing patterns; flag contradictions |
| ④ | Abstract Generalization | Distill concrete experience into transferable patterns |
| ⑤ | Context Persistence | Write checkpoint to disk; enable cross-session recovery |
| ⑥ | Trajectory Update | Record task evolution: delta-q (state change) and delta-p (direction change) |

⑥ feeds back into ① on the next cycle.

---

## Core Mechanisms

### 1. Dual-AI Gate (generator/evaluator separation)

A model should not evaluate its own output. CLS separates generation and verification:

- Generator model produces output (code, documents, analysis)
- Verifier model independently audits (correctness, consistency, safety)

P(system error) = P(generator error) × P(verifier error). At 10% per model, combined error rate ≈ 1%.

### 2. Fuse Board (stdlib-only circuit breakers)

6 independent fuses, pure Python stdlib — no imports from the modules they protect:

| Fuse | Protects against | Action |
|------|-----------------|--------|
| WRITE_PROTECT | Self-modification of core files | Block writes |
| RECURSION_LIMIT | Unbounded self-reference (depth > 5) | Truncate |
| TOKEN_BUDGET | API cost runaway | Block calls |
| PARALLEL_CAP | Concurrent destructive ops (> 3) | Block new tasks |
| CHECKPOINT_REQUIRED | Major change without rollback | Force save |
| PROXY_PURITY | Semantic drift in proxy layer | Field deletion only |

### 3. Symbolic Dynamics & Cross-Window Coordination

- **Symbolic audit**: Compresses long conversations into 3 numbers (topological entropy, spectral radius, alert count) + 1 status word. ~200ms per check.
- **Cross-window awareness**: Multiple sessions share a state file, mutually aware of focus, avoiding domain collisions.
- **Fact anchoring**: Every claim must reference a concrete file path and field value. Unanchored statements are structurally rejected.

---

## Repository Structure

```
cls-cognitive-loop/
├── README.md / README_zh.md
├── LICENSE (Apache 2.0)
├── demo.py                    # One-command demo
├── scripts/
│   ├── core-engine/           # Cognitive loop, fuse board, gates, symbolic observer
│   └── safety/                # Audit gate, failure learner, trust verification
├── docs/                      # Architecture, white paper, case studies
├── rules/                     # Safety and quality constraints
├── workflows/                 # Reusable task pipeline definitions
├── data/                      # Configuration and workflow definitions
├── CLAUDE_templates/          # Host configuration templates
└── assets/                    # Architecture diagrams
```

---

## Getting Started

### Requirements

- Python 3.10+
- (Optional) A second LLM API for the dual-AI gate

### Quick verification

```bash
# Full cognitive cycle demo
python demo.py

# Fuse board self-test (stdlib only)
python scripts/core-engine/fuse_board.py --test

# Dual-AI gate health check (requires API key)
python scripts/core-engine/qwen_gate.py --health
```

---

## Design Principles

1. **Interface over implementation** — Fixed signatures, swappable backends.
2. **Constraints live outside the model** — Safety rules stored in files the model cannot modify.
3. **Sparse activation, shared baseline** — Safety layer always on; domain modules load on demand.

---

## License

Apache 2.0. See [LICENSE](LICENSE).

Copyright 2026 The CLS Project Authors
