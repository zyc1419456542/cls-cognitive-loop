#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh_cls_nav.py — dsh(harness) 侧 CLS 注入薄壳 (2026-08-16 夜, maintainer指令: 全量趋同+复用轮子)
=============================================================
dsh 插件只当薄壳, 知识导航/审计逻辑全部复用 CC 轮子:
  - 导航: knowledge_nav_core (load_cards/prescreen/format_nav_cards) + api_pipeline opencode DS Flash
    (关系推理提示词与 unified_inject v2 逐字一致; iter-036 定架构: 文件级卡片+关系推理+≤5张逐字)
  - 审计: knowledge_inject_audit.log 统一写 knowledge_inject_log.jsonl (source 前缀 dsh-)
用法:
  python dsh_cls_nav.py nav "<锚点文本>"   → 用给定锚点导航, stdout 四字段注入文本(无相关则空)
  python dsh_cls_nav.py anchor             → 读状态文件取当前任务锚点(仿 unified_inject 三级来源)
  python dsh_cls_nav.py navstate           → 读状态文件锚点并导航(一步到位, cls-memory 推荐)
  python dsh_cls_nav.py audit <src> <label> <trigger> <文本> → 写审计, 无输出

2026-08-17 maintainer建议2: dsh 锚点改读状态文件(cog_step带window_id/goal.txt带sid/active_context),
不再依赖"人类消息记录器", 从根上消除"后台脉冲被当人类锚点"bug。
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "wheels"))

# 与 unified_inject v2 逐字一致 (iter-036)
NAV_SYSTEM = (
    "你是知识导航器。本窗口正在执行一个任务, 下面是knowledge压缩出来的认知卡片(历史工作记录)。\n"
    "思考每张卡片与当前任务的关系类型:\n"
    "  历史相关 = 同一条工作线的前序/后续, 与任务有直接承接关系\n"
    "  近似相关 = 不同工作但相似问题的处理经验, 可借鉴\n"
    "  不相关 = 无关\n"
    "铁律: ①这些知识只提供不保证对错, 历史早期结论可能已被推翻 ②时间越靠后越可信, 理由里必须标注卡片日期 ③最多选5张, 宁缺毋滥。\n"
    "输出格式: 第一行只写选中的卡片编号(顿号分隔), 都不相关写\"无相关\"; "
    "之后每行: \"<卡号> — 历史相关|近似相关 — <理由, 提及日期>\", 不要JSON不要其他格式。"
)

_MAX_AGE = 24 * 3600


def _flash(system: str, user: str, max_tokens: int = 800) -> str:
    """opencode DS Flash — iter-036 换轨定论: 知识层轮子全走 opencode 套餐。
    quiet=True (2026-08-18): 后台精选不打印完整提示词/候选卡片, 只留最终注入, 便于maintainer监控 cls 机制。"""
    from scripts.wheels.api_pipeline import call
    r = call("opencode", "mimo-v2.5",
             messages=[{"role": "system", "content": system},
                       {"role": "user", "content": user}],
             max_tokens=max_tokens, auto_route=False, timeout_s=90, quiet=True)
    if not r:
        return ""
    return (r.get("text") or "").strip()


def _anchor_from_state() -> str:
    """读状态文件取当前任务锚点 — 仿 unified_inject 三级来源 + 24h 守卫 + 窗口/sid 校验。
    全部失败/过期 → 返回空字符串(dsh_cls_nav nav 会静默不注入)。"""
    _sid8 = (os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or "unknown")[:8]

    def _fresh(p: Path) -> float | None:
        try:
            return p.stat().st_mtime
        except Exception:
            return None

    # ① cog_step 带 window_id 前缀校验 (别的窗口声明不算)
    csp = ROOT / "data" / "state" / "cog_step.json"
    if csp.exists():
        try:
            cs = json.loads(csp.read_text(encoding="utf-8"))
            win = (cs.get("_meta") or {}).get("window_id") or ""
            if win and str(win).startswith(_sid8):
                a = ((cs.get("description") or cs.get("label") or "").strip())[:80]
                if a:
                    return a
        except Exception:
            pass

    # ② goal.txt 带 sid 严格前缀 (仅本窗口 compact 写)
    gt = ROOT / "data" / "longchain" / "_state" / "goal.txt"
    mt = _fresh(gt)
    if mt and time.time() - mt < _MAX_AGE:
        try:
            g = gt.read_text(encoding="utf-8").strip()
            if g.startswith(f"sid:{_sid8}") and len(g) > 4:
                a = g[4:84].strip()
                if a:
                    return a
        except Exception:
            pass

    # ③ active_context.current_focus (机器级兜底)
    ac = ROOT / "state" / "active_context.json"
    if ac.exists():
        try:
            d = json.loads(ac.read_text(encoding="utf-8"))
            u = d.get("updated_at", "")
            ts = time.time()
            if u:
                try:
                    ts = datetime.fromisoformat(str(u).replace("Z", "+00:00")).timestamp()
                except Exception:
                    pass
            focus = (d.get("current_focus") or "").strip()
            if focus and time.time() - ts < _MAX_AGE:
                return focus[:80]
        except Exception:
            pass

    return ""


def _seen_store() -> Path:
    return Path(r"<DSH_HOME>\data\dsh_nav_seen.json")


