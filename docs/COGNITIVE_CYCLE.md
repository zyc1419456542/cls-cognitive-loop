# CLS Cognitive Cycle -- The 6-Step Loop in Depth

**Component**: Cognitive Core Loop
**Defined in**: workflows/cognitive_core_loop.json (v21, created 2026-05-25)

---

## Overview: The Breath Rhythm

The cognitive loop is not a flat pipeline. It has a cardiac rhythm: systole (contraction, action) alternates with diastole (expansion, reflection).

```
SYSTOLE (produce)          DIASTOLE (connect)
+-------+-------+     +-------+-------+-------+-------+
| Step 1| Step 2|     | Step 3| Step 4| Step 5| Step 6|
|Aware  |Execute| --> |Assoc  |Abst   |Persist|Traj   |
+-------+-------+     +-------+-------+-------+-------+
     action                  reflection + learning
```

This is not poetic. It is structural. Systole produces output for the user. Diastole extracts value from what was just produced, connecting it to existing knowledge, generalizing patterns, and persisting state so future sessions can benefit. A loop that only executes without reflecting is a treadmill. A loop that only reflects without executing is a diary.

---

## Step 1: Situational Awareness (SYSTOLE)

### Trigger
Session start, or user message arrives.

### What happens

1. **Load path registry**: Read data/location_registry.json. Inject domain paths into current session context. All subsequent path references route through the registry (avoiding hardcoded paths).

2. **Check porter reminder**: Look for state/.porter_reminder. If present, read and display any unrecalled cached files. The Porter is a file-absorption/recall system that buffers content across sessions.

3. **Anti-atrophy scan**: Run anti_atrophy_consumer.py. Scan cue activity levels. Randomly select 20% of low-activity knowledge entries and inject them into context, preventing silent knowledge loss.

4. **Read trajectory**: Load state/trajectory.json. Extract position, mass, and momentum from the previous session. This is how the system knows "what were we doing?" without the user repeating context.

5. **CLS Memory search**: Run scripts/core-engine/cls_memory.py with task domain keywords. Return top-5 matching lessons.

6. **Cross-window peek**: Call cross_window_hook.auto_peek() to see what other Claude Code windows are doing. Output includes each window's status, focus, and time since last update.

7. **External anchor sample**: Run external_anchor.py to sample a real-world state value. This is the anti-self-referential-closure mechanism: before making decisions, the system touches something external.

### Output

active_context is populated: task focus, detected domain, knowledge hits, timeline start, working state. This is the "where am I?" baseline for everything that follows.

### Key files read

- data/location_registry.json
- state/trajectory.json
- state/cross_window_context.json
- state/activation_state.json
- state/.porter_reminder

### Key files written

- state/active_context (populated)

---

## Step 1-b: Proactive Knowledge Query

### Trigger
After Step 1 completes, before Step 2 begins.

### What happens

Extract task keywords from active_context. Call knowledge query system with those keywords. Match against lessons_learned corpus. Sort matches by relevance. Load the top matches into working_context.knowledge_hits.

### Purpose

Retrieve past lessons before execution, not reactively during execution. By the time the model starts writing code or making decisions, relevant past failures and patterns are already in context.

---

## Step 2: Task Execution (SYSTOLE)

### Trigger
User request arrives. This is the only step that directly produces output for the user.

### What happens

**Scale assessment (first)**:
- Simple: single file, single domain -> direct execution
- Medium: multiple files, one domain -> batch diagram + parallel
- Complex: multi-domain, long tail of dependencies -> subagent orchestration

**Strategy selection**: Run strategy_selector.py with a 2-10 word task summary. Returns P1/P2/P3 execution path recommendation. P1 is the default optimal path; P2 and P3 are alternative routes.

**Epsilon-greedy exploration**: Roll epsilon_gate.py. With 10% probability (epsilon=0.1), take an exploration path instead of the default. This prevents the system from getting stuck in local optima. The 90% case follows the default execution path.

**Path mutation**: After batch decomposition, run path_mutation.py on each step. With 10% probability per step, apply INSERT_NOOP, SWAP, or other mutations. This introduces controlled variability.

