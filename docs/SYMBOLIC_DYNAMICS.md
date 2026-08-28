# Symbolic Dynamics: Real-Time Anomaly Detection for LLM Agent Pipelines

## Problem

An LLM agent running hundreds of tool calls produces a firehose of unstructured text. By the time a human notices something is wrong — repetitive loops, escalating errors, forbidden content — the damage is done. Post-hoc log analysis is too slow. Real-time monitoring is needed, but full-text analysis of every tool call is prohibitively expensive (both in latency and API cost).

## Core Idea

Treat the agent's tool-call sequence and hook decisions as a **discrete symbol stream** over a finite alphabet. Apply information-theoretic metrics (entropy rate, forbidden-word counts) to detect anomalies in real time. The key insight: **an agent going off the rails produces a statistically distinguishable symbol pattern** — different from normal operation in ways that can be detected without understanding the semantic content.

Concretely: 50,000+ tokens of raw conversation → 3 numbers + 1 status word. Detection latency: ~200ms. Zero LLM API calls.

---

## Architecture

```
Tool calls / Hook verdicts / Domain triggers
        │
        ▼
symbolic_observer.py
  Maps events → domain symbols
  Writes observations to JSONL
        │
        ▼
Symbolic Dynamics Engine
  8 domain engines compute:
    - Shannon entropy over symbol frequencies
    - Topological entropy (transition matrix)
    - Forbidden-word count (P0/P1 severity)
        │
        ▼
symbolic_verdict.json
  Aggregated verdict per domain:
    ok / warn / critical / diverging
        │
        ▼
PreToolUse CHECK 9 (SYMBOLIC gate)
  Real-time DENY/ASK based on domain health
```

---

## Domain Alphabet

The system defines 8 monitoring domains, each with a finite alphabet:

| Domain | What it monitors | Alphabet size | Sensitivity |
|--------|-----------------|---------------|-------------|
| hook | PreToolUse check outcomes (deny/ask/allow patterns) | 10 symbols | High — safety-critical |
| dialogue | User-AI conversation patterns | 10 symbols | Medium |
| cad | CAD modeling operations | 9 symbols | Medium |
| quant | Quantitative computation | 8 symbols | Medium |
| image | Image processing pipeline | 8 symbols | Medium |
| window | Cross-window coordination events | Variable | Low |
| pic | Plasma simulation operations | Variable | Low |
| retrieval | Knowledge retrieval patterns | Variable | Low |

Each domain's alphabet is defined in `data/symbolic_dynamics/domains/<domain>.json`, along with a transition probability matrix and forbidden-word list.

---

## Mathematical Foundation

The system models an agent's behavior as a discrete-time symbolic dynamical system over a finite alphabet A = {s₁, s₂, ..., sₙ}. Each tool call or hook verdict is mapped to a symbol aᵢ ∈ A, producing an observation sequence ω = a₁, a₂, a₃, ...

### 1. Shannon Entropy (Symbol Frequency)

Over a rolling window of N observations, compute the empirical probability of each symbol:

```
p(s_i) = count(s_i) / N
```


Shannon entropy measures the uncertainty of the symbol distribution:

```
H = -sum_i p(s_i) * log2(p(s_i))
```


**Interpretation**:
- H ≈ 0: The agent is repeating the same symbol — stuck in a loop or single-mode behavior.
- H ≈ log₂(n): Maximum entropy, all symbols equally likely — the agent is exploring broadly or behaving erratically.
- Sudden jump in H: Phase transition. The behavioral regime changed abruptly.

A healthy system shows moderate entropy fluctuating within a calibrated baseline. Both extremes are warning signals.

---

### 2. Topological Entropy (Transition Structure)

Shannon entropy only captures symbol frequencies — it treats the sequence as independent draws. Real anomalies live in the **transitions**.

Build an adjacency matrix T over the alphabet, where T[i][j] = 1 if symbol j has ever followed symbol i in the observation window. This defines a directed graph over the alphabet.

The **spectral radius** ρ(T) (largest eigenvalue) bounds the growth rate of distinct paths:

```
h_top = log2(rho(T))
```


**Interpretation**:
- ρ(T) ≈ 1: Sparse transition graph — constrained, predictable regime.
- ρ(T) growing: New transitions appearing — the agent is generating previously unseen symbol sequences.
- ρ(T) diverging: The graph is becoming fully connected. Mathematical signature of unconstrained behavior.

The system tracks the derivative of ρ(T) over consecutive windows. A sustained positive slope triggers the "diverging" state.

---

### 3. Forbidden Words (Subshift Constraints)

A **forbidden word** is a finite symbol sequence that must never appear. Example: three consecutive DENY verdicts in the hook domain signals an attack pattern or configuration error.

