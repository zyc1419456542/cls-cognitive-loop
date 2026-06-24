#!/usr/bin/env python3
"""
trust_labeler.py — 自动标签收集器  |  v1.0
=============================================
从 trust_gate.py 输出或 qwen_gate_log.jsonl 提取训练标签。

标签格式 (每行一个 JSON):
{
  "timestamp": "...",
  "source": "trust_gate" | "qwen_gate" | "file_check",
  "features": {...},          // 8维特征向量
  "verdict": "pass"|"flag"|"fail",
  "label": "trustworthy"|"suspicious"|"fabrication",
  "confidence": 0.0-1.0,
  "reason": "...",
  "cross_validation": {...}   // 可选，交叉验证结果
}

标签逻辑:
  - trust_gate pass + cross_validator consistent → trustworthy / 0.95
  - trust_gate flag (warn only)                  → suspicious / 0.6
  - trust_gate fail (block)                      → fabrication / 0.8
  - trust_gate fail + cross_validator orphan     → fabrication / 0.95
  - qwen_gate flag                               → suspicious / 0.5
  - file_check fail (文件不存在)                  → fabrication / 1.0

v1.1 (web DS review):
  - cross_validator 不一致置信度 0.9→0.6 (可能为系统bug, 不一定伪造)
  - 文件检查失败置信度保留 1.0 (确定性事实)
  - 增加每周抽检标记

用法:
    from scripts.safety.trust_labeler import label, collect_from_gate_log
    result = label(gate_result, file_path)
    collect_from_gate_log()  # 从 qwen_gate_log.jsonl 批量提取

CLI:
    python scripts/safety/trust_labeler.py --from-gate-log [--json]
    python scripts/safety/trust_labeler.py --from-trust-gate <file_path>
    python scripts/safety/trust_labeler.py --stats
"""

import json, sys, time
from pathlib import Path
from datetime import datetime, timezone

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

from scripts.safety.trust_features import extract as extract_features
from scripts.safety.cross_validator import status as cv_status

LABELS_FILE = _PROJECT_ROOT / "data" / "safety" / "trust_labels.jsonl"
GATE_LOG = _PROJECT_ROOT / "data" / "safety" / "qwen_gate_log.jsonl"


