# CLS Cognitive Operating Procedure

[![DOI](https://zenodo.org/badge/1278055077.svg)](https://doi.org/10.5281/zenodo.20830497)

> A production cognitive runtime for LLM agents: 6-step cognitive cycle, 4-region brain scheduler, 3-tier anomaly judge, and subconscious knowledge capture — all running on Claude Code hooks. Designed June 2025, operational July 2026.

---

## What CLS Actually Does (July 2026)

After 5 weeks of debugging against real Claude Code hooks, here's what's running in production on a Windows desktop with no GPU daemon:

```
Every tool call:
  PreToolUse Hook  → 15 deny gates (LIFE_CLAIM, COG_STEP, FAKE_MODEL, etc.)
  PostToolUse Hook → trajectory.jsonl (tool call audit log, ~140 entries/session)
                   → symbolic_observer (forbidden word detection, domain entropy)
                   → symbolic_judge (SiliconFlow Qwen2.5-7B: regex hit → semantic analysis → block/inject)
                   → cls_brain.tick() (every 10 rounds: freshness check, 4-region bidding, symbolic health)

Every 10 tool calls:
  auto_capture      → SiliconFlow Qwen2.5-7B: extract reusable knowledge → write memory files
                   → append session_memory.md (incremental summary, replaces unreliable SessionEnd)

Every capture #10:
  cross-session     → pattern detection across sessions → patterns.md

Every tick() call:
  rotate_telemetry  → trim 9 JSONL log files (voice_signal 5000→2000, alerts 3000→1000, etc.)
```

**Key numbers from production:**
- trajectory.jsonl: 140+ entries per session (was 0 before hook fix)
- brain_telemetry: 26 entries, updated every ~30min (was dead for 47 hours)
- symbolic_health: updated every 10 rounds (was dead for 23 days)
- voice_signal: 45,000+ broadcast entries, auto-rotated
- auto_capture: 3 knowledge items captured in first 24 hours

---

## Architecture (What Actually Runs)

```
                    Claude Code Session
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
    SessionStart      PreToolUse       PostToolUse
    (boot注入)       (15 deny闸门)     (observer+judge+brain+capture)
          │                │                │
          ▼                ▼                ▼
    .guidance_       hookSpecific      trajectory.jsonl
    injection        Output envelope   operations.jsonl
    (注入上下文)     {permission       alerts.jsonl
                     Decision:deny}    judge_log.jsonl
                                       auto_capture_log.jsonl
                                              │
                    ┌─────────────────────────┘
                    ▼
            cls_brain.tick()  [every 10 rounds]
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   freshness()  bidding()  _compute_symbolic_health()
   (5文件检查)  (4脑区竞价)  (告警统计+域覆盖率)
        │           │           │
        ▼           ▼           ▼
   guidance_    voice_      symbolic_
   injection    signal      health.json
```

### The 3-Tier Anomaly Judge Pipeline

```
Tool call → PostToolUse
  │
  ├─ Tier 1: symbolic_observer (regex scan, forbidden_words.json, <1ms)
  │   └─ hit → alerts.jsonl (type=forbidden_hit)
  │
  ├─ Tier 2: symbolic_judge (SiliconFlow Qwen2.5-7B, semantic analysis, <2s)
  │   └─ 5-layer JSON parsing fallback
  │   └─ block=true → stdout {"decision":"block"} → CC injects reason
  │   └─ injection → stdout hookSpecificOutput.additionalContext
  │
  └─ Tier 3: PreToolUse CHECK 15.3 (reads .guidance_block from previous judge)
      └─ 10min window → ASK user before next Write/Edit/Bash
```

### The Subconscious (auto_capture)

```
Primary model (DS V4 Pro)     Background model (Qwen2.5-7B)
─────────────────────────     ─────────────────────────
Works on user task            Watches trajectory.jsonl
Never told about capture      Never participates in dialogue
Generates tool calls          Extracts patterns every 10 rounds
                              Writes memory files silently
                              
Two models don't know each other exist.
This prevents self-reinforcing contamination.
```

---

## 6-Step Cognitive Cycle (Production Status)

| Step | Name | Status | Mechanism |
|------|------|--------|-----------|
| ① | Situational Awareness | ⚠️ Manual | active_context.json — requires human/AI refresh |
| ② | Task Execution | ✅ Auto | cog_step.json + CHECK 15 hard gate (15 deny rules) |
| ③ | Associative Learning | ✅ Auto | auto_capture every 10 rounds → SiliconFlow 7B → SHA-256 dedup → memory files |
| ④ | Abstract Generalization | ✅ Auto | Every 10 captures → cross-session pattern detection → patterns.md |
| ⑤ | Context Persistence | ✅ Auto | Incremental session_memory.md (every 10 rounds, replaces unreliable SessionEnd) |
| ⑥ | Trajectory Update | ✅ Auto | trajectory.jsonl — every tool call recorded (was broken for 34 days) |

---

## 4-Region Brain Scheduler

| Region | Function | Status |
|--------|----------|--------|
| Cortex (皮层) | Primary model reasoning | ✅ Every CC tool call |
| Hippocampus (海马) | FAISS semantic index + knowledge capture | ✅ FAISS auto-build on boot; auto_capture |
| Thalamus (丘脑) | tier_router context pre-check | ✅ auto_route=True (was opt-in, now default) |
| Brainstem (脑干) | State freshness + CHECK 15 + fuse board | ✅ CHECK 15 hard gate; symbolic_health auto-computed |

**Bidding**: Every 10 rounds, 4 regions compete via weighted scores. Winner broadcasts to voice_signal.jsonl.

---

## Key Engineering Learnings

### The 34-Day Debug

CLS was designed June 2025. By July 2026, all three subsystems (cognitive loop, brain scheduler, symbolic dynamics) appeared dead. After 5 weeks of multi-model consultation (DS Pro, Kimi, Fable 5, GPT-5.5), external code audits, and byte-level file inspection:

**Root cause: Four interface assumptions about Claude Code hooks were all wrong.**

| Assumption | Reality | Fix |
|-----------|---------|-----|
| JSON fields go at top level | Must be nested in `hookSpecificOutput` | Changed `_EmitDecision` output format |
| exit 2 blocks all tools | Only blocks Bash, not Write/Edit ([GH#13744](https://github.com/anthropics/claude-code/issues/13744)) | Changed to exit 0 + permissionDecision |
| Writing files = CC reads them | CC only reads stdout JSON ([GH#11224](https://github.com/anthropics/claude-code/issues/11224)) | Changed to stdout `{"decision":"block"}` |
| UTF-8 works everywhere | PS 5.1 Chinese Windows requires BOM ([GH#45065](https://github.com/anthropics/claude-code/issues/45065)) | Added `\xEF\xBB\xBF` to .ps1 files |

**Lesson**: External models (DS Pro, GPT, etc.) don't know CC's internal API. CC was designed for Opus. Developing CC infrastructure requires searching official docs + GitHub Issues, not relying on model "common sense."

### Other Critical Bugs

- `$PWD` ≠ project root in hook execution context → MCP writes to one path, hook reads from another → permanent deny
- `Add-Content -ErrorAction SilentlyContinue` swallows ALL errors → trajectory.jsonl never created
- `_EmitDecision` had a PowerShell variable shadowing bug ($Decision type confusion)

---

## Comparison (Updated July 2026)

| Approach | What it does | What CLS adds |
|----------|-------------|--------------|
| Raw LLM API | One-shot, stateless | 6-step cycle with hard hooks, subconscious capture, cross-session state |
| LangChain / CrewAI | Workflow orchestration | Cognitive cycle (not DAG); dual-model verification; stdlib-only circuit breakers |
| Claude Code built-in | Developer agent | 15 deny gates, 3-tier judge, subconscious memory, brain scheduler — all via CC's own hooks |
| llm-wiki / claude-mem | Session-end memory capture | Real-time capture every 10 rounds + subconscious model (not session-end dependent) |
| AutoGPT / BabyAGI | Open-ended task decomposition | Fixed cycle — no unbounded sub-task spawning |

---

## Getting Started

### Requirements

- Python 3.10+
- Claude Code 2.1.195 (hooks API)
- (Optional) SiliconFlow API key (free tier) for judge + capture

### Architecture applies to any LLM runtime with hook support

The cognitive cycle, brain scheduler, and judge pipeline are host-independent. The current implementation targets Claude Code hooks, but the architecture ports to any runtime that provides PreToolUse/PostToolUse/SessionStart lifecycle events.

```bash
# Fuse board self-test (stdlib only, no API key needed)
python scripts/core-engine/fuse_board.py --test
```

---

## Repository Structure

```
cls-cognitive-loop/
├── README.md
├── LICENSE (Apache 2.0)
├── demo.py
├── scripts/
│   ├── wheels/           # Production wheels:
│   │   ├── cls_brain.py          # 4-region scheduler (308 lines)
│   │   ├── symbolic_observer.py  # Tier-1 regex scanner (1111 lines)
│   │   ├── symbolic_judge.py     # Tier-2 small-model judge (327 lines)
│   │   ├── auto_capture.py       # Subconscious capture (350 lines)
│   │   ├── api_pipeline.py       # Unified API gateway (9 providers)
│   │   └── tier_router.py        # 6-tier cost-aware router
│   └── hooks/            # Claude Code hook scripts
│       ├── PreToolUse.ps1        # 15 deny gates (995 lines)
│       ├── PostToolUse.ps1       # observer+judge+brain+capture (475 lines)
│       └── SessionStart.ps1      # boot injection (618 lines)
├── docs/                 # Architecture, white paper
├── rules/                # Safety constraints
└── paper/                # Academic paper source
```

---

## Design Principles

1. **Interface over implementation** — The cognitive cycle is a fixed protocol; backends are swappable.
2. **Constraints live outside the model** — 15 deny rules in `.ps1` files the model cannot modify.
3. **Two models, zero mutual awareness** — The subconscious capture model never sees the primary model's context.
4. **Fail-open with audit trail** — Hook failures log to diag files but never block the main loop.
5. **No persistent GPU daemon** — SiliconFlow free API + local Ollama on-demand only.

---

## License

Apache 2.0. See [LICENSE](LICENSE).

Copyright 2026 The CLS Project Authors
