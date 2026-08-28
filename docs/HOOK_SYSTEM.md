# Hook System: The External Constraint Layer

CLS hooks are lifecycle event handlers that run outside the model's reach. They form the enforcement layer for all safety, audit, and state-management rules. The model cannot disable or modify them — they are configured as native Claude Code lifecycle callbacks.

---

## Architecture

CLS registers 7 lifecycle hooks, covering the full session lifecycle:

```
SessionStart ─→ PreToolUse (every call) ─→ PostToolUse (every call)
     │                    │                          │
     │                    ├─ PostToolUseFailure      │
     │                    │  (tool error auto-learn) │
     │                    │                          │
     ├─ PreCompact ───────┘                          │
     │  (state save before context compression)      │
     │                                              │
     ├─ Stop ───────────────────────────────────────┘
     │  (graceful session shutdown)
     │
     └─ SessionEnd
        (final safety net, process exit)
```

Each hook runs in a zero-window PowerShell process. All hooks are **fail-open**: if a hook crashes, the LLM operation proceeds normally. Every block decision is logged to an audit trail for post-hoc analysis.

---

## Hook Reference

### SessionStart
**When**: Session initialization.  
**Purpose**: Restore cross-session context — load trajectory, active context, porter cache, anti-atrophy scan. Decide cold-start vs warm-start based on activation state freshness.

### PreToolUse
**When**: Before every tool call (Read, Write, Edit, Bash, Grep, Glob, Agent, etc.).  
**Purpose**: 15+ safety checks. The primary enforcement point for CLS constraints.

Key checks:

| # | Check | Action | Guards against |
|---|-------|--------|---------------|
| 1 | AI_VISION_CROSSCHECK | ASK | Image read without local verification |
| 2 | MEMORY_BYPASS | ASK | Direct write to memory files |
| 4 | LIFE_CLAIM | DENY | Self-referential claims about unverifiable states |
| 5 | FAKE_MODEL | DENY | Fabricated model/product names |
| 7 | VERSION_LOCK | DENY | Unauthorized CC updates |
| 9 | SYMBOLIC | DENY/ASK | Real-time symbolic dynamics content analysis |
| 14 | CACHE_DISCIPLINE | ASK | Reading large files (>50KB) without offset/limit |
| 15 | COG_STEP | DENY | Write/Edit without cognitive step declaration — v3: TTL 300s + declare/ANCHOR bidirectional binding (see UAC_DECLARATION.md) |
| 16 | COMPUTE_GATE | Soft log | Mathematical content without compute claim |
| v3 | STANCE_RETREAT | ASK | Non-diagnostic Write/Edit while in retreat gear (never deny — gear can't deadlock) |
| v3 | STANCE_TEAMFIGHT | Inject | Delivery-path writes get enhanced audit + numeric-assertion reminder |
| v3 | CONSULT_GATE | DENY→pass | Fix-loop deny becomes consultation entry; valid consult_clear.json pass (TTL) re-allows |

Full registry: 17 numbered checks + 3 additional safety checks + 3 v3 stance/consultation gates.

### PostToolUse
**When**: After every tool call completes.  
**Purpose**: Fire-and-forget audit logging — capture retrieval tool usage for symbolic dynamics observation. Non-blocking.

### PostToolUseFailure
**When**: A tool call returns an error.  
**Purpose**: Automatic failure learning. Extracts error details and calls failure_learner to persist the pattern — no reliance on the model remembering to log failures.

### PreCompact
**When**: Before Claude Code auto-compacts context (512K token threshold).  
**Purpose**: Save cognitive state checkpoint before compression destroys in-context memory. Writes compact_checkpoint, backs up current_focus, updates cross-window context. Without this hook, compact silently discards all in-context state.

### Stop
**When**: Every turn ends (model stops responding).  
**Purpose**: Graceful shutdown — update last_operation, cross-window context, clean up compact_flag, write terminal cog_step. **v3**: pre-engagement inventory (unreviewed debts, ≤5 items) with content-hash dedup + 30-min cooldown — see DATA_FLYWHEEL.md §4 for the anti-nagging field fix.

### SessionEnd
**When**: Process exit — final event before CC terminates.  
**Purpose**: Last-resort safety net. Runs after Stop. Pushes final daemon heartbeat, cross-window update, cleans temporary markers.

---

## Design Principles

**Fail-Open**: Every hook defaults to `exit 0` on error. A broken hook never blocks the LLM.

**External to Model**: Hooks are process-level gates configured in settings.json. The model has no access to the hook process and cannot modify hook scripts (protected by WRITE_PROTECT fuse).

**Audit Trail**: Every deny/ask decision is logged with timestamp, check name, tool name, and detail for post-hoc analysis.

**Zero-Window**: Dual suppression (CREATE_NO_WINDOW + STARTUPINFO.SW_HIDE) eliminates console flash on Windows.

---

## What Makes This Architecture Distinct

Most LLM agent frameworks implement constraints as in-context instructions — rules in the system prompt that the model is asked to follow. This is unreliable: a model can ignore, forget, or creatively reinterpret in-context rules.

CLS hooks implement constraints as external process-level gates. The model cannot read hook output before the gate decision takes effect, and cannot modify hook scripts. This is an architectural property, not a behavioral one.

The combination of lifecycle coverage (7 hooks spanning session start to process exit) plus the breadth of checks (17+ in PreToolUse alone) makes this one of the most comprehensive hook-based safety architectures in the open-source LLM agent ecosystem.

---

## v3 Engineering Warning: Validate Gate OUTPUT, Not Just Logic

A stock deny gate (`探索缺口 hard gate`, live 2026-07-31 → 2026-08-27) emitted invalid JSON from day one — **and therefore never actually denied anything** — because of a PowerShell trap:

```powershell
# BROKEN: argument mode, not concatenation — prints FIVE separate lines
Write-Output '{"permissionDecision":"deny","permissionDecisionReason":"' + $reason + '"}'

# CORRECT: single envelope
Write-Output (@{permissionDecision="deny"; permissionDecisionReason=$reason} | ConvertTo-Json -Compress)
```

Fail-open hooks make broken output invisible: nothing errors, the tool call just always passes. **Every gate must be E2E-validated by feeding its stdout to a real JSON parser**, in CI or on deploy — presence of output proves nothing. Related trap: `Write-Output` on PowerShell 5.1 + non-ASCII stdin requires explicit UTF-8 handling or Chinese content corrupts silently.

---

## File Reference

| Component | Location |
|-----------|----------|
| Hook registration | `.claude/settings.json` |
| PreToolUse gate | `.claude/hooks/PreToolUse.ps1` (17+ checks) |
| PostToolUse audit | `.claude/hooks/PostToolUse.ps1` |
| Failure auto-learn | `.claude/hooks/PostToolUseFailure.ps1` |
| Pre-compact save | `.claude/hooks/PreCompact.ps1` |
| Graceful shutdown | `.claude/hooks/Stop.ps1` |
| Final cleanup | `.claude/hooks/SessionEnd.ps1` |
| Session init | `.claude/hooks/SessionStart.ps1` |
| Zero-window launcher | `.claude/hooks/_run_hidden.py` |
| Deployment templates | `CLAUDE_templates/` |
