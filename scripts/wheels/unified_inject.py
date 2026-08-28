#!/usr/bin/env python3
"""unified_inject.py — 知识联想注入
=====================================================
SF Qwen 一次性调: 读 anchor + KG 候选实体 → 选最相关实体 → 附带真实内容+源文件

@fix 2026-08-16 maintainer定格式: 知识联想必须带 "知识内容是XXX 文件名XXX",
    原"跨界灵感/本地记忆/知识实体"纯标签版"有点水" — 一无所知的AI看不懂要干嘛。
    实现: SF 只做选择(从候选实体名挑一个), 内容与文件名由本地 KG 确定性取出, 不靠模型生成。

输出格式: 【消息】知识导航(CLS): 系统提示 — 后台CLS发现knowledge中有一些知识对你的当前任务有帮助, 自动推送。
"""

import json, os, re, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# 知识卡片公共核心 (P2) — 兄弟模块, 保证可导入 (含 schtask/hook 间接调用场景)
_WHEELS = str(Path(__file__).resolve().parent)
if _WHEELS not in sys.path:
    sys.path.insert(0, _WHEELS)
from knowledge_nav_core import load_cards, prescreen, format_nav_cards, bigrams
from knowledge_inject_audit import log as _audit_ki  # P7 统一审计

CONCLUSIONS_FILE = Path(_WHEELS).parent.parent / "knowledge" / "知识图谱" / "kg_conclusions.jsonl"


def _match_conclusions(anchor: str, top_n: int = 3, min_shared: int = 3) -> str:
    """结论库匹配 (2026-08-20): incident-log教训/双轨结构化结论按 CJK bigram 命中锚点。

    与卡片不同, 结论库条目带 anchor_level(hard_gate/human_confirmed),
    是经事实验证的知识 → 注入时标注"优先采信"。
    纯 bigram 无向量: 结论库量小(百级), 且教训用语与任务用语重合度高。
    """
    if not CONCLUSIONS_FILE.exists():
        return ""
    entries = []
    try:
        for line in CONCLUSIONS_FILE.read_text(encoding="utf-8").strip().splitlines():
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return ""
    if not entries:
        return ""
    a_grams = bigrams(anchor)
    if not a_grams:
        return ""
    scored = []
    for e in entries:
        if e.get("type") == "source":
            continue  # 纯路径引用, 不是知识
        text = " ".join(str(e.get(k) or "") for k in ("fact", "title", "cause"))
        shared = a_grams & bigrams(text)
        if len(shared) >= min_shared:
            scored.append((len(shared), e))
    scored.sort(key=lambda x: -x[0])
    lines = []
    for _s, e in scored[:top_n]:
        tag = {"hard_gate": "事故验证", "human_confirmed": "maintainer确认"}.get(
            e.get("anchor_level", ""), "模型总结")
        src = e.get("baobi_id")
        loc = f"incident-log第{src}条" if src else str(e.get("source", ""))[:60]
        lines.append(f"- [{tag}|{e.get('ts','?')[:10]}] {str(e.get('fact'))[:180]} (源: {loc})")
    return "\n".join(lines)


def _parse_pick_json(response: str) -> dict | None:
    """容错解析 {pick, why} JSON — SF 免费模型偶发非JSON, 三级降级(与 content_gaze 同款)"""
    # 1) 标准解析
    m = re.search(r'\{[^{}]*\}', response, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group())
            if isinstance(d, dict) and d.get("pick"):
                return d
        except Exception:
            pass
    # 2) raw_decode 容错 (容忍 JSON 后杂文本)
    try:
        d, _ = json.JSONDecoder().raw_decode(response, response.find('{'))
        if isinstance(d, dict) and d.get("pick"):
            return d
    except Exception:
        pass
    # 3) 字段宽松提取 (JSON 内部语法错误时兜底)
    def _field(name):
        mm = re.search(name + r'["\']?\s*[:：]\s*["\']([^"\'\n}]{1,40})', response)
        return mm.group(1) if mm else ""
    d = {"pick": _field("pick"), "why": _field("why")}
    return d if d["pick"] else None


