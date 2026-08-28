#!/usr/bin/env python3
"""inject_feedback_analyzer.py — 注入质量反馈分析 + 自动调优 + 报告
读取 execution 窗口的注入评分 (injection_feedback.jsonl) → 分布分析 → 自动调整
(0/-1占比>40%冷却拉长2x, >60%自动关停, 漂移有用率低放宽cosine阈值) → 写回
inject_feedback_config.json (semantic_inject 运行时读取生效) → 生成报告供maintainer审查。

@since: 2026-08-01 | maintainer决策: 自动调整+报告可回滚, 事后偶尔审查
"""
import json, os, sys, time
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent.parent
FEEDBACK_FILE = ROOT / "data" / "state" / "injection_feedback.jsonl"
CONFIG_FILE = ROOT / "data" / "state" / "inject_feedback_config.json"
REPORT_DIR = ROOT / "assistant交付" / "📚 学习资料" / "学习进度"

# 自动调整阈值
COOLDOWN_BAD_RATIO = 0.40    # 0/-1 占比 > 40% → 冷却拉长 2x
DISABLE_BAD_RATIO = 0.60     # 0/-1 占比 > 60% → 关停该类型
THRESHOLD_DELTA_STEP = 0.05  # 漂移有用率低 → cosine 阈值 +0.05
DRIFT_USEFUL_MIN = 0.60      # 漂移类型有用率 < 60% 视为误报多
MIN_SAMPLES = 5              # 少于 5 条不自动调整 (样本不足)

INJECT_TYPES = ["漂移", "Ops", "认知", "统一"]

def _atomic_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)

