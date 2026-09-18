#!/usr/bin/env python3
"""
PromptSubmit_search.py — 智能搜索触发 hook
============================================
在用户提交 prompt 后、Claude 处理前，分析是否需要外部知识/最新信息。

设计原则:
  1. 快（纯规则，<70ms）—— 不阻塞用户交互
  2. 不搜（不自己调搜索引擎）—— 只注入指引，让 Claude 用 WebSearch 搜
  3. 不误报（少而精）—— 宁可漏过不可乱报
  4. 无 GPU / 无远端 API 依赖

架构:
  UserPromptSubmit（你输入）→ 本脚本（分析）→ additionalContext（注入）
                                              ↓
                                     Claude 看到指引 → 自动 WebSearch
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ─── 启动计时（为性能监控） ───
_start = time.perf_counter()

# ─── 编码 ───
SYS_ENC = "gbk" if sys.platform == "win32" else "utf-8"


# ═══════════════════════════════════════════════════
# 信号模式库
# ═══════════════════════════════════════════════════

# 🟢 明确需要搜索的信号（高置信度）
_SEARCH_SIGNALS_HIGH = [
    # 时间敏感词（问当前状态 / 最新信息）
    r'(?:最[新近]|目前|现在|当前|今天的|截至|截止|as\s+of|currently|nowadays)',
    r'(?:latest|recent|up[\s-]to[\s-]date|current\s+(?:status|price|version|news))',
    r'(?:202[5-9]|203\d)',  # 未来年份引用

    # 外部服务 / API / 库 — 版本和用法可能已过时
    r'(?:API\s*(?:变化|更新|版本|变更|upgrade|version|change|deprecat))',
    r'(?:library|package|module)\s+(?:version|latest|update|release)',
    r'(?:npm|pip|maven|nuget|cargo)\s+(?:install|publish|update)',

    # 价格 / 市场 / 政策（频繁变化）
    r'(?:价格|定价|费用|订阅|pricing|subscription|rate\s*limit)',
    r'(?:市场份额|market\s*share|stock\s*price|股价)',

    # 新闻 / 事件 / 公告
    r'(?:新闻| announced|released|published\s+\w+\s+(?:paper|article|report))',
    r'(?:paper\s*:\s*20|arXiv|最新\s*(?:研究|论文))',

    # 明确指令要求搜索
    r'(?:搜[索一]下|查[一]?查|帮我搜|google\s+it|search\s+for|look\s+up|find\s+out)',
    r'(?:research\s+(?:this|that|the)|do\s+(?:a\s+)?research)',
]

# 🟡 中等置信度信号（可能不需要搜索，需要进一步判断）
_SEARCH_SIGNALS_MED = [
    # 技术概念/工具的外部知识
    r'(?:什么是|what\s+is|how\s+(?:does|do|can|to)\s+\w+\s+(?:work|use|implement))',
    r'(?:怎么用|如何使用|怎样|how\s+to\s+use\s+\w+)',
    r'(?:explain|understanding|difference\s+between|comparison\s+of)',
    r'(?:tutorial|guide|documentation|docs\s+for|manual)',

    # 特定领域术语（可能是内部知识也可能是外部）
    r'(?:[Hh]all\s+[Tt]hruster|[Ee]lectric\s+[Pp]ropulsion|[Pp]lasma\s+[Dd]ischarge)',
    r'(?:[Ff]araday|[Bb]oltzmann|[Mm]axwell|[Pp]oisson|[Ss]putter)',
    r'(?:MCP\s+(?:server|tool|protocol)|Claude\s+Code)',
    # 库/框架名（外部知识为主）
    r'(?:build123d|cadquery|open[\s-]?cascade|numpy|pandas|torch|tensorflow|transformers)',
    r'(?:FastMCP|Model\s*Context\s*Protocol|LangChain|LangGraph|CrewAI)',

    # 最佳实践 / 标准 / 规范（可能变化）
    r'(?:best\s+practice|industry\s+standard|recommended\s+way|common\s+approach)',
    r'(?:design\s+pattern|architecture\s+pattern|anti[\s-]pattern)',

    # 概念探索结构（X的原理/机制/区别/工作方式——语义性问题，不依赖具体领域词）
    r'.{3,}(?:的原理|的机制|的区别|间区别|的比较|的对比|的工作原理|的工作方式|的基本概念)',
    r'(?:^|.{0,4})(?:、|和|与|vs|versus)\w{1,12}(?:区别|差异|对比)',
    r'(?:什么叫|何为|何为|何谓)',
    r'.{8,}(?:有什么区别|有啥区别|差在哪|差异|vs|versus|和\w+比)',
    r'.{6,}(?:一般多少|大概多少|典型值|typical|typical value|标准范围)',
    r'.{8,}(?:最新进展|研究现状|发展趋势|前沿方向|领域现状|综述|survey)',
]

# 🔴 明确不需要搜索的信号（高置信度排除）
_SEARCH_EXCLUDE = [
    # 项目内部引用（自己写的代码/knowledge）
    r'(?:我的|我们的|本项目|在 CLS 中|我[的们]项目|project\s+(?:root|dir))',
    r'(?:scripts/(?:wheels/)?\w+|CLAUDE\.md|\.mcp\.json|settings\.json)',
    r'(?:assistant|张maintainer|maintainer)',

    # 纯代码/技术问题（不需要外部知识的）
    r'(?:帮我[写改修]|请[写改修]|implement\s+(?:a\s+)?function)',
    r'(?:这个\s*(?:代码|函数|类|方法|脚本).*怎么|(?:代码|bug|error)\s+(?:review|fix|debug))',
    r'(?:refactor|重构|优化|optimize|clean\s+up)',

    # 当前上下文/对话历史（不需要搜）
    r'(?:我们刚才|刚才说|上一个问题|刚才的|as\s+(?:I|we)\s+(?:said|mentioned))',
    r'(?:继续|接着|续|continue|keep\s+going|pick\s+up)',

    # 时间词 + 内部上下文（"目前/现在/最近/最新"命中但其实是项目内问题）
    r'(?:目前|现在|最近)\s*(?:这个|那个|的)?.*?(?:代码|函数|配置|设置|文件|目录|路径|bug|commit|脚本|轮子|接口|类|模块)',
    r'(?:最新)\s*(?:版本|配置|设置|代码|函数|脚本)\s*(?:的)?.*?(?:怎么|如何|怎样|安装|使用)',
    r'(?:查一下|看看|检查|看下)\s*(?:这个|那个)?\s*(?:代码|函数|配置|文件|目录|commit|设置)',

    # 概念探索 + 内部上下文（"这个函数的原理"类——对内探索不搜）
    r'(?:这个|那个|本)\s*\w{0,4}(?:代码|函数|类|方法|脚本|模块|配置|变量)\s*的\s*(?:原理|机制|区别|工作方式)',
    r'(?:这|那)\w{1,4}(?:代码|函数|类|方法|脚本|模块|配置|变量)\s*的\s*(?:原理|机制|区别|工作方式)',

    # 文件路径/日期目录（incident-log#33 修复: 2026-08/xxx 或 file:///C:/... 里的年份是路径日期, 非需搜索信息）
    r'\d{4}-\d{2}\s*[\\/]',          # YYYY-MM 后跟路径分隔符（微信文件目录）
    r'file://[^\s；;，,\n]*',         # file:// 分享的文件 URL

    # 简短确认 / 闲聊
    r'^[好是行可以嗯ok]+\s*$',
    r'^(?:好的|可以|行|嗯|ok|OK|是的|对|没错|继续|接着说|谢谢|感谢)$',
    r'^\s*[,，.。!！?？\s]*$',
]


# ═══════════════════════════════════════════════════════
# 场景分类（纯正则） — 检测当前对话场景 → 挂载 L1 人格模块
# ═══════════════════════════════════════════════════════
# 设计：正则命中即录用。不命中 = general_chat（无非零漏报风险，但零误报）
# 准确度：正则预筛模式专门化，假阳性率 <1%；真阴性率 >95%
# 速度：纯文本匹配 <0.5ms，不调模型

# 场景 → L1 人格文件指针映射
_L1_MOUNT = {
    "technical_deep_dive": "persona/rhythm_guide.json (R01/R04) + persona/decision_tree.json (场景1/7)",
    "user_frustrated":     "persona/expression_library.json (重启) + persona/rhythm_guide.json (R08)",
    "user_flawed_plan":    "persona/decision_tree.json (场景3) + persona/rhythm_guide.json (R10)",
    "emergency_fix":       "persona/decision_tree.json (场景6)",
    "celebration":         "persona/expression_library.json (庆祝)",
    "learning_new":        "persona/decision_tree.json (场景7) + persona/rhythm_guide.json (R09)",
    "emotional_support":   "persona/expression_library.json (静观) + persona/rhythm_guide.json (沉默协议)",
    "criticism_received":  "persona/decision_tree.json (场景10) + 本我/meta_cognition.json (META-002)",
    "planning_discussion": "persona/decision_tree.json (场景2/5)",
}

# 场景正则模式（命中即录用，优先级：列表顺序 —— 越靠前越优先）
_SCENE_PATTERNS = [
    ("emergency_fix",      [r"崩了|挂了|出事了|紧急|立刻.*(?:看|查|修)|马上.*(?:处理|解决)|(?:线上|生产环境).*(?:崩|挂|出事了?|紧急)|损失.*数据|数据.*损失"]),
    ("user_frustrated",    [r"烦(?:死|躁)?|不做了?|搞不定|算了|又崩了|放?弃|累了|不想弄|搞了一天|怎么还(?:没好|不行)|一直(?:报错|失败)"]),
    ("criticism_received", [r"不对|错了[！!]?|不是这样|你没理解|搞错了|你搞错|你(?:根本|完全).*(?:没|不|错)|理解(?:错|有误|错误)"]),
    ("technical_deep_dive",[r"为什么.*(?:这样|如此|设计|实现)|原理|机制|架构|设计模式|根因|root.cause|源码|实现细节|底层|本质"]),
    ("learning_new",       [r"这是什么|怎么用|教教我|没学过|不太懂|第一次(?:见|用|接触)|从未|零基础|新手|入门|教程"]),
    ("celebration",        [r"成功了|跑通了|做完了|终于|搞定|完成了|实现了|成了[！!]|nice|yyds"]),
    ("emotional_support",  [r"好累|好烦|没意思|不想(?:做|动|弄)|没劲|孤独|难过|伤心|郁闷|emo(?:中|了)?|破防"]),
    ("planning_discussion",[r"(?:方案|计划|设计).*(?:讨论|确定|评审|review|选择|选型)|你觉得(?:怎么|如何|怎么样)|有什么(?:办法|建议|方案)"]),
    ("user_flawed_plan",   [r"你的方案(?:太|不|很)|你这个(?:方案|设计).*(?:不行|不好|有问题|不靠谱|太复杂)|改方案|换个(?:思路|方案|方向)"]),
]


def classify_scene(prompt: str) -> str:
    """纯正则场景分类。命中即返回场景名，不命中返回 general_chat。
    速度：<0.5ms，零外部依赖。"""
    for scene, pats in _SCENE_PATTERNS:
        if any(re.search(p, prompt) for p in pats):
            return scene
    return "general_chat"


def detect_search_need(prompt: str) -> tuple[bool, str]:
    """分析 prompt 是否需要搜索。返回 (是否需要, 原因)。

    决策树:
      1. 高置信度排除信号匹配 → 不需要搜索
      2. 高置信度搜索信号匹配 → 需要搜索
      3. 中等信号匹配 + 无排除信号 → 需要搜索
      4. 无信号匹配 → 不需要搜索
    """
    prompt_lower = prompt.lower()

    # Step 1: 排除检查（优先）
    for pat in _SEARCH_EXCLUDE:
        if re.search(pat, prompt, re.IGNORECASE):
            return False, f"excluded by: {pat[:40]}"

    # Step 2: 高置信度搜索信号
    for pat in _SEARCH_SIGNALS_HIGH:
        if re.search(pat, prompt, re.IGNORECASE):
            return True, f"high-confidence: {pat[:40]}"

    # Step 3: 中等信号 + 非极短问题（中文概念问题可能短至6-8字）
    if len(prompt) > 5:
        for pat in _SEARCH_SIGNALS_MED:
            if re.search(pat, prompt, re.IGNORECASE):
                return True, f"medium-confidence: {pat[:40]}"

    return False, "no signal"


# ═══════════════════════════════════════════════════
# 主逻辑
# ═══════════════════════════════════════════════════

def _strip_hook_injections(prompt_text: str) -> str:
    """剥离 hook 链上游注入块, 还原纯用户输入 (防注入自噬循环, incident-log#33)。

    hook 链 UserPromptSubmit 顺序执行 (PromptSubmit.ps1 → 本脚本 → semantic_inject.py),
    data.prompt 可能含上游注入文本。若拿注入文本跑搜索正则, 命中其字面关键词
    (vs/差异/区别/和) → 自我回放"秒出"。规则同 semantic_inject._strip_hook_injections。
    @since 2026-08-02
    """
    if not prompt_text or "UserPromptSubmit says:" not in prompt_text:
        return prompt_text
    idx = prompt_text.rfind("⎿")
    if idx < 0:
        return prompt_text
    tail = prompt_text[idx:].lstrip("⎿ \xa0\t\n")
    if not tail.startswith("UserPromptSubmit says:"):
        return prompt_text          # 用户自己打了 ⎿, 原样返回
    body = tail[len("UserPromptSubmit says:"):].lstrip(" \xa0\n")
    if body.startswith(("【", "[", "（", "系统注入")):
        last = None
        for m in re.finditer(r"。\s", body):
            last = m
        if last:
            clean = body[last.end():].strip()
            return clean if clean else body.strip()
    return body.strip()


def main():
    # 读取 stdin（CC 传入的 JSON）
    raw = sys.stdin.buffer.read().decode(SYS_ENC, errors="replace").strip()
    if not raw:
        sys.exit(0)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    prompt = data.get("prompt", "")
    # @fix 2026-08-02 incident-log#33: 剥离上游 hook 注入块, 防拿注入文本跑正则自我触发
    prompt = _strip_hook_injections(prompt)
    if not prompt:
        sys.exit(0)

    t0 = time.perf_counter()

    # ─── 双轨检测：搜索 + 场景 ───
    needs_search, search_reason = detect_search_need(prompt)
    scene = classify_scene(prompt)

    elapsed_ms = (time.perf_counter() - t0) * 1000

    # 构建输出
    additional_parts = []
    hook_info = {
        "hookEventName": "UserPromptSubmit",
        "latency_ms": round(elapsed_ms, 1),
    }

    # 轨道 A: 搜索指引
    if needs_search:
        additional_parts.append(
            f"[自动检测] 用户问题可能涉及需要最新外部知识 ({search_reason})。"
            f"如果训练数据中缺乏相关信息，请使用 WebSearch 或 WebFetch 获取后再回答。"
        )
        hook_info["search_triggered"] = True
        hook_info["search_reason"] = search_reason

    # 轨道 B: L1 人格模块注入
    if scene != "general_chat":
        l1_ref = _L1_MOUNT.get(scene, "")
        if l1_ref:
            additional_parts.append(
                f"[场景: {scene}] 如需人格参考: {l1_ref}"
            )
        hook_info["scene"] = scene

    # 输出
    if additional_parts:
        inj_text = " | ".join(additional_parts)
        result = {
            "additionalContext": inj_text,
            "hookSpecificOutput": hook_info,
            # 官方屏幕显示通道: systemMessage → CC 显示给用户 (stderr 无控制终端不显示)
            "systemMessage": "[CLS注入-搜索] " + inj_text[:800],
        }
        print(json.dumps(result, ensure_ascii=False))
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
