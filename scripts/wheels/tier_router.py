#!/usr/bin/env python3
"""
tier_router.py — 六层推理路由闸门 v1
====================================
两段式: ① 正则闸门(0ms,零成本) → ② 小模型分类(200ms,本地GPU)
设计参考: symbolic_observer 的正则→模型介入模式 + delegate-assistant 的 T0 分类器模式

六层:
  L0 — 本地嵌入 (一号CPU, 零成本)
  L1 — 网络免费API (零成本, 高频)
  L2 — assistant二号GPU (零成本, 闲置GPU, ollama-agent-router)
  L3 — 豆包/阿里套餐 (已付费, 不用白不用)
  L4 — DeepSeek Flash (¥0.02/MTok, 缓存命中更便宜)
  L5 — DeepSeek Flash / Opus (pro 临时下架 2026-08-16 夜, 全换 flash, 未来降价请回)

用法:
  from scripts.wheels.tier_router import route
  decision = route(prompt="分析这段代码", context={...})
  # → {"tier": "L4", "provider": "opencode", "model": "deepseek-v4-flash",
  #     "confidence": 0.85, "gate": "regex", "reason": "code_generation"}

集成:
  route() → api_pipeline.call(provider, model, messages=...)
"""

import json
import re
import os
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

# ── 路由配置 ──────────────────────────────────────────────
ROUTER_URL = os.environ.get("OLLAMA_AGENT_ROUTER_URL", "http://127.0.0.1:11435")
CLASSIFIER_MODEL = "qwen2.5:1.5b"
ROUTER_TIMEOUT = 30

# ── 六层定义 ──────────────────────────────────────────────
TIERS = {
    "L0": {"provider": "ollama",      "model": "nomic-embed-text",  "cost": "免费", "desc": "本地嵌入"},
    "L1": {"provider": "qwen",        "model": "qwen-plus",         "cost": "免费", "desc": "免费API"},
    "L2": {"provider": "ollama",      "model": "qwen3:32b",         "cost": "免费", "desc": "二号主力GPU (20GB)"},
    "L3": {"provider": "volc",        "model": "doubao-seed-2-1-pro-260628", "cost": "套餐", "desc": "豆包套餐"},
    "L4": {"provider": "deepseek",   "model": "deepseek-v4-flash", "cost": "官网", "desc": "官网DS Flash (2026-08-17)"},
    "L5": {"provider": "deepseek",   "model": "deepseek-v4-flash",   "cost": "官网", "desc": "官网DS Flash (2026-08-17)"},
}

# ── 正则闸门：规则表 ──────────────────────────────────────
# 格式: (匹配模式, 目标层, 置信度, 理由)
# 注意: \b 对中文无效(Python \w 不含中文), 中文关键词不包 \b
# 规则按优先级排列, 先匹配先得
REGEX_RULES = [
    # ── 写操作 → 高智能 ──
    (r'(?<![a-zA-Z])(Write|Edit|create|generate|build|design|implement|refactor)(?![a-zA-Z])', "L4", 0.85, "write_operation"),
    # ── L0: 语义检索/嵌入/相似度 → 本地嵌入 ──
    (r'(?<![a-zA-Z])(embed|embedding|vector|semantic)(?![a-zA-Z])|语义(搜索|检索|匹配)|向量(检索|搜索)|相似度|(检索|搜索|查询)knowledge|嵌入', "L0", 0.92, "semantic_embedding"),
    # ── 摘要/分类/标签 → 本地可做 (中英文) ──
    (r'(?<![a-zA-Z])(summarize|classify)(?![a-zA-Z])|分类|摘要|精简|浓缩|打标签|打标', "L2", 0.90, "classification_summary"),
    # ── 翻译 → 免费API ──
    (r'(?<![a-zA-Z])(translate)(?![a-zA-Z])|翻译|译', "L1", 0.88, "translation"),
    # ── 数学/物理/公式推导 → 重推理 ──
    (r'(?<![a-zA-Z])(deriv[ae]|prove|equation)(?![a-zA-Z])|证明|公式|推导|积分|微分|矩阵|张量|拓扑|数学|物理', "L4", 0.82, "math_reasoning"),
    # ── 代码审查/debug → 需要精度 ──
    (r'(?<![a-zA-Z])(review|audit|debug)(?![a-zA-Z])|审查|审计|调试|报错|崩溃|[Bb]ug|[Ee]rror', "L4", 0.80, "code_review"),
    # ── 文件读写/搜索 → 中等 ──
    (r'(?<![a-zA-Z])(Read|search|find|locate|grep|scan)(?![a-zA-Z])|搜(?!索knowledge)|找|查|读取', "L3", 0.80, "read_search"),
    # ── 简单对话/问答 → 本地GPU零成本 ──
    (r'(?<![a-zA-Z])(what|how|when|who|explain)(?![a-zA-Z])|解释|什么是|怎么样|为什么|如何|介绍', "L2", 0.78, "qa_simple"),
    # ── CAD/PIC/quant 域 → 域特定路由 ──
    (r'(?<![a-zA-Z])(CAD|STEP|build123d|FreeCAD)(?![a-zA-Z])|零件|装配|建模|图纸', "L4", 0.84, "cad_domain"),
    (r'(?<![a-zA-Z])(PIC|plasma)(?![a-zA-Z])|等离子|仿真|粒子模拟|推力器|放电', "L4", 0.84, "pic_domain"),
    (r'量化|回测|因子|策略|期权|期货|[Pp]ortfolio', "L4", 0.82, "quant_domain"),
    # ── 优化/分析/安全 → 中等以上 ──
    (r'优化|改进|性能|安全|漏洞|风险|分析|对比|评估|架构|重构', "L4", 0.76, "analysis_optimization"),
    # ── 长度兜底 (最后匹配, 模糊场景交给模型) ──
    (r'^[\s\S]{2000,}$', "L4", 0.75, "long_context"),
    (r'^[\s\S]{500,1999}$', "L3", 0.70, "medium_context"),
    (r'^[\s\S]{1,499}$', "L2", 0.55, "short_text_ambiguous"),
]

