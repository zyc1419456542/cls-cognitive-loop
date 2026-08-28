# Cross-Runtime Cognitive State — One Stack, Multiple Agent Runtimes

> v3 (August 2026). The full CLS cognitive stack now runs on **two agent runtimes**: Claude Code (hooks) and [DeepSeek Harness](https://deepseek.com/harness/) (dsh, Cordis plugin system, `agent/pre-step` + `tools/pre-execute` + `tools/result`). This doc describes the port pattern and the sharing protocol.

## Why Port At All

A single-runtime cognitive system is a petri dish: the runtime's quirks get mistaken for universal truths. Running the same governance on a second runtime with a *completely different* plugin model (in-process JS events vs. external PS/Python hooks) stress-tests every assumption. Two assumptions died on contact (see Lessons).

## Port Architecture: Thin Shell, Shared Organs

The dsh side is **13 small plugin bundles** — each a thin shell. All heavy logic stays in the original Python wheels, imported, not copied:

```
dsh plugin (JS, ~150 lines each)          shared Python wheels (single source of truth)
──────────────────────────────           ──────────────────────────────────────────
cls-gate     tools/pre-execute  ────────▶ cog_step.json freshness check (same rule
             tools/result      ────────▶  as CC CHECK 15); consult() function
cls-inject   agent/pre-step    ────────▶  (reused by both sides)
cls-memory   agent/pre-step    ────────▶  dsh_cls_nav.py: anchor from state files
                                          → knowledge cards → cheap model selects
                                          ≤5 cards (small model proposes, big model
                                          decides — three-tier memory rule)
cls-symbolic / cls-gaze / cls-cycle / cls-selfref / cls-wakeup / cls-cc-* / plan-graph
```

## The Sharing Protocol: Files, Not Code

```
                ┌─────────────────────────────────────┐
                │  data/state/cog_step.json           │  ← declarations
                │  data/state/consult_clear.json      │  ← consultation passes
                │  knowledge graph / conclusion lib   │  ← captured knowledge
                └──────────┬──────────────┬───────────┘
                           │ read/write   │ read/write
              ┌────────────▼───┐   ┌──────▼──────────┐
              │  Claude Code   │   │  DeepSeek       │
              │  (PS hooks +   │   │  Harness (JS    │
              │   MCP wheels)  │   │  plugins + MCP) │
              └────────────────┘   └─────────────────┘
```

Both runtimes enforce the same rules because they call the *same functions* on the *same files*: a dsh agent's write is denied under the identical freshness check, and its declaration (made via the CC MCP tool, or a CLI that imports the same wheel) unblocks it in both worlds.

## Ops Separation (deliberate)

Monitoring logs are **not** shared: each runtime keeps its own ops JSONL. Reason: the stuck-detectors use a global sliding window that does not filter by session — mixing two runtimes' ops would cross-contaminate alerts. Governance state is global; behavioral telemetry is per-runtime.

## Lessons (paid for in production bugs)

**1. Data shape determines portability, not logical similarity.**
The CC `UserPromptSubmit` hook receives *the new prompt*. The dsh `agent/pre-step` receives *the entire message history, with AGENTS.md persona content spliced in as user-role messages*. The ported injection plugin took "first 120 chars of first-turn text" as its task anchor — which on dsh was the **persona text**. Result: 76 consecutive "topic drift" false injections comparing work prompts against a persona anchor (similarity ≈0.01 forever). Fix: anchor and drift-comparison both use *the last real user message* (excluding plugin injections and known chrome prefixes). **Rule: dump the real message shapes before porting any handler.**

**2. Internal traffic flows through the same tool pipeline.**
The dsh web process runs an internal bookkeeping agent whose heartbeat writes `maintenance.json` — through `tools/*` events, so the new UAC gate denied it on every cycle. Any gate must recognize and exempt non-conversational internal traffic.

**3. `Write-Output 'a' + $var + 'b'` is argument mode, not concatenation** (PowerShell): it prints five separate lines and silently breaks the hook JSON. A stock deny gate had emitted invalid JSON since its birth — fail-open made it invisible. Every gate's output must be E2E-validated as parseable, not just present.

## Bundle Inventory (dsh side)

| Bundle | Role | CC counterpart |
|--------|------|----------------|
| cls-inject | tier classification + drift notice (once/session) | cognitive_gate / semantic route |
| cls-gate | dangerous-cmd/key-leak/loop denies + UAC + stuck detection + consult entry | PreToolUse CHECKs + ops_monitor |
| cls-memory | knowledge-card association via state-file anchors | unified_inject |
| cls-wakeup | session resume / boot injection | SessionStart stack |
| cls-symbolic | forbidden-word gate + L0 routing (reads CC word list, read-only) | symbolic_observer tier 1 |
| cls-gaze, cls-cycle, cls-selfref, cls-cc-commands, cls-cc-workflow, plan-graph | misc wiring | various |
