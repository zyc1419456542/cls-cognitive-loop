#!/usr/bin/env python3
"""knowledge_inject_audit.py — 知识注入统一审计 (P7, 2026-08-16 夜)
=============================================================
所有知识类注入 (卡片导航/规则卡片/灵感脉冲/候选推荐) 统一写
data/state/knowledge_inject_log.jsonl, 供质量/频率审计。

schema: {ts, source, label, trigger, length, preview}
source: unified_inject | always_injector | cls_inspiration | process_inject | cognitive_gate
"""
import json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOG = ROOT / "data" / "state" / "knowledge_inject_log.jsonl"
MAX_PREVIEW = 100


def log(source: str, text: str, label: str = "", trigger: str = "") -> None:
    """写一条知识注入审计记录。失败静默(审计不可阻塞注入主流程)。"""
    try:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": source,
            "label": label,
            "trigger": trigger,
            "length": len(text or ""),
            "preview": (text or "")[:MAX_PREVIEW],
        }
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def tail(n: int = 20) -> list[dict]:
    """读最近 n 条 (审计/调试用)"""
    if not LOG.exists():
        return []
    try:
        lines = LOG.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(l) for l in lines[-n:]]
    except Exception:
        return []


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    for e in tail(n):
        print(f"{e.get('ts')} | {e.get('source')} | {e.get('label')} | len={e.get('length')} | {e.get('preview','')[:50]}")