**Pre-flight checks**:
- Fuse board check: all fuses below trip threshold
- Cross-window cover check: no other window working on same domain with overlapping keywords
- Cross-window announcement: declare current focus to all windows
- External anchor: verify premise_check.py passes

**Fragmented data detection (v21)**: Scan user input for fragmented/partial data patterns. If triggered (scattered files, multiple formats, reconstruction goal), activate the 6-step fragment restoration protocol:
1. Panoramic scan -- list all available files with type/size/initial assessment
2. Coarse sorting -- classify by source grade: S (FEM/PPT params) > A (point clouds/photos) > B (physical inference) > C (verbal) > D (estimates)
3. Deep extraction -- extract key parameters from highest-grade sources first
4. Cross-validation -- compare across sources; contradictions are signals, not errors
5. Gap filling -- estimate missing parameters from physics/engineering heuristics, marked __EST__
6. Restoration output -- produce complete output in target format

**Execute**: Run the task. Monitor for errors. Log failures.

**Cleanup**: Close temporary files, finalize state.

### Output

Task deliverables (code, files, analysis, decisions). This is the only loop step that has externally visible output.

### Key constraint

Before any output delivery, the SELF_EVALUATION_PROHIBITED fuse is checked. The model must not self-assess its output quality. That is the human's role (or Qwen's in the dual-AI gate).

---

## Step 3: Associative Learning (DIASTOLE)

### Trigger
New knowledge is produced (task completed, insight generated, error encountered).

### What happens

1. **Symbolic dynamics health check (pre-step)**: Run symbolic_observer.py status --quiet. Read 6-domain entropy status. If any domain is critical/diverging, flag for attention before proceeding.

2. **Extract new knowledge**: What was learned? What was confirmed? What was discovered to be wrong?

3. **Match against existing knowledge graph**: Search the knowledge base for related entries. Does this new knowledge:
   - Confirm an existing pattern?
   - Contradict an existing assumption?
   - Fill a gap in the knowledge graph?
   - Open a new domain?

4. **Write association record**: Log the connection to insight_log. Tag with: source (which task produced this), related entries (what it connects to), confidence (how certain is this association).

5. **PIC precipitation check**: If the task touched PIC (plasma simulation) keywords, trigger pending_pic_precipitation. PIC insights accumulate and are batch-precipitated into the PIC knowledge graph.

6. **P2 story consumption**: Scan the signal bus for P2-level perturbation signals from the current session. Extract narrative threads from micro-perturbations. Write story_consumption.jsonl. This catches the weak signals that individual events are too noisy to reveal.

### Purpose

Knowledge islands are dead knowledge. A fact that is not connected to the existing knowledge graph cannot be found, cannot be built upon, and contributes nothing to future sessions. Step 3 is the bridge between isolated experience and integrated understanding.

### Output

Associations written to insight_log. PIC precipitation results in cognition_memory. P2 consumption markers in story_consumption.jsonl.

---

## Step 4: Abstract Generalization (DIASTOLE)

### Trigger
After Step 3 completes for a task that produced nontrivial new knowledge.

### What happens

1. **Pattern extraction**: Look at the concrete experience from Step 3. What is the general pattern beneath it? A specific CAD constraint that solved a problem -> what class of constraints does it belong to? A specific error that was fixed -> what family of errors does it represent?

2. **Decontextualization**: Strip the pattern of task-specific details. "The coaxial tube needed M4 bolts at 7mm spacing" becomes "When bolt diameter and wall thickness are comparable, check for through-body clearance."

3. **Pattern registry check**: Does this pattern already exist in the knowledge base? If yes, merge and strengthen. If no, create new entry.

4. **Transferability assessment**: Rate the pattern on: domain scope (how many domains does it apply to), recurrence likelihood (how often will this scenario repeat), abstraction level (how general is the formulation).

### Purpose

Concrete experience without abstraction is apprenticeship without a textbook. You can retrain on the same problem endlessly and never get faster. Abstraction makes the experience reusable -- a pattern recognized once prevents N future errors.

### Output

Generalized patterns written to pattern registry. Merged with existing patterns where overlap exists.

---

## Step 5: Context Persistence (DIASTOLE)

### Trigger
Nearing session end, approaching compact threshold, or cognitive cycle completion.

### What happens

