try:
    import session_identity as _SI
except ImportError:  # 以包形式(scripts.wheels.xxx)导入时走全限定名
    from scripts.wheels import session_identity as _SI


def _sid_match(window_id: str, sid8: str) -> bool:
    """窗口归属校验 —— 委托 session_identity 统一口径 (@fix 2026-09-10)。

    原实现 `w.startswith(sid8) or w.startswith("cc:"+sid8)` 有两个缺陷:
      ① 完全漏掉 dsh: 前缀(docstring 声称支持 "dsh:xxx", 代码里没有)
         → DSH 窗口永不匹配, cog_step 锚点在 DSH 侧恒被跳过;
      ② 两边都是 "unknown" 时返回 True → 陌生窗口互相认领(串窗口机制)。
    session_identity.matches() 只看 uuid 核(桥接的 cc/dsh 同核)、支持 [:8]/[:16]
    截断兼容、且未知身份 fail-closed。第二参数保留仅为兼容旧调用签名。
    """
    return _SI.matches(window_id, sid8)

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
    from scripts.wheels.mimo_free import mimo_call
    # @fix 2026-09-10 红框真凶: 原裸调 call("opencode","mimo-v2.5") → 真实 payload 下
    #   mimo+Go 实测 0/2 成功(平均 38.8s 返回空文本); 而 call() 失败时会打印 ANSI 错误框,
    #   该 stdout 被 DSH 注入插件当作知识卡内容投出 → 屏幕上反复出现 "[知识卡交付] + 红框"。
    #   改走 mimo_call 壳: ①串行锁+空文本重试+结构化 error ②内部走 _call_opencode_api,
    #   不经过 call() 的打印路径 → 失败不再污染注入内容。
    #   模型与 unified_inject._ds_chat 同源同配置(实测 DS Flash+Zen: 2/2 成功, 平均 4.2s, 输出完整)。
    r = mimo_call(user, system=system, max_tokens=max(max_tokens, 2000),
                  model="deepseek-v4-flash", endpoint="base_url", timeout_s=90,
                  extra_body={"thinking": {"type": "disabled"}})
    if not r.get("ok"):
        return ""
    return (r.get("text") or "").strip()


def _anchor_from_state(cli_sid: str = "") -> str:
    """读状态文件取当前任务锚点 — 仿 unified_inject 三级来源 + 24h 守卫 + 窗口/sid 校验。
    全部失败/过期 → 返回空字符串(dsh_cls_nav nav 会静默不注入)。"""
    # @fix 2026-09-17: navstate 的 sessionId CLI 参数(cls-memory 传 agent.id)此前只进 nav() 去重,
    #   没进窗口校验 —— dsh web spawn 的子进程无 CLAUDE_CODE_SESSION_ID/DSH_SESSION_ID 环境变量,
    #   _sid8 恒 unknown → 09-10 fail-closed 后 cog_step 锚点永不被认 → dsh 卡片注入自 08-18 断流一月。
    #   (08-16~18 的 16 条卡片注入全是手动显式锚点测试; 生产 navstate 路径从未成功过)
    _sid8 = (_SI.sid_key(cli_sid) if cli_sid else "") or _SI.sid_key() or "unknown"  # @fix 2026-09-10: 收敛到 session_identity(DSH-aware)

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
            if win and _sid_match(win, _sid8):
                # @fix 2026-09-17 串卡事故: cog_step 是全窗口共享单文件(最后写入者赢), 窗口匹配
                #   不充分 —— 同窗口任务切换后旧声明("用 ppt_design_builder")仍被当锚点选卡,
                #   PPT卡串进仿真窗口(0917 14:46 实录)。写侧 TTL 300s, 读侧同样要新鲜度:
                #   声明 written_at 超 30min 视为过期 → 落下级来源(活跃窗口频繁重declare不受影响)。
                _wa = float((cs.get("_meta") or {}).get("written_at") or 0)
                if _wa and time.time() - _wa <= 1800:
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
    # @add 2026-09-17 maintainer批: 卡确定注入(去重已过) → hit_count+1 写回(第五环点火)
    try:
        from retrieval_pipeline import bump_hits
        bump_hits([rel])
    except Exception:
        pass
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


def cmd_declare(phase: str, label: str, description: str = "", window_id: str = "",
                intent: str = "", basis: str = "", selfcheck: str = "") -> None:
    """dsh 侧 cog 声明 (2026-08-27 UAC, maintainer批): 复用 CC 的 cog_step_declare — 同一把锁同一个文件
    data/state/cog_step.json, CC 与 dsh 双侧共用一套声明协议(TTL 300s / window_id / fencing)。
    cls-gate 的 UAC 闸校验此文件, deny 文案指引运行本命令后重试。
    @fix 2026-09-10: window_id 补为显式形参并透传 — 原实现不传, dsh 侧声明一律记成 unknown,
      与 cls-gate 的"window_id必传"要求不符。
    @add 2026-09-15 maintainer批: 补 intent/basis/selfcheck 形参并透传。
      **事由(实测死锁路径)**: 同日给 cog_step_declare 加了"认知层字段质量闸门"
      (缺 intent/basis → 警告; 同一窗口连续 3 次 → 拒绝声明), 而本 CLI 当时只能把这些
      当 description 文本传 → CC/CLI 侧**永远填不上** → 连续 3 次后写不了文件 = 死锁。
      ⇒ 正解是补齐传参能力, 不是削弱闸门。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    from mcp_cls_tools import cog_step_declare
    r = cog_step_declare(phase=int(phase) if phase.isdigit() else 2, label=label,
                         description=description, window_id=window_id,
                         intent=intent, basis=basis, selfcheck=selfcheck)
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
        print(nav(_anchor_from_state(sid), sid))  # @fix 2026-09-17: sid 同步进锚点窗口校验
    elif cmd == "audit":
        audit_entry(sys.argv[2], sys.argv[3], sys.argv[4], " ".join(sys.argv[5:]))
    elif cmd == "declare":
        # declare <phase 1-6> <label> [description...]
        #         [window_id=dsh:xxx] [intent=...] [basis=...] [selfcheck=...]
        # @fix 2026-09-10: window_id 从尾部 token 解析 — cls-gate 要求 dsh 侧必传, 否则记成 unknown
        # @add 2026-09-15: intent/basis/selfcheck 同法解析 —— 认知层字段质量闸门要求能传进来,
        #   否则本 CLI(CC 侧也走它)永远填不上 → 连续 3 次后被拒绝声明 = 写不了文件。
        _rest = list(sys.argv[4:])
        _wid = ""
        _kv = {}
        for _t in list(_rest):
            for _k in ("window_id", "intent", "basis", "selfcheck"):
                if _t.startswith(_k + "="):
                    _v = _t.split("=", 1)[1].strip().strip('"').strip("'")
                    _rest.remove(_t)
                    if _k == "window_id":
                        _wid = _v
                    else:
                        _kv[_k] = _v
                    break
        cmd_declare(sys.argv[2], sys.argv[3], " ".join(_rest), _wid,
                    _kv.get("intent", ""), _kv.get("basis", ""), _kv.get("selfcheck", ""))
    elif cmd == "consult":
        # consult <四段解释...>
        cmd_consult(" ".join(sys.argv[2:]))
