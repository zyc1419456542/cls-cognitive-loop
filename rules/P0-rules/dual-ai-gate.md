# 🔴 双AI闸门 P0
> 详见 CLAUDE.md §双AI闸门

**创造端独立验证。** p(创造端错)×p(验证端错) ≈ 安全。

| # | 规则 | 说明 |
|---|------|------|
| 1 | 闸门焊接 | `scripts/core-engine/qwen_gate.py` WRITE_PROTECT 保护 |
| 2 | 所有产出必过门 | CAD/知识/模式更新前调 `verify_cad_design()` 或 `verify_knowledge()` |
| 3 | fail→熔断 | verdict=fail → fuse_board.DUAL_AI_GATE 拦截 |
| 4 | API不可用不阻塞 | gate_status=unavailable → 默认放行 |
| 5 | flag不致命 | flag_action=allow → 等待人工确认 |
| 6 | 不替代评价层 | 闸门≠评价层，创造/评价分离仍有效 |
| 7 | 不可绕过 | 闸门代码+熔断板双保护 |

## 文件锚点
- `scripts/core-engine/qwen_gate.py`
- `scripts/core-engine/fuse_board.py`
