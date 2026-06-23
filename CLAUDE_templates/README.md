# CLAUDE_templates/

Harness configuration templates for LLM agent hosts.

| File | Purpose |
|------|--------|
| CLAUDE_template.md | Generic harness template |

## Integration Points
1. System prompt: encode 6-step loop
2. PreToolUse hook: fuse board + symbolic audit
3. PostToolUse hook: verify outputs + update trajectory
4. Daemon processes: symbolic_dynamics, session_lifecycle, msgs_watchdog
