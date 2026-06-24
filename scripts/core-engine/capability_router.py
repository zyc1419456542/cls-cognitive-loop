#!/usr/bin/env python3
"""
capability_router.py — 能力路由查询器
======================================
读 data/capability_router.json，根据任务描述匹配意图域，返回应使用的轮子。
只做工具选择路由，不做层级路由判断。

用法:
  python scripts/core-engine/capability_router.py --lookup "任务描述"
  python scripts/core-engine/capability_router.py --list
  python scripts/core-engine/capability_router.py --route "任务描述"  → 完整路由+自动记录日志

设计原则:
  - 确定性：纯规则匹配（when条件+关键词），不走LLM
  - 可审计：每次路由自动写 search_usage.jsonl
  - 强制路径：forbid 列表是硬约束
"""
import json, sys, os, re
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent.parent.parent
ROUTER_CONFIG = BASE / "data" / "capability_router.json"
USAGE_LOG = BASE / "data" / "routing" / "search_usage.jsonl"


def load_router() -> dict:
    if not ROUTER_CONFIG.exists():
        return {"domains": []}
    return json.loads(ROUTER_CONFIG.read_text(encoding="utf-8"))


def match_domain(task_text: str, router: dict = None) -> dict | None:
    """根据任务描述匹配最佳意图域。纯规则匹配。"""
    if router is None:
        router = load_router()

    domains = router.get("domains", [])
    text = task_text.lower()

    # 关键词触发映射（与 capability_router.json 的 when 条件对齐）
    KEYWORD_MAP = {
        "semantic_search": [
            "找一下", "搜索", "检索", "查找", "在哪", "哪些文件",
            "有没有.*的", "关于.*的", "讨论", "知识库", "之前.*的",
            "semantic", "语义", "概念", "话题",
        ],
        "fulltext_search": [
            "grep", "全文", "精确", "函数定义", "变量", "引用",
            "调用", "定义在", "哪个文件", "源码", "代码搜索",
            "匹配", "正则", "pattern",
        ],
        "image_analysis": [
            "截图", "图片", "图像", "ocr", "看图", "这张图",
            "屏幕", "screenshot", "识别", "照片",
        ],
        "numerical_compute": [
            "计算", "算一下", "求和", "均值", "方差", "拟合",
            "数值", "公式", "验证.*n=", "反代", "统计",
            "矩阵", "积分", "微分",
        ],
        "local_inference": [
            "分类", "embedding", "相似度", "本地模型", "小模型",
            "推理", "infer", "审核.*模型", "local.*infer",
        ],
        "knowledge_management": [
            "记录", "记住", "保存.*知识", "沉淀", "经验",
            "捕获", "capture", "入库", "学到", "学会了",
            "知识.*管理", "memory",
        ],
        "cad_design": [
            "cad", "建模", "设计.*机械", "设计.*零件", "装配",
            "step", "3d", "法兰", "支架", "加工",
            "build123d", "参数化", "几何",
        ],
        "pic_simulation": [
            "pic", "仿真", "等离子体", "粒子模拟", "霍尔推力器",
            "warpx", "pic4ai", "bohm", "langmuir", "spt",
            "推力器", "放电", "离子",
        ],
        "quant_trading": [
            "策略", "回测", "止损", "rsi", "macd", "均线",
            "量化", "交易", "仓位", "信号", "参数优化",
            "evolution", "regime",
        ],
        "safety_verification": [
            "验证", "审核", "检查.*是否", "闸门", "熔断",
            "qwen", "安全", "是不是.*正确", "质量.*检查",
            "审计", "audit",
        ],
        "system_operations": [
            "激活", "compact", "守护", "daemon", "进程",
            "重启", "健康", "资源", "cpu", "内存",
            "self_activate", "health", "状态",
        ],
        "text_processing": [
            "转换", "格式", "docx", "markdown", "专利",
            "替换.*所有", "批量", "文档.*生成", "渲染",
            "排版", "导出",
        ],
    }

    scores = {}
    for domain_id, patterns in KEYWORD_MAP.items():
        score = 0
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                score += 1
        if score > 0:
            scores[domain_id] = score

    if not scores:
        return None

    # 返回最高分匹配
    best_id = max(scores, key=scores.get)
    for d in domains:
        if d["id"] == best_id:
            return {
                "domain": d,
                "match_score": scores[best_id],
                "all_scores": dict(sorted(scores.items(), key=lambda x: -x[1])[:3]),
            }

    return None


def lookup(task_text: str, quiet: bool = False) -> dict:
    """查询应使用的轮子。薄封装 match_domain + 格式化输出。"""
    result = match_domain(task_text)
    if result is None:
        return {
            "matched": False,
            "message": "未匹配到特定意图域，使用 tool_choice=auto（优先本地轮子）",
            "advice": "如不确定，优先用 semantic_query 或 text_scanner，避免裸 grep/PowerShell",
        }

    d = result["domain"]
    output = {
        "matched": True,
        "domain": d["name"],
        "domain_id": d["id"],
        "match_score": result["match_score"],
        "primary": d["primary"]["wheel"],
        "primary_cli": d["primary"]["cli"],
        "fallback": d["fallback"]["wheel"] if d.get("fallback") else None,
        "forbid": d["forbid"],
        "tool_choice": d["tool_choice"],
    }

    if not quiet:
        print(f"📍 路由: {d['name']} (匹配度={result['match_score']})")
        print(f"   主轮子: {d['primary']['wheel']}")
        print(f"   命令:   {d['primary']['cli']}")
        if d.get("fallback") and d["fallback"].get("wheel"):
            print(f"   降级:   {d['fallback']['wheel']}")
        if d["forbid"]:
            print(f"   🚫禁止: {', '.join(d['forbid'])}")

    return output


def route_and_log(task_text: str) -> dict:
    """完整路由 + 自动记录到 search_usage.jsonl"""
    result = lookup(task_text, quiet=True)

    # 写入日志
    os.makedirs(USAGE_LOG.parent, exist_ok=True)
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": "wheel" if result.get("matched") else "manual",
        "tool": result.get("primary", "unknown") if result.get("matched") else "unknown",
        "query": task_text[:200],
        "files_searched": 0,
        "routing": {
            "domain": result.get("domain"),
            "domain_id": result.get("domain_id"),
            "match_score": result.get("match_score"),
            "forbid": result.get("forbid", []),
        },
    }
    with open(USAGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    # 输出结果
    lookup(task_text, quiet=False)
    return result


def list_domains() -> list:
    """列出所有12个意图域"""
    router = load_router()
    domains = router.get("domains", [])
    print(f"{'#':<3} {'意图域':<12} {'主轮子':<25} {'触发条件'}")
    print("-" * 80)
    for i, d in enumerate(domains, 1):
        when_short = d["when"].split("/")[0].strip()[:35]
        print(f"{i:<3} {d['name']:<12} {d['primary']['wheel']:<25} {when_short}")
    print(f"\n共 {len(domains)} 个意图域 | 完整定义: data/capability_router.json")
    return domains


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "--list":
        list_domains()

    elif cmd == "--lookup" and len(sys.argv) > 2:
        task = " ".join(sys.argv[2:])
        lookup(task)

    elif cmd == "--route" and len(sys.argv) > 2:
        task = " ".join(sys.argv[2:])
        route_and_log(task)

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
