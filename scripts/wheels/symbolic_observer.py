#!/usr/bin/env python3
"""
symbolic_observer.py — 符号动力学观测轮子
========================================
**不分析，只搬运。** 把流水线事件编码成符号观测 → 喂给引擎 → 记录结果。

历史教义（Hadamard 1898 / Morse-Hedlund 1938）：
  符号动力学曾是"从连续到离散的桥"——把不可解的测地流离散化成符号序列。
  我们的观测轮子就是这个桥的反向：把离散的流水线事件映射成符号域的观测。

用法:
  python scripts/wheels/symbolic_observer.py capture <text>  # 捕获用户消息
  python scripts/wheels/symbolic_observer.py snapshot [domain] # 熵快照
  python scripts/wheels/symbolic_observer.py status            # 健康摘要
  python scripts/wheels/symbolic_observer.py trend [domain]    # 熵趋势
  python scripts/wheels/symbolic_observer.py replay <file>     # 重放历史消息

集成点:
  - pipeline.py cooldown → snapshot
  - 每次用户消息 → capture (手动或自动)
  - self_activate → status 检查

数据流:
  用户消息 → channel_scanner → 符号映射 → 域引擎.observe() → 禁止词/熵检查
  → 写 data/symbolic_dynamics/observations/<domain>.jsonl
  → 异常时 data/symbolic_dynamics/alerts.jsonl
"""

import json, os, sys, time, glob
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent
OBS_DIR = ROOT / "data" / "symbolic_dynamics" / "observations"
DOMAIN_DIR = ROOT / "data" / "symbolic_dynamics" / "domains"
ALERTS_FILE = ROOT / "data" / "symbolic_dynamics" / "alerts.jsonl"
OBS_DIR.mkdir(parents=True, exist_ok=True)
DOMAIN_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT / "scripts"))
from symbolic_dynamics_engine import (
    create_dialogue_domain, create_cad_domain,
    create_quant_domain, create_hook_enforcement_domain,
    create_window_domain, create_pic_domain,
    create_image_domain, create_retrieval_domain,
    DomainEngine, entropy_rate
)


# ═══════════════════════════════════════════
# 域引擎管理（持久化）
# ═══════════════════════════════════════════

_DOMAIN_FACTORIES = {
    "dialogue": create_dialogue_domain,
    "cad": create_cad_domain,
    "quant": create_quant_domain,
    "hook": create_hook_enforcement_domain,
    "window": create_window_domain,
    "pic": create_pic_domain,
    "image": create_image_domain,
    "retrieval": create_retrieval_domain,
}

_DOMAIN_FILE = {
    "dialogue": DOMAIN_DIR / "dialogue.json",
    "cad": DOMAIN_DIR / "cad.json",
    "quant": DOMAIN_DIR / "quant.json",
    "hook": DOMAIN_DIR / "hook.json",
    "window": DOMAIN_DIR / "window.json",
    "pic": DOMAIN_DIR / "pic.json",
    "image": DOMAIN_DIR / "image.json",
    "retrieval": DOMAIN_DIR / "retrieval.json",
}

def _load_domain(name: str) -> DomainEngine:
    """加载域引擎，优先从磁盘快照恢复"""
    f = _DOMAIN_FACTORIES.get(name)
    if f is None:
        print(f"[OBS] 未知域: {name}", file=sys.stderr)
        sys.exit(1)

    snap_file = _DOMAIN_FILE[name]
    if snap_file.exists():
        try:
            snap = json.loads(snap_file.read_text(encoding='utf-8'))
            # 从快照重建：直接从工厂创建后，用快照覆盖
            engine = f(load_observations=snap.get("observations", []))
            # 用快照的禁止词替换默认的（按 pattern 去重）
            snap_patterns = {json.dumps(fw["pattern"], sort_keys=True): fw
                            for fw in snap.get("forbidden_words", [])}
            # 保留不在快照中的默认禁止词，追加快照独有的
            seen_patterns = set()
            merged = []
            for fw in engine.forbidden_words:
                key = json.dumps(fw["pattern"], sort_keys=True)
                if key not in seen_patterns:
                    seen_patterns.add(key)
                    merged.append(fw)
            for key, fw in snap_patterns.items():
                if key not in seen_patterns:
                    seen_patterns.add(key)
                    merged.append(fw)
            engine.forbidden_words = merged
            return engine
        except Exception as e:
            print(f"[OBS] 快照加载失败，新建: {e}", file=sys.stderr)
            return f()
    return f()