def _load_seen() -> dict:
    try:
        return json.loads(_seen_store().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_seen(d) -> None:
    try:
        _seen_store().parent.mkdir(parents=True, exist_ok=True)
        tmp = _seen_store().with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_seen_store())
    except Exception:
        pass


def nav(anchor: str, session_id: str = "") -> str:
    """返回给主模型注入的「干净知识 + 对当前任务的帮助」——中间筛选由后台小模型完成，
    只把最有用的 1 条知识 + 一句对当前任务的帮助返回。绝不把关联分析/筛选过程灌给主模型。
    2026-08-18 maintainer: 去重按 session_id(真正的窗口/人)记, 任务锚点只管找知识 — 多窗口各记各的, 不串不误杀。"""
    from knowledge_nav_core import load_cards, prescreen, format_nav_cards
    cards = load_cards().get("cards", {}) or {}
    if not cards or not anchor:
        return ""
    top = prescreen(anchor, cards)
    if not top:
        return ""
    id_map, card_text = format_nav_cards(top)

    # 去重 key = session_id(真正的窗口/人); 无则回退任务锚点(仅保证单进程内不崩)
    key = session_id.strip() if session_id and session_id.strip() else ("anchor:" + anchor.strip()[:30])
    seen = _load_seen().get(key, []) or []
    seen_set = set(seen)

    SEL_SYSTEM = (
        "你是知识精选器。当前任务与一批历史认知卡片: 从里面挑『对你当前任务最有用的 1 条知识』(绝不选 2 条以上, 宁缺毋滥)。\n"
        "铁律: ①卡片只提供不保证对错, 早期结论可能已被推翻, 理由标日期 ②时间越近越可信 ③只输出唯一一条能直接帮到当前任务的知识。\n"
        "输出格式(严格两行, 不要其他):\n"
        "第一行: 选中的卡片编号(只写一个编号, 不相关写\"无相关\")\n"
        "第二行: 这条知识对当前任务的具体帮助(1-2句, 指出可怎么用/借鉴)。"
    )
    nav_text = _flash(SEL_SYSTEM, f"当前任务: {anchor}\n\n候选知识卡片:\n{card_text}", max_tokens=400)
    if not nav_text or "无相关" in nav_text.splitlines()[0]:
        return ""
    lines = [l.strip() for l in nav_text.strip().splitlines() if l.strip()]
    if not lines:
        return ""
    picked_id = lines[0].strip()
    if picked_id not in id_map:
        return ""
    rel = id_map[picked_id]
    # 去重: 本锚点已给过这条 → 不重复灌(除非同锚点有"新增的不同卡", 但那也很少: 仍停)
    if rel in seen_set:
        return ""
    help_line = lines[1] if len(lines) > 1 else ""
    c = cards[rel]

    result = (
        f"【知识】《{c.get('title') or '无题'}》({c.get('date') or '日期未知'})\n"
        f"内容: {c.get('content') or ''}"
    )
    if (c.get("lesson") or "").strip() and c.get("lesson") != "无":
        result += f"\n教训: {c.get('lesson')}"
    if help_line:
        result += f"\n→ 对当前任务的帮助: {help_line[:160]}"
    result += "\n(知识只提供不保证对错, 时间越近越可信)"
    # 记录已给
    all_seen = _load_seen()
    all_seen[key] = all_seen.get(key, []) + [rel]
    _save_seen(all_seen)
    from knowledge_inject_audit import log as _audit
    _audit("dsh-nav", result, "知识精选", f"anchor:{anchor[:20]} picked:{rel}")
    return result


def audit_entry(source: str, label: str, trigger: str, text: str) -> None:
    from knowledge_inject_audit import log as _audit
    _audit(source, text, label, trigger)


def cmd_declare(phase: str, label: str, description: str = "") -> None:
    """dsh 侧 cog 声明 (2026-08-27 UAC, maintainer批): 复用 CC 的 cog_step_declare — 同一把锁同一个文件
    data/state/cog_step.json, CC 与 dsh 双侧共用一套声明协议(TTL 300s / window_id / fencing)。
    cls-gate 的 UAC 闸校验此文件, deny 文案指引运行本命令后重试。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    from mcp_cls_tools import cog_step_declare
    r = cog_step_declare(phase=int(phase) if phase.isdigit() else 2, label=label, description=description)
    print(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False))


def cmd_consult(explanation: str) -> None:
    """dsh 侧会诊 (2026-08-27, maintainer批): 复用 CC 的 consult 纯函数 — 修复循环卡住时提交四段解释,
    qwen 复核意见打到 stdout(由 dsh 会话呈现), 同时写 consult_clear.json 通行证(CC 侧同名文件同协议)。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    from mcp_cls_tools import consult
    print(consult(explanation))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "nav":
        print(nav(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif cmd == "anchor":
        print(_anchor_from_state())
    elif cmd == "navstate":
        # navstate [sessionId] — sessionId 是真正去重 key(窗口/人)
        sid = sys.argv[2] if len(sys.argv) > 2 else ""
        print(nav(_anchor_from_state(), sid))
    elif cmd == "audit":
        audit_entry(sys.argv[2], sys.argv[3], sys.argv[4], " ".join(sys.argv[5:]))
    elif cmd == "declare":
        # declare <phase 1-6> <label> [description...]
        cmd_declare(sys.argv[2], sys.argv[3], " ".join(sys.argv[4:]))
    elif cmd == "consult":
        # consult <四段解释...>
        cmd_consult(" ".join(sys.argv[2:]))