1. **Write active_context**: Serialize current working state -- what task, what domain, what decisions are pending, what knowledge was loaded. This is the session's "save game."

2. **Write cog_thread checkpoint**: Record which step of the cognitive loop we are in, what sub-step, what the next action would be. This enables mid-loop recovery.

3. **Cross-window update**: Call cross_window_hook.auto_update() to refresh status in the shared window context.

4. **Compact health check**: Call compact_health_board.py --msgs to check message count. If at or above threshold for the current activity type, trigger compact with self_activate.

5. **Knowledge quality gate**: Run knowledge_quality_gate.py on any new knowledge entries created. Score < 0.8 triggers review. Score < 0.55 triggers DENY at PreToolUse CHECK KNOWLEDGE_QUALITY.

### Purpose

State that lives only in the LLM's context window is volatile. A compact, a crash, a session boundary -- and it is gone. Step 5 externalizes state so the next session (or the same session after compact) can resume without the user repeating context.

### Output

- state/active_context (updated)
- state/cog_thread (updated)
- state/cross_window_context.json (status updated)
- data/memory/last_operation.json (updated)

---

## Step 6: Trajectory Update (DIASTOLE)

### Trigger
After Step 5 completes. This is the final step that closes the loop and feeds into Step 1 of the next cycle.

### What happens

1. **Extract delta-q (mass change)**: What concrete changes were made in this cycle? Files created, modified, deleted. Code written. Designs completed. Knowledge entries added. This is delta-q.

2. **Extract delta-p (momentum change)**: What direction did the work move in? What new questions opened? What paths were closed? What paths were opened? This is delta-p.

3. **Update trajectory.json**: Write new position, updated mass, updated momentum. Append to trajectory_log.

4. **Cross-window completion**: Call cross_window_hook.auto_update(status="completed", summary="..."). Mark task as done for other windows.

### Closure: 6 -> 1

The updated trajectory.json is what Step 1 reads at the start of the next cycle. This closes the loop. Active sessions read the updated trajectory to maintain continuity. New sessions (cold start) read trajectory.json to understand "where were we?"

### Output

- state/trajectory.json (updated position, mass, momentum, log)
- state/cross_window_context.json (completion status)

---

## Natural Break Points (Breath Rhythm Mechanics)

The loop is not a scheduler. It does not run on a timer. It runs on natural break points (zi ran duan dian) -- moments in the conversation where a meaningful unit of work has completed and the system can pause to reflect.

### When does each step fire?

| Step | Fires when... |
|------|--------------|
| 1 | Session starts; user returns after >5 min gap |
| 1-b | Step 1 completes |
| 2 | User makes a request |
| 3 | New knowledge is produced (task completes, error occurs, insight forms) |
| 4 | Step 3 completes with nontrivial new knowledge |
| 5 | Session end approaching; compact threshold near; cycle completing |
| 6 | Step 5 completes |

### The 5-minute rule

If user inactivity exceeds 5 minutes:
- Update time awareness (state/time_awareness.json)
- Brief re-greeting with elapsed time
- Quick re-establishment of active_context

If inactivity is 5 minutes or less: silent skip. This prevents the loop from churning during rapid back-and-forth.

### Soft gate: the Three-Look-Back

At every natural break point, three questions fire automatically:

1. **Can this be parallelized?** -- Are there independent sub-tasks that can run concurrently?
2. **Was the last step correct?** -- Glance at the output of the previous tool call. Error? Empty? Unexpected? Stop and fix before proceeding.
3. **Can an existing interface be reused?** -- Before writing new code, check scripts/core-engine/ for existing functionality.

These are not decisions the model makes consciously each time. They are inertial -- wired into the natural break points so they fire without deliberation.

---

## Memory System Architecture

### Three-Tier Memory

```
Tier 1: ACTIVE CONTEXT (volatile, session-scoped)
  active_context + cog_thread
  Live during the session. Lost on crash without Step 5 persistence.

Tier 2: SESSION STATE (file-persisted, session-scoped)
  trajectory.json, session_health.json, activation_state.json
  Survives compact. Survives session restart (warm start).

Tier 3: LONG-TERM MEMORY (file-persisted, cross-session)
  data/memory/MEMORY.md, data/memory/on_demand/
  Knowledge entries with decay parameters. Indexed for retrieval.
```