def _save_domain(name: str, engine: DomainEngine):
    """持久化域引擎状态"""
    try:
        engine.compute()  # 确保最新
        snap = engine.snapshot()
        _DOMAIN_FILE[name].write_text(
            json.dumps(snap, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
    except Exception as e:
        print(f"[OBS] 快照保存失败: {e}", file=sys.stderr)


# ═══════════════════════════════════════════
# 消息 → 符号映射层
# ═══════════════════════════════════════════

def _classify_text(text: str) -> dict:
    """用 channel_scanner 分类消息"""
    try:
        from channel_scanner_proto import scan_input
        return scan_input(text)
    except ImportError:
        return {}
    except Exception:
        return {}


# channel_scanner 的 action_type → 对话域符号映射
_ACTION_TO_SYMBOL = {
    "追问": "ask",
    "命令": "command",
    "跳转": "jump",
    "构建": "construct",
    "修正": "correct",
    "评价": "evaluate",
    "情感": "emotion",
    "确认": "confirm",
    "陈述": "synthesize",  # 默认落到综合
}

# channel_scanner 的 domain → 符号动力学域映射
_DOMAIN_TO_ENGINE = {
    "CAD": "cad",
    "QUANT": "quant",
    "PIC": "pic",      # PIC<介质>仿真→物理质量检查域
    "CODE": "dialogue",  # 代码对话走对话域
    "MATH": "dialogue",  # 数学对话走对话域
    "SYSTEM": "dialogue",  # 系统运维对话走对话域
    "WINDOW": "window",  # 跨窗口感知走窗口域
    "IMAGE": "image",   # 图像分析走图像域
}

def _map_to_observation(text: str) -> list[dict]:
    """把用户消息编码成一个或多个符号观测

    返回: [{"domain": str, "symbol": str, "raw": str, "ts": str}, ...]
    """
    cls = _classify_text(text)
    action = cls.get("主要动作", "陈述")
    symbol = _ACTION_TO_SYMBOL.get(action, "synthesize")
    domains = cls.get("域", []) or ["DIALOGUE"]

    results = []
    seen_engines = set()  # 去重：同一引擎只记一次
    for d in domains:
        engine_name = _DOMAIN_TO_ENGINE.get(d, "dialogue")
        if engine_name is None or engine_name in seen_engines:
            continue
        seen_engines.add(engine_name)
        results.append({
            "domain": engine_name,
            "symbol": symbol,
            "action": action,
            "emotion": cls.get("峰值情感"),
            "route": cls.get("路由", "mixed"),
            "raw": text[:100],
            "ts": datetime.now().isoformat(),
        })

    # 如果没有匹配到域（全部 None），至少走 dialogue
    if not results:
        results.append({
            "domain": "dialogue",
            "symbol": symbol,
            "action": action,
            "emotion": cls.get("峰值情感"),
            "route": cls.get("路由", "mixed"),
            "raw": text[:100],
            "ts": datetime.now().isoformat(),
        })

    return results


# ═══════════════════════════════════════════
# 观测记录 + 引擎交互
# ═══════════════════════════════════════════

def _get_obs_file(domain: str) -> Path:
    return OBS_DIR / f"{domain}.jsonl"


def _save_observation(domain: str, entry: dict):
    """追加一条观测到 JSONL"""
    obs_file = _get_obs_file(domain)
    with open(obs_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _compute_observed_entropy_rate(engine: DomainEngine) -> float:
    """从实际观测序列计算经验熵率（反映真实行为的不确定性）"""
    obs = engine.observations
    if len(obs) < 3:
        return 0.0  # 观测太少，不可靠

    n = len(engine.symbols)
    # 构建经验转移矩阵
    emp_T = np.zeros((n, n))
    for i in range(len(obs) - 1):
        a, b = obs[i], obs[i+1]
        if a in engine.sym_to_idx and b in engine.sym_to_idx:
            emp_T[engine.sym_to_idx[a], engine.sym_to_idx[b]] += 1.0

    # 行归一化
    row_sums = emp_T.sum(axis=1)
    for i in range(n):
        if row_sums[i] > 0:
            emp_T[i] /= row_sums[i]

    # 检查是否可算
    nnz_rows = (row_sums > 0).sum()
    if nnz_rows < 2:
        return 0.0

    try:
        return entropy_rate(emp_T)
    except Exception:
        return 0.0


def _check_anomaly(domain: str, result: dict, engine: DomainEngine, observed_h: float = 0.0) -> list[dict]:
    """检查异常：禁止词命中 + 熵超限 + 熔断板桥接

    返回 alert 列表。禁止词 severity >= 0.8 时自动通知熔断板。
    """
    alerts = []
    # 0. 数值越界检测（新增 — PIC/CAD物理约束审计）
    for nv in result.get("numeric_violations", []):
        alert = {
            "domain": domain, "type": "numeric_violation",
            "symbol": nv["symbol"], "value": nv["value"],
            "unit": nv.get("unit", ""), "range": nv.get("range", []),
            "violation": nv["violation"], "reason": nv["reason"],
            "severity": nv["severity"],
            "ts": datetime.now().isoformat(),
        }
        alerts.append(alert)
        with open(ALERTS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(alert, ensure_ascii=False) + "\n")
        # 数值越界 severity>=0.8 触发熔断板桥接
        if nv["severity"] >= 0.8:
            try:
                sys.path.insert(0, str(ROOT / "scripts"))
                from fuse_board import fuse_board
                fuse_board.check("DUAL_AI_GATE", {
                    "source": "symbolic_observer/numeric_violation",
                    "domain": domain, "symbol": nv["symbol"],
                    "reason": nv["reason"], "severity": nv["severity"],
                })
            except Exception:
                pass

    # 1. 禁止词命中
    for hit in result.get("forbidden_hits", []):
        alert = {
            "domain": domain,
            "type": "forbidden_hit",
            "pattern": hit["pattern"],
            "reason": hit["reason"],
            "severity": hit["severity"],
            "ts": datetime.now().isoformat(),
        }
        alerts.append(alert)
        with open(ALERTS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(alert, ensure_ascii=False) + "\n")

        # ── 熔断板桥接（来源: 认知循环 v19, 阈值: fuse_severity_threshold=0.8）──
        if hit["severity"] >= 0.8:
            try:
                sys.path.insert(0, str(ROOT / "scripts"))
                from fuse_board import fuse_board
                fuse_board.check("DUAL_AI_GATE", {
                    "source": "symbolic_observer",
                    "domain": domain,
                    "pattern": hit["pattern"],
                    "reason": hit["reason"],
                    "severity": hit["severity"],
                })
            except Exception:
                pass  # 熔断板不可用时静默降级

    # 2. 熵超限
    h = result.get("entropy", 0)
    stability = result.get("stability", "unknown")
    n = result.get("alphabet_size", 10)

    if stability == "frozen" and result.get("obs_count", 0) > 10:
        alert = {
            "domain": domain,
            "type": "entropy_frozen",
            "entropy": h,
            "obs_count": result["obs_count"],
            "suggestion": "系统卡死，建议引入随机扰动",
            "ts": datetime.now().isoformat(),
        }
        alerts.append(alert)
    elif stability == "diverging" and result.get("obs_count", 0) > 10:
        alert = {
            "domain": domain,
            "type": "entropy_diverging",
            "entropy": h,
            "obs_count": result["obs_count"],
            "suggestion": "路径发散，建议收紧约束或增禁止词",
            "ts": datetime.now().isoformat(),
        }
        alerts.append(alert)

    for a in alerts:
        if a["type"] not in ("forbidden_hit",):
            with open(ALERTS_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")

    return alerts


# ═══════════════════════════════════════════
# 跨窗口感知 → 符号观测桥接
# ═══════════════════════════════════════════

def _scan_windows_and_observe() -> dict:
    """调用 cross_window_awareness.peek() → 编码窗口状态 → 喂入window域引擎

    返回: {"symbol": str, "window_count": int, "domains_active": [...], "conflicts": [...]}
    """
    try:
        from cross_window_awareness import peek, detect_conflicts
    except ImportError:
        return {"symbol": "window_silence", "window_count": 0, "active_count": 0, "domains_active": [], "error": "import_failed"}

    windows = peek()
    if not windows:
        # 无窗口在线 → 静默
        return {"symbol": "window_silence", "window_count": 0, "active_count": 0, "domains_active": [], "conflicts": [], "overlaps": []}

    active = [w for w in windows if w.get("status") == "active"]
    domains = {}
    for w in active:
        d = w.get("domain", "general")
        domains[d] = domains.get(d, 0) + 1

    # 冲突检测：同一域 + 关键词重叠（不只是同域）
    # 同域做不同的事=正常多任务；同域+重叠关键词=真重复劳动
    conflicts = []
    overlaps = []
    for d, count in domains.items():
        if count < 2 or not d:
            continue
        # 找出该域下的所有活跃窗口
        domain_windows = [w for w in active if w.get("domain") == d]
        for i, w1 in enumerate(domain_windows):
            for w2 in domain_windows[i+1:]:
                f1 = w1.get("focus", "").lower()
                f2 = w2.get("focus", "").lower()
                if f1 and f2:
                    words1 = set(f1.split())
                    words2 = set(f2.split())
                    common = words1 & words2 - {
                        "的", "和", "了", "在", "是", "the", "a", "an", "to", "of", "in",
                        "?", "-", "—", "for", "with", "and", "or", "not", "is", "be", "on",
                        "记事本", "管理员:", "select", "个人", "microsoft", "另外", "edge",
                        "claude", "code",
                    }
                    if len(common) >= 2:
                        conflicts.append({
                            "domain": d, "count": count,
                            "w1": w1.get("window_id"), "w2": w2.get("window_id"),
                            "common_words": list(common)[:5],
                        })

    # 跨域重叠检测：不同域但关键词重叠
    for i, w1 in enumerate(active):
        for w2 in active[i+1:]:
            if w1.get("domain") == w2.get("domain"):
                continue  # 同域已在冲突检测中处理
            f1 = w1.get("focus", "").lower()
            f2 = w2.get("focus", "").lower()
            if f1 and f2:
                words1 = set(f1.split())
                words2 = set(f2.split())
                common = words1 & words2 - {
                    "的", "和", "了", "在", "是", "the", "a", "an", "to", "of", "in",
                    "?", "-", "—", "for", "with", "and", "or", "not", "is", "be", "on",
                    "记事本", "管理员:", "select", "个人", "microsoft", "另外", "edge",
                    "claude", "code",
                }
                if len(common) >= 3:  # 跨域需要更多重叠才算
                    overlaps.append({
                        "w1": w1.get("window_id"), "w2": w2.get("window_id"),
                        "domain1": w1.get("domain"), "domain2": w2.get("domain"),
                        "common_words": list(common)[:5],
                    })

    # 编码符号
    if conflicts:
        symbol = "window_conflict"
    elif overlaps:
        symbol = "window_overlap"
    elif len(active) >= 1:
        symbol = "window_active"
    elif len(windows) >= 1:
        symbol = "window_idle"
    else:
        symbol = "window_silence"

    return {
        "symbol": symbol,
        "window_count": len(windows),
        "active_count": len(active),
        "domains_active": list(domains.keys()),
        "conflicts": conflicts,
        "overlaps": overlaps,
    }


def _feed_window_observation(result: dict):
    """把窗口扫描结果喂入窗口域引擎并持久化"""
    engine = _load_domain("window")
    symbol = result["symbol"]

    if symbol not in engine.sym_to_idx:
        symbol = "window_healthy"

    engine.observe(symbol)

    obs_entry = {
        "domain": "window",
        "symbol": symbol,
        "action": "window_scan",
        "window_count": result.get("window_count", 0),
        "active_count": result.get("active_count", 0),
        "domains_active": result.get("domains_active", []),
        "conflicts": result.get("conflicts", []),
        "overlaps": result.get("overlaps", []),
        "raw": f"windows={result.get('window_count',0)} active={result.get('active_count',0)} {symbol}",
        "ts": datetime.now().isoformat(),
    }

    _save_observation("window", obs_entry)

    # 计算 + 异常检查
    engine_result = engine.compute()
    obs_h = _compute_observed_entropy_rate(engine)
    engine_result["obs_entropy_rate"] = obs_h

    _save_domain("window", engine)
    alerts = _check_anomaly("window", engine_result, engine, obs_h)

    # 如果检测到冲突，也更新 cross_window_context.json（保持兼容）
    if result.get("conflicts"):
        try:
            import json as _json
            from pathlib import Path as _Path
            ctx_file = _Path(__file__).resolve().parent.parent.parent / "state" / "cross_window_context.json"
            if ctx_file.exists():
                ctx = _json.loads(ctx_file.read_text(encoding="utf-8"))
                for w in ctx:
                    w["_last_symbolic_scan"] = datetime.now().isoformat()
                    if any(c["domain"] == w.get("domain") for c in result["conflicts"]):
                        w["_conflict_flag"] = True
                ctx_file.write_text(_json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return {
        "engine_result": engine_result,
        "alerts": alerts,
        "obs_h": obs_h,
    }


# ═══════════════════════════════════════════
# CLI 命令
# ═══════════════════════════════════════════

def cmd_capture(args):
    """捕获用户消息 → 编码 → 喂引擎 → 检查异常"""
    # 解析标志
    quiet = False
    text_parts = []
    for a in args:
        if a == "--quiet":
            quiet = True
        else:
            text_parts.append(a)

    text = " ".join(text_parts)
    if not text:
        msg = {"error": "用法: symbolic_observer.py capture <text>"}
        print(json.dumps(msg, ensure_ascii=False) if quiet else "[OBS] 用法: symbolic_observer.py capture <text>")
        return

    obs_list = _map_to_observation(text)
    if not obs_list:
        msg = {"error": f"无法编码: {text[:40]}...", "text": text[:100]}
        print(json.dumps(msg, ensure_ascii=False) if quiet else f"[OBS] [!] 无法编码: {text[:40]}...")
        return

    all_results = []
    for obs in obs_list:
        domain = obs["domain"]
        symbol = obs["symbol"]

        # 加载/创建域引擎
        engine = _load_domain(domain)

        # 验证符号是否在符号表中
        if symbol not in engine.sym_to_idx:
            # 尝试用默认值
            symbol = "synthesize" if domain == "dialogue" else list(engine.sym_to_idx.keys())[0]
            obs["symbol"] = symbol

        # 记录观测
        engine.observe(symbol)

        # 保存观测日志
        _save_observation(domain, obs)

        # 计算
        result = engine.compute()

        # 经验熵率
        obs_h = _compute_observed_entropy_rate(engine)
        result["obs_entropy_rate"] = obs_h

        # 持久化引擎状态
        _save_domain(domain, engine)

        # 异常检查
        alerts = _check_anomaly(domain, result, engine, obs_h)

        all_results.append({
            "domain": domain,
            "symbol": symbol,
            "entropy": result["entropy"],
            "obs_entropy_rate": obs_h,
            "stability": result["stability"],
            "obs_count": result["obs_count"],
            "alphabet_size": result["alphabet_size"],
            "forbidden_count": result["forbidden_count"],
            "alerts": alerts,
        })

        if quiet:
            continue

        print(f"[OBS] {domain}.observe({symbol})  "
              f"h_top={result['entropy']:.4f}  "
              f"h_obs={obs_h:.4f}  "
              f"[{result['stability']}]  "
              f"obs={result['obs_count']}  "
              f"|Σ|={result['alphabet_size']}  "
              f"禁止词={result['forbidden_count']}")
        for a in alerts:
            print(f"  [!] [{a['type']}] {a.get('reason', a.get('suggestion', ''))}")

    if quiet:
        print(json.dumps({"captured": all_results}, ensure_ascii=False))


def cmd_snapshot(args):
    """输出所有/指定域的快照"""
    domains = [args[0]] if args else ["dialogue", "cad", "quant", "hook", "window", "image", "retrieval"]

    for name in domains:
        engine = _load_domain(name)
        result = engine.compute()
        obs_h = _compute_observed_entropy_rate(engine)
        obs_file = _get_obs_file(name)
        obs_count = 0
        if obs_file.exists():
            with open(obs_file, 'r', encoding='utf-8') as f:
                obs_count = sum(1 for _ in f if _.strip())

        print(f"\n{'='*50}")
        print(f"域: {name}")
        print(f"{'='*50}")
        print(f"  字母表 |Σ|:     {result['alphabet_size']}")
        print(f"  平均后续数:     {result['avg_successors']}")
        print(f"  拓扑熵 h_top:   {result['entropy']:.4f}  (转移矩阵允许的路径复杂度)")
        print(f"  经验熵率 h_obs: {obs_h:.4f}  (实际观测的行为不确定性)")
        print(f"  谱半径:         {result['spectral_radius']}")
        print(f"  稳定性:         {result['stability']}")
        print(f"  禁止词数:       {result['forbidden_count']}")
        print(f"  观测数:         {obs_count} (内存: {result['obs_count']})")
        print(f"  稳态分布(前3):")

        if result["stationary_ranking"]:
            for item in result["stationary_ranking"][:3]:
                print(f"    {item['symbol']:15s}  {item['frequency']:.4f}  ({item.get('name','')})")

        if result["forbidden_hits"]:
            print(f"  [!] 禁止词命中 ({len(result['forbidden_hits'])} 条):")
            for h in result["forbidden_hits"]:
                print(f"    [{h['severity']:.1f}] {h['pattern']} — {h['reason']}")

        if result.get("numeric_violations"):
            print(f"  [NV] 数值越界 ({len(result['numeric_violations'])} 条):")
            for nv in result["numeric_violations"]:
                print(f"    [{nv['severity']:.1f}] {nv['symbol']}={nv['value']} {nv.get('unit','')} — {nv['reason']}")


def cmd_status(args):
    """快速健康摘要。--quiet 返回 JSON。"""
    quiet = "--quiet" in args

    # 自动扫描窗口状态（每次 status 都更新跨窗口感知）
    window_scan = _scan_windows_and_observe()
    _feed_window_observation(window_scan)

    domains_status = []
    for name in ["dialogue", "cad", "quant", "hook", "window", "pic", "image", "retrieval"]:
        engine = _load_domain(name)
        result = engine.compute()
        obs_h = _compute_observed_entropy_rate(engine)

        status_icon = {
            "frozen": "[Frozen]", "converged": "[PASS]", "stable": "[GO]",
            "exploring": "[Search]", "diverging": "[Fire]", "unknown": "[?]"
        }
        icon = status_icon.get(result["stability"], "[?]")
        obs_file = _get_obs_file(name)
        obs_count = sum(1 for _ in open(obs_file, 'r', encoding='utf-8') if _.strip()) if obs_file.exists() else 0

        h = result["entropy"]
        # 健康度判断
        if result["stability"] == "converged" or result["stability"] == "stable":
            health = "健康"
        elif result["stability"] == "exploring":
            health = "探索中"
        elif result["stability"] == "frozen":
            health = "卡死"
        elif result["stability"] == "diverging":
            health = "发散"
        else:
            health = "未知"

        ds = {
            "domain": name,
            "icon": icon.strip("[]"),
            "alphabet_size": result["alphabet_size"],
            "entropy": round(h, 4),
            "obs_entropy_rate": round(obs_h, 4),
            "stability": result["stability"],
            "obs_count": obs_count,
            "health": health,
        }
        domains_status.append(ds)

        if not quiet:
            alive_line = (f"  {icon} {name:10s} |Σ|={result['alphabet_size']:2d}  "
                         f"h={h:.4f}  h_obs={obs_h:.4f}  "
                         f"[{result['stability']:>10s}]  "
                         f"观测总={obs_count:4d}  [GO] {health}")
            # will print below

    alerts_count = 0
    if ALERTS_FILE.exists():
        with open(ALERTS_FILE, 'r', encoding='utf-8') as f:
            alerts_count = sum(1 for _ in f if _.strip())

    if quiet:
        print(json.dumps({
            "domains": domains_status,
            "alerts_total": alerts_count,
            "overall": "healthy" if all(d["stability"] in ("stable", "converged") for d in domains_status) else "attention",
        }, ensure_ascii=False))
    else:
        print(f"\n{'='*50}")
        print(f"符号动力学系统状态")
        print(f"{'='*50}")
        for d in domains_status:
            print(f"  [{d['icon']}] {d['domain']:10s} |Σ|={d['alphabet_size']:2d}  "
                  f"h={d['entropy']:.4f}  h_obs={d['obs_entropy_rate']:.4f}  "
                  f"[{d['stability']:>10s}]  "
                  f"观测总={d['obs_count']:4d}  [GO] {d['health']}")
        print(f"\n  告警总数: {alerts_count}")
        if alerts_count > 0:
            print(f"  查看: python scripts/wheels/symbolic_observer.py snapshot <域>")
        print()


def cmd_trend(args):
    """熵趋势：从观测文件计算每 N 条的熵值"""
    domain = args[0] if args else "dialogue"
    window = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20

    obs_file = _get_obs_file(domain)
    if not obs_file.exists():
        print(f"[OBS] 无观测数据: {domain}")
        return

    engine = _load_domain(domain)
    all_obs = []
    with open(obs_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    all_obs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if len(all_obs) < window:
        print(f"[OBS] 观测不足 {window} 条 (共 {len(all_obs)} 条)，无法计算趋势")
        return

    # 按窗口计算熵（拓扑熵 + 经验熵率）
    entropies = []
    for i in range(0, len(all_obs), window):
        batch = all_obs[i:i+window]
        symbols = [o.get("symbol", "?") for o in batch]
        # 临时域引擎
        temp_engine = _DOMAIN_FACTORIES[domain]()
        for s in symbols:
            if s in temp_engine.sym_to_idx:
                temp_engine.observe(s)
        r = temp_engine.compute()
        obs_h = _compute_observed_entropy_rate(temp_engine)
        entropies.append({
            "window": i // window,
            "start": i,
            "end": min(i+window, len(all_obs)),
            "entropy": r["entropy"],
            "obs_entropy_rate": obs_h,
            "stability": r["stability"],
        })

    print(f"\n{'='*50}")
    print(f"熵趋势: {domain} (窗口={window})")
    print(f"{'='*50}")
    for e in entropies[-20:]:  # 最多显示 20 个窗口
        bar_len = int(e["obs_entropy_rate"] * 10) if e["obs_entropy_rate"] > 0 else 0
        bar = "█" * min(bar_len, 30)
        print(f"  #{e['window']:3d} [{e['start']:3d}-{e['end']:3d}]  "
              f"h_top={e['entropy']:.4f}  h_obs={e['obs_entropy_rate']:.4f}  "
              f"{bar}  [{e['stability']}]")

    # 总结
    if len(entropies) >= 2:
        first = entropies[0]["obs_entropy_rate"]
        last = entropies[-1]["obs_entropy_rate"]
        if first > 0 or last > 0:
            delta = last - first
            trend_arrow = "↑" if delta > 0.1 else ("↓" if delta < -0.1 else "→")
            print(f"\n  经验熵率趋势: {first:.4f} → {last:.4f}  {trend_arrow} (Δ={delta:+.4f})")
        else:
            toph_first = entropies[0]["entropy"]
            toph_last = entropies[-1]["entropy"]
            print(f"\n  拓扑熵 (静态): {toph_first:.4f} → {toph_last:.4f} (不变, 需加禁止词才变)")
        print(f"  解释: h_obs → 0 = 行为收敛(可能卡循环); h_obs ↑ = 行为发散")
        print(f"        目标区间: 0.2~1.0 (稳定但灵活)")


def _cmd_window_scan(args):
    """跨窗口扫描: 枚举活跃窗口 → 符号编码 → 喂入窗口域引擎"""
    quiet = "--quiet" in args

    result = _scan_windows_and_observe()
    fed = _feed_window_observation(result)

    if quiet:
        print(json.dumps({
            "scan": result,
            "engine": {
                "entropy": fed["engine_result"]["entropy"],
                "stability": fed["engine_result"]["stability"],
                "obs_count": fed["engine_result"]["obs_count"],
                "forbidden_hits": len(fed["engine_result"]["forbidden_hits"]),
            },
            "alerts": len(fed["alerts"]),
        }, ensure_ascii=False))
        return

    print(f"\n{'='*50}")
    print(f"跨窗口扫描")
    print(f"{'='*50}")
    print(f"  活跃窗口: {result['active_count']}/{result['window_count']}")
    print(f"  活跃域:   {result['domains_active'] or ['(无)']}")
    print(f"  符号:     {result['symbol']}")

    if result.get("conflicts"):
        print(f"\n  ⚠️ 冲突检测:")
        for c in result["conflicts"]:
            print(f"    - {c['domain']} 域 {c['count']} 个窗口同时活跃")

    if result.get("overlaps"):
        print(f"\n  ⚡ 任务重叠:")
        for o in result["overlaps"]:
            print(f"    - {o['w1']} ↔ {o['w2']}: {o['common_words']}")

    eng = fed["engine_result"]
    print(f"\n  窗口域引擎: h={eng['entropy']:.4f}  [{eng['stability']}]  obs={eng['obs_count']}")
    if fed["alerts"]:
        print(f"  🚨 告警: {len(fed['alerts'])} 条")
        for a in fed["alerts"]:
            print(f"    [{a['type']}] {a.get('reason', a.get('suggestion', ''))}")
    print()


def cmd_replay(args):
    """重放历史消息（JSONL 文件或原始文本文件），逐条 capture"""
    path = args[0] if args else None
    if not path or not os.path.exists(path):
        print("[OBS] 用法: symbolic_observer.py replay <file>")
        return

    filepath = Path(path)
    text = filepath.read_text(encoding='utf-8', errors='replace')

    # 按行处理
    lines = text.split('\n')
    count = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # 尝试 JSONL 解析
        if line.startswith('{'):
            try:
                entry = json.loads(line)
                msg = entry.get("text", entry.get("message", entry.get("raw", "")))
            except json.JSONDecodeError:
                msg = line
        else:
            msg = line

        if len(msg) < 5:
            continue

        cmd_capture([msg])
        count += 1
        if count % 10 == 0:
            _save_all_domains()

    _save_all_domains()
    print(f"\n[OBS] 重放完成: {count} 条消息")


# ═══════════════════════════════════════════
# 工具调用审计 — retrieval 域
# ═══════════════════════════════════════════

_TOOL_TO_SYMBOL = {
    "semantic_query": "query",
    "inference_router": "infer",
    "text_scanner": "scan",
    "grep": "grep",
    "glob": "grep",
    "webfetch": "fetch",
    "websearch": "fetch",
}


def cmd_tool_call(args):
    """捕获工具调用: symbolic_observer.py tool_call <tool_name> <summary>

    双写:
      1. retrieval 域 — 检索工具(语义搜索/扫描/infer)的审计
      2. operations 域 — 全量工具调用的操作符号(R/W/B/G/S/E/T)统计

    映射:
      semantic_query  → query    (retrieval域)
      inference_router→ infer    (retrieval域)
      text_scanner    → scan     (retrieval域)
      Grep/Glob       → grep     (retrieval域)
      WebFetch/Search → fetch    (retrieval域)
      其他            → unknown  (retrieval域)
      同时从 summary 提取操作符号(R/W/B/G/S/E/T) → operations域

    纯统计监控，不拦截、不阻塞。fire-and-forget 调用。
    CPU only，零 GPU 占用。
    """
    tool_name = args[0] if args else "unknown"
    summary = " ".join(args[1:]) if len(args) > 1 else tool_name

    # ── 1. retrieval 域 ──
    tool_lower = tool_name.lower()
    ret_symbol = "unknown"
    for key, sym in _TOOL_TO_SYMBOL.items():
        if key in tool_lower:
            ret_symbol = sym
            break

    ret_engine = _load_domain("retrieval")
    if ret_symbol not in ret_engine.sym_to_idx:
        ret_symbol = "unknown"
    ret_engine.observe(ret_symbol)

    ret_entry = {
        "domain": "retrieval",
        "symbol": ret_symbol,
        "tool": tool_name,
        "summary": summary[:200],
        "ts": datetime.now().isoformat(),
    }
    _save_observation("retrieval", ret_entry)
    _save_domain("retrieval", ret_engine)

    # ── 2. operations 域（全量工具操作符号，iter-050 落地）──
    # 从 summary 中提取操作符号（Hook 传入格式: "R file_path" 或 "W file_path"）
    ops_symbol = "T"  # 默认 Tool
    OPS_SYMBOLS = {"R", "W", "B", "G", "S", "E", "T", "I"}
    first_token = summary.split()[0] if summary else ""
    if first_token in OPS_SYMBOLS:
        ops_symbol = first_token

    ops_entry = {
        "symbol": ops_symbol,
        "tool": tool_name,
        "summary": summary[:200],
        "ts": datetime.now().isoformat(),
    }
    ops_file = OBS_DIR / "operations.jsonl"
    with open(ops_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(ops_entry, ensure_ascii=False) + "\n")


def _save_all_domains():
    for name in _DOMAIN_FACTORIES:
        try:
            engine = _load_domain(name)
            _save_domain(name, engine)
        except Exception:
            pass


# ═══════════════════════════════════════════
# WVM 增强: 时序价值评分 + 前缀随机化 + 反向增强
# 来源: World Value Models (arXiv:2606.24742, ByteDance 2026)
# ═══════════════════════════════════════════

def cmd_wvm_score(args):
    """WVM 风格时序评分: 对观测序列做连续价值评分而非单点熵值"""
    domain = args[0] if args else "retrieval"
    window = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
    obs_file = _get_obs_file(domain)
    if not obs_file.exists():
        print(json.dumps({"error": f"无观测数据: {domain}"}, ensure_ascii=False))
        return
    obs = []
    for line in obs_file.read_text(encoding='utf-8').strip().split('\n'):
        if line.strip():
            try: obs.append(json.loads(line))
            except: continue
    if len(obs) < window:
        print(json.dumps({"error": f"观测不足{window}条"}, ensure_ascii=False))
        return

    engine = _load_domain(domain)
    scores = []
    for i in range(0, len(obs) - window, max(1, window // 4)):
        batch = obs[i:i + window]
        symbols = [o.get("symbol", "?") for o in batch]

        # 计算窗口熵 + 重复率
        unique = len(set(symbols))
        total = len(symbols)
        diversity = unique / max(total, 1)

        # 前缀随机化: 50%概率打乱前3个符号, 防模型偷懒 (WVM prefix randomization)
        import random
        if random.random() < 0.5 and len(symbols) >= 5:
            prefix = list(symbols[:min(3, len(symbols))])
            random.shuffle(prefix)
            symbols[:len(prefix)] = prefix

        # 停滞检测: 连续相同符号比例
        repeats = sum(1 for j in range(1, len(symbols)) if symbols[j] == symbols[j - 1])
        stagnation = repeats / max(total - 1, 1)

        # 综合进展分数 (0=完全停滞, 1=正常前进)
        progress = diversity * (1 - stagnation)
        scores.append({
            "index": i,
            "progress": round(progress, 4),
            "diversity": round(diversity, 4),
            "stagnation": round(stagnation, 4),
            "symbol_count": total,
        })

    # 整体趋势: 最后3窗口 vs 前3窗口
    if len(scores) >= 6:
        early = sum(s["progress"] for s in scores[:3]) / 3
        late = sum(s["progress"] for s in scores[-3:]) / 3
        trend = "improving" if late > early * 1.1 else ("declining" if late < early * 0.9 else "stable")
    else:
        trend = "insufficient_data"

    print(json.dumps({
        "domain": domain, "window": window,
        "scores": scores, "trend": trend,
        "total_windows": len(scores),
    }, ensure_ascii=False, indent=2))


def cmd_wvm_augment(args):
    """WVM 风格反向增强: 从真实序列生成合成回归/停滞数据"""
    domain = args[0] if args else "retrieval"
    obs_file = _get_obs_file(domain)
    if not obs_file.exists():
        print(json.dumps({"error": f"无观测数据: {domain}"}, ensure_ascii=False))
        return
    obs = []
    for line in obs_file.read_text(encoding='utf-8').strip().split('\n'):
        if line.strip():
            try: obs.append(json.loads(line))
            except: continue
    if len(obs) < 20:
        print(json.dumps({"error": "观测不足20条"}, ensure_ascii=False))
        return

    symbols = [o.get("symbol", "?") for o in obs]

    # 原始: 正常进展
    normal = symbols[-50:]

    # 倒放: 模拟退步 (reverse augmentation)
    reversed_seq = list(reversed(normal))

    # 重复: 模拟停滞 (frame repeat)
    stagnant = []
    for s in normal:
        stagnant.append(s)
        if len(stagnant) % 3 == 0:
            stagnant.append(stagnant[-1])  # 每3步重复一次

    # 混合: 正常→停滞→恢复
    mixed = normal[:15] + [normal[15]] * 10 + normal[25:]

    print(json.dumps({
        "domain": domain,
        "original_length": len(normal),
        "augmented": {
            "normal": normal,
            "regression": reversed_seq,
            "stagnation": stagnant,
            "mixed": mixed,
        },
        "usage": "将这些合成序列喂入引擎→计算熵曲线→用回归/停滞模式校准漂移阈值",
    }, ensure_ascii=False, indent=2))


# ═══════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════

def main():

    if len(sys.argv) < 2:
        print(__doc__.strip())
        return

    cmd = sys.argv[1]
    args = sys.argv[2:]

    cmds = {
        "capture": cmd_capture,
        "snapshot": cmd_snapshot,
        "status": cmd_status,
        "trend": cmd_trend,
        "replay": cmd_replay,
        "window-scan": lambda args: _cmd_window_scan(args),
        "tool_call": cmd_tool_call,
        "wvm-score": cmd_wvm_score,
        "wvm-augment": cmd_wvm_augment,
    }

    if cmd not in cmds:
        print(f"[OBS] 未知命令: {cmd}")
        print(f"  可用: {', '.join(cmds.keys())}")
        return

    cmds[cmd](args)


if __name__ == "__main__":
    main()
