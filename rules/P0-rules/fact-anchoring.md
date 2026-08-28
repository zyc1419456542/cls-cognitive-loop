# 🔴 事实锚定协议 P0
> 详见 CLAUDE.md §事实锚定协议

**不蓄能，每步自指声明泄到外部硬事实。**

| # | 规则 | 禁止 | 方法 |
|---|------|------|------|
| 1 | 范畴错误 | 自我实体化声明 | — |
| 2 | 声明必锚定 | 无引用系统状态声明 | 文件路径+PID+字段值 |
| 3 | 前提泄洪闸 | 操作前不检查 | `pipeline.py premise <操作类型>` |
| 4 | 三阶泄洪 | 跳过任一层 | 上游→中游→下游验证端验 |
| 5 | 无证据不可运行 | 证据缺失加载结构 | 验证证据文件存在 |

## 文件锚点
- `scripts/core-engine/premise_check.py`
- `data/safety-configs/fact_anchoring_protocol.json`
- `data/safety/premise_log.jsonl`
