#!/usr/bin/env python3
"""
trust_gate.py — 多维信任闸门裁决器  |  v1.0
==============================================
串联 trust_features → 多维门限 → cross_validator → 裁决。

输入: 文件路径
输出: {"verdict": "pass"|"flag"|"fail", "violations": [...], ...}

门限逻辑 (硬编码, 不调 LLM):
  - 8 个维度独立检查，每个维度有 warn/block 区间
  - any block → fail
  - any warn (无 block) → flag
  - 全通过 → pass

v1.1 改进:
  - 短文本跳过熵/谱半径特征
  - NaN/None 特征值跳过该维度检查
  - 应急门检查 (emergency_bypass.flag)
  - 连环失败冷却 (基于文件内容哈希)
  - 交叉验证结果附加到裁决中 (warn 不 block)

用法:
    from scripts.safety.trust_gate import gate
    result = gate("path/to/delivery.md")
    if result["verdict"] == "fail":
        print(f"拦截: {result['violations']}")

CLI:
    python scripts/safety/trust_gate.py --file <path> [--json] [--force]
"""

import json, os, sys, time, hashlib
from pathlib import Path
from datetime import datetime, timezone

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

from scripts.safety.trust_features import extract as extract_features
from scripts.safety.cross_validator import validate as cross_validate


CONFIG_PATH = _PROJECT_ROOT / "data" / "safety" / "trust_gate_config.json"
EMERGENCY_BYPASS = _PROJECT_ROOT / "data" / "safety" / "emergency_bypass.flag"
COOLDOWN_FILE = _PROJECT_ROOT / "data" / "safety" / "trust_cooldown.json"


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        raise RuntimeError(f"无法加载配置文件: {CONFIG_PATH}")


def _check_emergency() -> bool:
    """检查应急门是否开启"""
    return EMERGENCY_BYPASS.exists()