def load_feedback(limit: int | None = None) -> list:
    """读评分记录 → [{ts, type, score, ...}]"""
    entries = []
    if not FEEDBACK_FILE.exists():
        return entries
    with open(FEEDBACK_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if "type" in d and "score" in d:
                    entries.append(d)
            except Exception:
                continue
    if limit:
        entries = entries[-limit:]
    return entries

def compute_distribution(entries: list) -> dict:
    """按类型统计 1/0/-1 分布 + 有用率"""
    dist = {}
    for t in INJECT_TYPES:
        scores = [e["score"] for e in entries if e.get("type") == t]
        if not scores:
            continue
        c = Counter(scores)
        total = len(scores)
        useful = c.get(1, 0) / total
        bad = (c.get(0, 0) + c.get(-1, 0)) / total
        dist[t] = {
            "n": total, "score_1": c.get(1, 0), "score_0": c.get(0, 0),
            "score_-1": c.get(-1, 0), "useful_rate": round(useful, 3), "bad_rate": round(bad, 3),
        }
    return dist

def decide_adjustments(dist: dict, prev_config: dict) -> dict:
    """决策自动调整 (可逆)"""
    adj = {"on": {}, "off": [], "notes": []}
    for t, st in dist.items():
        if st["n"] < MIN_SAMPLES:
            continue
        if st["bad_rate"] > DISABLE_BAD_RATIO:
            adj["off"].append(t)
            adj["notes"].append(f"{t}: 0/-1占{st['bad_rate']:.0%} > 60% → 自动关停")
        elif st["bad_rate"] > COOLDOWN_BAD_RATIO:
            adj["on"][t] = {"cooldown_mult": 2.0}
            adj["notes"].append(f"{t}: 0/-1占{st['bad_rate']:.0%} > 40% → 冷却拉长2x")
    # 漂移阈值: 有用率低 → 放宽 cosine 阈值
    delta = prev_config.get("threshold_delta", 0.0)
    if "漂移" in dist and dist["漂移"]["n"] >= MIN_SAMPLES:
        if dist["漂移"]["useful_rate"] < DRIFT_USEFUL_MIN:
            delta = round(delta + THRESHOLD_DELTA_STEP, 2)
            adj["notes"].append(f"漂移: 有用率{dist['漂移']['useful_rate']:.0%} < {DRIFT_USEFUL_MIN:.0%} → cosine阈值+{THRESHOLD_DELTA_STEP} (现值 {0.40 + delta})")
        elif delta > 0 and dist["漂移"]["useful_rate"] > 0.85:
            delta = round(delta - THRESHOLD_DELTA_STEP, 2)
            adj["notes"].append(f"漂移: 有用率{dist['漂移']['useful_rate']:.0%} 高 → 恢复阈值-{THRESHOLD_DELTA_STEP} (现值 {0.40 + delta})")
    adj["threshold_delta"] = delta
    return adj

def save_config(adj: dict, prev_config: dict) -> Path:
    """合并调整写回 inject_feedback_config.json (semantic_inject 读取)"""
    # 保留上次 off 状态, 更新本次
    adjustments = {}
    for t in INJECT_TYPES:
        prev_status = prev_config.get("types", {}).get(t, "on")
        adjustments[t] = "off" if t in adj["off"] else ("on" if prev_status == "on" else prev_status)
    cfg = {
        "types": adjustments,
        "cooldown_mult": {t: 2.0 for t in adj["on"]},
        "threshold_delta": adj["threshold_delta"],
        "notes": adj["notes"],
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _atomic_write(CONFIG_FILE, cfg)
    return CONFIG_FILE

def generate_report(dist: dict, adj: dict, total: int) -> Path:
    """生成报告供maintainer审查"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date = time.strftime("%Y%m%d")
    report = REPORT_DIR / f"injection_quality_report_{date}.md"
    lines = [
        f"# 📊 注入质量报告 {time.strftime('%Y-%m-%d')}",
        "",
        f"> 自动生成 | 数据: injection_feedback.jsonl {total}条 | 自动调整+可回滚",
        "",
        "## 评分分布",
        "",
        "| 类型 | 样本 | 1(有用) | 0(噪音) | -1(误导) | 有用率 | 差评率 |",
        "|------|------|--------|--------|---------|--------|--------|",
    ]
    for t, st in sorted(dist.items()):
        lines.append(f"| {t} | {st['n']} | {st['score_1']} | {st['score_0']} | {st['score_-1']} | {st['useful_rate']:.0%} | {st['bad_rate']:.0%} |")
    if not dist:
        lines.append("| (无评分数据) |")
    lines += [
        "",
        "## 自动调整 (已生效, 可回滚)",
        "",
    ]
    if adj["notes"]:
        lines += [f"- [自动] {n}" for n in adj["notes"]]
    else:
        lines.append("- 无调整 (样本不足或均在阈值内)")
    lines += [
        "",
        "## 待maintainer决策",
        "",
        "- (如某类型连续差评, 考虑彻底移除其注入逻辑; 可回滚: 编辑 `data/state/inject_feedback_config.json`)",
        "",
        f"config: `{CONFIG_FILE}` | threshold_delta={adj['threshold_delta']}",
        "",
    ]
    report.write_text("\n".join(lines), encoding="utf-8")
    return report

def cmd_analyze(limit: int | None = 50):
    entries = load_feedback(limit)
    prev_cfg = {}
    if CONFIG_FILE.exists():
        try:
            prev_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            prev_cfg = {}
    dist = compute_distribution(entries)
    adj = decide_adjustments(dist, prev_cfg)
    cfg_path = save_config(adj, prev_cfg)
    report = generate_report(dist, adj, len(entries))
    print(f"评分记录: {len(entries)} 条 | 自动调整: {len(adj['notes'])} 项")
    for n in adj["notes"]:
        print(f"  - {n}")
    print(f"config: {cfg_path}")
    print(f"报告:   {report}")
    return 0

def cmd_status():
    if not CONFIG_FILE.exists():
        print("config 未生成 (尚无足够评分数据)")
        return 0
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    print("=== inject_feedback_config.json ===")
    print("注入类型状态:", cfg.get("types"))
    print("冷却倍率:", cfg.get("cooldown_mult"))
    print(f"漂移阈值delta: {cfg.get('threshold_delta')} (cosine=0.40+delta)")
    if cfg.get("notes"):
        print("最近调整:")
        for n in cfg["notes"]:
            print("  -", n)
    return 0

def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "analyze"
    if cmd == "analyze":
        limit = None
        for a in args[1:]:
            if a.startswith("--window"):
                limit = int(args[args.index(a) + 1])
        return cmd_analyze(limit)
    if cmd == "status":
        return cmd_status()
    print("用法: inject_feedback_analyzer.py analyze [--window N] | status")
    return 1

if __name__ == "__main__":
    sys.exit(main())
