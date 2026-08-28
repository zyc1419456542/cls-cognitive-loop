# 🔴 编码标准 P0 — 编码炸弹 + 数值铁律
> 仅在写 Python 代码/调 subprocess 时适用

## subprocess 编码炸弹防御

| # | 规则 | 说明 |
|---|------|------|
| 1 | Windows命令显式 encoding | `subprocess.run/Popen(text=True)` → `encoding="gbk"` |
| 2 | 新脚本模板 | 顶部加 `_SYS_ENC = "gbk" if sys.platform == "win32" else "utf-8"` |
| 3 | daemon scan_pids 高危 | 编码错误 → 误判PID → 重复孵化 → 句柄泄漏 |
| 4 | health_check.err 监控 | UnicodeDecodeError 出现立即修 |

## 数值计算铁律
1. **计算不进推理流** — 任何数值计算在 `scripts/core-engine/` 用 Python 写死
2. **能算就不估** — 一切可计算的数字全部本地算
3. **数字必有来源** — 标注来自哪个轮子、哪行代码

## 文件锚点
- `data/memory/incidents/crash_lessons_compiled.md` — crash-lessons
