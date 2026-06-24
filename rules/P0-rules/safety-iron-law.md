# 🛡️ 安全铁律 P0+
> 详见 CLAUDE.md §安全铁律

**不靠AI记住，靠文件锁死。**

| # | 规则 | 说明 |
|---|------|------|
| 1 | 操作前必检 | `data/safety/preflight/check_all.ps1` |
| 2 | 操作后必验 | `data/safety/postflight/verify.ps1` |
| 3 | API预算硬限 | 日2000/会话500，超出即停 |
| 4 | Defender不去碰 | 全程开启，不关不停不排除不对抗 |
| 5 | 操作边界 | 只操作项目目录，不动系统目录 |
| 6 | 扫外部=违法 | 不扫未授权外部IP/端口/服务 |
| 7 | 三缺一不可 | 约束必须同时写 CLAUDE.md + 记忆 + 配置文件 |
| 8 | 自指缺陷意识到 | AI管不住自己，写文件才是规则 |

## 熔断板
6个熔断器: 写保护 / 递归上限(深度≤5) / Token预算(会话50万) / 并行上限(≤3) / 检查点强制 / 代理纯净
`scripts/core-engine/fuse_board.py` — 独立stdlib，不import认知模块

## 文件锚点
- `scripts/core-engine/fuse_board.py` — 熔断板
- `data/safety-configs/fuses_config.json`
- `data/safety/preflight/check_all.ps1`
