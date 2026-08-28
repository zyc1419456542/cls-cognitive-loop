# The Difficulty Ladder — Stuck Detection, Strategy Forcing, and External Consultation

> v3 flagship mechanism (August 2026). Design question: **when an agent is stuck, should the system restrict it — or give it someone to talk to?**

## The Problem

Every agent runtime has the failure mode: the model retries the same fix, edits the same file, runs the same failing command — quality degrades with each round ("fix-loop degradation", one of the seven deadly patterns in CLS). The conventional answer is a hard stop (deny after N retries). CLS v2 had that. It turns out a pure block is the weakest form of help:

- The agent doesn't know **what** to do differently.
- The block gives no path forward, so the model learns to route around it (bash redirection instead of Write/Edit tools is the natural bypass).
- Work is not a game: restricting inputs ≠ making progress. The agent needs **a second opinion**, not a timeout.

## The Ladder

```
                    ┌─────────────────────────────────────────────┐
                    │ ops log (every tool call, categorized)      │
                    └──────────────────┬──────────────────────────┘
                                       │ sliding window, last 12 ops
              ┌────────────────────────┼───────────────────────────┐
              ▼                        ▼                           ▼
     same file mutated ≥3      mutate→verify pairs ≥2      same command failed ≥3
     (Edit/Write OR bash       (verify = Read tool OR      (pre-existing v2 rule)
      sed/redirection/          running the test suite
      Set-Content)              /the mutated file itself)
              │                        │                           │
              └───────────┬────────────┴───────────────────────────┘
                          ▼
        ┌──────────────────────────────────────────┐
        │ Tier 1 · skirmish (reference-level)      │
        │ Inject a FORCED STRATEGY CHANGE:         │
        │   "Stop editing. ① List ≥2 root-cause    │
        │    hypotheses ② Write a verification     │
        │    method for each ③ Test the cheapest   │
        │    one first. Only then edit again."     │
        │ No blocking. Once per session.           │
        └──────────────────┬───────────────────────┘
                           │ degradation continues (3rd write→verify cycle)
                           ▼
        ┌──────────────────────────────────────────┐
        │ Tier 2 · consultation entry (deny)       │
        │ The deny reason IS the assignment:       │
        │   "submit a 4-part explanation:          │
        │    ① what was tried  ② why it failed    │
        │    ③ root-cause hypothesis               │
        │    ④ why this attempt will differ"      │
        └──────────────────┬───────────────────────┘
                           ▼
        ┌──────────────────────────────────────────┐
        │ Tier 3 · external review (consult)       │
        │ Agent submits explanation via tool call  │
        │ → cheap external model (Qwen free tier)  │
        │   reviews AS A COLLEAGUE: points out the │
        │   weakest assumption, suggests root-cause│
        │   direction, rules [proceed / one-more-  │
        │   round / change-approach]               │
        │ → opinion injected back into the session │
        │ → 10-min clearance pass: writes resume   │
        └──────────────────────────────────────────┘
```

Key property: **the deny is an entrance, not a wall.** Blocking without an outlet trains bypass behavior; the consultation deny contains the exact, copy-pasteable assignment.

## Detection Design Notes

**Recall over tool names.** v1 detectors watched the `Write`/`Edit` tools. Real models prefer bash: `sed -i`, output redirection, `Set-Content`. All are classified as `mutate` via command regex (with stderr-redirect `2>/dev/null` excluded). Verification is likewise behavioral: the modern agent verifies by *running the test suite*, not by re-reading the file — so `edit → python test_x.py` counts as a write-verify oscillation pair, same as `edit → Read`.

**Deterministic, zero-daemon.** All three signals read the ops JSONL that already exists for monitoring. No new processes, no model calls for detection.

**Anti-nagging.** Each tier fires at most once per session per type (injection-fatigue lesson: an unheeded reminder repeated every turn trains the model to ignore all injections).

## Consultation Protocol

The reviewer is a cheap external model (production: SiliconFlow Qwen2.5-7B, free; any OpenAI-compatible endpoint works). Prompt asks it to **speak to the agent in second person, like a colleague** — not to grade it. Output contract: first line a verdict keyword (`proceed` / `one-more-round` / `change-approach`), then ≤200 chars of advice.

Failure handling is fail-open: if the reviewer is unreachable, the clearance pass is still issued (shorter TTL, 5 min) and tagged `reviewer_unavailable` — **the help channel must never become a deadlock source.**

## Production Evidence (first weekend, no human in loop)

| # | Session context | Verdict | Reviewer advice (abridged) |
|---|----------------|---------|----------------------------|
| 1 | LaTeX section editing, denied on 3rd retry | one-more-round | "your claim lacks cross-validation; check the <part-A> branch first" |
| 2 | MATLAB probe-algorithm fix (5 edits pending) | change-approach | "you keep treating symptoms; the stack trace points one layer earlier" |
| 3 | MATLAB current-negative bug | change-approach | — |
| 4 | Python plotting script blocked by gate | change-approach | — |
| 5 | MATLAB threshold-relaxation fix | one-more-round | — |

The agent called the consult tool unprompted in all five cases (the deny text instructed it), and subsequent edits followed the reviewer's direction.

## What This Is Not

- Not a routing layer (that's the network proxy's job). The consultation happens at the **cognitive** layer, triggered by behavioral evidence, audited in `consult_log.jsonl`.
- Not first to suggest "ask another model" (blog-level recipes exist). The contribution: **framework-level** (gates + passes + TTL), **trajectory-audited**, and **tiered** (cheap strategy nudge before expensive consultation).

## Files (production names)

- `ops_freq.jsonl` — categorized op log (detection source)
- `consult_log.jsonl` — every consultation: verdict, opinion brief, clearance expiry
- `consult_clear.json` — the clearance pass (shared across runtimes, see CROSS_RUNTIME.md)
- `active_context.json: stance` — gear field set by tier-1 detection
