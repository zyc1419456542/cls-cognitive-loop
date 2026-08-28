#!/usr/bin/env python3
"""unified_monitor.py — Fast/Slow 双层认知监视器
================================================
替代 4脑区 GWT 竞价, 基于 DS-MCM (Sun et al., 2026):
  Fast Monitor (每工具调用, <1ms): 读 ops_health → 规则匹配
  Slow Monitor (Fast 告警时, ~1s): SF Qwen 语义审计 → guidance 注入

@since: 2026-07-26
"""

import json, sys, time, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# 状态文件
OPS_HEALTH = ROOT / "data" / "state" / "ops_health.json"
DRIFT_LOG = ROOT / "data" / "state" / "drift_log.jsonl"
AUTONOMY = ROOT / "data" / "state" / "autonomy_state.json"
GUIDANCE = ROOT / "data" / "state" / "guidance_injection.md"
MONITOR_STATE = ROOT / "data" / "state" / "_unified_monitor_state.json"

# Fast Monitor 规则
FAST_RULES = {
    "explore_gap": {
        "patterns": ["探索缺口", "Web缺口", "Memory缺口"],
        "guidance": "连续多轮修改未查资料。建议: WebSearch 或 Read memory 后再继续。",
        "severity": "warn",
    },
    "burst": {
        "patterns": ["连续.*次调用同一工具", "工具调用集中"],
        "guidance": "陷入重复操作模式。建议: 换策略, 或先查资料再动手。",
        "severity": "warn",
    },
    "repair_loop": {
        "patterns": ["修复循环", "write→read"],
        "guidance": "可能的修复循环退化。建议: 先确认根因再修改, 限3轮内停止。",
        "severity": "critical",
    },
    "low_diversity": {
        "patterns": [],
        "guidance": "工具使用单一。建议: 考虑 WebSearch 或不同策略扩展思路。",
        "severity": "info",
    },
    "autonomous_drift": {
        "patterns": [],
        "guidance": "自主模式检测到漂移。请对照冻结目标检查当前方向。",
        "severity": "critical",
    },
}

# Slow Monitor 配置
SF_MODEL = "Qwen/Qwen2.5-7B-Instruct"
SLOW_COOLDOWN = 15  # Slow Monitor 最小间隔 (秒), 防频繁调用


# ── Fast Monitor ──────────────────────────────

def _load_ops_health() -> dict:
    if OPS_HEALTH.exists():
        try:
            return json.loads(OPS_HEALTH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_autonomy() -> dict:
    if AUTONOMY.exists():
        try:
            return json.loads(AUTONOMY.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def fast_check() -> list[dict]:
    """Fast Monitor: 读状态文件 → 规则匹配 → 返回告警列表 (<1ms)"""
    alerts = []
    ops = _load_ops_health()
    auto = _load_autonomy()

    # 1. 探索缺口
    for alert_text in ops.get("alerts", []):
        for rule_id, rule in FAST_RULES.items():
            if rule_id == "explore_gap":
                for pat in rule["patterns"]:
                    if pat in alert_text:
                        alerts.append({"rule": rule_id, "guidance": rule["guidance"],
                                       "severity": rule["severity"], "source": "fast"})

    # 2. Burst
    for alert_text in ops.get("alerts", []):
        if "burst" not in [a["rule"] for a in alerts]:
            for rule_id in ["burst", "repair_loop"]:
                for pat in FAST_RULES[rule_id]["patterns"]:
                    if re.search(pat, alert_text):
                        alerts.append({"rule": rule_id,
                                       "guidance": FAST_RULES[rule_id]["guidance"],
                                       "severity": FAST_RULES[rule_id]["severity"],
                                       "source": "fast"})

    # 3. 低多样性
    if ops.get("diversity", 1.0) < 0.2 and ops.get("window", 1) >= 10:
        alerts.append({"rule": "low_diversity",
                       "guidance": FAST_RULES["low_diversity"]["guidance"],
                       "severity": "info", "source": "fast"})

    # 4. 自主漂移
    if auto.get("autonomous"):
        alerts.append({"rule": "autonomous_drift",
                       "guidance": FAST_RULES["autonomous_drift"]["guidance"],
                       "severity": "critical", "source": "fast"})

    return alerts


# ── Slow Monitor ─────────────────────────────

def _slow_cooldown_ok() -> bool:
    """检查 Slow Monitor 冷却时间"""
    if not MONITOR_STATE.exists():
        return True
    try:
        state = json.loads(MONITOR_STATE.read_text(encoding="utf-8"))
        return time.time() - state.get("last_slow_call", 0) > SLOW_COOLDOWN
    except Exception:
        return True


def slow_check(alerts: list[dict]) -> str | None:
    """Slow Monitor: SF Qwen 语义审计 (仅 Fast 有 critical 告警时触发)"""
    critical_alerts = [a for a in alerts if a["severity"] == "critical"]
    if not critical_alerts:
        return None
    if not _slow_cooldown_ok():
        return None

    import urllib.request
    try:
        sf_cfg = json.loads((ROOT / "keys" / "siliconflow_config.json").read_text(encoding="utf-8"))
        ctx = "告警: " + "; ".join(a["guidance"][:40] for a in critical_alerts)
        body = json.dumps({
            "model": SF_MODEL,
            "messages": [
                {"role": "system", "content": "你是认知监视器。根据告警生成<=50字中文纠正建议。只输出建议,不要解释。"},
                {"role": "user", "content": ctx[:300]}
            ],
            "max_tokens": 60, "temperature": 0.3
        }).encode()
        req = urllib.request.Request(
            sf_cfg["base_url"].rstrip("/") + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {sf_cfg['api_key']}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            text = json.loads(resp.read().decode())["choices"][0]["message"]["content"].strip()

        # 更新冷却
        MONITOR_STATE.parent.mkdir(parents=True, exist_ok=True)
        MONITOR_STATE.write_text(json.dumps({"last_slow_call": time.time()}, ensure_ascii=False), encoding="utf-8")
        return text
    except Exception:
        return None


# ── 主入口 ───────────────────────────────────

def tick() -> dict:
    """主入口: Fast → Slow → 写 guidance_injection"""
    t0 = time.time()

    # Fast Monitor
    alerts = fast_check()

    # Slow Monitor (条件触发)
    slow_text = slow_check(alerts) if alerts else None

    # 生成 guidance
    parts = []
    for a in alerts:
        parts.append(f"[Fast/{a['severity']}] {a['guidance']}")
    if slow_text:
        parts.insert(0, f"[Slow/Audit] {slow_text}")

    if parts:
        guidance_text = "\n".join(parts)
        GUIDANCE.parent.mkdir(parents=True, exist_ok=True)
        GUIDANCE.write_text(guidance_text, encoding="utf-8")

    return {
        "fast_alerts": len(alerts),
        "slow_triggered": slow_text is not None,
        "guidance_written": len(parts) > 0,
        "elapsed_ms": round((time.time() - t0) * 1000),
        "alerts": [a["rule"] for a in alerts],
    }


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "--status":
            if GUIDANCE.exists():
                print(GUIDANCE.read_text(encoding="utf-8"))
            else:
                print("No guidance yet")
            return
        elif sys.argv[1] == "--fast":
            print(json.dumps(fast_check(), ensure_ascii=False, indent=2))
            return

    result = tick()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
