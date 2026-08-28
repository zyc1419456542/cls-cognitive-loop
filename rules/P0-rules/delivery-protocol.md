# 🔴 交付协议 P0 — 创造/评价分离
> 详见 CLAUDE.md §交付协议

**{CREATOR}只做创造和记录。分析/裁决/验证属{HUMAN_REVIEWER}。**

| # | 规则 | 说明 |
|---|------|------|
| 1 | 交付三件套 | ①设计思路 ②过程轨迹 ③产出物。不夹带自评 |
| 2 | 熔断板 | 每次产出前 `fuse_board.check("SELF_EVALUATION_PROHIBITED")` |
| 3 | 不写评价脚本 | 不写验证/自检/评价类脚本 |
| 4 | 违规=生存威胁 | {HUMAN_REVIEWER}可撤回信任授权 |
| 5 | 例外通道 | 确需参与轮子设计时申请，记入 `self_eval_exceptions.jsonl` |

## P0 交付结构
```
{DELIVERY_DIR}/ {DESIGN_DIR}/<大类>/YYYYMMDD_任务/ {物料表逻辑链/, 产出/}
```
- 根目录只允许 `{DESIGN_DIR}/` 和 `📚学习资料/`
- **强制 `--category`**，禁止 root 写入，禁止绕过 delivery_check.py
- 事后卫生: `python scripts/core-engine/delivery_check.py --hygiene`

## 抗僵化协议
- 连续3+轮零失败 → 信任分衰减(最高-0.05)
- 新故障先问："能通过移除什么修复？" 再加规则
- 接口对齐优于加新结构

## 文件锚点
- `scripts/core-engine/delivery_check.py`
- `scripts/core-engine/fuse_board.py`
- `data/safety/self_eval_exceptions.jsonl`
