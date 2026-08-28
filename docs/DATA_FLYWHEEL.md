# The Data Flywheel — Anchored Knowledge, Dual-Track Records, Layer-Tagged Trajectories

> v3 (August 2026). CLS's endgame is not a better-monitored agent; it is **curated training data for the next agent**. This doc describes the three data structures designed for that.

## Positioning

The agent-memory ecosystem (Mem0, Zep/Graphiti, Letta, and the 2026 memory-clone wave) optimizes **recall**: store everything, retrieve the right chunk. CLS's flywheel optimizes **trust and causality**: every captured fact carries how-much-it-can-be-trusted metadata, and every trajectory entry carries which cognitive layer produced it. You cannot SFT a model on "what happened" into a model that knows "why it happened".

## 1. Anchor Levels — trust grading at capture time

Every captured conclusion is stamped with one of three anchors:

| Level | Meaning | Weight at recall |
|-------|---------|------------------|
| `human_confirmed` | The human confirmed the knowledge in this conversation | highest; record verbatim |
| `hard_gate` | Verified by crossing a hard gate (compiled, tests green, ≥3-round debug won) | high |
| `model_authored` | The model's own inference | low; subject to review debt |

Review debt is enforced: model-authored conclusions >7 days unreviewed surface in the **pre-engagement inventory** (see below). Zombie knowledge does not silently enter the training set.

## 2. Dual-Track Records — machine track + human track, one write

Progress is recorded once, lands twice: a human-readable narrative file and a machine-parsable YAML-frontmatter block (date/track/conclusion/decision/lesson/highlight/difficulty/source). The structuring happens **at write time by the big model itself** — zero extra LLM passes later, and the act of structuring reinforces the session's own attention. A passive parser (no LLM) harvests the markers into the conclusion library on a daily schedule.

## 3. Layer-Tagged Trajectories — causal labels for future SFT

Every trajectory entry (one per tool call) now carries:

```
layer=reflex    → reaction to a gate denial              (evidence: deny in audit log, 15s window)
layer=rhythm    → driven by an injection                 (evidence: injection log, same session, 30s)
layer=strategy  → driven by a stance/goal-state change   (evidence: state-file mtime, 60s)
layer=human     → direct human instruction               (evidence: last-human-input timestamp, 120s)
(no tag)        → no evidence — omit, never guess
```

All deterministic, zero model calls. Why it matters: human experts reason in layers (situation → tempo → reflex); transformers are architecturally flat. SFT on untagged trajectories reproduces the flatness; **layer-tagged trajectories are the prerequisite for training hierarchy into the next model**. When local open-weight models reach this capability class (projected: a 30B-class flash model on consumer GPUs within a few years), the flywheel's output is the fine-tuning corpus — with CLS's gates and external review demoted to training-time filters, and the runtime shell slimmed to almost nothing.

## 4. Pre-Engagement Inventory ("CD check")

Before a turn ends, the Stop hook inventories: overdue data sources, unreviewed `model_authored` conclusions, files with line-start `@unverified` markers — at most 5 items surfaced.

**Field-tested anti-nagging (v3.0.1 hotfix, kept as design law)**: the first version re-injected the identical list on every turn (chronic conditions never clear), producing an unusable nagging loop within one session. Fix, now law:

1. content-hash dedup — same list → silent
2. 30-minute cooldown — same list may resurface at most every 30 min
3. first run silent — establish baseline before first speak
4. marker matching anchored to line-start — prose that *mentions* `@unverified` is not review debt

The injection-fatigue principle behind it: an unheeded reminder repeated every turn teaches the model to ignore *all* injections — the shepherd crying wolf destroys the channel.

## 5. Knowledge Cards

Daily job builds cards from the day's records (one card per source file: content digest, lesson/highlight fields, embedding vector, source hash). Recall path: current-task anchor (from goal/step/context state files — **never** from raw chat text, see CROSS_RUNTIME lesson 1) → cheap model *selects* ≤5 candidate cards (small model proposes, big model decides) → card contents injected verbatim. Supersede-by-similarity marks outdated cards rather than deleting them (history is append-only).
