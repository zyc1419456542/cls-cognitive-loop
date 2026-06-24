# 🔴 C盘写入纪律 P0 — crash-lessons#6
> **决不允许向C盘写入任何产出物。曾因C盘炸盘全瘫。**

## crash-lessons前置检查（每次操作前）
1. subprocess `encoding="gbk"` 传了没？
2. 目标路径在C盘吗？→ 改到数据盘（如 E:）
3. daemon 重复孵化检查 (PID)
4. 幻觉闸门 fail-open (`except` 返回 ok=True)？
5. Hook CWD 漂移（用相对路径）？

## 允许/禁止

| 路径 | 允许 | 禁止 |
|------|------|------|
| `~/.claude/` | ✅ 用户配置 | — |
| `~/.claude/projects/` | ✅ Memory (~5MB/session) | — |
| `Temp\claude_backup_*` | ✅ CC备份 | 超3GB需清理 |
| 所有模型缓存 | ❌ | 改向 `{CACHE_DIR}` |
| pip/HF/torch/ollama 缓存 | ❌ | 改向数据盘 env vars |

## 产出写入铁律
- 产出 → `{PROJECT_ROOT}` 下子目录
- 临时文件 → `temp/` 或系统TEMP（用完即删）
- Memory → CC自动管理，不手动写
- 截图 → `data/screenshots/`

## 文件锚点
- `scripts/core-engine/c_drive_guard.py`
