# CLS Cognitive Loop

> **An LLM agent harness that structures reasoning into a verifiable 6-step cognitive cycle — with dual-model verification, stdlib-only circuit breakers, and cross-session learning.**

*Author: **认知工坊** (Cognitive Workshop) · QQ 1419456542 · zyc2018@mail.ustc.edu.cn*

---

## What problem does CLS solve?

**LLM agents drift on long tasks.** Each step looks reasonable in isolation, but the whole goes off track — because the model has no stable state system. `prompt + history` is just a text sequence that expands, self-references, and forgets early constraints.

**CLS adds a structural spine.** Every task passes through 6 mandatory steps (situational awareness → execution → learning → generalization → persistence → trajectory update) that lock in state between steps and across sessions.

**Independent origin.** First code commit [`73ee3a84`](https://github.com/zyc1419456542/cls-cognitive-loop/commit/73ee3a84) on **2026-05-25** — roughly 2 weeks before "Loop Engineering" entered public AI discourse (Addy Osmani, June 7 2026).

---

## How is CLS different?

| You might be thinking of | What that does | What CLS adds |
|------------------------|---------------|--------------|
| **Raw LLM API** (ChatGPT, Claude, etc.) | One-shot generation, no state | Structured 6-step cycle with persistent state, safety checks, and self-correction between every step |
| **LangChain / LlamaIndex** | Workflow orchestration + RAG | Cognitive cycle (not a pipeline), dual-AI verification (not self-evaluation), stdlib-only circuit breakers (not soft guards) |
| **AutoGPT / BabyAGI** | Autonomous task decomposition | Same loop every time — not open-ended sub-task spawning. Prioritizes stability and verifiability over exploration |
| **Loop Engineering** (Google/Anthropic, June 2026) | The *concept* of looping harnesses | CLS had working code 2 weeks before the concept was named. Dual-AI gate (generator/evaluator separation) and fuse board (stdlib-only safety layer) are unique to CLS |
| **Claude Code / Cursor** | Developer tools with agentic features | A harness *architecture* you can integrate into any host, not a standalone product |

---

## Architecture in 30 seconds

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
           │ Dual-AI Gate (DS→Qwen verify) │
           │ Symbolic Dynamics Audit       │
           └───────────────────────────────┘
```

**The 6 steps (1 line each):**

| Step | Name | Core Idea |
|------|------|-----------|
| ① | Situational Awareness | Read state files, check what other sessions are doing, know where you are before starting |
| ② | Task Execution | Scope → pre-flight checks (fuse board, premise validation) → do it → verify |
| ③ | Associative Learning | Match new knowledge against existing patterns; flag contradictions |
| ④ | Abstract Generalization | Distill concrete experience into transferable patterns |
| ⑤ | Context Persistence | Write checkpoint to disk — survive restart, resume across sessions |
| ⑥ | Trajectory Update | Record how the question evolved (Δq) and how progress shifted (Δp) |

The cycle closes: ⑥ feeds back into ① on the next task. Not a linear pipeline — a cycle that tightens with each iteration.

---

## Key Differentiators

### 1. Independent origin (timeline evidence)

CLS was built in production, not from theory. The timeline is verifiable:

| Date | Event |
|------|-------|
| **2026-05-25** | CLS 6-step cognitive cycle first committed (`73ee3a84`) |
| 2026-06-02 | Fuse Board (circuit breaker system) activated |
| 2026-06-03 | Dual-AI Gate (generator/evaluator separation) enforced |
| 2026-06-07 | Addy Osmani publishes "Loop Engineering" blog post |
| Mid-June 2026 | Boris Cherny & Peter Steinberger public talks on loop-based harnesses |

**What this means:** CLS didn't follow a trend — it independently converged on the same insight that the industry named 2 weeks later. The architecture is battle-tested, not theoretical.

### 2. Dual-AI Gate (generator/evaluator separation)

One model should never evaluate its own output. CLS enforces:

- **DeepSeek** creates (code, designs, knowledge)
- **Qwen** independently verifies (correctness, safety, consistency)

Statistical guarantee: P(system error) = P(DS error) × P(QW error). At 10% per model → ~1% combined error rate. Falls back to Haiku if Qwen is unavailable.

### 3. Fuse Board (stdlib-only circuit breakers)

6 independent fuses, written in **pure Python stdlib** — no imports from the cognitive modules they protect. Cannot be tampered with by the system it guards.

| Fuse | Protects Against | Action |
|------|-----------------|--------|
| WRITE_PROTECT | Self-modification of core files | Block writes to protected paths |
| RECURSION_LIMIT | Unbounded self-reference (max depth: 5) | Truncate + 30min cooldown |
| TOKEN_BUDGET | API cost runaway (2M daily / 500K session) | Block API calls |
| PARALLEL_CAP | Simultaneous destructive ops (max 3) | Block new parallel tasks |
| CHECKPOINT_REQUIRED | No rollback before major changes | Force save every 300s |
| PROXY_PURITY | Semantic drift in proxy layer | Allow only field deletion |

`FuseBackend` abstract interface means the software implementation can be swapped for hardware without changing calling code.

### 4. Symbolic Dynamics & Cross-Window Coordination

- **Symbolic audit:** 50K-token conversation → 3 numbers (topological entropy, spectral radius, alert count) + 1 status word. ~200ms per check.
- **Cross-window awareness:** Multiple Claude Code sessions share a state file, peeking at each other's focus to avoid domain collisions.
- **Fact anchoring:** Every claim must reference a concrete file path and field value. Statements without evidence are structurally rejected.

---

## Repository Structure

```
cls-cognitive-loop/
├── README.md                 # This file
├── README_zh.md              # Chinese version
├── LICENSE                   # Apache 2.0
├── docs/                     # White paper, case studies, timeline evidence
├── scripts/
│   ├── core-engine/          # Loop executor, strategy selector, convergence lock
│   └── safety/               # Fuse board, dual-AI gate, premise check, symbolic engine
├── rules/
│   ├── P0-rules/             # Non-negotiable safety & quality rules
│   └── philosophy/           # 11 design principles
├── workflows/                # Reusable task pipeline definitions
├── data/
│   ├── workflows/            # JSON workflow schemas
│   └── safety-configs/       # Fuse thresholds, gate parameters
├── CLAUDE_templates/         # Harness configuration templates
└── assets/                   # Architecture diagrams
```

---

## Getting Started

### Prerequisites

- Claude Code (or any LLM agent host — the architecture is host-independent)
- Python 3.10+  
- (Optional) A second LLM API for the dual-AI gate

### Quick validation

```bash
# Fuse board self-test (pure stdlib, no dependencies)
python scripts/safety/fuse_board.py --test

# Symbolic dynamics on sample data
python scripts/safety/symbolic_dynamics_engine.py --test

# Dual-AI gate health check (requires API key)
python scripts/safety/qwen_gate.py --health
```

### Design Philosophy (in 3 points)

1. **Interface > Implementation** — Signatures are fixed; backends swap. Software fuses today, hardware fuses tomorrow, same API.
2. **Triple Product, Not AGI** — CLS = AI × CLS × Human. Zero if any factor is zero. Not general intelligence, but a stable closed loop.
3. **Sparse Activation, Shared Baseline** — Safety is always on. Skills load on demand. If routing fails, the baseline holds.

---

## Why open source?

CLS is the result of a month of production development — built to solve real problems (CAD design, educational content auditing, cross-domain delivery), not as a research project. The architecture is proven in practice. Open sourcing it invites scrutiny, contribution, and independent verification.

> *"The loop does not make the model smarter. It makes the system more stable, more reusable, and more cumulative. Short tasks, verified individually, chained into a cycle — that is the architecture."*

---

## License

Apache 2.0. See [LICENSE](LICENSE).

Copyright 2026 The CLS Project Authors