def _check_cooldown(file_path: str) -> dict | None:
    """检查连环失败冷却。返回 None 表示可以继续，否则返回拒绝信息。"""
    if not COOLDOWN_FILE.exists():
        return None
    try:
        cd = json.loads(COOLDOWN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None

    # 用文件内容哈希（而非路径）
    try:
        content = Path(file_path).read_text(encoding="utf-8")
        fhash = hashlib.sha256(content.encode()).hexdigest()[:16]
    except Exception:
        fhash = hashlib.sha256(file_path.encode()).hexdigest()[:16]

    if fhash not in cd:
        return None

    entry = cd[fhash]
    if entry.get("in_cooldown"):
        cooldown_until = entry.get("cooldown_until", "")
        if cooldown_until:
            try:
                until = datetime.fromisoformat(cooldown_until)
                if datetime.now(timezone.utc) < until:
                    remaining = (until - datetime.now(timezone.utc)).total_seconds() / 60
                    return {
                        "blocked": True,
                        "reason": f"连环失败冷却中，{remaining:.0f}分钟后重试",
                        "fail_count": entry.get("fail_count", 0),
                    }
            except Exception:
                pass

    return None


def _update_cooldown(file_path: str, verdict: str):
    """更新冷却状态"""
    try:
        content = Path(file_path).read_text(encoding="utf-8")
        fhash = hashlib.sha256(content.encode()).hexdigest()[:16]
    except Exception:
        fhash = hashlib.sha256(file_path.encode()).hexdigest()[:16]

    try:
        cd = {}
        if COOLDOWN_FILE.exists():
            cd = json.loads(COOLDOWN_FILE.read_text(encoding="utf-8"))
    except Exception:
        cd = {}

    if fhash not in cd:
        cd[fhash] = {"hash": fhash, "fail_count": 0, "in_cooldown": False}

    if verdict == "fail":
        cd[fhash]["fail_count"] = cd[fhash].get("fail_count", 0) + 1
        cd[fhash]["last_fail"] = datetime.now(timezone.utc).isoformat()
    else:
        cd[fhash]["fail_count"] = 0
        cd[fhash]["in_cooldown"] = False

    # 连续 fail >= 3 → 冷却
    cfg = _load_config()
    max_fails = cfg.get("global", {}).get("session_cooldown", {}).get("max_consecutive_fails", 3)
    cooldown_min = cfg.get("global", {}).get("session_cooldown", {}).get("cooldown_minutes", 30)

    if cd[fhash].get("fail_count", 0) >= max_fails:
        cd[fhash]["in_cooldown"] = True
        cd[fhash]["cooldown_until"] = (
            datetime.now(timezone.utc) + __import__("datetime").timedelta(minutes=cooldown_min)
        ).isoformat()

    COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(COOLDOWN_FILE, "w", encoding="utf-8") as f:
        json.dump(cd, f, ensure_ascii=False, indent=2)


def _apply_gate(features: dict, config: dict) -> dict:
    """多维门限裁决 (硬编码)"""
    violations = []
    dims = config.get("dimensions", {})

    for dim, rules in dims.items():
        val = features.get(dim)

        # 跳过无效值
        if val is None:
            if rules.get("skip_if_null", True):
                continue
            else:
                val = 0.0
        if isinstance(val, float) and (np.isnan(val) or np.isinf(val)):
            if rules.get("skip_if_null", True):
                continue
            else:
                val = 0.0

        block_min = rules.get("block_min")
        block_max = rules.get("block_max")
        warn_min = rules.get("warn_min")
        warn_max = rules.get("warn_max")

        # Block 检查 (先检查 block，再 warn)
        if block_min is not None and val < block_min:
            violations.append({
                "feature": dim,
                "value": round(float(val), 4),
                "range": f"< {block_min}",
                "severity": "block",
                "description": rules.get("description", ""),
            })
        elif block_max is not None and val > block_max:
            violations.append({
                "feature": dim,
                "value": round(float(val), 4),
                "range": f"> {block_max}",
                "severity": "block",
                "description": rules.get("description", ""),
            })
        # Warn 检查
        elif warn_min is not None and val < warn_min:
            violations.append({
                "feature": dim,
                "value": round(float(val), 4),
                "range": f"< {warn_min}",
                "severity": "warn",
                "description": rules.get("description", ""),
            })
        elif warn_max is not None and val > warn_max:
            violations.append({
                "feature": dim,
                "value": round(float(val), 4),
                "range": f"> {warn_max}",
                "severity": "warn",
                "description": rules.get("description", ""),
            })

    blocks = [v for v in violations if v["severity"] == "block"]
    warns = [v for v in violations if v["severity"] == "warn"]

    if blocks:
        return {"verdict": "fail", "violations": violations, "block_count": len(blocks), "warn_count": len(warns)}
    elif warns:
        return {"verdict": "flag", "violations": violations, "block_count": 0, "warn_count": len(warns)}
    else:
        return {"verdict": "pass", "violations": [], "block_count": 0, "warn_count": 0}


def gate(file_path: str, force: bool = False, report: bool = False) -> dict:
    """
    主入口 — 对文件执行完整的信任闸门检查。

    参数:
        file_path: 待检查的 .md 文件路径
        force: 跳过应急门和冷却检查
        report: 在被检查文件旁边写入 trust_gate_report.md

    返回: {verdict, violations, features, cross_validation, cooldown, emergency, elapsed_ms}
    """
    t0 = time.perf_counter()

    # ── 应急门 ──
    if not force and _check_emergency():
        return {
            "verdict": "pass",
            "violations": [],
            "features": {},
            "cross_validation": None,
            "emergency": True,
            "elapsed_ms": 0.0,
            "reason": "应急门开启，所有检查跳过",
        }

    # ── 冷却检查 ──
    if not force:
        cd = _check_cooldown(file_path)
        if cd and cd.get("blocked"):
            return {
                "verdict": "fail",
                "violations": [{"feature": "cooldown", "severity": "block", "reason": cd["reason"]}],
                "features": {},
                "cross_validation": None,
                "cooldown": cd,
                "emergency": False,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            }

    config = _load_config()

    # ── 读文件 ──
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except Exception as e:
        return {
            "verdict": "fail",
            "violations": [{"feature": "io_error", "severity": "block", "reason": str(e)}],
            "features": {},
            "cross_validation": None,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    # ── 特征提取 ──
    feat_result = extract_features(text)
    features = feat_result["features"]
    feat_meta = feat_result["meta"]

    # ── 门限裁决 ──
    gate_result = _apply_gate(features, config)

    # ── 交叉验证 (仅当 qwen_gate 相关特征出现问题时才跑完整版) ──
    cv_result = None
    cv_config = config.get("cross_validation", {})
    if cv_config.get("enabled", True):
        try:
            cv_full = cross_validate(use_cache=True)
            cv_result = {
                "consistent": cv_full["consistent"],
                "matched": cv_full["matched_pairs"],
                "orphans_gate": len(cv_full["orphan_gate_entries"]),
                "orphans_api": len(cv_full["orphan_api_calls"]),
                "mismatched": len(cv_full["mismatched_pairs"]),
            }
            # 交叉验证不一致 → 附加 warn
            if not cv_full["consistent"] and cv_config.get("warn_on_inconsistency", True):
                if gate_result["verdict"] == "pass":
                    gate_result["verdict"] = "flag"
                    gate_result["violations"].append({
                        "feature": "cross_validation",
                        "severity": "warn",
                        "reason": f"交叉验证发现不一致: {cv_result['orphans_gate']}孤立gate / {cv_result['mismatched']}不匹配",
                    })
                    gate_result["warn_count"] = gate_result.get("warn_count", 0) + 1
        except Exception:
            cv_result = {"error": "交叉验证引擎异常"}

    # ── 更新冷却 ──
    _update_cooldown(file_path, gate_result["verdict"])

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    result = {
        "verdict": gate_result["verdict"],
        "violations": gate_result["violations"],
        "block_count": gate_result.get("block_count", 0),
        "warn_count": gate_result.get("warn_count", 0),
        "features": features,
        "feature_meta": feat_meta,
        "cross_validation": cv_result,
        "emergency": False,
        "elapsed_ms": elapsed_ms,
    }

    # ── 写入可见报告 ──
    if report:
        try:
            _write_report(file_path, result)
        except Exception:
            pass

    return result


def _write_report(file_path: str, result: dict):
    """在被检查文件旁边写入可读的信任闸门报告 (trust_gate_report.md)"""
    report_path = Path(file_path).with_suffix(".trust_gate.md")
    verdict_icon = {"pass": "✅", "flag": "⚠️", "fail": "🛑"}
    icon = verdict_icon.get(result["verdict"], "❓")
    f = result.get("features", {})
    cv = result.get("cross_validation", {})
    meta = result.get("feature_meta", {})

    lines = [
        f"# {icon} 信任闸门: {result['verdict'].upper()}",
        "",
        f"**文件**: `{file_path}`",
        f"**耗时**: {result.get('elapsed_ms', 0)}ms | **block**: {result.get('block_count', 0)} | **warn**: {result.get('warn_count', 0)}",
        "",
    ]

    if result.get("emergency"):
        lines.append("> ⚠️ 应急门开启 — 跳过所有检查")
    elif result.get("cooldown"):
        lines.append(f"> 🛑 冷却中: {result['cooldown'].get('reason', '')}")
    else:
        # 特征向量表
        if f:
            lines.append("## 特征向量")
            lines.append("")
            lines.append("| 维度 | 值 | 区间 |")
            lines.append("|------|-----|------|")
            cfg = _load_config().get("dimensions", {})
            for dim, val in f.items():
                rules = cfg.get(dim, {})
                wmin, wmax = rules.get("warn_min"), rules.get("warn_max")
                bmin, bmax = rules.get("block_min"), rules.get("block_max")
                parts = []
                if bmin is not None: parts.append(f"block<{bmin}")
                if bmax is not None: parts.append(f"block>{bmax}")
                if wmin is not None: parts.append(f"warn<{wmin}")
                if wmax is not None: parts.append(f"warn>{wmax}")
                range_str = ", ".join(parts) if parts else "—"
                lines.append(f"| {dim} | {val} | {range_str} |")
            if meta:
                lines.append(f"| *(chars/lines/tokens)* | {meta.get('total_chars','?')}/{meta.get('total_lines','?')}/{meta.get('token_count','?')} | — |")
            lines.append("")

        # 违规
        violations = result.get("violations", [])
        if violations:
            lines.append("## 违规")
            lines.append("")
            for v in violations:
                sev = "🛑" if v.get("severity") == "block" else "⚠️"
                lines.append(f"- {sev} **[{v.get('severity')}]** `{v.get('feature', '?')}`: {v.get('reason', v.get('description', str(v.get('range', ''))))}")

        # 交叉验证
        if cv and not cv.get("error"):
            cv_icon = "✅" if cv.get("consistent") else "⚠️"
            lines.append("")
            lines.append(f"## {cv_icon} 交叉验证")
            lines.append("")
            lines.append(f"| matched | orphans_gate | orphans_api | mismatched |")
            lines.append(f"|---------|--------------|-------------|------------|")
            lines.append(f"| {cv.get('matched', 0)} | {cv.get('orphans_gate', 0)} | {cv.get('orphans_api', 0)} | {cv.get('mismatched', 0)} |")

    lines.append("")
    lines.append(f"*{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} — trust_gate.py v1.0*")

    report_text = "\n".join(lines)
    report_path.write_text(report_text, encoding="utf-8")

    # ── 同时写入永久审计归档 ──
    archive_dir = _PROJECT_ROOT / "data" / "audit" / "trust_gate_archive"
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        # 文件名: YYYYMMDD_HHMMSS_原文件名_verdict.md
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_name = Path(file_path).stem.replace(" ", "_")[:40]
        archive_path = archive_dir / f"{ts}_{safe_name}_{result.get('verdict', '?')}.md"
        archive_path.write_text(report_text, encoding="utf-8")

        # 更新索引
        index_path = archive_dir / "INDEX.md"
        existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
        if not existing:
            existing = "# Trust Gate Audit Index\n\n| Time | File | Verdict | Block | Warn |\n|------|------|------|-------|------|\n"
        new_row = f"| {ts} | `{file_path}` | {result.get('verdict', '?')} | {result.get('block_count', 0)} | {result.get('warn_count', 0)} |"
        index_path.write_text(existing.rstrip() + "\n" + new_row + "\n", encoding="utf-8")
    except Exception:
        pass


def should_check(file_path: str) -> bool:
    """判断文件是否应被信任闸门检查"""
    path_str = str(file_path).replace("\\", "/")
    config = _load_config()
    delivery_dirs = config.get("global", {}).get("delivery_dirs", ["output", "data/memory"])
    extensions = config.get("global", {}).get("file_extensions", [".md"])
    min_chars = config.get("global", {}).get("min_chars_for_check", 200)

    # 扩展名检查
    if not any(path_str.endswith(ext) for ext in extensions):
        return False

    # 目录检查
    if not any(d in path_str for d in delivery_dirs):
        return False

    # 文件大小检查
    try:
        if Path(file_path).stat().st_size < min_chars:
            return False
    except Exception:
        return False

    return True


# ── CLI ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="多维信任闸门裁决器")
    parser.add_argument("--file", type=str, required=True, help="待检查的文件路径")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--force", action="store_true", help="跳过应急门和冷却")
    parser.add_argument("--no-cross-validate", action="store_true", help="跳过交叉验证 (更快)")
    parser.add_argument("--report", action="store_true", help="在被检查文件旁边写入可见报告 (.trust_gate.md)")
    args = parser.parse_args()

    if not args.file:
        parser.error("--file 是必选参数")

    result = gate(args.file, force=args.force, report=args.report)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        verdict_icon = {"pass": "✅", "flag": "⚠️", "fail": "🛑"}
        icon = verdict_icon.get(result["verdict"], "❓")

        print(f"\n{icon} 信任闸门裁决: {result['verdict'].upper()}")
        print("=" * 60)
        print(f"  耗时: {result['elapsed_ms']}ms")

        if result.get("emergency"):
            print(f"  ⚠ 应急门开启 — 跳过所有检查")
            return

        if result.get("cooldown"):
            print(f"  🛑 冷却中: {result['cooldown']['reason']}")
            return

        f = result.get("features", {})
        if f:
            print(f"\n  特征向量:")
            for k, v in f.items():
                print(f"    {k:28s}: {v}")

        if result["violations"]:
            print(f"\n  违规 ({len(result['violations'])}):")
            for v in result["violations"]:
                sev_icon = "🛑" if v["severity"] == "block" else "⚠️"
                print(f"    {sev_icon} [{v['severity']}] {v.get('feature','?')}: {v.get('reason', v.get('description', str(v.get('range',''))))}")

        cv = result.get("cross_validation")
        if cv:
            cv_icon = "✅" if cv.get("consistent") else "⚠"
            print(f"\n  {cv_icon} 交叉验证: matched={cv.get('matched')}, orphans={cv.get('orphans_gate')}, mismatched={cv.get('mismatched')}")

        print("=" * 60)

    sys.exit(1 if result["verdict"] == "fail" else 0)


if __name__ == "__main__":
    main()