# ── 熔断/降级配置 ────────────────────────────────────────
FALLBACK_CHAIN = ["L3", "L4", "L5"]  # 失败时顺次升级
CONFIDENCE_MIN = 0.65                 # 低于此值升级一层


def _regex_classify(text: str) -> dict | None:
    """正则闸门: 快速匹配规则表, 返回 tier 决策或 None(需模型介入)"""
    text_lower = text.lower()

    for pattern, tier, confidence, reason in REGEX_RULES:
        if re.search(pattern, text_lower, re.IGNORECASE):
            if confidence < CONFIDENCE_MIN:
                return None  # 置信度不够, 交给模型
            return {
                "tier": tier,
                "provider": TIERS[tier]["provider"],
                "model": TIERS[tier]["model"],
                "confidence": confidence,
                "gate": "regex",
                "reason": reason,
            }
    return None


def _model_classify(text: str, context: dict | None = None) -> dict:
    """小模型分类: 调用 qwen2.5:1.5b 判断任务复杂度层级"""
    ctx = json.dumps(context or {}, ensure_ascii=False)[:500]
    prompt = (
        "分析以下任务的复杂度, 选择最合适的推理层级. "
        "只输出一个JSON对象, 包含 tier(层级) confidence(置信度,0-1) reason(理由,<=15字). "
        "不要解释, 不要Markdown.\n\n"
        "六层可选:\n"
        "L0-本地嵌入(关键词匹配/语义检索)\n"
        "L1-免费API(翻译/简单文本处理)\n"
        "L2-二号GPU(分类/摘要/简单问答, 零成本)\n"
        "L3-豆包套餐(中等对话/文件搜索/代码阅读, 已付费)\n"
        "L4-DS Flash(代码生成/复杂推理/领域任务/CAD/PIC/数学, ¥0.02/MTok)\n"
        "L5-DS Flash(极复杂推导/多步规划/长链审计, ¥0.02/MTok, 非必要不选)\n\n"
        f"上下文: {ctx}\n"
        f"任务: {text[:1200]}"
    )

    try:
        data = json.dumps({
            "model": CLASSIFIER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_predict": 100, "temperature": 0.05},
        }).encode()
        req = Request(
            f"{ROUTER_URL}/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=ROUTER_TIMEOUT) as resp:
            body = json.loads(resp.read())
            response_text = body.get("choices", [{}])[0].get("message", {}).get("content", "")

        # 尝试解析 JSON
        json_match = re.search(r'\{[^}]+\}', response_text)
        if json_match:
            result = json.loads(json_match.group())
            tier = result.get("tier", "L3")
            confidence = float(result.get("confidence", 0.7))
            reason = str(result.get("reason", "model_default"))[:30]

            # 校验 tier 有效性
            if tier not in TIERS:
                tier = "L3"
            # 置信度不足 → 升级一层
            if confidence < CONFIDENCE_MIN:
                tier_idx = int(tier[1])
                tier = f"L{min(tier_idx + 1, 5)}"
                reason = f"low_conf_upgrade:{reason}"

            return {
                "tier": tier,
                "provider": TIERS[tier]["provider"],
                "model": TIERS[tier]["model"],
                "confidence": confidence,
                "gate": "model",
                "reason": reason,
            }
    except Exception:
        pass

    # 模型不可用 → 保守降级到 L3
    return {
        "tier": "L3",
        "provider": TIERS["L3"]["provider"],
        "model": TIERS["L3"]["model"],
        "confidence": 0.5,
        "gate": "fallback",
        "reason": "model_unavailable",
    }



# ═══════════════════════════════════════════════════════
# 丘脑过滤 (2026-07-19): 小模型预检→上下文补全→大模型推理
# DS Flash (~¥0.001/次), <700ms, JSON完美, <10s超时fail-open
# ═══════════════════════════════════════════════════════

SF_API_URL = "https://api.deepseek.com/v1/chat/completions"
SF_API_KEY = os.environ.get("DEEPSEEK_PRO_KEY", "")  # 不fallback到ANTHROPIC_KEY
SF_MODEL = "deepseek-v4-flash"  # DS-V4-Flash via API (丘脑预检, ~¥0.001/call)
THALAMUS_TIMEOUT = 10  # 秒, 超时则跳过

# 简单查询跳过丘脑 (节省延迟)
SKIP_PATTERNS = [
    r'^.{1,10}$',           # ≤10字
    r'^(git|push|commit)',  # git操作
    r'^(echo|cat|ls|cd)',   # shell命令
    r'^[\d+\-*/% ]+$',      # 纯数学计算
]

_thalamus_cache = {}  # 查询→补全缓存 (同session内复用)

def _should_skip_thalamus(prompt: str) -> bool:
    for pat in SKIP_PATTERNS:
        if re.search(pat, prompt, re.IGNORECASE):
            return True
    return False


# 脱敏: 发送到第三方API前清除密钥/Token/PII
_SENSITIVE_RE = [
    (r'sk-[a-zA-Z0-9]{20,}', '[API_KEY_REDACTED]'),
    (r'Bearer\s+[a-zA-Z0-9_\-\.]{20,}', '[TOKEN_REDACTED]'),
    (r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', '[EMAIL_REDACTED]'),
    (r'\d{3}[-.]?\d{4}[-.]?\d{4}', '[PHONE_REDACTED]'),
]
def _sanitize(text: str) -> str:
    for pat, repl in _SENSITIVE_RE:
        import re as _re2
        text = _re2.sub(pat, repl, text)
    return text

def _thalamus_check(prompt: str, context: dict | None = None) -> dict:
    """小模型预检: 判断上下文完整性, 返回缺失信息列表"""
    if _should_skip_thalamus(prompt):
        return {"ok": True, "complete": True, "missing": [], "enriched": prompt}

    # 缓存命中
    cache_key = prompt[:200]
    if cache_key in _thalamus_cache:
        return _thalamus_cache[cache_key]

    if not SF_API_KEY:
        return {"ok": False, "reason": "no_key", "enriched": prompt}

    # 构建检测提示
    ctx_desc = ""
    if context:
        ctx_desc = f"当前上下文: {json.dumps(context, ensure_ascii=False)[:300]}"
    safe_prompt = _sanitize(prompt)
    check_prompt = f"""{ctx_desc}
用户输入: {safe_prompt}

你是上下文完整性检查器。判断:
1. 这个查询需要什么前置信息?
2. 当前上下文是否完整? (是/否)
3. 如果缺失, 列出缺失的具体信息项 (≤3项, 每项≤20字)
4. 如果完整, 回复 "COMPLETE"

回复JSON: {{"complete": true/false, "missing": ["信息项1", "信息项2"], "hint": "一句话建议"}}"""

    try:
        import urllib.request
        req = urllib.request.Request(SF_API_URL,
            data=json.dumps({
                "model": SF_MODEL,
                "messages": [{"role": "user", "content": check_prompt}],
                "max_tokens": 100, "temperature": 0.1
            }, ensure_ascii=False).encode(),
            headers={"Authorization": f"Bearer {SF_API_KEY}", "Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=THALAMUS_TIMEOUT)
        body = json.loads(resp.read().decode())
        text = body["choices"][0]["message"]["content"]

        # 解析JSON (Qwen2.5-7B可能输出格式不完美, 多策略兜底)
        import re as _re
        result = None
        # 策略1: 标准JSON
        m = _re.search(r'\{[^}]+\}', text)
        if m:
            try:
                result = json.loads(m.group())
            except:
                pass
        # 策略2: 修复常见错误后重试
        if not result:
            fixed = _re.sub(r'"(true|false)"', r'', text)  # "false" → false
            fixed = _re.sub(r'[^-]+', '', fixed)      # 去非ASCII垃圾
            fixed = _re.sub(r'\s+', ' ', fixed).strip()
            m = _re.search(r'\{[^}]+\}', fixed)
            if m:
                try:
                    # 手动提取字段
                    raw = m.group()
                    complete = '"complete": true' in raw or '"complete":true' in raw or 'complete: true' in raw.lower()
                    missing_match = _re.findall(r'"missing"\s*:\s*\[([^\]]+)\]', raw)
                    hint_match = _re.search(r'"hint"\s*:\s*"([^"]+)"', raw)
                    missing = []
                    if missing_match:
                        items = missing_match[0].replace('"','').split(',')
                        missing = [i.strip() for i in items if i.strip() and len(i.strip()) > 1]
                    hint = hint_match.group(1) if hint_match else ''
                    result = {"complete": complete, "missing": missing, "hint": hint}
                except:
                    pass
        # 策略3: 完全失败 → 放行
        if not result:
            result = {"complete": True, "missing": [], "hint": ""}
        # 统一的result→缓存→返回
        result["ok"] = True
        result["enriched"] = prompt
        if not result.get("complete") and result.get("missing"):
            missing_str = "; ".join(result["missing"])
            result["enriched"] = f"[前置信息需求: {missing_str}] {prompt}"
        _thalamus_cache[cache_key] = result
        return result
    except Exception:
        pass

    fallback = {"ok": True, "complete": True, "missing": [], "enriched": prompt}
    _thalamus_cache[cache_key] = fallback
    return fallback

def route(prompt: str, context: dict | None = None,
          preferred_tier: str | None = None) -> dict:
    """推理路由主入口: 正则→模型→执行

    Args:
        prompt: 用户任务描述
        context: 可选上下文 {"tool": "Write", "domain": "cad", "tokens": 5000}
        preferred_tier: 显式指定层级, 跳过闸门 (如 "L5")

    Returns:
        {"tier": "L4", "provider": "opencode", "model": "deepseek-v4-flash",
         "confidence": 0.85, "gate": "regex", "reason": "code_generation"}

    调用方用返回值调 api_pipeline.call(provider, model, messages=...)
    """
    # 显式指定 → 跳过所有闸门
    if preferred_tier and preferred_tier in TIERS:
        return {
            "tier": preferred_tier,
            "provider": TIERS[preferred_tier]["provider"],
            "model": TIERS[preferred_tier]["model"],
            "confidence": 1.0,
            "gate": "explicit",
            "reason": "user_specified",
        }

    # ① 丘脑过滤: 小模型预检上下文完整性 (新增)
    thalamus = _thalamus_check(prompt, context)
    enriched_prompt = thalamus.get("enriched", prompt)
    if not thalamus.get("complete", True):
        # 上下文不完整 → 仍然路由, 但prompt已自动注入缺失信息
        prompt = enriched_prompt

    # ② 正则闸门
    result = _regex_classify(prompt)
    if result:
        # 标注丘脑参与
        if not thalamus.get("complete", True):
            result["_thalamus"] = {"missing": thalamus.get("missing", []), "hint": thalamus.get("hint", "")}
        return result

    # ③ 小模型分类
    decision = _model_classify(prompt, context)
    if isinstance(decision, dict) and not thalamus.get("complete", True):
        decision["_thalamus"] = {"missing": thalamus.get("missing", []), "hint": thalamus.get("hint", "")}
    return decision


def route_and_call(prompt: str, system: str = "",
                   context: dict | None = None,
                   max_tokens: int = 1024,
                   temperature: float = 0.1) -> dict | None:
    """route() + api_pipeline.call() 一站式调用

    返回 api_pipeline 结果, 或降级重试后的结果。
    """
    from scripts.wheels.api_pipeline import call

    decision = route(prompt, context)
    provider = decision["provider"]
    model = decision["model"]
    tier = decision["tier"]

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    result = call(provider, model, messages=messages,
                  max_tokens=max_tokens, temperature=temperature)

    # 如果失败且非 L5, 顺次降级
    if not result or not result.get("ok"):
        tier_idx = int(tier[1])
        for fallback_tier in FALLBACK_CHAIN:
            fb_idx = int(fallback_tier[1])
            if fb_idx <= tier_idx:
                continue
            fb = TIERS[fallback_tier]
            result = call(fb["provider"], fb["model"], messages=messages,
                          max_tokens=max_tokens, temperature=temperature)
            if result and result.get("ok"):
                result["_routed_from"] = tier
                result["_routed_to"] = fallback_tier
                break

    if result:
        result["_routing"] = decision

    return result


# ── CLI ───────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python tier_router.py classify <prompt>")
        print("      python tier_router.py route <prompt>")
        sys.exit(1)

    cmd = sys.argv[1]
    text = sys.argv[2] if len(sys.argv) > 2 else "Hello, classify this task"

    if cmd == "classify":
        decision = route(text)
        print(json.dumps(decision, ensure_ascii=False, indent=2))
    elif cmd == "route":
        result = route_and_call(text)
        if result:
            routing = result.pop("_routing", {})
            print(f"[{routing.get('tier','?')}] {routing.get('gate','?')}: {routing.get('reason','?')}")
            print(f"text: {result.get('text','')[:300]}")
            print(f"tokens: {result.get('tokens',{})}")
        else:
            print("ERROR: 所有层级调用失败")
