# 🔴 上传纪律 P0 — 每次 push 必执行
> 轮子: `scripts/core-engine/upload_log.py`

| # | 步骤 | 命令 |
|---|------|------|
| 1 | push前写日志 | `python scripts/core-engine/upload_log.py log` |
| 2 | 融合前差异比对 | `upload_log.py diff {MACHINE_BRANCH} {OTHER_MACHINE_BRANCH}` |
| 3 | 三机就绪检查 | `upload_log.py fusion-check` |
| 4 | 差异指导融合决策 | 冲突标记(⚠) + 硬编码路径检测 |

日志自动输出到 `data/upload_logs/`: `upload_log.jsonl` (机器可读) + `upload_YYYYMMDD_HHMMSS.md` (人可读)。

## 日常操作

```bash
git add -A && git commit -m "[machine] 做什么"
python scripts/core-engine/upload_log.py log
git push origin {MACHINE_BRANCH}
```

## 文件锚点

- `scripts/core-engine/upload_log.py`
