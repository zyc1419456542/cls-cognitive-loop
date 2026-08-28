#!/usr/bin/env python3
"""anchor_capture.py — ANCHOR 行强制落盘 (2026-08-20 maintainer定调)
================================================================
每轮回复首行的 ANCHOR: 声明是大模型对自身行为的原生自述 —
比 trajectory 工具流水高一个语义层, 是:
  ①小模型分析当前行为的优质语料
  ②未来行为数据库的原始数据
  ③CLS 自训中小模型微调的候选数据集

由 Stop.ps1 调用 (stdin 传入 CC hook JSON, 含 transcript_path)。
解析 transcript 最后一条 assistant 文本消息, ANCHOR: 开头则追加日志。

用法: python anchor_capture.py < transcript.json   (Stop hook 管道)
@since: 2026-08-20
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOG = ROOT / "data" / "state" / "cog_anchor_log.jsonl"


def main():
    try:
        hook = json.loads(sys.stdin.read())
    except Exception:
        return
    tp = hook.get("transcript_path") or hook.get("transcriptPath")
    if not tp or not Path(tp).exists():
        return

    # 从尾部找最后一条 assistant 文本消息 (倒扫, 最多看200行)
    try:
        lines = Path(tp).read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except Exception:
        return
    for line in reversed(lines[-200:]):
        try:
            e = json.loads(line)
        except Exception:
            continue
        msg = e.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"] \
            if isinstance(content, list) else [str(content or "")]
        first_line = next((t.strip() for t in texts if t.strip()), "")
        if first_line.startswith("ANCHOR:"):
            anchor = first_line[len("ANCHOR:"):].strip()[:200]
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "sid": (e.get("sessionId") or "")[:12],
                "anchor": anchor,
                "transcript": str(tp),
            }
            try:
                LOG.parent.mkdir(parents=True, exist_ok=True)
                with open(LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass
        return  # 只看最后一条 assistant 消息


if __name__ == "__main__":
    main()