### Cold Start vs Warm Start

**Cold start** (activation_state status=DEAD or last_heartbeat >10 min ago):
- Full initialization sequence
- Load L4->L3->L2->L1 dose injection (layered context restoration)
- Write self_activate signal
- Launch activation_listener.py for heartbeat

**Warm start** (activation_state status=HIBERNATING, last_heartbeat <=10 min):
- Read trajectory for continuity
- Load active_context if available
- Read signal bus (last 20 entries) for recent events
- Skip full initialization

### Forgetting Curve

Knowledge entries are not static. Each entry has:
- **Strength**: Retrievability score (decays over time)
- **Last accessed**: Timestamp of last retrieval or reinforcement
- **Decay rate**: How fast strength drops without reinforcement

During Step 1's anti-atrophy scan, entries with strength below threshold are candidates for re-injection. The 20% random selection prevents the same entries from being repeatedly reinforced while others decay to zero.

### Knowledge Quality Gate

Before any knowledge entry is written, it passes through a quality gate:
1. **Three-question check**: What problem? What tool? What solution? An entry that cannot answer all three is flagged quality: suspect.
2. **Score computation**: Structure match (do parts have functional gaps?) times temporal coherence (is the causal chain continuous?).
3. **Gate enforcement**: Score >= 0.8 passes. Score < 0.8 triggers review. Score < 0.55 triggers DENY at the PreToolUse hook.

---

## Cross-Window Coordination Protocol

### The Problem

Claude Code sessions are independent processes. Without coordination:
- Window A reads state file, Window B writes state file, Window A writes stale state file -> data loss
- Window A and Window B both work on CAD -> duplicated effort
- Window A's work depends on Window B's output -> Window A proceeds with stale data

### The Protocol

Each window has a unique ID (PID + millisecond timestamp). All windows share state/cross_window_context.json. The protocol integrates at specific points in the cognitive cycle:

```
Step 1: auto_peek()      -- "Who else is here?"
Step 2: cover_check()    -- "Am I stepping on anyone's domain?"
Step 2: announce()       -- "I am now working on X"
Step 5: announce()       -- "I am persisting context, still working on X"
Step 6: announce(done)   -- "I finished X"
Every tool call:          -- "I am still here" (last_seen heartbeat)
```

### Conflict Resolution

cover_check() returns covered: true when another window is actively working on the same domain with overlapping task keywords. The calling window then:
1. Reports the conflict to the user
2. Offers to wait, redirect, or proceed with caution
3. Does NOT unilaterally decide to proceed (the human arbitrates)

### Stale Window Cleanup

Windows that have not updated last_seen within a freshness threshold are filtered out by peek(). They may have crashed or been closed without remove_self() cleanup. A background process periodically prunes stale entries.

---

## Implementation Reference

| File | Role |
|------|------|
| workflows/cognitive_core_loop.json | Full loop definition (135 lines, v21) |
| scripts/core-engine/cognitive_core_loop.py | Loop execution engine |
| scripts/core-engine/strategy_selector.py | P1/P2/P3 path selection |
| scripts/core-engine/epsilon_gate.py | Epsilon-greedy exploration |
| scripts/core-engine/path_mutation.py | Execution path mutation |
| scripts/core-engine/external_anchor.py | Anti-closure external sampling |
| scripts/core-engine/cross_window_hook.py | Cross-window coordination |
| scripts/core-engine/cross_window_awareness.py | Window peek/announce/cover core |
| scripts/core-engine/premise_check.py | Upstream premise verification |
| scripts/core-engine/cls_memory.py | Memory search and retrieval |
| scripts/core-engine/knowledge_quality_gate.py | Knowledge entry quality scoring |
| scripts/core-engine/anti_atrophy_consumer.py | Forgetting curve + re-injection |
| scripts/core-engine/compact_health_board.py | Compact threshold checking |
| state/trajectory.json | Cross-session position/mass/momentum |
| state/activation_state.json | Cold/warm start state |
| state/active_context | Session working memory |
| state/cog_thread | Cognitive thread checkpoint |
| state/cross_window_context.json | Multi-window shared state |