def _append_label(entry: dict):
    """追加一条标签到 trust_labels.jsonl"""
    LABELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    with open(LABELS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def label(gate_result: dict, file_path: str = None) -> dict:
    """
    从 trust_gate 输出中提取标签。

    参数:
        gate_result: trust_gate.gate() 返回的结果
        file_path: 可选，源文件路径

    返回标签 dict
    """
    verdict = gate_result.get("verdict", "flag")
    cv = gate_result.get("cross_validation", {})
    violations = gate_result.get("violations", [])
    features = gate_result.get("features", {})

    # 确定标签和置信度
    if gate_result.get("emergency"):
        label_type = "trustworthy"
        confidence = 0.3  # 来源:经验值, 应急门置信度(跳过全部检查时默认低置信度)
        reason = "应急门开启，跳过检查"
    elif verdict == "pass":
        if cv and cv.get("consistent"):
            label_type = "trustworthy"
            confidence = 0.95  # 来源:对标统计置信度惯例, 95%(全维度通过+交叉验证一致)
            reason = "全维度通过 + 交叉验证一致"
        else:
            label_type = "trustworthy"
            confidence = 0.85  # 来源:经验折中值, 85%(通过检查但无交叉验证)
            reason = "全维度通过"
    elif verdict == "flag":
        # 检查是否有交叉验证警告
        has_cv_warn = any(v.get("feature") == "cross_validation" for v in violations)
        if has_cv_warn:
            label_type = "suspicious"
            confidence = 0.6  # 来源:经验值(v1.1调低), 60%(交叉验证不一致降低置信度)
            reason = f"特征门限通过但交叉验证不一致: {cv.get('orphans_gate', 0)}孤立gate"
        else:
            label_type = "suspicious"
            confidence = 0.6  # 来源:经验值, 60%(warn维度触发时置信度)
            warn_features = [v["feature"] for v in violations if v["severity"] == "warn"]
            reason = f"warn维度: {', '.join(warn_features[:3])}"
    elif verdict == "fail":
        blocks = [v for v in violations if v["severity"] == "block"]
        has_cv_orphan = cv and cv.get("orphans_gate", 0) > 0

        if has_cv_orphan or (cv and not cv.get("consistent")):
            label_type = "fabrication"
            confidence = 0.95  # 来源:对标全通过置信度, 95%(block+交叉验证孤儿)
            reason = f"block + 交叉验证孤儿: {[b['feature'] for b in blocks[:3]]}"
        else:
            label_type = "fabrication"
            confidence = 0.8  # 来源:经验折中值, block无孤儿时置信度
            reason = f"block维度: {[b['feature'] for b in blocks[:3]]}"
    else:
        label_type = "suspicious"
        confidence = 0.5  # 来源:经验值 — 未知裁决时的默认置信度50%
        reason = f"未知裁决: {verdict}"

    entry = {
        "source": "trust_gate",
        "features": features,
        "verdict": verdict,
        "label": label_type,
        "confidence": confidence,
        "reason": reason,
        "cross_validation": cv,
        "violation_count": len(violations),
        "block_count": gate_result.get("block_count", 0),
        "warn_count": gate_result.get("warn_count", 0),
    }

    if file_path:
        entry["file"] = str(file_path)

    _append_label(entry)
    return entry


def label_from_qwen_gate(entry: dict) -> dict:
    """
    从 qwen_gate_log.jsonl 的单条记录中提取标签。

    qwen_gate 标签逻辑:
      - gate went through, verdict pass      → trustworthy / 0.85
      - gate went through, verdict flag      → suspicious / 0.5
      - gate went through, verdict fail      → fabrication / 0.7
      - gate returned unavailable            → label = "unlabeled" (gate没跑到)
      - confidence=1.0 + empty reasons       → suspicious / 0.3 (可能是伪造)
    """
    gate_type = entry.get("gate_type", "unknown")
    verdict = entry.get("verdict", "")
    claim = entry.get("claim_summary", "") or entry.get("design_name", "")
    confidence = entry.get("confidence", 0.5)
    reason_text = str(entry.get("reason", "") or entry.get("reasons", ""))
    elapsed = entry.get("elapsed_s", 0)

    # 检测可疑模式
    suspicious_patterns = []

    # 1. 置信度=1.0 但理由为空 → 可疑
    if confidence >= 1.0 and len(reason_text.strip()) < 10:  # 1.0:完美置信度+空理由=可疑模式检测阈值
        suspicious_patterns.append("perfect_confidence_empty_reason")

    # 2. elapsed_s 高度重复 (如 1.79s 批量出现)
    # 无法单条判断，在 collect_from_gate_log 中统计

    # 3. reasons 中全是结构描述而非实质验证
    structural_only = all(
        kw in reason_text.lower()
        for kw in ["结构", "sections", "四章节", "完整"]
    ) if reason_text else False
    if structural_only and len(reason_text) < 200:
        suspicious_patterns.append("structural_only_reason")

    # 确定标签
    if verdict.startswith("fail"):
        label_type = "fabrication"
        base_confidence = 0.7  # 来源:经验值 — qwen_gate block裁决的基线置信度70%
    elif verdict.startswith("flag"):
        label_type = "suspicious"
        base_confidence = 0.5  # 来源:经验值 — qwen_gate flag裁决的基线置信度50%
    elif verdict.startswith("pass"):
        label_type = "trustworthy"
        base_confidence = 0.85  # 来源:经验值 — qwen_gate pass裁决的基线置信度85%
    else:
        label_type = "unlabeled"
        base_confidence = 0.0  # 0%:gate未运行时的默认值,表示无信息

    # 可疑模式降低置信度
    if suspicious_patterns:
        base_confidence = max(0.1, base_confidence - 0.3)  # 可疑模式扣除0.3,最低保留0.1;经验值
        label_type = "suspicious" if label_type == "trustworthy" else label_type

    entry_label = {
        "source": "qwen_gate",
        "gate_type": gate_type,
        "verdict": verdict,
        "label": label_type,
        "confidence": round(base_confidence, 2),
        "reason": f"qwen_gate原始裁决={verdict}, 置信度={confidence}, "
                  f"可疑模式={suspicious_patterns if suspicious_patterns else '无'}, "
                  f"原始理由={reason_text[:120]}",
        "claim_preview": claim[:120] if claim else "",
        "elapsed_s": elapsed,
    }

    if suspicious_patterns:
        entry_label["suspicious_patterns"] = suspicious_patterns

    return entry_label


def label_file_check(file_path: str, exists: bool, expected_refs: list = None) -> dict:
    """
    从文件存在性检查中提取标签。

    参数:
        file_path: 被检查的文件路径
        exists: 文件是否存在
        expected_refs: 期望的文件引用列表
    """
    if not exists:
        entry = {
            "source": "file_check",
            "file": str(file_path),
            "label": "fabrication",
            "confidence": 1.0,  # 100%:文件不存在=确定性事实,保留1.0(v1.1)
            "reason": "文件不存在——确凿的伪造证据",
        }
        if expected_refs:
            entry["expected_refs"] = expected_refs
    else:
        entry = {
            "source": "file_check",
            "file": str(file_path),
            "label": "trustworthy",
            "confidence": 0.9,  # 90%:文件存在=高置信度但非确定性,经验值
            "reason": "文件存在，引用有效",
        }

    _append_label(entry)
    return entry


def collect_from_gate_log(log_path: str = None, max_entries: int = None) -> list[dict]:
    """
    从 qwen_gate_log.jsonl 批量提取标签。

    返回: 标签列表
    """
    log_file = Path(log_path) if log_path else GATE_LOG
    if not log_file.exists():
        return []

    labels = []
    elapsed_values = []

    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            elapsed = entry.get("elapsed_s", 0)
            elapsed_values.append(elapsed)

    # 检测 elapsed_s 批量重复 (mode detection)
    from collections import Counter
    elapsed_counter = Counter(round(e, 2) for e in elapsed_values if e > 0)
    common_elapsed = elapsed_counter.most_common(3)

    # 重新遍历生成标签
    with open(log_file, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if max_entries and i >= max_entries:
                break
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            lbl = label_from_qwen_gate(entry)

            # 附加批量统计信息
            elapsed = round(entry.get("elapsed_s", 0), 2)
            if elapsed > 0:
                freq = elapsed_counter.get(elapsed, 0)
                total = len(elapsed_values)
                if freq > total * 0.1:  # 10%:重复时间模式检测阈值(elapsed_s频率>10%标记可疑)
                    lbl["confidence"] = round(max(0.1, lbl["confidence"] - 0.2), 2)  # 重复模式扣除0.2,最低保留0.1;经验值
                    if "suspicious_patterns" not in lbl:
                        lbl["suspicious_patterns"] = []
                    lbl["suspicious_patterns"].append(
                        f"elapsed_s={elapsed}在{total}条中出现{freq}次(重复模式)"
                    )

            _append_label(lbl)
            labels.append(lbl)

    return labels


def stats() -> dict:
    """标签存储统计"""
    if not LABELS_FILE.exists():
        return {"total": 0, "by_label": {}, "by_source": {}, "file": str(LABELS_FILE)}

    labels = []
    with open(LABELS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    labels.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    by_label = {}
    by_source = {}
    confidences = []

    for lbl in labels:
        lt = lbl.get("label", "unknown")
        by_label[lt] = by_label.get(lt, 0) + 1

        src = lbl.get("source", "unknown")
        by_source[src] = by_source.get(src, 0) + 1

        conf = lbl.get("confidence")
        if conf is not None:
            confidences.append(conf)

    return {
        "total": len(labels),
        "by_label": by_label,
        "by_source": by_source,
        "avg_confidence": round(sum(confidences) / max(len(confidences), 1), 3) if confidences else 0,
        "file": str(LABELS_FILE),
    }


# ── CLI ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="自动标签收集器")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--from-gate-log", action="store_true", help="从 qwen_gate_log.jsonl 批量提取")
    group.add_argument("--from-trust-gate", type=str, help="从 trust_gate 结果文件提取")
    group.add_argument("--stats", action="store_true", help="标签存储统计")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--max", type=int, help="最大处理条数")
    parser.add_argument("--file-check", type=str, help="文件存在性检查")
    parser.add_argument("--refs", type=str, nargs="*", help="期望的文件引用 (配合 --file-check)")
    args = parser.parse_args()

    if args.stats:
        s = stats()
        if args.json:
            print(json.dumps(s, ensure_ascii=False, indent=2))
        else:
            print(f"标签存储: {s['total']} 条")
            print(f"  按标签: {s['by_label']}")
            print(f"  按来源: {s['by_source']}")
            print(f"  平均置信度: {s['avg_confidence']}")
            print(f"  文件: {s['file']}")
        return

    if args.from_gate_log:
        labels = collect_from_gate_log(max_entries=args.max)
        if args.json:
            print(json.dumps(labels, ensure_ascii=False, indent=2))
        else:
            print(f"从 qwen_gate_log.jsonl 提取 {len(labels)} 条标签")
            by_type = {}
            for lbl in labels:
                t = lbl.get("label", "?")
                by_type[t] = by_type.get(t, 0) + 1
            print(f"  分布: {by_type}")
            suspicious = [l for l in labels if l.get("suspicious_patterns")]
            if suspicious:
                print(f"  可疑模式: {len(suspicious)} 条")
                for s in suspicious[:3]:
                    print(f"    - {s.get('suspicious_patterns', [])} | {s.get('claim_preview', '')[:60]}")
        return

    if args.from_trust_gate:
        with open(args.from_trust_gate, "r", encoding="utf-8") as f:
            gate_result = json.load(f)
        lbl = label(gate_result, file_path=args.from_trust_gate.replace("_gate_result", ""))
        if args.json:
            print(json.dumps(lbl, ensure_ascii=False, indent=2))
        else:
            print(f"标签: {lbl['label']} (置信度={lbl['confidence']})")
            print(f"  原因: {lbl['reason']}")
        return

    if args.file_check:
        exists = Path(args.file_check).exists()
        lbl = label_file_check(args.file_check, exists, args.refs)
        if args.json:
            print(json.dumps(lbl, ensure_ascii=False, indent=2))
        else:
            print(f"文件检查: {lbl['label']} (置信度={lbl['confidence']})")
            print(f"  原因: {lbl['reason']}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
