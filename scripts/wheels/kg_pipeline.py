#!/usr/bin/env python3
"""kg_pipeline.py — CLS 知识图谱整合管线
==========================================
wikimap (结构索引) + memory_consolidator (语义KG) → 统一知识图谱

触发: Windows Task Scheduler (每30分钟)
      或手动: python scripts/wheels/kg_pipeline.py

输出: knowledge/知识图谱/
  ├── MAP.md              (wikimap 结构地图)
  ├── knowledge_graph.md  (语义知识图谱)
  ├── kg_index.json       (结构化索引)
  └── pipeline_log.jsonl  (运行日志)

@since: 2026-07-26
"""

import json, sys, time, subprocess, os
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent.parent
KG_DIR = ROOT / "knowledge" / "知识图谱"
LOG_FILE = KG_DIR / "pipeline_log.jsonl"

# wikimap 只扫描知识目录
WIKIMAP_DIRS = [
    "assistant交付/📚 学习资料/学习进度",
    "knowledge/05_CLS认知系统架构/认知系统迭代",
    "knowledge/进度文件",
    "assistant交付/🎨 assistant设计",
]

# memory_consolidator 输入源
CONSOLIDATOR_DIRS = [
    ROOT / "assistant交付/📚 学习资料/学习进度",
    ROOT / "knowledge/05_CLS认知系统架构/认知系统迭代",
    ROOT / "assistant交付/🎨 assistant设计",
]


def _log(entry: dict):
    KG_DIR.mkdir(parents=True, exist_ok=True)
    entry["ts"] = datetime.now().isoformat()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_wikimap() -> dict:
    """增量结构索引 (wikimap update)"""
    t0 = time.time()
    try:
        result = subprocess.run(
            ["wikimap", "update"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=300
        )
        elapsed = time.time() - t0
        output = (result.stdout + result.stderr)[:500]
        files_indexed = 0
        for line in output.split("\n"):
            if "files indexed" in line:
                try:
                    files_indexed = int(line.split("files indexed")[0].strip().split()[-1])
                except: pass

        return {"ok": True, "files": files_indexed, "elapsed_s": round(elapsed, 1), "output": output[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "elapsed_s": round(time.time() - t0, 1)}


def run_consolidator() -> dict:
    """语义知识图谱 (memory_consolidator)"""
    t0 = time.time()
    try:
        # Import directly to avoid subprocess overhead
        sys.path.insert(0, str(ROOT / "scripts" / "wheels"))
        from memory_consolidator import consolidate
        result = consolidate()
        result["elapsed_s"] = round(time.time() - t0, 1)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "elapsed_s": round(time.time() - t0, 1)}


def run_pipeline():
    """主入口: wikimap → consolidator → 合并报告"""
    log = {"wikimap": None, "consolidator": None}

    # 1. wikimap 结构索引
    wm = run_wikimap()
    log["wikimap"] = wm

    # 2. consolidator 语义图谱
    cm = run_consolidator()
    log["consolidator"] = cm

    # 3. 合并状态
    files_struct = wm.get("files", 0) if wm.get("ok") else 0
    files_semantic = cm.get("processed", 0) if cm.get("status") == "ok" else 0
    kg_entities = cm.get("total_entities", 0) if cm.get("status") == "ok" else 0

    summary = {
        "structure": f"{files_struct} files indexed",
        "semantic": f"{files_semantic} new, {kg_entities} total entities",
        "wikimap_ok": wm.get("ok", False),
        "consolidator_ok": cm.get("status") == "ok",
    }

    # 4. 写运行日志
    _log({
        "pipeline": "kg_pipeline",
        "summary": summary,
        "wikimap": {k: v for k, v in wm.items() if k != "output"},
        "consolidator": {k: v for k, v in cm.items() if k != "log"},
    })

    return summary


def status():
    """查看知识图谱当前状态"""
    report = {"kg_dir": str(KG_DIR)}

    # wikimap MAP
    map_file = ROOT / "MAP.md"
    report["wikimap_map"] = {"exists": map_file.exists(), "size": map_file.stat().st_size if map_file.exists() else 0}

    # consolidator KG
    kg_file = KG_DIR / "knowledge_graph.md"
    kg_json = KG_DIR / "kg_index.json"
    report["kg_md"] = {"exists": kg_file.exists(), "size": kg_file.stat().st_size if kg_file.exists() else 0}
    report["kg_json"] = {"exists": kg_json.exists(), "size": kg_json.stat().st_size if kg_json.exists() else 0}

    if kg_json.exists():
        try:
            kg = json.loads(kg_json.read_text(encoding="utf-8"))
            report["entities"] = len(kg.get("entities", {}))
            report["domains"] = list(kg.get("domains", {}).keys())
            report["updated"] = kg.get("updated")
        except: pass

    # 最近运行日志
    if LOG_FILE.exists():
        lines = []
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    lines.append(line.strip())
        if lines:
            report["last_run"] = json.loads(lines[-1]).get("ts")

    return report


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "--status":
            print(json.dumps(status(), ensure_ascii=False, indent=2))
            return
        elif sys.argv[1] == "--wikimap-only":
            wm = run_wikimap()
            print(json.dumps(wm, ensure_ascii=False, indent=2))
            return
        elif sys.argv[1] == "--consolidator-only":
            cm = run_consolidator()
            print(json.dumps(cm, ensure_ascii=False, indent=2))
            return

    summary = run_pipeline()
    # @add 2026-09-15 maintainer批: **前置** —— 回填 beh/task_id 到 intent_stream（零模型, 见该脚本 docstring）。
    #   为什么必须是前置, 不能省: beh/task_id 是**从工具调用推导**的（44 条声明 × 9199 条工具流水
    #   按滑动窗口共现配对），不该让 AI 手填。AI 每写一条新声明, 这条声明就是"裸"的。
    #   若直接物化, trajectory_materialize 只能给缺 task_id 的行**就地各起一条新线**
    #   （见该函数 @fix 注释）→ 每条新声明都算独立任务 → **返工率虚低、线数虚高**。
    #   完整重算（含新行参与共现）只能在这里做。
    #   失败不阻断: 推导是派生视图, 坏了不该拖垮 KG; 报 error 字段供对账。
    try:
        from behavior_derive import derive as derive_beh
        _bd = derive_beh(write=True)
        summary["behavior_derive"] = _bd.get("error") or {"written": _bd.get("written")}
    except Exception as e:
        summary["behavior_derive"] = f"skipped: {type(e).__name__}: {e}"
    # @add 2026-09-12 maintainer批: 顺带物化意图流 → state/trajectory.json（零模型，见该脚本 docstring）。
    #   挂这里而不是新建计划任务: CLS_Consolidator 本就是每 30min 的知识整合，同频。
    #   失败不阻断主流程 —— 物化是派生视图，坏了不该拖垮 KG。
    try:
        from trajectory_materialize import materialize
        summary["trajectory_materialize"] = materialize()
    except Exception as e:
        summary["trajectory_materialize"] = f"skipped: {type(e).__name__}: {e}"
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