def inject() -> str:
    """主入口: 返回四字段注入 (消息|为什么|级别|内容), 内容带真实知识+文件名"""
    # 注入质量反馈 config — 统一 类型 off → 关停本注入 (analyzer 自动调整)
    # @fix 2026-08-01: maintainer决策"语义分析类注入全部都这样" → 知识导航(统一)可被反馈环关停
    try:
        _cfg = ROOT / "data" / "state" / "inject_feedback_config.json"
        if _cfg.exists():
            _types = json.loads(_cfg.read_text(encoding="utf-8")).get("types", {}) or {}
            if _types.get("统一", "on") != "on":
                return ""
    except Exception:
        pass

    # 任务锚点解析 — @fix 2026-08-16 一号融合审计: 原只读 drift_anchor.json,
    # 其唯一写者 drift_embedding.py 两机均无调用者(全量 grep 证实) → 锚点成化石
    # (一号停在 7/9 "PIC B场扫描" → 知识导航永远拿旧任务匹配)。改为三级来源+24h过期守卫:
    # ①goal.txt(PreCompact 每轮 compact 更新) ②active_context.json current_focus(每会话)
    # ③drift_anchor.json(兜底)。全过期 → 静默跳过(宁缺毋错, 防瞎联想)。
    # @fix 2026-08-16 窗口隔离(maintainer要求: 一窗口一注入, 不串窗口):
    # 新增第零级 — 本窗口 cog_step 声明(window_id 前缀校验); goal.txt 带 sid 校验
    # (PreCompact 现写 "sid:<8位>" 前缀, 他窗口的 goal 不算)。
    _MAX_AGE = 24 * 3600
    anchor = ""
    _sid8 = (os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or "unknown")[:8]

    def _fresh_mtime(p: Path) -> float | None:
        try:
            return p.stat().st_mtime
        except Exception:
            return None

    # ⓪ 本 session 最近用户消息 (2026-08-19 maintainer定: anchor 信息源以"你正在做的事"为真相。
    # semantic_inject 每 session 独立记录 prompt_history_{sid}.json 最近10条用户消息 —
    # session_id 同源取自 CLAUDE_CODE_SESSION_ID, 与本文件 _sid8 一致, 不会串窗口。
    # 取最近一条非空消息前80字做锚点, 比状态文件更接近"现在在干什么"。)
    try:
        _ph = ROOT / "data" / "state" / f"prompt_history_{_sid8}.json"
        if _ph.exists():
            _hist = json.loads(_ph.read_text(encoding="utf-8"))
            if isinstance(_hist, list):
                for _m in reversed(_hist):
                    _m = (str(_m) or "").strip()
                    if len(_m) >= 6:  # 跳过"继续"/"ok"类碎片
                        anchor = _m[:80]
                        break
    except Exception:
        pass

    # ① 本窗口 cog_step 声明 (window_id 前缀校验 — 别的窗口声明不算; Stop hook 已改写 cog_step_end.json 不再污染)
    # @fix 2026-08-16 安全审查: window_id 缺失必须拒绝(不采信未知窗口声明), 原 (not _win) 放行为 fail-open 错误
    _csp = ROOT / "data" / "state" / "cog_step.json"
    if _csp.exists():
        try:
            _cs = json.loads(_csp.read_text(encoding="utf-8"))
            _win = (_cs.get("_meta") or {}).get("window_id") or ""
            if _win and str(_win).startswith(_sid8):
                anchor = ((_cs.get("description") or _cs.get("label") or "").strip())[:80]
        except Exception:
            pass

    # ② goal.txt (带 sid 校验: 仅本窗口 compact 写的算)
    if not anchor:
        _gt = ROOT / "data" / "longchain" / "_state" / "goal.txt"
        _mt = _fresh_mtime(_gt)
        if _mt and time.time() - _mt < _MAX_AGE:
            try:
                _g = _gt.read_text(encoding="utf-8").strip()
                # @fix 2026-08-16 安全审查: 严格前缀匹配, 子串查找可被正文含 sid8 字样误放行
                if not _g.startswith(f"sid:{_sid8}"):
                    _g = ""  # 无前缀或别的窗口的 goal → 不算
                if _g:
                    anchor = _g[:80]
            except Exception:
                pass

    # ③ active_context.json current_focus (机器级兜底)
    if not anchor:
        _ac = ROOT / "state" / "active_context.json"
        if _ac.exists():
            try:
                _d = json.loads(_ac.read_text(encoding="utf-8"))
                _u = _d.get("updated_at", "")
                _ts = time.time()
                if _u:
                    try:
                        from datetime import datetime as _dt
                        _ts = _dt.fromisoformat(str(_u).replace("Z", "+00:00")).timestamp()
                    except Exception:
                        pass
                _focus = (_d.get("current_focus") or "").strip()
                if _focus and time.time() - _ts < _MAX_AGE:
                    anchor = _focus[:80]
            except Exception:
                pass

    # ④ drift_anchor.json 兜底 (同样带 24h 守卫)
    if not anchor:
        af = ROOT / "data" / "state" / "drift_anchor.json"
        if af.exists():
            try:
                _a = json.loads(af.read_text(encoding="utf-8"))
                _created = _a.get("created_at") or 0
                if time.time() - float(_created) < _MAX_AGE:
                    anchor = str(_a.get("goal", ""))[:80]
            except Exception:
                pass
    if not anchor:
        return ""

    # ── 知识卡片导航 (v2, 2026-08-16 maintainer定架构: 文件级卡片 + 关系推理 + ≤5张) ──
    # 流程: ①公共核心粗筛 top20(本地词面) ②opencode flash 思考每张与任务的关系类型(历史/近似/不相关)
    #       ③模型第一行点名(顿号分隔), 正文本地按名逐字拼接 ④全不相关 → 静默。
    cards = load_cards().get("cards", {}) or {}
    if not cards:
        return ""

    # ① 粗筛: 锚点与卡片(title+content+lesson) CJK bigram 重叠打分, 取 top20
    # ①-0 废卡过滤 (2026-08-20 supersede 借鉴): 被 superseded_by 标记的卡不进候选
    cards = {k: c for k, c in cards.items() if not c.get("superseded_by")}
    top = prescreen(anchor, cards)
    if not top:
        return ""
    id_map, card_text = format_nav_cards(top)

    # ② DS Flash 关系思考
    nav_system = (
        "你是知识导航器。本窗口正在执行一个任务, 下面是knowledge压缩出来的认知卡片(历史工作记录)。\n"
        "思考每张卡片与当前任务的关系类型:\n"
        "  历史相关 = 同一条工作线的前序/后续, 与任务有直接承接关系\n"
        "  近似相关 = 不同工作但相似问题的处理经验, 可借鉴\n"
        "  不相关 = 无关\n"
        "铁律: ①这些知识只提供不保证对错, 历史早期结论可能已被推翻 ②时间越靠后越可信, 理由里必须标注卡片日期 ③最多选5张, 宁缺毋滥。\n"
        "输出格式: 第一行只写选中的卡片编号(顿号分隔), 都不相关写\"无相关\"; "
        "之后每行: \"<卡号> — 历史相关|近似相关 — <理由, 提及日期>\", 不要JSON不要其他格式。"
    )
    nav_text = _ds_chat(nav_system, f"本窗口正在: {anchor}\n\n知识卡片:\n{card_text}", max_tokens=800)
    if not nav_text or "无相关" in nav_text.splitlines()[0]:
        return ""
    lines = [l.strip() for l in nav_text.strip().splitlines() if l.strip()]
    first = lines[0].replace("，", "、")
    picked_ids = [p.strip() for p in re.split(r"[、,]", first) if p.strip()]
    # @fix 2026-08-19: 模型可能输出 "1" 或 "卡1" — 统一归一到 "卡N" 再匹配,
    # 否则裸数字全被 p in id_map 滤掉 → 永远静默(注入不稳定根因之三)。
    def _norm_id(x: str) -> str:
        x = x.strip()
        return x if x.startswith("卡") else (f"卡{x}" if x.isdigit() else x)
    picked_ids = [_norm_id(p) for p in picked_ids]
    picked_ids = [p for p in picked_ids if p in id_map][:5]
    if not picked_ids:
        return ""

    # ③ 本地逐字组装 (理由段用模型原文, 卡片正文从卡片库逐字取)
    reason_lines = [l for l in lines[1:] if l]
    card_blocks = []
    for cid in picked_ids:
        c = cards[id_map[cid]]
        block = f"- 《{c.get('title') or '无题'}》(日期:{c.get('date') or '无'}) 内容:{c.get('content') or ''}"
        if (c.get("lesson") or "").strip() and c.get("lesson") != "无":
            block += f" | 教训:{c.get('lesson')}"
        if (c.get("highlight") or "").strip() and c.get("highlight") != "无":
            block += f" | 亮点:{c.get('highlight')}"
        block += "\\n  源文件: " + str(id_map[cid])
        card_blocks.append(block)
    result = (
        "【消息】CLS后台提醒: 后台整理发现knowledge中有与当前任务可能有用的工作记录, 自动推送非指令。"
        f"【为什么】你当前的任务是「{anchor[:60]}」, 以下卡片经关联判断与该任务存在承接或借鉴关系。"
        "【级别】参考 — 相关就纳入思考, 不相关可忽略。"
        "【内容】关联分析(理由为具体逻辑关系, 非泛泛):\n" + "\n".join(reason_lines[:5]) + "\n── 知识卡片 ──\n"
        + "\n".join(card_blocks))

    # ── 已知教训并联 (2026-08-20 结论库): incident-log/双轨结构化教训按锚点匹配附加 ──
    try:
        concl_tail = _match_conclusions(anchor)
        if concl_tail:
            result += "\n── 已知教训(经事实验证的结论, 优先采信) ──\n" + concl_tail
    except Exception:
        pass

    result += "\n(以上知识只提供不保证对错, 时间越近越可信; 源文件如有需求可自行查看)"
    _audit_ki("unified_inject", result, "知识卡片导航", f"anchor:{anchor[:20]}")
    # @since 2026-08-19 maintainer要求: 注入时 CC 屏幕显示框图 — 选中卡片可视化
    _write_viz(anchor, picked_ids, id_map, cards, reason_lines)
    return result


