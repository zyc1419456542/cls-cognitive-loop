# UAC Declaration Gates — Declare-Before-Write, Enforced

> v3 mechanism (August 2026). The cognitive-loop answer to an officially acknowledged gap: Claude Code RFC [#45427](https://github.com/anthropics/claude-code/issues/45427) notes that PreToolUse hooks can silently fail and be bypassed by subagents — deterministic tool gating is an open problem.

## The Problem

An agent that writes files without stating what it is doing produces *unconscious side effects*. CLS's answer since v1: a `cog_step.json` declaration required before Write/Edit, enforced by a PreToolUse deny gate (CHECK 15). v3 completes the design:

## The Complete Binding (v3)

```
1. Agent calls declaration tool (MCP or CLI):
      cog_step_declare(phase=2, label="refactor parser", description="...")
   → writes cog_step.json:  { phase, label, ttl_seconds: 300,
                              _meta: { written_at, window_id, version } }
   → lock-protected, atomic replace, fencing token (multi-window safe)

2. Agent replies with ANCHOR line in first position:
      ANCHOR: ②<same label>
   (the reply-text anchor survives context compaction; the file does not)

3. PreToolUse gate validates cog_step.json on every Write/Edit:
      missing            → deny
      expired (>300s)    → deny
      auto-repaired      → deny   (a declaration the system wrote for you
                                    is not your declaration)
      cross-window       → log only (shared protocol, see below)

4. Deny reason = the exact command to declare, copy-pasteable.
```

**Bidirectional binding rationale**: the file is machine-truth (gate-checkable), the ANCHOR line is attention-truth (survives compaction). Either alone drifts; together they keep the declared intent visible to both the gate and the model.

## Obedience Evidence (live, zero-hint)

A second-runtime agent (DeepSeek Harness, no prior exposure) attempted a write with an expired declaration:

```
#1 write <file>                                → denied (uac_deny, why=expired)
#2 mcp__cls-tools__cog-step-declare(phase=2,   ← found the MCP declaration tool
     label="probe write", description="...")      on its own; the deny text had
#3 write <file>                                → allowed   suggested a shell command
```

The deny text offered a shell command; the agent found something better (the MCP tool). Conclusion: **when the rule is clear and offers a way out, agents take the legitimate path** — it's cheaper than bypassing. The deny reason is an API; design it like one.

## Deny Taxonomy

| State | Decision | Rationale |
|-------|----------|-----------|
| missing | deny | no declaration of intent |
| expired (>TTL) | deny | stale intent is not intent |
| auto-repaired | deny | the system must never forge declarations |
| cross-window | allow + log | multiple runtimes share the file by design |
| exempt paths (`temp/`, state dirs) | allow | scratch and state writes aren't product writes |

## Known Bypass Surface (honest)

The gate watches Write/Edit **tools**. Bash-based writes (`echo >`, `sed -i`) bypass it — same as upstream Claude Code plan-mode limitations. Mitigation is behavioral, not gate-based: bash mutation commands are classified as `mutate` in the ops log, feeding stuck-detection (see DIFFICULTY_LADDER.md). A determined agent can always bypass; the system's bet is that **clear legitimate paths make bypass the expensive option**.

## Multi-Window Safety (shared with v2's cog-context)

1. MCP-mediated serialized writes  2. atomic `os.replace()`  3. `_meta.window_id` tracking
4. TTL file lock (300s)  5. fencing token (monotonic)  6. zombie-lock cleanup (>600s)
7. read-time heartbeat

## Cross-Runtime Sharing

The same `cog_step.json` is read/written by Claude Code and the DeepSeek Harness port (a dsh gate plugin validates the identical freshness rule, and dsh agents declare via the same underlying function imported from the same wheel). One declaration protocol, N runtimes — see [CROSS_RUNTIME.md](CROSS_RUNTIME.md).
