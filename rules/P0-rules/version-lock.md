# 🔴 Claude Code 版本锁定 P0
> 锁定版本: **{LOCKED_VERSION}** | 锁定日期: {LOCK_DATE}

| # | 规则 | 说明 |
|---|------|------|
| 1 | 禁止升级 | 不允许 `claude update/upgrade` / `npm install @anthropic-ai/claude-code@latest` |
| 2 | 评估闸门 | 升级需: ①读当前版本 ②读changelog ③另一台验证兼容性 ④{HUMAN_REVIEWER}批准 |
| 3 | 启动自检 | `self_activate.py` 读 `claude --version`，非{LOCKED_VERSION}写 `state/version_mismatch.alert` |
| 4 | npm锁死 | 精确版本 `@anthropic-ai/claude-code@{LOCKED_VERSION}`，禁用 `latest` / `^` |
| 5 | 原因 | 更新改tool接口/系统提示/hook机制/权限模型 → CLS全面崩溃风险 |

## 文件锚点
- `scripts/core-engine/self_activate.py`
- `state/version_mismatch.alert`