def _write_viz(anchor, picked_ids, id_map, cards, reason_lines):
    """选中卡片写入 viz 状态文件; SessionStart/box 消费后删除。"""
    try:
        entries = []
        for cid in picked_ids:
            c = cards[id_map[cid]]
            entries.append({
                "id": cid, "title": c.get("title") or "无题",
                "date": c.get("date") or "无",
                "content": (c.get("content") or "")[:60],
                "file": id_map[cid],
            })
        viz = {"anchor": anchor[:60], "ts": time.strftime("%Y-%m-%d %H:%M"),
               "cards": entries, "reasons": reason_lines[:5]}
        vp = ROOT / "data" / "state" / "card_inject_viz.json"
        vp.write_text(json.dumps(viz, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _ds_chat(system: str, user: str, max_tokens: int = 800) -> str:
    """知识注入统一入口 → opencode DS Flash (2026-08-16 夜对齐: MiMo 实测把推理链写进 content、
    不遵守"第一行点名"格式契约, 换回 opencode; 走 api_pipeline 可换)"""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts.wheels.api_pipeline import call
        # @fix 2026-08-19: 选卡是轻判断, mimo 思考模式会把 max_tokens 全花在
        # reasoning_content 上 → content 空或截断(finish=length) → 点名行乱/空。
        # thinking disabled 后实测格式正常。8021 token 给足正文余量。
        r = call("opencode", "mimo-v2.5",
                 messages=[{"role": "system", "content": system},
                           {"role": "user", "content": user}],
                 max_tokens=max(max_tokens, 2000), auto_route=False, timeout_s=90,
                 extra_body={"thinking": {"type": "disabled"}})
        return (r.get("text") or "").strip() if r and r.get("ok") else ""
    except Exception:
        return ""


if __name__ == "__main__":
    result = inject()
    if result:
        print(result)
        # api_pipeline 同款彩色框 (ANSI) — python 直跑调试可见
        try:
            vp = ROOT / "data" / "state" / "card_inject_viz.json"
            if vp.exists():
                v = json.loads(vp.read_text(encoding="utf-8"))
                G, R, Y, B, N = "[92m", "[96m", "[93m", "[94m", "[0m"
                W = 64
                print()
                print(G + "=" * W + N)
                print(G + f"  [CLS] 知识卡片注入  {v['ts']}" + N)
                print(G + "=" * W + N)
                print(B + f"  当前任务: {v['anchor']}"[:W] + N)
                print(Y + "-" * W + N)
                for c in v["cards"]:
                    print(Y + f"  [{c['id']}] {c['date']}  {c['title'][:30]}" + N)
                    print("        源: " + c["file"][:52])
                for r in v.get("reasons", [])[:5]:
                    print(R + "  " + r[:W] + N)
                print(G + "=" * W + N)
        except Exception:
            pass
