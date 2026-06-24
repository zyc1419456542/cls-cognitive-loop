# 🔴 API Key 安全 P0+
> **决不允许把任何 API Key / Token / 密码提交到 git。**

| # | 规则 | 说明 |
|---|------|------|
| 1 | 提交前扫描 | `git`前自动扫 `sk-` 模式，有则阻止 |
| 2 | 占位符 | Key 用 `<your-api-key>` 替代 |
| 3 | 敏感文件不进 git | `keys/` → `.gitignore` |
| 4 | 历史泄露 | git rebase → force push |
| 5 | pull 安全预检 | pull前 `peek()` 检查其他窗口 |

## 文件锚点
- `.gitignore`
- `CLAUDE_templates/pre-tool-hook.template.ps1` (CHECK #2)