This defines a **subshift of finite type** (SFT): the set of all infinite sequences over A that avoid a finite set of forbidden patterns. The SFT is the "legal" behavior space.

Severity levels:
- **P0 (block)**: Active attack, configuration error, or system compromise.
- **P1 (warn)**: Statistically unusual, warrants attention.

The observer scans each new observation against the forbidden word list using a sliding window. A match triggers the corresponding severity.

**Zero-observation penalty**: A safety-critical domain with zero observations in the window is itself treated as a forbidden condition — unmonitored safety infrastructure is indistinguishable from compromised infrastructure.

---

### 4. Stability Score (Combined Metric)

The three signals are combined into a weighted score:


f₁, f₂ measure deviation from calibrated baselines. f₃ is a step function keyed to forbidden-word severity.

Mapped to four states:


All metrics use a rolling window (default 100 observations). As the window advances, old observations age out — temporary anomalies decay naturally.

---

### Why This Works for LLM Agents

LLM agents are inherently probabilistic. A static rule can't cover the space of possible failure modes because the model can fail in novel ways. Symbolic dynamics doesn't enumerate failure modes — it monitors the **statistical signature** of behavior:

- **Stuck in a reasoning loop** → low Shannon entropy (same pattern repeating).
- **Drifting into hallucination** → rising spectral radius (new, unconstrained transitions).
- **Under prompt injection** → forbidden-word hits (attacker pattern forces agent through a known-dangerous symbol sequence).

These signals are **orthogonal to semantic content**. You don't need to understand *what* the agent is saying to detect *that* its behavior has changed.

---

## Health States

| State | Meaning | Action |
|-------|---------|--------|
| ok | Entropy within baseline, no forbidden words | Normal operation |
| warn | Entropy elevated but within normal range | Flag for attention |
| critical | Entropy diverging, forbidden words tripped, or zero observations in safety-critical domain | Active intervention |
| diverging | Entropy rate exceeds safe threshold | Emergency response |

---

## Integration with the Cognitive Loop

The symbolic dynamics pipeline integrates at three points:

1. **Step 3 (Associative Learning)**: Before knowledge association, the observer runs a health check. If the hook domain is critical, knowledge operations are deferred.

2. **PreToolUse CHECK 9**: Before every Write/Edit/Bash, the symbolic client queries the daemon's current verdict. A deny verdict blocks the operation in real time.

3. **Delivery Check**: Before output delivery, the verdict is sampled again. Critical or diverging state triggers a delivery gate flag.

---

## Why This Approach Is Distinct

Most LLM monitoring relies on one of:
- **Post-hoc log analysis**: Too slow. The damage is done.
- **LLM-as-judge**: Expensive (API cost per check) and slow (seconds per call).
- **Static rule matching**: Brittle. Can't detect novel failure modes.

Symbolic dynamics is:
- **Real-time**: ~200ms per check, runs on every tool call.
- **Zero API cost**: Pure computation, no LLM calls.
- **Pattern-aware**: Detects statistical anomalies that static rules miss — repetitive loops, phase transitions, divergent behavior — without needing to define every failure mode in advance.
- **Compressive**: 50K+ tokens → 3 numbers + 1 state word. Human-operable dashboard from raw event firehose.

This is not a replacement for semantic verification (that's the dual-AI gate's role). It is an orthogonal signal — a statistical health check that catches problems semantic verification might miss (and vice versa).

---

## File Reference

| Component | Location |
|-----------|----------|
| Observer (event→symbol) | `scripts/core-engine/symbolic_observer.py` |
| Domain definitions | `data/symbolic_dynamics/domains/` |
| Forbidden words | `data/symbolic_dynamics/forbidden_words.json` |
| Current verdict | `data/symbolic_dynamics/symbolic_verdict.json` |
| Observation log | `data/symbolic_dynamics/observations/` |

---

## Positioning in the Academic Landscape

For a detailed comparison with existing approaches (Semantic Entropy, LSD, Entropix, Klarity, etc.), see [SYMBOLIC_DYNAMICS_LANDSCAPE.md](SYMBOLIC_DYNAMICS_LANDSCAPE.md).

Key differentiator: **CLS monitors agent behavior (tool-call sequences), not model output (text semantics).** Almost all existing work — from Farquhar et al. (Nature 2024) to Entropix (2025) to LSD (2025) — analyzes the *content* of what the model says. CLS analyzes the *pattern* of what the agent does. These are orthogonal signals: an agent can produce semantically perfect output while trapped in a behavioral loop. The symbolic dynamics pipeline catches the loop; semantic methods don't.

The use of **subshift of finite type** to define a "legal behavior space" for LLM agents, and the combination of Shannon entropy + topological entropy + forbidden words as a real-time tri-metric stability score, appears to be unique in the open literature as of mid-2026.
