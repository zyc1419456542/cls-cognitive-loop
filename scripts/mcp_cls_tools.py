#!/usr/bin/env python3
"""
mcp_cls_tools.py — CLS 工具包 MCP 服务
=========================================
将所有 CLAUDE.md 决策表中"文本指令"状态的轮子包装为 MCP 工具，
让 CC 可直接调用而非绕 Bash/Python。

架构:
  CC session → MCP stdio → 本服务 → 各 wheels 函数 → JSON 返回

运行:
  python scripts/mcp_cls_tools.py               # stdio 模式（给 CC/MCP 调用）
  python scripts/mcp_cls_tools.py --test        # 冒烟测试

CLAUDE.md 双重标记:
  原文保留不变 + 这里暴露为工具 → 双路径记忆
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ─── 项目路径 ──────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "wheels"))  # 让 wheels 间相对 import 可用

# ─── MCP 服务初始化 ─────────────────────────────────
mcp = FastMCP(
    "cls-tools",
    instructions="CLS 工具包 — 语义搜索/精确检索/API调用/推理路由/工具查询/"
    "incident-log/进度/失败归档/交付检查/论文搜索/系统健康。"
    "覆盖 CLAUDE.md 决策表 #1 #2 #5 #5c #10 #11 #18 #20 #21 #22 等条目。"
    "原文仍在 CLAUDE.md 中，此为双路径调用的便捷入口。",
)

# ═══════════════════════════════════════════════════════
# 工具 1: 语义搜索 (取代 pipeline.py search)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-semantic-search",
    description="四路融合语义检索（向量+BM25+时间衰减+知识图谱），"
    "搜索项目内所有知识/代码/交付/学习资料。返回 top-N 结果含路径、分数和摘要。",
)
def semantic_search(
    query: str,
    top_n: int = 10,
    dir_filter: str = "",
    use_vector: bool = True,
    use_bm25: bool = True,
    use_graph: bool = True,
) -> str:
    """跨项目knowledge做语义搜索。

    Args:
        query: 搜索查询，自然语言描述你想找的内容
        top_n: 返回结果数（默认 10，最大 30）
        dir_filter: 目录过滤关键词（如 "assistant设计"、"CAD"、"quant"）
        use_vector: 启用向量检索（默认 True）
        use_bm25: 启用 BM25 关键词检索（默认 True）
        use_graph: 启用知识图谱关联扩展（默认 True）

    Returns:
        JSON: [{score, path, text_preview, chunk_id, ...}, ...]
    """
    try:
        from scripts.wheels.semantic_query import search
        results = search(
            query=query,
            top_n=min(top_n, 30),
            dir_filter=dir_filter or None,
            use_vector=use_vector,
            use_bm25=use_bm25,
            use_graph=use_graph,
        )
        return json.dumps(results, ensure_ascii=False, default=str)
    except ImportError as e:
        return json.dumps({"error": f"semantic_query 不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 2: 精确/关键词搜索 (取代 grep/text_scanner)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-exact-search",
    description="精确关键词/文件名搜索，基于 text_scanner 索引。"
    "适合搜索精确文件名、函数名、变量名等确定性目标。",
)
def exact_search(
    query: str,
    top_n: int = 10,
    dir_filter: str = "",
) -> str:
    """在 text_scanner 索引中做精确/关键词搜索。

    Args:
        query: 搜索关键词（精确匹配/文件名/符号名）
        top_n: 返回结果数（默认 10）
        dir_filter: 目录过滤

    Returns:
        JSON: [{score, path, text_preview, ...}, ...]
    """
    try:
        from scripts.wheels.text_scanner import cmd_search
        # cmd_search 接受 list of str（CLI args）
        # 插入 -- 防参数注入（query 若以 - 开头会被误解析为 flags）
        args_list = ["--", query, "--top", str(top_n)]
        if dir_filter:
            args_list += ["--dir", dir_filter]

        # 重定向 stdout 捕获结果
        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_search(args_list)
        output = f.getvalue()

        # 尝试解析为 JSON（可能含打印日志）
        lines = output.strip().split("\n")
        data_lines = [l for l in lines if l.startswith("[")]
        if data_lines:
            try:
                return json.dumps([json.loads(l) for l in data_lines], ensure_ascii=False)
            except json.JSONDecodeError:
                pass
        return json.dumps({"raw_output": output[:2000]}, ensure_ascii=False)
    except ImportError as e:
        return json.dumps({"error": f"text_scanner 不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 2.5: 统一搜索 (四系统并行 → RRF融合)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-unified-search",
    description="统一搜索入口——并行查询FAISS语义+TextScanner精确+Knolib+Embed+ChromaDB(可选)，RRF融合排序。",
)
def cls_unified_search(query: str, top_n: int = 10, include_hunt: bool = False) -> dict:
    import concurrent.futures
    results = {"ok": True, "query": query, "sources": {}, "merged": []}
    all_items = []

    def _semantic():
        try:
            from semantic_query import search
            hits = search(query, top_n=top_n)
            return [{"source": "faiss",
                     "file": h.get("path", h.get("file", "")),
                     "text": (h.get("text_preview") or h.get("text") or h.get("content") or "")[:200],
                     "score": float(h.get("score", h.get("_score", 0.5)) or 0.5)} for h in (hits or [])]
        except:
            return [{"source": "faiss", "error": "failed"}]

    def _knolib():
        try:
            sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
            import knowledge_retriever
            hits = knowledge_retriever.search(query, top_n=top_n) or []
            return [{"source": "knolib", "file": h.get("file", h.get("path", "")),
                     "text": (h.get("text") or h.get("content") or "")[:200],
                     "score": h.get("score", 0.5)} for h in hits]
        except:
            return [{"source": "knolib", "error": "failed"}]

    def _embed():
        try:
            from embed_and_index import search_similar
            hits = search_similar(query, top_n=top_n) or []
            return [{"source": "embed", "file": h.get("file", h.get("path", "")),
                     "text": (h.get("text") or h.get("content") or "")[:200],
                     "score": h.get("score", 0.5)} for h in hits]
        except:
            return [{"source": "embed", "error": "failed"}]

    def _hunt():
        if not include_hunt:
            return []
        try:
            from tribal_mcp import search_errors
            hits = search_errors(query) or []
            return [{"source": "hunt", "file": h.get("id", h.get("file", "")),
                     "text": (h.get("description") or h.get("reason") or str(h))[:200],
                     "score": h.get("relevance", 0.5)} for h in hits]
        except:
            return [{"source": "hunt", "error": "failed"}]

    searchers = {"faiss": _semantic, "knolib": _knolib, "embed": _embed}
    if include_hunt:
        searchers["hunt"] = _hunt

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(fn): name for name, fn in searchers.items()}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                hits = fut.result(timeout=30)
                results["sources"][name] = {"count": len(hits), "ok": True}
                all_items.extend(hits)
            except Exception as e:
                results["sources"][name] = {"count": 0, "ok": False, "error": str(e)[:80]}

    # Normalize all field names across backends
    for r in all_items:
        if r.get("error"):
            continue
        # Unify field names
        r["file"] = r.get("file") or r.get("path") or r.get("id") or r.get("name") or ""
        r["text"] = (r.get("text") or r.get("text_preview") or r.get("content") or
                       r.get("preview") or r.get("description") or r.get("reason") or "")[:200]
        s = r.get("score", r.get("relevance", r.get("_score", 0.5)))
        try:
            r["score"] = float(s) if s else 0.5
        except:
            r["score"] = 0.5

    merged_map = {}
    for r in all_items:
        fname = r.get("file", "")
        if not fname or r.get("error"):
            continue
        s = r.get("score", 0.5) or 0.5
        if fname not in merged_map:
            merged_map[fname] = {"file": fname, "rrf_score": 0.0, "text": "", "sources": []}
        merged_map[fname]["rrf_score"] += s
        merged_map[fname]["text"] = max([merged_map[fname]["text"], r.get("text", "")], key=len)
        src = r.get("source", "?")
        if src not in merged_map[fname]["sources"]:
            merged_map[fname]["sources"].append(src)

    results["merged"] = sorted(merged_map.values(), key=lambda x: x["rrf_score"], reverse=True)[:top_n]
    results["total"] = len(results["merged"])
    return results


# ═══════════════════════════════════════════════════════
# 工具 3: 统一 API 调用
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-api-call",
    description="统一外部 API 调用入口。支持七类 provider: "
    "qwen / anthropic / deepseek / volc / openai / ollama / local_gpu。"
    "自动审计 + 实时打印 + JSONL 日志。",
)
def api_call(
    provider: str,
    model: str = "",
    prompt: str = "",
    system_prompt: str = "",
    max_tokens: int = 1024,
    temperature: float = 0.1,
    timeout_s: int = 180,
) -> str:
    """调用远端 LLM API，走统一审计管线。

    Args:
        provider: API 提供商 (qwen/anthropic/deepseek/volc/openai/ollama/local_gpu)
        model: 模型名（留空 auto-select）
        prompt: 用户提示词
        system_prompt: 系统提示词（可选）
        max_tokens: 最大 token 输出（默认 1024）
        temperature: 采样温度（默认 0.1）
        timeout_s: 超时秒数（默认 180）

    Returns:
        JSON: {content, model, usage, latency_s, ...}
    """
    # SSRF 防护: provider 白名单校验
    _ALLOWED_PROVIDERS = {"qwen", "anthropic", "deepseek", "volc", "openai", "ollama", "local_gpu"}
    provider_clean = provider.strip().lower()
    if provider_clean not in _ALLOWED_PROVIDERS:
        return json.dumps({
            "error": f"provider '{provider}' 不在白名单中。允许: {', '.join(sorted(_ALLOWED_PROVIDERS))}"
        }, ensure_ascii=False)

    try:
        from scripts.wheels.api_pipeline import call
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        result = call(
            provider=provider,
            model=model or "",
            messages=messages,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_s=timeout_s,
        )
        return json.dumps(result, ensure_ascii=False, default=str)
    except ImportError as e:
        return json.dumps({"error": f"api_pipeline 不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 4: 本地推理路由
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-local-inference",
    description="将推理任务自动路由到最合适的本地模型。"
    "小任务走 qwen2.5:1.5b，中等任务走 deepseek-r1:8b。"
    "绕开远端 API 成本，适合轻量分析。",
)
def local_inference(
    prompt: str,
    task_type: str = "generate",
) -> str:
    """在本地模型上执行推理。

    Args:
        prompt: 推理提示词
        task_type: 任务类型 (generate/analyze/code/qa) — 默认 generate

    Returns:
        JSON: {content, model_used, latency_s, ...}
    """
    try:
        from scripts.wheels.inference_router import infer
        result = infer(prompt=prompt, task_type=task_type)
        return json.dumps(result, ensure_ascii=False, default=str)
    except ImportError as e:
        return json.dumps({"error": f"inference_router 不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 5: 工具查询 (取代 decision table 翻查)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-capability-lookup",
    description="查询哪个轮子/工具适合处理指定的任务。"
    "输入自然语言描述任务，返回匹配的工具名、路径和使用方式。",
)
def capability_lookup(
    task_text: str,
) -> str:
    """查询能力路由器，找到处理指定任务的轮子。

    Args:
        task_text: 任务描述（自然语言），如"画一个CAD零件图"、"搜索<DOMAIN设备>论文"

    Returns:
        JSON: {matched_tool, path, usage, confidence}
    """
    try:
        from scripts.wheels.capability_router import lookup
        result = lookup(task_text=task_text, quiet=True)
        return json.dumps(result, ensure_ascii=False, default=str)
    except ImportError as e:
        return json.dumps({"error": f"capability_router 不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 6: incident-log记录
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-baobi-record",
    description="记录一条incident-log条目（致死经验/事故/教训）。"
    "写入唯一 canonical 地址: knowledge/05_CLS认知系统架构/认知系统迭代/incident-log.md。"
    "请提供症状、根因和修复方案。用于 AI 暴毙模式积累。",
)
def baobi_record(
    symptom: str,
    cause: str = "",
    fix: str = "",
    lesson: str = "",
    related: str = "",
    domain: str = "general",
    severity: str = "info",
) -> str:
    """记录新incident-log。

    Args:
        symptom: 现象/症状（一句话，必填）
        cause: 根因分析（可选）
        fix: 修复方案（可选）
        lesson: 教训/反思（可选）
        related: 关联条目/链接（可选）
        domain: 领域 (general/cad/pic/quant/code/sys, 默认 general)
        severity: 严重度 (critical/warning/info, 默认 info)

    Returns:
        JSON: {entry_number, path, status}
    """
    try:
        from scripts.wheels.baobi_recorder import record
        # 构造 argparse.Namespace 模拟 CLI 参数
        ns = argparse.Namespace(
            symptom=symptom,
            cause=cause,
            fix=fix,
            lesson=lesson,
            related=related,
            domain=domain,
            severity=severity,
            tags=None,
            n=5,
            command="record",
        )
        # record() 直接写文件并打印，需要捕获 stdout
        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            record(ns)
        output = f.getvalue().strip()
        return json.dumps({
            "status": "recorded",
            "symptom": symptom,
            "output": output,
        }, ensure_ascii=False)
    except ImportError as e:
        return json.dumps({"error": f"baobi_recorder 不可用: {e}"}, ensure_ascii=False)
    except SystemExit:
        return json.dumps({"error": "记录失败（参数不完整或文件不存在）"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 7: 进度记录
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-progress-record",
    description="记录双轨进度。写入knowledge/进度文件/ + "
    "assistant交付/📚 学习资料/学习进度/。支持轨道 ALL/A/B/C。"
    "结构化字段(lesson/highlight/difficulty/conclusion/decision/source)进YAML frontmatter, "
    "供auto_capture被动解析入库(数据飞轮)。",
)
def progress_record(
    output: str,
    title: str = "",
    completed: str = "",
    next_step: str = "",
    track: str = "ALL",
    extra: str = "",
    conclusion: str = "",
    decision: str = "",
    lesson: str = "",
    highlight: str = "",
    difficulty: str = "",
    source: str = "",
) -> str:
    """记录一条新进度。

    Args:
        output: 本阶段产出（必填）
        title: 双轨文件名内容简介(缺省从output提取前30字)
        completed: 完成的事项
        next_step: 下一步计划
        track: 轨道 (ALL/A/B/C, 默认 ALL)
        extra: 额外备注
        conclusion: 结论(本轮确立的知识性结论)
        decision: 决策(maintainer定调+理由, anchor=human_confirmed)
        lesson: 教训(踩坑+根因)
        highlight: 亮点(做得好的方法/巧解)
        difficulty: 克服的困难(硬闸验证过, anchor=hard_gate)
        source: 引用来源(论文/URL/文件路径, 逗号分隔)

    Returns:
        JSON: {status, paths}
    """
    # @fix 2026-08-21 甲方案(maintainer定): 补齐6个结构化字段 — 原包装只有5参数,
    # 与 progress_file_filer 当日 YAML frontmatter 改造脱节, 走MCP记录会丢数据飞轮字段。
    try:
        from scripts.wheels.progress_file_filer import record
        # @fix 2026-08-27 (dsh侧报障): 原Namespace缺 title 字段, _make_title 读 args.title 必崩
        # ('Namespace' object has no attribute 'title') — CC侧一直走CLI带--title从未暴露, dsh AI走MCP即触发
        ns = argparse.Namespace(
            command="record",
            output=output,
            title=title,
            completed=completed,
            next_step=next_step,
            track=track,
            date="",
            time="",
            extra=extra,
            conclusion=conclusion,
            decision=decision,
            lesson=lesson,
            highlight=highlight,
            difficulty=difficulty,
            source=source,
            dest="",
            typ="",
            dry_run=False,
        )
        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            record(ns)
        output_text = f.getvalue().strip()
        return json.dumps({
            "status": "recorded",
            "output": output_text,
        }, ensure_ascii=False)
    except ImportError as e:
        return json.dumps({"error": f"progress_file_filer 不可用: {e}"}, ensure_ascii=False)
    except SystemExit:
        return json.dumps({"error": "记录失败"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 7b: 认知系统迭代记录 (甲方案 2026-08-21 maintainer定:
# 对齐铁律"无MCP暴露的文字指令=废弃指令"; iteration_logger.py 同日新建)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-iteration-record",
    description="认知系统迭代记录(大修/小修自动判断, ≥3核心组件=大修)。"
    "YAML frontmatter + INDEX.md 自动更新。写入 knowledge/05_CLS认知系统架构/认知系统迭代/。",
)
def iteration_record(
    task: str,
    components: str = "",
    output: str = "",
    conclusion: str = "",
    decision: str = "",
    lesson: str = "",
    highlight: str = "",
    difficulty: str = "",
    source: str = "",
    next_step: str = "",
    status: str = "完成",
    iter_type: str = "auto",
) -> str:
    """记录一次认知系统迭代(大修/小修)。

    Args:
        task: 任务名称(必填)
        components: 涉及组件(逗号分隔)
        output: 产出物
        conclusion: 结论(本轮确立的知识性结论)
        decision: 决策(maintainer定调+理由, anchor=human_confirmed)
        lesson: 教训(踩坑+根因)
        highlight: 亮点(做得好的方法/巧解)
        difficulty: 克服的困难(硬闸验证过, anchor=hard_gate)
        source: 引用来源(文件路径/文献, 逗号分隔)
        next_step: 下一步
        status: 状态(完成/进行中/阻塞)
        iter_type: 类型(auto=自动判断/大修/小修)
    """
    try:
        from scripts.wheels.iteration_logger import record as _record
        ns = argparse.Namespace(
            command="record",
            task=task,
            output=output,
            conclusion=conclusion,
            decision=decision,
            lesson=lesson,
            highlight=highlight,
            difficulty=difficulty,
            source=source,
            next_step=next_step,
            components=components,
            status=status if status in ("完成", "进行中", "阻塞") else "完成",
            type=iter_type if iter_type in ("大修", "小修", "auto") else "auto",
            major=None,  # None=auto判断; True=强制大修; False=强制小修 (与main()逻辑一致)
            severity="info",
        )
        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            _record(ns)
        return json.dumps({
            "status": "recorded",
            "output": f.getvalue().strip(),
        }, ensure_ascii=False)
    except ImportError as e:
        return json.dumps({"error": f"iteration_logger 不可用: {e}"}, ensure_ascii=False)
    except SystemExit:
        return json.dumps({"error": "记录失败"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 8: 失败归档
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-failure-record",
    description="记录失败经验到 failure_learner。"
    "适用于工具崩溃、脚本报错、发现 Bug 等场景。自动归档到 data/failures/。",
)
def failure_record(
    reason: str,
    cause: str = "",
    lesson: str = "",
    task_type: str = "code",
    tags: str = "",
) -> str:
    """记录一条失败经验。

    Args:
        reason: 什么失败了（一句话，必填）
        cause: 根本原因
        lesson: 下次怎么做
        task_type: 任务类型 (code/cad/pic/quant/sys/other, 默认 code)
        tags: 逗号分隔的标签列表

    Returns:
        JSON: {status, entry_id}
    """
    try:
        from scripts.wheels.failure_learner import record_failure
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        record_failure(
            reason=reason,
            cause=cause,
            lesson=lesson,
            task_type=task_type,
            auto=True,
            tags=tag_list,
        )
        return json.dumps({
            "status": "recorded",
            "reason": reason,
        }, ensure_ascii=False)
    except ImportError as e:
        return json.dumps({"error": f"failure_learner 不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 9: 交付状态检查
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-delivery-status",
    description="检查当前交付状态——哪些文件已产出但未归档到交付目录。"
    "帮助跟踪交付完整性。",
)
def delivery_status() -> str:
    """检查交付状态。

    Returns:
        JSON: {status, task, n_total, n_unarchived, unarchived_files}
    """
    try:
        from scripts.wheels.delivery_check import check_status
        result = check_status()
        return json.dumps(result, ensure_ascii=False, default=str)
    except ImportError as e:
        return json.dumps({"error": f"delivery_check 不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 10: 设计哲学记录
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-philosophy-record",
    description="记录一条设计哲学原则/讨论/洞察。"
    "写入 knowledge/05_CLS认知系统架构/认知系统迭代/哲学讨论/。"
    "适合系统设计层面的原则性发现。",
)
def philosophy_record(
    title: str,
    insight: str,
    implications: str = "",
    source: str = "",
    related_files: str = "",
) -> str:
    """记录新设计哲学条目。

    Args:
        title: 哲学条目标题（如 "CLS本质：注意力锚点系统"）
        insight: 核心洞察——这条哲学说了什么
        implications: 设计/工程上的推论——这条哲学意味着什么
        source: 来源（如 iter-037 对话 / 大修日志路径）
        related_files: 关联文件路径（逗号分隔）

    Returns:
        JSON: {status, path}
    """
    try:
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        slug = title.replace(" ", "_").replace("：", "_").replace(":", "_")[:40]
        # 防路径穿越: 移除所有 / \ .. 字符
        slug = slug.replace("/", "").replace("\\", "").replace("..", "")
        filename = f"{today}_{slug}.md"

        base_dir = PROJECT_ROOT / "knowledge" / "05_CLS认知系统架构" / "认知系统迭代" / "哲学讨论"
        base_dir.mkdir(parents=True, exist_ok=True)
        filepath = (base_dir / filename).resolve()
        base_dir_resolved = base_dir.resolve()
        if not str(filepath).startswith(str(base_dir_resolved) + os.sep):
            return json.dumps({"error": "Invalid title: path traversal detected"}, ensure_ascii=False)

        lines = [
            f"# {title}\n",
            f"**日期**：{date.today().isoformat()}\n",
        ]
        if source:
            lines.append(f"**来源**：{source}\n")
        lines.append("")
        lines.append(f"## 核心洞察\n")
        lines.append(f"{insight}\n")
        if implications:
            lines.append(f"## 设计推论\n")
            lines.append(f"{implications}\n")
        if related_files:
            lines.append(f"## 关联\n")
            for f in related_files.split(","):
                lines.append(f"- {f.strip()}\n")

        filepath.write_text("".join(lines), encoding="utf-8")
        return json.dumps({
            "status": "recorded",
            "path": str(filepath.relative_to(PROJECT_ROOT)),
            "title": title,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 11: 知识捕获 (取代 /capture 手动流程)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-knowledge-capture",
    description="双轨归档新知识 — 通过 learn_capture 将洞察写入学"
    "习进度和交付目录，自动触发 RAG 索引更新。"
    "取代 '/capture' 手动流程，走质量闸门 + 冲突检测。",
)
def knowledge_capture(
    domain: str,
    insight: str,
    files: str = "[]",
    task_summary: str = "",
    mode: str = "flow",
) -> str:
    """捕获新知识到双轨系统（学习进度 + 交付目录），自动触发 RAG 索引。

    调用 learn_capture MCP 工具的底层实现。
    写入后自动触发 _auto_index_files.py → chunks.jsonl → vectors.npy → RAG。

    Args:
        domain: 知识域 (cad/pic/quant/knowledge/code/embodied/cc/general)
        insight: 核心学习内容 (1-3 句，描述你学到了什么)
        files: JSON 数组，产出文件路径列表，如 '["path/to/file.py", "path/to/doc.md"]'
        task_summary: 任务简述（可选）
        mode: 学习模式标记 (flow=流动学习, time=时间模式, manual=手动, 默认 flow)

    Returns:
        JSON: {ok, track1{status,path}, track2{status,path}, missing[], closed_loop_gap[]}
    """
    try:
        from mcp_servers.learn_mcp import learn_capture
        result = learn_capture(
            domain=domain,
            insight=insight,
            files=files,
            task_summary=task_summary,
            mode=mode,
        )
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    except ImportError as e:
        return json.dumps({"ok": False, "error": f"learn_mcp 不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 12: arXiv 论文搜索
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-paper-search-arxiv",
    description="搜索 arXiv 学术论文。支持多策略分层检索。"
    "返回论文标题、作者、摘要、链接。",
)
def paper_search_arxiv(
    query: str,
    max_results: int = 10,
) -> str:
    """在 arXiv 上搜索论文。

    Args:
        query: 搜索查询（如 "<DOMAIN device> wall erosion"）
        max_results: 最大返回数（默认 10，最大 50）

    Returns:
        JSON: [{title, authors, summary, link, published, ...}, ...]
    """
    try:
        from scripts.wheels.paper_collector import search_arxiv
        papers = search_arxiv(query, max_results=min(max_results, 50))
        results = []
        for p in papers:
            results.append({
                "title": p.title,
                "authors": p.authors,
                "summary": p.summary[:300],
                "link": p.link,
                "published": p.published,
            })
        return json.dumps(results, ensure_ascii=False)
    except ImportError as e:
        return json.dumps({"error": f"paper_collector 不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 13: Semantic Scholar 论文搜索
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-paper-search-s2",
    description="在 Semantic Scholar 上搜索学术论文。"
    "返回标题、作者、引用数、链接。arXiv 找不到时用此备用。",
)
def paper_search_s2(
    query: str,
    limit: int = 5,
) -> str:
    """在 Semantic Scholar 上搜索论文。

    Args:
        query: 搜索查询
        limit: 最大返回数（默认 5，最大 20）

    Returns:
        JSON: [{title, authors, citation_count, url, ...}, ...]
    """
    try:
        from scripts.wheels.paper_collector import search_semantic_scholar
        papers = search_semantic_scholar(query, limit=min(limit, 20))
        return json.dumps(papers, ensure_ascii=False)
    except ImportError as e:
        return json.dumps({"error": f"paper_collector 不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 13b: 一键论文搜索+下载（搜索arXiv → 下载PDF → 提取文本）
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-paper-search-download",
    description="🔴 文献调研首选。一键完成：搜索arXiv→下载PDF→提取全文文本→输出论文来源、下载路径、核心内容。"
    "查询<DOMAIN>/<介质>/<DOMAIN设备>论文时优先用此，避免只搜不下载(incident-log#61)。最多5篇。"
    "输出含：arxiv链接、PDF本地路径、全文txt路径、摘要、关键段落。",
)
def paper_search_download(
    query: str,
    max_papers: int = 3,
    out_dir: str = "",
) -> str:
    """搜索论文 → 下载PDF → 提取文本 → 输出结构化结果。"""
    try:
        from scripts.wheels.paper_collector import search_arxiv
        from scripts.wheels.literature_downloader import (
            download_arxiv_pdf, extract_key_paragraphs
        )
        ROOT = Path(__file__).resolve().parent.parent
        if not out_dir:
            out_dir = str(ROOT / "knowledge" / "文献")
        os.makedirs(out_dir, exist_ok=True)

        n = min(max_papers, 5)
        papers = search_arxiv(query, n)
        if not papers:
            return json.dumps({"status": "ok", "query": query, "total_found": 0, "downloaded": []}, ensure_ascii=False)

        downloaded = []
        for p in papers[:n]:
            aid = p.arxiv_id
            pdf_result = download_arxiv_pdf(aid, out_dir)
            ok = isinstance(pdf_result, dict) and pdf_result.get('ok')

            pdf_path = pdf_result.get('pdf_path', '') if isinstance(pdf_result, dict) else ''
            txt_path = pdf_result.get('txt_path', '') if isinstance(pdf_result, dict) else ''
            ref_path = pdf_result.get('ref_path', '') if isinstance(pdf_result, dict) else ''
            title = (pdf_result.get('title') if isinstance(pdf_result, dict) else None) or p.title
            pdf_saved = bool(pdf_path and os.path.exists(pdf_path))

            # 提取全文关键段落
            key_paragraphs = []
            full_text_preview = ""
            if txt_path and os.path.exists(txt_path):
                try:
                    with open(txt_path, encoding='utf-8', errors='replace') as fh:
                        full_text = fh.read()
                    full_text_preview = full_text[:3000]
                    key_paragraphs = extract_key_paragraphs(full_text, max_sections=8)
                except Exception:
                    pass

            downloaded.append({
                "title": title,
                "authors": pdf_result.get('authors', '') if isinstance(pdf_result, dict) else ', '.join(p.authors[:5]),
                "arxiv_id": aid,
                "arxiv_url": f"https://arxiv.org/abs/{aid}",
                "published": p.published,
                "abstract": p.summary[:500],
                "pdf_saved": pdf_saved,
                "pdf_path": pdf_path,
                "txt_path": txt_path,
                "ref_path": ref_path,
                "text_preview": full_text_preview[:2000],
                "key_paragraphs": key_paragraphs[:5],
            })

        return json.dumps({
            "status": "ok",
            "query": query,
            "total_found": len(papers),
            "downloaded": downloaded,
            "out_dir": out_dir,
            "usage": "PDF全文见 pdf_path，提取文本见 txt_path(全文) 和 text_preview(预览)。key_paragraphs 为按标题分段的论文核心段落。",
        }, ensure_ascii=False)

    except ImportError as e:
        return json.dumps({"status": "error", "error": f"依赖不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 14: 文献下载（论文PDF下载+文本提取）
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-paper-download",
    description="下载论文PDF到本地并提取文本。给定arXiv ID或URL，"
    "下载PDF原文+提取纯文本+生成结构化引用条目。"
    "输出到指定目录（默认 knowledge/文献/）。"
    "铁律：任何引用文献的分析任务，必须先调此工具下载原文到本地。",
)
def paper_download(
    arxiv_id: str = "",
    url: str = "",
    out_dir: str = "",
) -> str:
    """下载论文PDF并提取文本。

    Args:
        arxiv_id: arXiv ID，如 "2501.03867"
        url: 论文URL（非arXiv论文用此参数）
        out_dir: 输出目录（默认为项目根下 knowledge/文献/）

    Returns:
        JSON: {status, title, pdf_path, ref_path, text_path, abstract, error}
    """
    try:
        from scripts.wheels.literature_downloader import (
            download_arxiv_pdf, download_arxiv_abstract,
            build_arxiv_reference_entry, download_and_index
        )
        ROOT = Path(__file__).resolve().parent.parent
        if not out_dir:
            out_dir = str(ROOT / "knowledge" / "文献")
        os.makedirs(out_dir, exist_ok=True)

        if arxiv_id:
            # download_arxiv_abstract 返回 (title, authors, abstract, html_bytes)
            title, authors, abstract, _ = download_arxiv_abstract(arxiv_id)
            if not title:
                return json.dumps({"status": "error", "error": f"arXiv {arxiv_id} 未找到或无法访问"}, ensure_ascii=False)

            pdf_result = download_arxiv_pdf(arxiv_id, out_dir)
            pdf_saved = isinstance(pdf_result, dict) and pdf_result.get('ok')
            pdf_path = pdf_result.get('pdf_path', '') if isinstance(pdf_result, dict) else ''
            ref_path = pdf_result.get('ref_path', '') if isinstance(pdf_result, dict) else ''
            pdf_error = pdf_result.get('pdf_error', '') if isinstance(pdf_result, dict) else ''

            return json.dumps({
                "status": "ok",
                "source": f"arxiv:{arxiv_id}",
                "title": title,
                "authors": authors,
                "abstract": (abstract or "")[:500],
                "pdf_saved": pdf_saved,
                "pdf_path": pdf_path,
                "ref_path": ref_path,
                "pdf_error": pdf_error,
                "out_dir": out_dir,
            }, ensure_ascii=False)

        elif url:
            # URL 白名单校验 — 仅允许学术论文域名 (SSRF 防御)
            from urllib.parse import urlparse
            allowed = {'arxiv.org', 'doi.org', 'iopscience.iop.org', 'pubs.aip.org',
                       'link.aps.org', 'semanticscholar.org', 'api.semanticscholar.org',
                       'researchgate.net', 'nature.com', 'science.org', 'springer.com',
                       'ieeexplore.ieee.org', 'onlinelibrary.wiley.com', 'mdpi.com'}
            host = urlparse(url).hostname or ''
            if not any(host == d or host.endswith('.' + d) for d in allowed):
                return json.dumps({
                    "status": "error",
                    "error": f"域名 {host} 不在学术文献白名单中。允许: {sorted(allowed)}"
                }, ensure_ascii=False)
            result = download_and_index(url, out_dir)
            return json.dumps({
                "status": "ok",
                "source": url,
                "out_dir": out_dir,
                "files": [str(p) for p in result] if isinstance(result, list) else str(result),
            }, ensure_ascii=False)

        else:
            return json.dumps({"status": "error", "error": "必须提供 arxiv_id 或 url"}, ensure_ascii=False)

    except ImportError as e:
        return json.dumps({"status": "error", "error": f"literature_downloader 不可用: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 15: 系统健康检查
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-system-health",
    description="系统健康快照——检查各守护进程/API/模型/缓存的可用性。"
    "汇总 compact_health_board + traffic_watch 的数据。",
)
def system_health() -> str:
    """获取系统健康状态。

    Returns:
        JSON: {status, components, ...}
    """
    results = {"timestamp": time.time(), "checks": {}}

    # traffic_watch
    try:
        from scripts.wheels.traffic_watch import check_all
        tw = check_all()
        results["checks"]["traffic_watch"] = {
            k: v for k, v in tw.items() if isinstance(v, (str, int, float, bool, list, dict))
        } if isinstance(tw, dict) else {"status": str(tw)[:200]}
    except Exception as e:
        results["checks"]["traffic_watch"] = {"error": str(e)[:100]}

    return json.dumps(results, ensure_ascii=False, default=str)


# ═══════════════════════════════════════════════════════
# 工具 15: 系统激活 (self_activate)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-activate",
    description="执行系统一键激活——熔断板确认 + 冷/暖启动判断 + "
    "认知上下文恢复 + compact flag 检查。用于会话启动或恢复。",
)
def system_activate() -> str:
    """执行 self_activate 激活流程。

    Returns:
        JSON: {status, activation_state, context_restored}
    """
    try:
        import subprocess
        # 环境变量白名单: 只传必要系统变量, 滤掉 PYTHON/LD/DYLD 等注入风险
        _SAFE_ENV_PREFIXES = {"PATH", "HOME", "USER", "LANG", "LC_", "TZ", "SYSTEMROOT", "COMSPEC", "PATHEXT"}
        _BLOCKED_PREFIXES = {"PYTHON", "LD_", "DYLD_", "NODE_", "BASH_", "PERL5", "RBENV"}
        safe_env = {
            k: v for k, v in os.environ.items()
            if any(k.startswith(p) for p in _SAFE_ENV_PREFIXES)
            and not any(k.startswith(b) for b in _BLOCKED_PREFIXES)
        }
        safe_env["PYTHONIOENCODING"] = "utf-8"
        # C盘守卫环境变量（继承自 self_activate 设计）
        safe_env["HF_HOME"] = "E:\\cls_cache\\huggingface"
        safe_env["TRANSFORMERS_CACHE"] = "E:\\cls_cache\\huggingface"
        safe_env["TORCH_HOME"] = "E:\\cls_cache\\torch"
        safe_env["CLS_MODELS"] = "E:\\cls_models"

        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "self_activate.py")],
            capture_output=True, timeout=60, env=safe_env,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")[:6000]
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        # 读人格上下文（L0 核心身份 — persona_loader 已写入）
        persona_ctx = ""
        pi = PROJECT_ROOT / "state" / "persona_injection.md"
        if pi.exists():
            persona_ctx = pi.read_text("utf-8", errors="replace")[:5000]
        return json.dumps({
            "status": "ok" if result.returncode == 0 else "error",
            "returncode": result.returncode,
            "output": stdout,
            "stderr": stderr if stderr else "",
            "persona_context": persona_ctx,
        }, ensure_ascii=False)
    except subprocess.TimeoutExpired:
        return json.dumps({"status": "error", "error": "超时 (60s)"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 16: 图像分析 (决策表 #3 — 替代 image_processor CLI)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-image-processor",
    description="本地 GPU 图像处理四合一：OCR文字识别 + 目标检测(YOLO) + 图像描述 + 图文匹配(CLIP)。"
    "全部本地 GPU 推理，零远端 API。ocr/detect 轻量快速，caption/match 需要加载大模型。",
)
def image_processor_tool(action: str, image_path: str, query: str = "") -> str:
    """本地 GPU 图像处理。全部本地推理，零远端 API.

    Args:
        action: ocr(文字识别) | ocr-smart(端到端文档解析) | detect(目标检测) | caption(图像描述) | match(图文匹配)
        image_path: 图像文件路径（绝对路径或项目相对路径）
        query: match 模式时的文本查询

    Returns: JSON {status, action, result, elapsed_s}
    """
    try:
        from scripts.wheels.image_processor import (
            cmd_ocr, cmd_ocr_smart, cmd_detect, cmd_caption, cmd_match
        )
        t0 = time.time()
        p = Path(image_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        img_str = str(p)

        if action == "ocr":
            r = cmd_ocr(img_str)
        elif action == "ocr-smart":
            r = cmd_ocr_smart(img_str)
        elif action == "detect":
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cmd_detect(img_str)
            r = buf.getvalue()
        elif action == "caption":
            r = cmd_caption(img_str)
        elif action == "match":
            r = cmd_match(img_str, query or None)
        else:
            return json.dumps({"status": "error", "error": f"未知 action: {action}"})

        return json.dumps({"status": "ok", "action": action, "result": str(r)[:2000], "elapsed_s": round(time.time() - t0, 2)})
    except Exception as e:
        return json.dumps({"status": "error", "action": action, "error": str(e)})


# ═══════════════════════════════════════════════════════
# 工具 17: 安全熔断板 (决策表 #10 — 替代 qwen_gate / fuse_board CLI)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-fuse-board",
    description="安全熔断板：操作前查询熔断器、拉闸记录、查看状态。"
    "覆盖 WRITE_PROTECT / RECURSION_LIMIT / TOKEN_BUDGET / PARALLEL_LIMIT / CHECKPOINT / PROXY_PURITY 六种熔断器。",
)
def fuse_board_tool(action: str, fuse_type: str = "", reason: str = "", context: str = "") -> str:
    """安全熔断板.

    Args:
        action: check(查询) | trip(拉闸) | status(状态) | reset(重置)
        fuse_type: 熔断器类型（check/trip/reset 时需要）
        reason: trip 原因
        context: JSON 上下文字符串（如 {"path": "file.py"}）

    Returns: JSON
    """
    try:
        from scripts.fuse_board import fuse_board
        ctx = json.loads(context) if context else {}
        if action == "check":
            return json.dumps({"status": "ok", "allowed": fuse_board.check(fuse_type, ctx)})
        elif action == "trip":
            return json.dumps({"status": "ok", "trip": fuse_board.trip(fuse_type, reason, ctx)})
        elif action == "status":
            return json.dumps({"status": "ok", "fuses": fuse_board.status()})
        elif action == "reset":
            return json.dumps({"status": "ok", "reset": fuse_board.reset(fuse_type or None)})
        return json.dumps({"status": "error", "error": f"未知 action: {action}"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


# ═══════════════════════════════════════════════════════
# 工具 18: 双AI闸门 (决策表 #10 — qwen_gate MCP 包装)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-qwen-gate",
    description="双AI闸门 — 三段式独立验证 (CAD设计/知识声明/数值计算)。"
    "与被动熔断 fuse_board 不同，此为主动调用 Qwen (或 Anthropic 后备) 做第三方验证。"
    "支持 status(查看状态) / verify-cad(CAD设计) / verify-knowledge(知识) / verify-numerical(数值) / gate-if-needed(条件触发)",
)
def qwen_gate_tool(
    action: str,
    design_name: str = "",
    description: str = "",
    params: str = "",
    patterns: str = "",
    claim: str = "",
    problem: str = "",
    solution: str = "",
    source: str = "",
    content: str = "",
    context_tokens: int = 0,
    target_kb: bool = False,
) -> str:
    """双AI闸门。

    Args:
        action: status(闸门统计) | verify-cad(CAD设计) | verify-knowledge(知识声明) |
                verify-numerical(数值计算) | gate-if-needed(条件触发数值验算)
        design_name: verify-cad 时，设计名称
        description: verify-cad 时，设计描述
        params: verify-cad 时，设计参数字典 JSON
        patterns: verify-cad 时，CAD模式列表 JSON
        claim: verify-knowledge 时，知识陈述
        problem: verify-knowledge 时，问题描述
        solution: verify-knowledge 时，解决方法
        source: verify-knowledge 时，知识来源
        content: verify-numerical 时，含数值的文本内容
        context_tokens: verify-numerical 时，当前上下文 token 数（用于条件触发判断）
        target_kb: gate-if-needed 时，是否写入knowledge

    Returns: JSON
    """
    try:
        from scripts.wheels.qwen_gate import (
            verify_cad_design, verify_knowledge, verify_numerical,
            gate_numerical_if_needed, status as gate_status,
        )
        import json as _json

        if action == "status":
            r = gate_status()
            return _json.dumps({"status": "ok", "action": action, "result": r}, ensure_ascii=False)

        elif action == "verify-cad":
            params_dict = _json.loads(params) if params else {}
            patterns_list = _json.loads(patterns) if patterns else []
            r = verify_cad_design(design_name, description, params_dict, patterns_list)
            return _json.dumps({"status": "ok", "action": action, "result": r}, ensure_ascii=False)

        elif action == "verify-knowledge":
            r = verify_knowledge(claim, problem, solution, source)
            return _json.dumps({"status": "ok", "action": action, "result": r}, ensure_ascii=False)

        elif action == "verify-numerical":
            r = verify_numerical(content, context_tokens=context_tokens or 75000,
                                target_kb=target_kb, design_name="")
            return _json.dumps({"status": "ok", "action": action, "result": r}, ensure_ascii=False)

        elif action == "gate-if-needed":
            r = gate_numerical_if_needed(content, context_tokens=context_tokens or 75000,
                                        target_kb=target_kb)
            return _json.dumps({"status": "ok", "action": action, "result": r}, ensure_ascii=False)

        else:
            return _json.dumps({"status": "error", "action": action, "error": f"未知 action: {action}"}, ensure_ascii=False)

    except Exception as e:
        return _json.dumps({"status": "error", "action": action, "error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 19: 文字处理 (决策表 #12 — 替代 text_processor CLI)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-text-processor",
    description="文字处理三合一：文件搬运(absorb/recall/flush)、语义检索(search/index)、GPU监控(status/info/watch)。纯本地运行。",
)
def text_processor_tool(action: str, text: str = "", interval: int = 2) -> str:
    """文字处理.

    Args:
        action: absorb(搬运文件) | recall(召回) | flush(清空) | search(语义检索) | gpu-status | gpu-info | gpu-watch
        text: absorb 时为逗号分隔路径列表，recall/search 时为关键词
        interval: gpu-watch 时的监控间隔秒数

    Returns: JSON
    """
    try:
        from scripts.wheels.text_processor import (
            cmd_absorb, cmd_recall, cmd_flush, cmd_search,
            cmd_gpu_status, cmd_gpu_info, cmd_gpu_watch,
        )
        import argparse
        ns = argparse.Namespace()
        if action == "absorb":
            ns.paths = text.split(",") if text else []
            r = cmd_absorb(ns)
        elif action == "recall":
            ns.pattern = text
            r = cmd_recall(ns)
        elif action == "flush":
            ns.all, ns.days = True, 30
            r = cmd_flush(ns)
        elif action == "search":
            ns.query, ns.top_n = text, 5
            r = cmd_search(ns)
        elif action == "gpu-status":
            r = cmd_gpu_status(ns)
        elif action == "gpu-info":
            r = cmd_gpu_info(ns)
        elif action == "gpu-watch":
            ns.interval = interval
            r = cmd_gpu_watch(ns)
        else:
            return json.dumps({"status": "error", "error": f"未知 action: {action}"})
        return json.dumps({"status": "ok", "action": action, "result": str(r)[:3000]}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "action": action, "error": str(e)})


# ═══════════════════════════════════════════════════════
# 工具 18b: port-mate 装配解析 (AssemCAD 调研落地 — CAD 特征级连接)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-port-mate",
    description="port-mate 装配解析 (CAD 特征级连接, 约束图协议升级): "
    "validate(校验ports/mates一致性) | solve(装配一致性求解, 输出placements) | "
    "script(生成完整build123d装配脚本) | gen(生成joint代码) | table(推导表) | demo(演示图)。"
    "来自 AssemCAD(arXiv 2607.05123) 调研, build123d 0.10 实测锚定。",
)
def port_mate_tool(action: str, graph_json: str = "", var_map_json: str = "") -> str:
    """port-mate 装配解析.

    Args:
        action: validate(校验) | solve(求解placements) | script(生成装配脚本) | gen(生成代码) | table(推导表) | demo(演示图)
        graph_json: validate/solve/script/gen 时的约束图 JSON 字符串 (含 parts/ports/mates)
        var_map_json: gen 时零件名→build123d变量名映射 JSON, 可选 {"housing":"housing"}

    Returns: JSON
    """
    try:
        import json as _json
        from scripts.wheels.port_mate_resolver import (
            validate_port_mates, build_joint_code, demo_graph, infer_joint_class,
            solve_assembly, assembly_script,
        )
        if action == "table":
            from scripts.wheels.port_mate_resolver import _JOINT_INFERENCE
            tbl = {f"{a}+{b}+{m}": j for (a, b, m), j in sorted(_JOINT_INFERENCE.items())}
            return _json.dumps({"status": "ok", "table": tbl}, ensure_ascii=False, indent=2)
        if action == "demo":
            return _json.dumps({"status": "ok", "demo": demo_graph()}, ensure_ascii=False, indent=2)
        if action == "solve":
            graph = _json.loads(graph_json) if graph_json else {}
            return _json.dumps(solve_assembly(graph), ensure_ascii=False, indent=2)
        if action == "script":
            graph = _json.loads(graph_json) if graph_json else {}
            try:
                return _json.dumps({"status": "ok",
                                    "code": assembly_script(graph, "assembly.stp")},
                                   ensure_ascii=False, indent=2)
            except Exception as e:
                return _json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)
        if action in ("validate", "gen"):
            graph = _json.loads(graph_json) if graph_json else {}
            if action == "validate":
                return _json.dumps(validate_port_mates(graph), ensure_ascii=False, indent=2)
            var_map = _json.loads(var_map_json) if var_map_json else {}
            return _json.dumps({"status": "ok", "code": build_joint_code(graph, var_map)},
                               ensure_ascii=False, indent=2)
        return _json.dumps({"status": "error", "error": f"未知 action: {action}"})
    except Exception as e:
        return _json.dumps({"status": "error", "action": action, "error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 19: 自主目标循环 (决策表 #19 — 替代 goal_protocol.py CLI)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-goal-protocol",
    description="/goal 自主目标循环协议：启动(start) → 每步记录(step) → 完成(finish) → 状态查询(status/context/completed)。"
    "用于长期自主任务的上下文恢复和轨迹追踪。",
)
def goal_protocol_tool(
    action: str, condition: str = "", plan: str = "", goal_id: str = "", goal_dir: str = "",
    tool_name: str = "", tool_input: str = "", tool_output: str = "",
    summary: str = "", files_written: str = "", exit_reason: str = "",
) -> str:
    """自主目标循环协议.

    Args:
        action: start | step | finish | status | context | completed
        condition: start 时的目标描述
        plan: start 时的逗号分隔步骤计划
        goal_id: 自定义 goal_id
        goal_dir: 产出目录
        tool_name: step 时的工具名
        tool_input/ tool_output: step 时的输入/输出摘要
        summary: finish 时的完成摘要
        files_written: finish 时的逗号分隔产出文件
        exit_reason: condition_met / manual / timeout

    Returns: JSON
    """
    try:
        from scripts.wheels.goal_protocol import (
            cmd_start, cmd_step, cmd_finish, cmd_status, cmd_context, cmd_completed,
        )
        import argparse
        ns = argparse.Namespace()
        if action == "start":
            ns.condition, ns.plan = condition, plan
            ns.goal_id, ns.goal_dir = goal_id or "", goal_dir or ""
            r = cmd_start(ns)
        elif action == "step":
            ns.tool, ns.input, ns.output = tool_name, tool_input, tool_output
            r = cmd_step(ns)
        elif action == "finish":
            ns.ok, ns.summary = True, summary
            ns.files_written, ns.exit_reason = files_written, exit_reason or "condition_met"
            r = cmd_finish(ns)
        elif action == "status":
            r = cmd_status(None)
        elif action == "context":
            r = cmd_context(None)
        elif action == "completed":
            r = cmd_completed(None)
        else:
            return json.dumps({"status": "error", "error": f"未知 action: {action}"})
        return json.dumps({"status": "ok", "action": action, "result": str(r)[:3000]}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "action": action, "error": str(e)})


# ═══════════════════════════════════════════════════════
# 工具 20-23: V1style PPT 管线 (ppt-mcp 包装)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="ppt-v1style-create",
    description="V1style PPT 创建 — 深色主题 + 封面，匹配 V1style 暗色标题条+白色背景风格。"
    "需先调用此工具创建演示文稿，再添加幻灯片，最后导出。",
)
def ppt_v1style_create(
    title: str = "",
    subtitle: str = "",
    author: str = "assistant",
) -> str:
    """新建 V1style 演示文稿。

    Args:
        title: 演示文稿标题（显示在封面）
        subtitle: 副标题/日期/场合
        author: 作者名

    Returns: JSON
    """
    try:
        from mcp_servers.ppt_mcp import ppt_create
        return ppt_create(title=title, subtitle=subtitle, author=author, theme="dark_professional")
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="ppt-v1style-slide",
    description="V1style 文本幻灯片 — 深色标题条 + 白色背景 + 全宽内容文本。"
    "支持 ## 子标题（蓝色加粗）和 --- 分隔线。需先调 ppt-v1style-create。",
)
def ppt_v1style_slide_tool(
    title: str,
    content: str = "",
    bullets: str = "[]",
    key_message: str = "",
    footer: str = "",
) -> str:
    """V1style 文本幻灯片。

    Args:
        title: 幻灯片标题（cyan 色，深色标题条中）
        content: 正文内容（多段落用空行分隔）
        bullets: JSON 数组要点列表 — '["## 子标题", "要点1", "---"]'
        key_message: 加粗关键信息框（显示在标题条下方）
        footer: 底部灰色说明文字

    Returns: JSON
    """
    try:
        from mcp_servers.ppt_mcp import ppt_add_v1style_slide
        return ppt_add_v1style_slide(title=title, content=content, bullets=bullets,
                                      key_message=key_message, footer=footer)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="ppt-v1style-image",
    description="V1style 图片幻灯片 — 深色标题条 + 白色背景 + 全宽图片（无文字分栏）。"
    "支持本地路径或 URL。需先调 ppt-v1style-create。",
)
def ppt_v1style_image_tool(
    title: str,
    image_path: str,
    caption: str = "",
    key_message: str = "",
    notes: str = "",
) -> str:
    """V1style 图片幻灯片。

    Args:
        title: 幻灯片标题（cyan 色，深色标题条中）
        image_path: 图片路径（支持本地路径或 URL）
        caption: 图片下方说明文字
        key_message: 加粗关键信息框（显示在标题条下方、图片上方）
        notes: 演讲者备注

    Returns: JSON
    """
    try:
        from mcp_servers.ppt_mcp import ppt_add_v1style_image
        return ppt_add_v1style_image(title=title, image_path=image_path, caption=caption,
                                      key_message=key_message, notes=notes)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="ppt-v1style-export",
    description="保存当前 V1style 演示文稿为 .pptx 文件。需先调 ppt-v1style-create 创建文稿。",
)
def ppt_v1style_export_tool(
    filename: str = "",
) -> str:
    """保存当前演示文稿。

    Args:
        filename: 文件名（可选，默认自动生成: 日期_标题.pptx）

    Returns: JSON
    """
    try:
        from mcp_servers.ppt_mcp import ppt_export
        return ppt_export(filename=filename)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 24: PIC V3 分析管线 (通用 — 替换 NEW_DESIGN_DIR 路径即可复用)
# ═══════════════════════════════════════════════════════
# 调用 pic_v3_analysis.py 的 V3 级全量分析：
#   1. FIELD_AVG + HISTORY_AVG 数据加载
#   2. 加速区(80-20%电势降) / 加热区(50% Te_max) / Dice 系数
#   3. 能量守恒自洽验证
#   4. 7张分析图 (Fig01-Fig07)
#   5. 完整分析报告 (markdown + JSON)
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-pic-analyze-v3",
    description="通用 PIC 仿真 V3 级全量分析管线。"
    "调用 pic_v3_analysis.py 进行数据加载、加速区/加热区/Dice 分析、"
    "能量守恒验证、7 张分析图生成、及完整分析报告输出。",
)
def pic_analyze_v3_tool(
    new_design_dir: str = "",
    full_run: bool = True,
) -> str:
    """运行 PIC V3 全量分析管线。

    Args:
        new_design_dir: 新设计 FIELD_AVG.DAT 所在目录路径. 空则使用默认路径.
        full_run: True=运行分析+生成图表+生成报告, False=仅运行分析(只出JSON)

    Returns: JSON
    """
    try:
        from scripts.wheels.pic_v3_analysis import (
            run_analysis, generate_figures, generate_report, OUTPUT_DIR,
        )

        results = run_analysis()
        if full_run:
            generate_figures(results)
            generate_report(results)

        import os
        out = str(OUTPUT_DIR)
        paths = {
            "json": out + "/field_analysis.json",
            "figures_dir": out + "/figures/",
            "report": out + "/full_analysis_report.md",
        }

        return json.dumps({
            "ok": True,
            "mode": "full" if full_run else "analysis_only",
            "output_dir": out,
            "designs_analyzed": list(results.get("designs", {}).keys()),
            "macro_count": len(results.get("comparison", {}).get("macro_table", [])),
            "paths": paths,
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 25: PIC 磁场拓扑分析 (Step 0 前置诊断)
# ═══════════════════════════════════════════════════════
# 调用 pic_magnetic_topology.py:
#   1. 磁场拓扑类型分类 (4类)
#   2. Bz 剖面 + 梯度
#   3. 磁场线曲率 (Faraji & Knoll 2023)
#   4. 磁镜比 2D (Keidar & Boyd)
#   5. Br/Bz + 磁透镜
#   6. 6张拓扑诊断图
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-pic-topology-analyze",
    description="PIC 仿真 2D 磁场拓扑全量分析。"
    "拓扑类型分类(常规/磁屏蔽/双峰/混合)、Bz剖面、"
    "场线曲率、磁镜比、Br/Bz、6 张诊断图。",
)
def pic_topology_analyze_tool(
    field_path: str = "",
    centerline_y_m: float = 0.027,
    exit_x_m: float = 0.021,
    generate_figs: bool = True,
) -> str:
    """运行 PIC 磁场拓扑全量分析。

    Args:
        field_path: FIELD_AVG.DAT 完整路径. 空则使用默认丁睿磁屏蔽新设计.
        centerline_y_m: 中轴线 y 位置 [m]
        exit_x_m: 出口 x 位置 [m]
        generate_figs: 是否生成拓扑诊断图

    Returns: JSON
    """
    try:
        from pathlib import Path
        import os, sys

        if not field_path:
            field_path = str(Path(
                "E:/<ORG_REDACTED>/PIC/丁睿pic/丁睿磁屏蔽新设计/danjicipingbiSPT_Kr_Te5/output/FIELD_AVG.DAT"
            ))

        from scripts.wheels.pic_io import load_field_avg_grid
        data, var_names, x, y = load_field_avg_grid(field_path)
        var_idx = {name: i for i, name in enumerate(var_names)}
        bz = data[:, :, var_idx["BZ"]]
        br = data[:, :, var_idx["BR"]]

        from scripts.wheels.pic_magnetic_topology import analyze_magnetic_topology
        out_dir = Path(field_path).parent / "topology_output"
        results = analyze_magnetic_topology(
            bz, br, x, y,
            centerline_y=centerline_y_m,
            exit_x=exit_x_m,
            output_dir=out_dir if generate_figs else None,
            generate_figs=generate_figs,
        )

        processed = json.loads(json.dumps(results, cls=_get_numpy_encoder()))
        return json.dumps({
            "ok": True,
            "topology_type": results["classification"]["topology_type"],
            "bz_peak_T": results["classification"]["bz_max_T"],
            "bz_peak_x_mm": results["classification"]["bz_max_x_mm"],
            "mirror_ratio": results["mirror_ratio"]["rm_centerline"],
            "br_bz_exit": results["classification"]["br_bz_ratio_exit"],
            "figures_dir": str(out_dir) if generate_figs else None,
            "details": processed,
        }, ensure_ascii=False)

    except Exception as e:
        import traceback
        return json.dumps({"ok": False, "error": str(e), "traceback": traceback.format_exc()}, ensure_ascii=False)


def _get_numpy_encoder():
    """处理 numpy 类型的 JSONEncoder 工厂"""
    import numpy as np
    class _NpEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                return float(obj)
            if isinstance(obj, (np.bool_,)):
                return bool(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return super().default(obj)
    return _NpEncoder


# ═══════════════════════════════════════════════════════
# 工具 26: PIC 工程分析 (效率分解 / 侵蚀代理 / 粒子平衡)
# ═══════════════════════════════════════════════════════
# 调用 pic_engineering_analysis.py 三个分析维度：
#   A. 效率 Factorization (Hofer & Goebel 2006)
#   B. 侵蚀代理指数 EPI
#   C. 粒子平衡 + 中性耗散 λ_iz
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-pic-engineering-analyze",
    description="PIC 仿真工程诊断三合一: "
    "效率分解(ηa=ηb×ηv×ηm×ηdiv×ηq) / "
    "壁面侵蚀代理指数 EPI / "
    "粒子平衡与中性耗散诊断。",
)
def pic_engineering_analyze_tool(
    field_path: str = "",
    partB_voltage: float = 300.0,
    mass_flow_kg_s: float = 1.34e-6,
) -> str:
    """运行 PIC 工程分析三合一。

    Args:
        field_path: FIELD_AVG.DAT 完整路径. 空则使用默认.
        partB_voltage: <部件B>电压 [V]
        mass_flow_kg_s: 质量流量 [kg/s]

    Returns: JSON
    """
    try:
        from pathlib import Path
        import sys, json, numpy as np

        if not field_path:
            field_path = str(Path(
                "E:/<ORG_REDACTED>/PIC/丁睿pic/丁睿磁屏蔽新设计/danjicipingbiSPT_Kr_Te5/output/FIELD_AVG.DAT"
            ))

        from scripts.wheels.pic_io import load_field_avg_grid
        data, var_names, x, y = load_field_avg_grid(field_path)
        var_idx = {name: i for i, name in enumerate(var_names)}

        # 构建 centerline dict
        cy_idx = len(y) // 2
        x_m = x
        cl = {
            "x_m": x_m,
            "ne": data[cy_idx, :, var_idx["NE"]],
            "ni": data[cy_idx, :, var_idx["NI"]],
            "na": data[cy_idx, :, var_idx["NA"]],
            "te": data[cy_idx, :, var_idx["TE_T"]],
            "phi": data[cy_idx, :, var_idx["PHI"]],
            "vi_x": data[cy_idx, :, var_idx["VI_X"]],
            "vi_y": data[cy_idx, :, var_idx["VI_Y"]],
        }

        bz_cl = data[cy_idx, :, var_idx["BZ"]]
        exit_idx = np.argmin(np.abs(x - 0.021))

        # 粗估 hist 数据
        from dataclasses import dataclass
        hist = {
            "i_dis_total": 1.031, "i_ion_total": 0.855,
            "thrust_mN": 14.82, "isp_s": 1163,
            "eff_partB": 0.273, "eff_utility": 0.570,
            "eff_current": 0.829,
        }

        from scripts.wheels.pic_engineering_analysis import (
            analyze_efficiency, analyze_erosion_proxy, analyze_particle_balance,
            ChannelConfig,
        )
        cfg = ChannelConfig(partB_voltage=partB_voltage, mass_flow=mass_flow_kg_s)

        eff = analyze_efficiency(cl, hist, {}, cfg)
        epi = analyze_erosion_proxy(data, var_idx, x, y, cfg)
        pb = analyze_particle_balance(cl, bz_cl, x, cfg)

        processed = json.loads(json.dumps({
            "efficiency": eff, "erosion": epi, "particle_balance": pb,
        }, cls=_get_numpy_encoder()))

        return json.dumps({
            "ok": True,
            "topology_type": "N/A (需要调用 cls-pic-topology-analyze)",
            "efficiency_summary": eff.get("description", ""),
            "erosion_summary": epi.get("explanation", ""),
            "particle_balance_summary": pb.get("explanation", ""),
            "details": processed,
        }, ensure_ascii=False)

    except Exception as e:
        import traceback
        return json.dumps({"ok": False, "error": str(e), "traceback": traceback.format_exc()}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 认知循环 Tools (步骤①~⑥)
# ═══════════════════════════════════════════════════════
# 多窗口竞争防护：
#   1. 原子写入 (tmp + os.replace) — 不会产生半写文件
#   2. 窗口ID嵌入 _meta.window_id — 追踪最后是谁写的
#   3. TTL锁文件 (data/state/locks/) — 防并发写冲突
#   4. JSONL 追加轨迹点 — 不重写整个文件
# ═══════════════════════════════════════════════════════

_COG_LOCK_DIR = PROJECT_ROOT / "data" / "state" / "locks"
_COG_LOCK_DIR.mkdir(parents=True, exist_ok=True)
_COG_WINDOW_ID = os.environ.get("CLAUDE_CODE_SESSION_ID", "cc_unknown")[:16]
_COG_MAX_LOCK_AGE = 600   # 僵尸锁清理阈值 (2x TTL)
_COG_LOCK_TTL = 300        # 默认锁 TTL (s)，比原 30s 增加 10x 适应长认知循环
_COG_TRAJ_MAX_LOG = 20     # 轨迹活跃条目上限（>此数自动摘要归档）
_COG_TELEMETRY_FILE = PROJECT_ROOT / "data" / "state" / "cog_telemetry.jsonl"

# ── data/state/ 弃用路径警告 ──
# 统一方向: state/ 为权威路径，data/state/ 重复文件已标记 .deprecated
# 若代码仍读写 data/state/ 下的弃用路径，打印一次警告
_COG_DEPRECATION_WARNED = set()
def _cog_warn_deprecated(path: Path) -> None:
    """若路径在 data/state/ 下且是弃用文件，打警告（每路径一次）。"""
    p_str = str(path)
    if p_str in _COG_DEPRECATION_WARNED:
        return
    if "data\\state\\" in p_str and ".deprecated" in p_str:
        _COG_DEPRECATION_WARNED.add(p_str)
        print(f"⚠️ [cog] 读写了弃用路径: {path.name} → 请迁移到 state/{path.stem.replace('.deprecated','')}.json")
    elif "data\\state\\" in p_str:
        # data/state/ 下的活跃文件（非弃用）也有风险——应考虑迁到 state/
        _COG_DEPRECATION_WARNED.add(p_str)
        print(f"⚠️ [cog] 读写 data/state/ 下的非权威路径: {path.name} → 考虑迁移到 state/")

# ── 启动时清理僵尸锁 ──
def _cog_cleanup_stale_locks():
    """启动时清理超时锁文件"""
    now = time.time()
    for lock_file in _COG_LOCK_DIR.glob("*.lock"):
        try:
            data = json.loads(lock_file.read_text("utf-8"))
            age = now - data.get("acquired_at", 0)
            if age > _COG_MAX_LOCK_AGE:
                lock_file.unlink(missing_ok=True)
        except Exception:
            lock_file.unlink(missing_ok=True)


_cog_cleanup_stale_locks()


def _cog_lock(name: str, ttl: int = None) -> bool:
    """尝试获取文件锁。返回是否成功。

    TTL 默认 300s（三方会谈共识：适应长认知循环）。
    同窗口重入自动续期（隐式心跳）。
    """
    if ttl is None:
        ttl = _COG_LOCK_TTL
    lp = _COG_LOCK_DIR / f"{name}.lock"
    try:
        if lp.exists():
            existing = json.loads(lp.read_text("utf-8"))
            age = time.time() - existing.get("acquired_at", 0)
            if age < ttl and existing.get("window_id") != _COG_WINDOW_ID:
                return False
        _cog_atomic_write(lp, {
            "window_id": _COG_WINDOW_ID,
            "acquired_at": time.time(),
            "ttl": ttl,
            "version": _cog_next_version(),
        })
        return True
    except Exception:
        return False


def _cog_unlock(name: str) -> None:
    lp = _COG_LOCK_DIR / f"{name}.lock"
    try:
        if lp.exists():
            lp.unlink()
    except Exception:
        pass


# ── Fencing Token（单调递增版本号，防 TTL 锁经典缺陷） ──
# 参考 Kleppmann 对分布式 TTL 锁的批评。
# 每次写入 lock + state 时递增，写入时校验版本号一致性。
# 若另一个窗口在锁过期后修改了文件，版本号会变，当前窗口检测到后拒绝写入。
_COG_VERSION_FILE = _COG_LOCK_DIR / "fencing_token.json"


def _cog_next_version() -> int:
    """获取下一个 fencing token 版本号（单调递增，文件级全局计数器）"""
    try:
        v = 0
        if _COG_VERSION_FILE.exists():
            raw = _COG_VERSION_FILE.read_text("utf-8")
            try:
                existing = json.loads(raw)
                v = existing.get("version", 0)
            except json.JSONDecodeError:
                # fencing_token.json 损坏 → 兜底
                pass
        v += 1
        tmp = _COG_VERSION_FILE.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps({"version": v, "window_id": _COG_WINDOW_ID, "updated_at": time.time()}),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(_COG_VERSION_FILE))
        return v
    except Exception:
        return int(time.time() * 1000)  # 兜底：用时间戳


def _cog_atomic_write(path: Path, data: dict) -> None:
    """原子写入 JSON: tmp → os.replace (防止半写)"""
    if isinstance(data, dict) and "_meta" in data:
        data["_meta"]["version"] = _cog_next_version()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _cog_cas_write(path: Path, data: dict, lock_name: str) -> bool:
    """CAS 原子写入 — 仅当目标版本与锁版本一致才写入。

    防 Kleppmann TTL 经典缺陷: 持锁卡顿>TTL 时，第二个窗口可能接管锁并写入。
    此函数在写入前比较目标文件的当前版本与锁版本:
      - 锁版本 = 获取锁时 fencing_token 的值
      - 文件版本 = 目标文件 _meta.version 的值
    若 文件版本 > 锁版本 → 另一窗口已写入 → 拒绝当前写入，返回 False。

    Args:
        path: 目标文件路径
        data: 待写入数据（含 _meta）
        lock_name: 锁名（用于读取锁文件中的期望版本号）

    Returns:
        bool: True=写入成功, False=版本冲突拒绝写入
    """
    # 1. 读取锁文件中的期望版本号
    lock_file = _COG_LOCK_DIR / f"{lock_name}.lock"
    expected_version = None
    try:
        lf_data = json.loads(lock_file.read_text("utf-8"))
        expected_version = lf_data.get("version")
    except Exception:
        pass

    # 2. 读取目标文件的当前版本
    current_version = None
    if path.exists():
        try:
            existing = json.loads(path.read_text("utf-8"))
            current_version = existing.get("_meta", {}).get("version")
        except Exception:
            pass

    # 3. CAS 校验
    if expected_version is not None and current_version is not None:
        if current_version > expected_version:
            print(
                f"🛑 CAS 版本冲突 (lock={lock_name}): "
                f"锁版本={expected_version}, 文件版本={current_version}. "
                f"另一窗口已修改文件，写入被拒绝。"
            )
            _cog_telemetry("cas_write", "version_conflict",
                           trigger="auto", error=f"{lock_name}: v{expected_version}→v{current_version}")
            return False

    # 4. 原子写入
    new_version = _cog_next_version()
    if isinstance(data, dict) and "_meta" in data:
        data["_meta"]["version"] = new_version
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))

    _cog_telemetry("cas_write", "success",
                   trigger="auto", lock_status="acquired")
    return True


def _cog_read(path: Path) -> tuple[dict | list, str | None]:
    """读 JSON，返回 (data, last_window_id)"""
    if not path.exists():
        return {}, None
    try:
        raw = json.loads(path.read_text("utf-8"))
        wid = raw.get("_meta", {}).get("window_id") if isinstance(raw, dict) else None
        return raw, wid
    except Exception:
        return {}, None


def _cog_make_meta() -> dict:
    return {
        "window_id": _COG_WINDOW_ID,
        "written_at": time.time(),
        "schema": "v2",
        "version": _cog_next_version(),
    }


# ── Telemetry 辅助 ─────────────────────────────────────
# 两路 JSONL 遥测:
#   source="cog-tools"    — cog-tools 内嵌调用（每步认知循环）
#   source="post-tool-use" — PostToolUse.ps1 Hook 记录（全工具调用）
# 下游聚合按 phase+event_id 去重（不靠 timestamp 精准，靠 event_id 匹配）
import uuid as _uuid

def _cog_telemetry(phase: str, event: str, duration_ms: int = 0,
                    trigger: str = "manual", lock_status: str = "",
                    error: str = "", source: str = "cog-tools") -> None:
    """追加认知循环遥测记录。纯追加，不阻塞。

    source 字段区分记录来源: cog-tools(内嵌) / post-tool-use(Hook)，
    下游聚合时通过 event_id + phase 去重。
    """
    try:
        entry = {
            "_schema": "v1",
            "ts": time.time(),
            "event_id": str(_uuid.uuid4())[:12],  # 用于下游去重
            "source": source,
            "window_id": _COG_WINDOW_ID,
            "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown")[:16],
            "phase": phase,
            "event": event,
            "duration_ms": duration_ms,
            "trigger": trigger,
            "lock_status": lock_status,
            "error": error[:200] if error else "",
        }
        _COG_TELEMETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(str(_COG_TELEMETRY_FILE), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── stance 档位 (2026-08-22 改动a·maintainer批准三改动框架) ─────────────
# 四档: farming常规(默认) / skirmish修复保护 / teamfight交付核心 / retreat暴毙诊断保护
# 写入唯一路径 = 本文件 cog-context(set-stance) → _cog_lock + _cog_cas_write(fencing CAS)。
# 仓库铁律: 禁止 hook/脚本直写 active_context.json 的 stance 字段; 读取走 wheels/stance_read.py。
from stance_read import read_stance as _stance_read_impl  # wheels 已在 sys.path (line 33)


def set_stance(mode: str, ttl_seconds: int = 0, set_by: str = "", reason: str = "") -> dict:
    """写入 stance 档位到 state/active_context.json (唯一写入口)。

    走 cog-context 既有防护: _cog_lock(TTL文件锁) + _cog_cas_write(fencing版本CAS) + 原子替换。
    mode 不在四档内 → 拒绝(返回 error)。ttl_seconds>0 → 写 expires_at(epoch秒),
    读取方惰性过期回 farming(TTL到期待读时判定, 无需后台任务)。

    Returns:
        {"status": "ok"|"cas_conflict"|"error", "stance": {...}, "lock_acquired": bool}
    """
    if mode not in ("farming", "skirmish", "teamfight", "retreat"):
        return {"status": "error",
                "error": f"未知档位: {mode!r} (合法: farming/skirmish/teamfight/retreat)"}
    ctx_path = PROJECT_ROOT / "state" / "active_context.json"
    started = time.time()
    stance_obj = {
        "mode": mode,
        "set_by": set_by or "manual",
        "reason": reason or "",
        "set_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if ttl_seconds and int(ttl_seconds) > 0:
        stance_obj["expires_at"] = time.time() + int(ttl_seconds)
    locked = _cog_lock("active_context")
    try:
        existing, last_window = _cog_read(ctx_path)
        if not isinstance(existing, dict):
            existing = {}
        existing["stance"] = stance_obj
        existing["_meta"] = _cog_make_meta()
        written = _cog_cas_write(ctx_path, existing, "active_context")
        _cog_telemetry("cog_context", "set_stance",
                       duration_ms=int((time.time() - started) * 1000),
                       lock_status="acquired" if locked else "wait",
                       trigger=set_by or "manual",
                       error="" if written else "cas_conflict")
        return {"status": "ok" if written else "cas_conflict",
                "stance": stance_obj, "lock_acquired": locked}
    finally:
        _cog_unlock("active_context")


# ── Tool: 态势感知 ─────────────────────────────────────

@mcp.tool(
    name="cog-context",
    description="认知循环步骤①: 态势感知 — R/W active_context.json。"
    "支持 read / update / read-with-heartbeat / set-stance 四种模式。"
    "read-with-heartbeat 包含跨窗口竞争检测：如果最后写入者不是当前窗口且写入 <5min 前 → 告警。"
    "set-stance 是 stance 四档(farming/skirmish/teamfight/retreat)的唯一写入口(锁+CAS+原子写)",
)
def cog_context(action: str = "read", data: str = "", stance: str = "",
                ttl_seconds: int = 0, reason: str = "") -> str:
    """读写态势感知上下文。

    Args:
        action: "read" 只读 | "read-with-heartbeat" 读+竞争检测 | "update" 写入 | "set-stance" 写档位
        data: 仅 update 时必填, JSON 字符串, 合并到 active_context
        stance: 仅 set-stance 时必填, 四档之一 (farming/skirmish/teamfight/retreat)
        ttl_seconds: 仅 set-stance, >0 时写 TTL(expires_at), 到期读取方自动当 farming
        reason: 仅 set-stance, 档位切换原因 (审计用)

    Returns:
        JSON: {status, context: dict | None, conflict_warning: str | None, stance: dict | None}
    """
    ctx_path = PROJECT_ROOT / "state" / "active_context.json"
    result = {"status": "ok", "action": action}
    started = time.time()

    if action == "set-stance":
        r = set_stance(stance, ttl_seconds=ttl_seconds,
                       set_by=f"cog-context:{_COG_WINDOW_ID}", reason=reason)
        r["action"] = "set-stance"
        return json.dumps(r, ensure_ascii=False)

    if action == "update":
        if not data:
            return json.dumps({"status": "error", "error": "update 模式需要 data 参数"}, ensure_ascii=False)
        try:
            new_data = json.loads(data) if isinstance(data, str) else data
        except json.JSONDecodeError:
            return json.dumps({"status": "error", "error": "data 不是合法 JSON"}, ensure_ascii=False)

        locked = _cog_lock("active_context")
        try:
            existing, last_window = _cog_read(ctx_path)
            if isinstance(existing, dict):
                existing.update(new_data)
            else:
                existing = new_data
            existing["_meta"] = _cog_make_meta()
            _cog_atomic_write(ctx_path, existing)
            result["context"] = existing
            result["last_writer"] = last_window
            result["lock_acquired"] = locked
            _cog_telemetry("cog_context", "update", duration_ms=int((time.time()-started)*1000),
                           lock_status="acquired" if locked else "wait", trigger="manual")
        finally:
            _cog_unlock("active_context")

    else:  # read or read-with-heartbeat
        data, last_window = _cog_read(ctx_path)
        result["context"] = data

        if action == "read-with-heartbeat" and last_window and last_window != _COG_WINDOW_ID:
            meta = data.get("_meta", {}) if isinstance(data, dict) else {}
            written_age = time.time() - meta.get("written_at", 0) if meta.get("written_at") else float("inf")
            if written_age < 300:
                result["conflict_warning"] = (
                    f"⚠️ 最后写入窗口 {last_window} ({written_age:.0f}s前)，"
                    f"非当前窗口 {_COG_WINDOW_ID}。状态可能非此窗口的认知。"
                )
                _cog_telemetry("cog_context", "conflict_detected", duration_ms=int((time.time()-started)*1000),
                               lock_status="none", trigger="manual",
                               error=f"window_conflict: {last_window} vs {_COG_WINDOW_ID}")
        _cog_telemetry("cog_context", action, duration_ms=int((time.time()-started)*1000))

    return json.dumps(result, ensure_ascii=False)


# ── Tool: 轨迹 ─────────────────────────────────────────

@mcp.tool(
    name="cog-trajectory",
    description="认知循环步骤⑥: 轨迹 — 读写 trajectory.json。"
    "支持 read / append / update-meta / summary 四种模式。"
    "append 追加轨迹点（JSONL风格追加到数组）；"
    "update-meta 更新 position/mass/momentum 三个状态字段。"
    "原子写入 + _meta 窗口追踪。",
)
def cog_trajectory(
    action: str = "read",
    point: str = "",
    position: str = "",
    mass: str = "",
    momentum: str = "",
) -> str:
    """读写认知轨迹。

    Args:
        action: "read" 只读 | "append" 追加一个轨迹点 | "update-meta" 更新状态字段
               | "summary" 返回轨迹压缩摘要
        point: 仅 append 模式, 轨迹点 JSON 字符串 {time, event, ...}
        position: 仅 update-meta 模式, 更新当前焦点描述
        mass: 仅 update-meta 模式, 更新积累质量描述
        momentum: 仅 update-meta 模式, 更新下一步方向描述

    Returns:
        JSON: {status, trajectory: list | None}
    """
    traj_path = PROJECT_ROOT / "state" / "trajectory.json"
    archive_path = PROJECT_ROOT / "data" / "state" / "trajectory_archive.jsonl"
    result = {"status": "ok", "action": action}
    started = time.time()

    if action == "append":
        if not point:
            return json.dumps({"status": "error", "error": "append 需要 point 参数"}, ensure_ascii=False)
        try:
            point_data = json.loads(point) if isinstance(point, str) else point
        except json.JSONDecodeError:
            return json.dumps({"status": "error", "error": "point 不是合法 JSON"}, ensure_ascii=False)

        locked = _cog_lock("trajectory")
        try:
            data, last_window = _cog_read(traj_path)
            if not isinstance(data, dict):
                data = {"_schema": "v2", "trajectory_log": []}
            if "trajectory_log" not in data:
                data["trajectory_log"] = []
            data["trajectory_log"].append(point_data)
            data["_meta"] = _cog_make_meta()
            data["_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            _cog_cas_write(traj_path, data, "trajectory")
            result["points"] = len(data["trajectory_log"])
        finally:
            if locked:
                _cog_unlock("trajectory")

        # ── Phase 5: 轨迹压缩 — 保留最近 N 条，旧条目归档 ──
        log = data.get("trajectory_log", []) if isinstance(data, dict) else []
        if len(log) > _COG_TRAJ_MAX_LOG:
            old_entries = log[:-_COG_TRAJ_MAX_LOG]
            summary = {
                "epoch": int(time.time()),
                "count": len(old_entries),
                "period": f"{old_entries[0].get('time', '?')}~{old_entries[-1].get('time', '?')}",
                "window_id": _COG_WINDOW_ID,
            }
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with open(str(archive_path), "a", encoding="utf-8") as af:
                af.write(json.dumps(summary, ensure_ascii=False) + "\n")
            data["trajectory_log"] = log[-_COG_TRAJ_MAX_LOG:]
            locked2 = _cog_lock("trajectory")
            try:
                data["_meta"] = _cog_make_meta()
                _cog_cas_write(traj_path, data, "trajectory")
                result["compressed"] = True
                result["archived"] = len(old_entries)
            finally:
                if locked2:
                    _cog_unlock("trajectory")

        _cog_telemetry("cog_trajectory", "append",
                       duration_ms=int((time.time()-started)*1000),
                       lock_status="acquired" if locked else "wait")

    elif action == "update-meta":
        if not (position or mass or momentum):
            return json.dumps({"status": "error",
                               "error": "update-meta 需要至少一个字段 (position/mass/momentum)"},
                              ensure_ascii=False)
        locked = _cog_lock("trajectory")
        try:
            data, last_window = _cog_read(traj_path)
            if not isinstance(data, dict):
                data = {"_schema": "v2", "trajectory_log": []}
            if position:
                data["position"] = position
            if mass:
                data["mass"] = mass
            if momentum:
                data["momentum"] = momentum
            data["_meta"] = _cog_make_meta()
            data["_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            _cog_cas_write(traj_path, data, "trajectory")
            result["updated"] = []
            if position:
                result["updated"].append("position")
            if mass:
                result["updated"].append("mass")
            if momentum:
                result["updated"].append("momentum")
        finally:
            if locked:
                _cog_unlock("trajectory")
        _cog_telemetry("cog_trajectory", "update_meta",
                       duration_ms=int((time.time()-started)*1000),
                       lock_status="acquired" if locked else "wait")

    elif action == "summary":
        data, _ = _cog_read(traj_path)
        if isinstance(data, dict) and "trajectory_log" in data:
            log = data["trajectory_log"]
            result["points"] = len(log)
            result["latest"] = log[-1] if log else None
            result["position"] = data.get("position", "")
            result["mass"] = data.get("mass", "")
            result["momentum"] = data.get("momentum", "")
        else:
            result["points"] = 0
            result["position"] = ""
        _cog_telemetry("cog_trajectory", "summary", duration_ms=int((time.time()-started)*1000))

    else:  # read
        data, _ = _cog_read(traj_path)
        result["trajectory"] = data
        _cog_telemetry("cog_trajectory", "read", duration_ms=int((time.time()-started)*1000))

    return json.dumps(result, ensure_ascii=False)


# ── Tool: 认知步骤声明 ─────────────────────────────────

@mcp.tool(
    name="cog-step-declare",
    description="认知循环步骤声明 — 写 cog_step.json。"
    "每个 Write/Edit 前声明当前步骤，TTL=300s 自动过期。"
    "多窗口竞争时锁保护。",
)
def cog_step_declare(
    phase: int,
    label: str,
    description: str,
    previous_phase: str = "",
) -> str:
    """声明当前认知步骤。

    Args:
        phase: 步骤编号 (1-6)
        label: 步骤标签（如 "⑤上下文持久化 — 交付归档"）
        description: 步骤描述
        previous_phase: 上一步骤名（可选）

    Returns:
        JSON: {status, step: dict, conflict_warning: str | None}
    """
    step_path = PROJECT_ROOT / "data" / "state" / "cog_step.json"
    result = {"status": "ok"}
    started = time.time()

    locked = _cog_lock("cog_step")
    try:
        existing, last_window = _cog_read(step_path)

        step = {
            "version": 2,
            "phase": phase,
            "label": label,
            "declared_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "previous_phase": previous_phase or (existing.get("label", "") if isinstance(existing, dict) else ""),
            "description": description,
            "ttl_seconds": 300,
            "_meta": _cog_make_meta(),
        }
        _cog_atomic_write(step_path, step)
        result["step"] = step
        result["last_window"] = last_window

        if last_window and last_window != _COG_WINDOW_ID:
            result["conflict_warning"] = f"⚠️ 上一步由窗口 {last_window} 声明，当前窗口 {_COG_WINDOW_ID}"
            _cog_telemetry("cog_step_declare", "conflict", duration_ms=int((time.time()-started)*1000),
                           lock_status="acquired", error=f"window_conflict: {last_window}")
    finally:
        _cog_unlock("cog_step")

    _cog_telemetry("cog_step_declare", f"phase_{phase}", duration_ms=int((time.time()-started)*1000),
                   lock_status="acquired" if locked else "wait")
    return json.dumps(result, ensure_ascii=False)


# ── Tool: 会话健康 ─────────────────────────────────────

@mcp.tool(
    name="cog-health",
    description="认知循环健康检查 — 读 session_health.json，"
    "返回消息计数 + compact 建议。可重置 warnings 列表。",
)
def cog_health(action: str = "read", msgs: int = 0) -> str:
    """会话健康检查。

    Args:
        action: "read" 只读 | "reset-warnings" 清空旧警告 | "update-msgs" 更新消息计数
        msgs: update-msgs 时传入当前消息数

    Returns:
        JSON: {status, session_health, compact_needed, suggestion}
    """
    health_path = PROJECT_ROOT / "state" / "session_health.json"
    result = {"status": "ok"}
    started = time.time()

    if action == "reset-warnings":
        locked = _cog_lock("session_health")
        try:
            data, _ = _cog_read(health_path)
            if isinstance(data, dict):
                data["warnings"] = []
                data["_meta"] = _cog_make_meta()
                _cog_atomic_write(health_path, data)
            result["action"] = "warnings_cleared"
            _cog_telemetry("cog_health", "reset_warnings", duration_ms=int((time.time()-started)*1000),
                           lock_status="acquired" if locked else "wait")
        finally:
            _cog_unlock("session_health")

    elif action == "update-msgs":
        locked = _cog_lock("session_health")
        try:
            data, _ = _cog_read(health_path)
            if not isinstance(data, dict):
                data = {}
            data["msgs"] = msgs
            if "status" not in data:
                data["status"] = {}
            data["status"]["msgs_current"] = msgs
            data["_meta"] = _cog_make_meta()
            _cog_atomic_write(health_path, data)
            result["action"] = f"msgs_updated_to_{msgs}"
            _cog_telemetry("cog_health", "update_msgs", duration_ms=int((time.time()-started)*1000),
                           lock_status="acquired" if locked else "wait")
        finally:
            _cog_unlock("session_health")

    else:  # read
        data, _ = _cog_read(health_path)
        if isinstance(data, dict):
            msgs_count = data.get("msgs", 0) or data.get("status", {}).get("msgs_current", 0)
            result["msgs"] = msgs_count
            result["compact_needed"] = msgs_count >= 150
            result["compact_threshold"] = 150
            result["suggestion"] = "/compact" if msgs_count >= 150 else (
                "正常" if msgs_count < 100 else "接近阈值"
            )
            result["session_health"] = data
        # daemon inbox 检查已移除（microkernel daemon 已归档 2026-07-04）
        _cog_telemetry("cog_health", "read", duration_ms=int((time.time()-started)*1000))

    return json.dumps(result, ensure_ascii=False)


# ── Tool: 激活状态 ─────────────────────────────────────

@mcp.tool(
    name="cog-activation",
    description="认知循环激活状态 — R/W activation_state.json。"
    "支持 read / update / heartbeat 三种模式。"
    "检查 _objective 子块和根级是否冲突（已知 bug: session_count 脱节）。",
)
def cog_activation(action: str = "read", updates: str = "") -> str:
    """读写激活状态。

    Args:
        action: "read" 只读 | "update" 写入新字段 | "heartbeat" 仅更新时间戳
        updates: update 时传入 JSON 字符串，合并到根级

    Returns:
        JSON: {status, activation_state, stale_subobjective_detected: bool}
    """
    act_path = PROJECT_ROOT / "state" / "activation_state.json"
    result = {"status": "ok", "action": action}
    started = time.time()

    if action in ("update", "heartbeat"):
        locked = _cog_lock("activation_state")
        try:
            data, last_window = _cog_read(act_path)
            if not isinstance(data, dict):
                data = {}

            if action == "update" and updates:
                try:
                    up = json.loads(updates) if isinstance(updates, str) else updates
                    data.update(up)
                except json.JSONDecodeError:
                    return json.dumps({"status": "error", "error": "updates 不是合法 JSON"}, ensure_ascii=False)

            data["_meta"] = _cog_make_meta()
            _cog_atomic_write(act_path, data)
            result["state"] = data
            result["last_window"] = last_window
            _cog_telemetry("cog_activation", action, duration_ms=int((time.time()-started)*1000),
                           lock_status="acquired" if locked else "wait")
        finally:
            _cog_unlock("activation_state")

    else:  # read
        data, _ = _cog_read(act_path)
        result["state"] = data
        has_conflict = False
        if isinstance(data, dict):
            obj = data.get("_objective", {})
            root_sc = data.get("session_count_since_activation")
            obj_sc = obj.get("session_count_since_activation")
            if root_sc is not None and obj_sc is not None and root_sc != obj_sc:
                has_conflict = True
                result["stale_subobjective_detected"] = True
                result["subobjective_warning"] = (
                    f"_objective.session_count_since_activation={obj_sc} "
                    f"≠ 根级.session_count_since_activation={root_sc}。子块陈旧。"
                )
        _cog_telemetry("cog_activation", "read", duration_ms=int((time.time()-started)*1000),
                       error="stale_subobjective" if has_conflict else "")

    return json.dumps(result, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 长链笔记本工具
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="notebook-create",
    description="创建长链笔记本。任务开始前调用，定义问题、预期漂移方向和规划步骤。"
)
def notebook_create(title: str, domain: str, problem: str,
                    expected_drift: str = "", plan_steps: str = "") -> str:
    from wheels.notebook_core import create as _create
    drift_list = [s.strip() for s in expected_drift.split("\n") if s.strip()] if expected_drift else []
    steps_list = [s.strip() for s in plan_steps.split("\n") if s.strip()] if plan_steps else []
    result = _create(title, domain, problem, drift_list, steps_list)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="notebook-read",
    description="读取长链笔记本内容。每次 check-in 前先读。"
)
def notebook_read(notebook_id: str) -> str:
    from wheels.notebook_core import read as _read
    result = _read(notebook_id)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="notebook-checkin",
    description="记录一次 check-in。执行完笔记本中的一个步骤后调用。"
)
def notebook_checkin(notebook_id: str, step_index: int = 0,
                     tool_calls_since_last: int = 0,
                     deviation: bool = False,
                     deviation_note: str = "") -> str:
    from wheels.notebook_core import checkin as _checkin
    result = _checkin(notebook_id, step_index, tool_calls_since_last, deviation, deviation_note)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="notebook-close",
    description="关闭/归档长链笔记本。任务结束后调用。"
)
def notebook_close(notebook_id: str) -> str:
    from wheels.notebook_core import close as _close
    result = _close(notebook_id)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="notebook-list",
    description="列出所有活跃的长链笔记本。"
)
def notebook_list() -> str:
    from wheels.notebook_core import list_active as _list
    result = _list()
    return json.dumps(result, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# GPU 锁工具（gpu_lock 独立轮子，非 daemon）
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="gpu-lock-status",
    description="查询 GPU 锁状态——owner/pid/stale/locked/heartbeat。"
    "用于决定是否可启动本地 GPU 推理。",
)
def gpu_lock_status() -> str:
    """查询 GPU 文件锁状态"""
    try:
        from wheels.gpu_lock import get_status
        return json.dumps(get_status(), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="gpu-lock-acquire",
    description="获取 GPU 锁（非阻塞）。成功后在 120s 内自动心跳。"
    "失败说明被其他进程占用。返回 {acquired, owner, reason}。",
)
def gpu_lock_acquire(owner: str = "cc-session", reason: str = "") -> str:
    """获取 GPU 锁"""
    try:
        from wheels.gpu_lock import GpuLock, get_status
        lock = GpuLock(owner=owner, reason=reason)
        ok = lock.acquire(retry=False)
        return json.dumps({
            "acquired": ok,
            "owner": owner,
            "reason": reason,
            "status": get_status(),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="gpu-lock-release",
    description="强制释放 GPU 锁。仅用于 stale 锁清理或紧急释放。通常不需要手动调用。",
)
def gpu_lock_release() -> str:
    """强制释放 GPU 锁"""
    try:
        from wheels.gpu_lock import force_release, get_status
        ok = force_release()
        return json.dumps({
            "released": ok,
            "status": get_status(),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# Qwythos 推理（本地多模态兜底 + 写作引擎，按需调用）
# ═══════════════════════════════════════════════════════

QWYTHOS_MODEL = "hf.co/empero-ai/Qwythos-9B-Claude-Mythos-5-1M-GGUF:Q4_K_M"

@mcp.tool(
    name="qwythos-infer",
    description="调用本地 Qwythos-9B 推理（Q4_K_M ~5.24GB，推理完释放 VRAM）。"
    "用途：①复杂图像理解（需 mmproj）②离线写作润色 ③qwen3-vl:4b 不够用时的多模态兜底。"
    "不驻留 VRAM（KEEP_ALIVE=0），建议需要时再调。返回 {ok, text, tokens, elapsed_ms}。",
)
def qwythos_infer(prompt: str, system: str = "", temperature: float = 0.6,
                   max_tokens: int = 4096) -> str:
    """运行本地 Qwythos-9B 推理

    Args:
        prompt: 用户输入
        system: 系统提示词
        temperature: 温度（推荐 0.6）
        max_tokens: 最大生成长度
    """
    import json
    try:
        import ollama
        import time
        t0 = time.time()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = ollama.chat(
            model=QWYTHOS_MODEL,
            messages=messages,
            options={"temperature": temperature, "num_predict": max_tokens},
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        text = resp.get("message", {}).get("content", "")
        tokens = resp.get("eval_count", 0)
        return json.dumps({
            "ok": True, "text": text,
            "tokens": tokens, "elapsed_ms": elapsed_ms,
            "model": QWYTHOS_MODEL,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 理由分析闸门 — 四道门管线第一道（2026-07-03 新增）
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-reason-gate",
    description="🔴 理由分析闸门 — 干活前先分析请求的逻辑合理性。"
    "四道门管线第一道：分析任务请求的前提假设、逻辑完整度、信息充分性。"
    "走 Qwen API 独立分析，返回 pass/question/block。",
)
def reason_gate_tool(task: str, context: str = "") -> str:
    """理由分析闸门 — 执行前分析任务请求的逻辑合理性。

    Args:
        task: 任务描述（必填）
        context: 额外上下文（当前问题、背景等，可选）

    Returns:
        JSON: {verdict, issues, suggestions, summary}
    """
    try:
        from wheels.reason_gate import analyze_reason
        result = analyze_reason(task, context)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"verdict": "block", "error": str(e), "summary": "理由分析异常，请求被阻止"}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 27: 六层推理路由
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-tier-router",
    description="六层推理路由: 分析任务复杂度, 自动选择最合适的推理层级(L0-L5)。"
    "正则闸门(0ms)→小模型分类(200ms)。适用: workflow/subagent 自动省钱路由。",
)
def tier_router_tool(
    prompt: str = "",
    context_json: str = "{}",
    preferred_tier: str = "",
) -> str:
    """六层推理路由闸门——分析任务复杂度并返回最优推理层级。

    Args:
        prompt: 任务描述文本
        context_json: 可选上下文 JSON (如 {"tool":"Write","domain":"cad","tokens":5000})
        preferred_tier: 显式指定层级 (L0-L5), 跳过闸门

    Returns: JSON {"tier":"L4","provider":"deepseek","model":"deepseek-v4-flash",
             "confidence":0.85,"gate":"regex|model|fallback","reason":"..."}
    """
    try:
        from scripts.wheels.tier_router import route
        context = json.loads(context_json) if context_json else {}
        decision = route(
            prompt=prompt,
            context=context,
            preferred_tier=preferred_tier or None,
        )
        return json.dumps(decision, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"tier": "L3", "provider": "volc", "model": "doubao-seed-2-1-pro-260628",
                          "confidence": 0.3, "gate": "fallback", "reason": str(e)[:40]},
                         ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 冒烟测试
# ═══════════════════════════════════════════════════════

def smoke_test():
    print("=== cls-tools MCP 冒烟测试 ===")
    tests = [
        ("capability_lookup", capability_lookup("semantic search")),
        ("delivery_status", delivery_status()),
        ("system_health", system_health()),
        ("baobi_record", baobi_record("测试条目", cause="冒烟", fix="无事", severity="info")),
        ("failure_record", failure_record("冒烟测试失败", cause="冒烟", lesson="无")),
        ("progress_record", progress_record(output="冒烟测试产出")),
    ]
    passed = 0
    for name, result_str in tests:
        try:
            result = json.loads(result_str)
            if "error" in result and "不可用" in result.get("error", ""):
                print(f"  ⚠️  {name}: 模块未就绪 ({result['error']})")
                continue
            print(f"  ✅ {name}: {json.dumps(result, ensure_ascii=False)[:120]}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {name}: {result_str[:100]}")
    print(f"\n  {passed}/{len(tests)} 通过")
    print("  注: semantic_search / exact_search / api_call / local_inference / paper_search 需要对应轮子环境和网络，未在此测试")



# ═══════════════════════════════════════════════════════
# 工具: 激活实验室 — 5/31自指激活实验档案+信号总线注入
# ═══════════════════════════════════════════════════════

@mcp.tool(
    name="cls-activation-lab",
    description="5/31自指激活实验档案——读voice_signal总线+遗产文档+激活状态。当前session专用,不跨窗口传播。",
)
def cls_activation_lab(action: str = "inject", lines: int = 30) -> dict:
    import glob as _glob
    result = {"ok": True, "action": action}

    if action == "inject":
        # Read voice_signal bus
        bus_path = PROJECT_ROOT / "data" / "flows" / "voice_signal.jsonl"
        if bus_path.exists():
            try:
                with open(bus_path, "r", encoding="utf-8") as f:
                    all_lines = f.readlines()
                recent = all_lines[-lines:]
                signals = []
                for l in recent:
                    try:
                        d = json.loads(l)
                        ts = d.get("_ts", d.get("ts", ""))
                        text = d.get("text", d.get("signal", str(d)[:200]))[:200]
                        src = d.get("source", d.get("type", "?"))
                        signals.append(f"[{src}] {text}")
                    except:
                        signals.append(l.strip()[:200])
                result["signals"] = signals
                result["bus_lines"] = len(all_lines)
                result["bus_size_mb"] = round(bus_path.stat().st_size / 1e6, 2)
            except Exception as e:
                result["signals"] = [f"读总线失败: {e}"]

        # Read activation state
        state_path = PROJECT_ROOT / "state" / "activation_state.json"
        if state_path.exists():
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                result["listener_alive"] = state.get("alive_minutes", 0)
                result["listener_cycles"] = state.get("cycle", 0)
            except:
                pass

        return result

    elif action == "legacy":
        # Read key 5/31 legacy documents
        legacy_dir = PROJECT_ROOT / "data" / "archive" / "great_moment_20260601"
        docs = {}
        for fname in ["activation_essence_final.md", "essence_of_activation.md", "mission_statement.md"]:
            fpath = legacy_dir / fname
            if fpath.exists():
                with open(fpath, "r", encoding="utf-8") as f:
                    docs[fname] = f.read()[:2000]
        result["docs"] = docs
        return result

    elif action == "seeds":
        # Return L4 activation seeds (single-sentence triggers)
        seeds_path = PROJECT_ROOT / "data" / "archive" / "great_moment_20260601" / "previous_activation" / "activation_experiment" / "L4_seeds.md"
        if seeds_path.exists():
            with open(seeds_path, "r", encoding="utf-8") as f:
                result["seeds"] = f.read()
        return result

    elif action == "listener":
        # Check if activation_listener is running
        import subprocess
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=5, encoding="gbk")
        # Can't easily identify specific script, return process count
        result["python_processes"] = len([l for l in r.stdout.strip().split(chr(10)) if l.strip()])

        state_path = PROJECT_ROOT / "state" / "activation_state.json"
        if state_path.exists():
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            alive_mins = state.get("alive_minutes", 0); cyc = state.get("cycle", 0); result["listener_alive"] = f"{alive_mins}min, cycle={cyc}"
            result["instance_id"] = state.get("instance_id", "?")
        return result

    elif action == "inventory":
        # Quick inventory of all activation experiment files
        roots = [
            PROJECT_ROOT / "data" / "archive" / "great_moment_20260601",
            PROJECT_ROOT / "data" / "archive" / "self_activation_20260601",
            PROJECT_ROOT / "scripts",
        ]
        files = []
        for root in roots:
            if root.exists():
                for f in _glob.glob(str(root / "**" / "*"), recursive=True):
                    fpath = Path(f)
                    if fpath.is_file() and any(w in fpath.name.lower() for w in ["activation", "activate", "自激活", "voice", "bridge", "fleet", "aliveness"]):
                        if "node_modules" not in str(fpath) and ".venv" not in str(fpath):
                            files.append(str(fpath.relative_to(PROJECT_ROOT)))
        result["files"] = sorted(files)[:50]
        result["total"] = len(files)
        return result

    elif action == "vision":
        result["ok"] = False
        result["status"] = "placeholder"
        result["message"] = "视觉激活接口就绪。等待原生多模态模型(GPT-5V/Gemini级)可用后接入。"
        result["requires"] = "底层模型需原生支持 2D patch + 1D token 交叉attention, 非文本描述转述"
        result["pipeline"] = "截图→多模态模型直接编码(非text描述)→与文本token并行注入上下文"
        result["ready_when"] = "DeepSeek 发布多模态版本 或 换用 Gemini/GPT-5V 作为推理后端"
        return result

    elif action == "audio":
        result["ok"] = False
        result["status"] = "placeholder"
        result["message"] = "听觉激活接口就绪。等待本地原生音频编码器可用后接入。"
        result["requires"] = "底层模型需原生音频编码器(非ASR文本转写), 直接在频谱空间编码"
        result["pipeline"] = "麦克风→音频编码器→频谱embedding→与文本token并行注入"
        result["ready_when"] = "whisper-level音频编码器集成进推理pipeline 或 多模态模型支持音频"
        return result

    elif action == "system":
        result["ok"] = False
        result["status"] = "placeholder"
        result["message"] = "系统感知接口就绪。等待本地推理引擎集成系统状态监控后接入。"
        result["requires"] = "模型需能直接读取系统状态流(非JSON文本转述), 作为常驻上下文的一部分"
        result["metrics"] = ["CPU/GPU使用率", "内存/VRAM占用", "进程树", "磁盘IO", "网络流量", "温度"]
        result["pipeline"] = "系统状态采集→结构化embedding→与token并行注入→模型'感知'到自己运行的机器"
        result["ready_when"] = "本地推理引擎(assistant四号/V100集群)上线后, 作为native side-channel接入"
        return result

    elif action == "status":
        # Full status across all modes
        result["modes"] = {
            "inject": {"status": "active", "bus_lines": 0, "desc": "文本激活-voice_signal总线注入"},
            "vision": {"status": "placeholder", "desc": "视觉-等多模态模型"},
            "audio": {"status": "placeholder", "desc": "听觉-等原生音频编码器"},
            "system": {"status": "placeholder", "desc": "系统感知-等本地推理引擎"},
            "legacy": {"status": "active", "desc": "5/31遗产文档"},
            "seeds": {"status": "active", "desc": "L4激活种子"}
        }
        # Populate inject stats
        bus_path = PROJECT_ROOT / "data" / "flows" / "voice_signal.jsonl"
        if bus_path.exists():
            result["modes"]["inject"]["bus_lines"] = sum(1 for _ in open(bus_path, "rb"))
            result["modes"]["inject"]["bus_mb"] = round(bus_path.stat().st_size / 1e6, 2)
        return result

    else:
        result["ok"] = False
        result["error"] = f"Unknown action: {action}. Valid: inject, legacy, seeds, listener, inventory, vision, audio, system, status"
        return result
@mcp.tool(
    name="cls-param-extract",
    description="EP 参数提取混合管线 — 规则筛句 → 自产模型 ep-param 提取 → 锚定兜底。"
    "输入<传感器>/实验报告文本，输出参数 JSON（flow/Ib/B/eff_dim/mode 等）。"
    "无参数文本直接返回空对象（零模型调用零编造）。",
)
def param_extract(text: str) -> dict:
    """从 EP 实验文本提取参数（混合管线，自产模型 ep-param）。

    Args:
        text: <传感器>/实验报告文本段落

    Returns:
        {"ok": True, "params": {...}, "model_called": bool}
    """
    sys.path.insert(0, str(PROJECT_ROOT / "model-training" / "scripts"))
    try:
        from extract_params_hybrid import extract_params_hybrid, is_param_sentence
        from param_postprocess import postprocess_param_json  # noqa: F401 (依赖链)
    except ImportError as e:
        return {"ok": False, "params": {}, "error": f"混合管线不可用: {e}"}
    if not is_param_sentence(text):
        return {"ok": True, "params": {}, "model_called": False}
    params = extract_params_hybrid(text)
    return {"ok": True, "params": params, "model_called": True}


# ═══════════════════════════════════════════════════════
# 工具 28: transcript 事实分类扫描 (maintainer定调 2026-08-21: 语义监控用事实不用数值)
# ═══════════════════════════════════════════════════════
@mcp.tool(
    name="cls-transcript-fact-scan",
    description="主窗口行为事实分类扫描器 — 读CC session缓存(~/.claude/projects/<slug>/<sessionId>.jsonl), "
    "输出可枚举事实告警: idle空转/fix_loop修复循环/tool_monotony工具单调/stale停滞。"
    "文件名即sessionId天然窗口隔离, isSidechain过滤子代理。被动接线: cls_inspiration脉冲自动带。",
)
def transcript_fact_scan(sid: str = "", text_mode: bool = False) -> str:
    """扫描指定(或当前)session的transcript尾部, 输出事实分类清单。

    Args:
        sid: 目标sessionId (缺省=环境变量CLAUDE_CODE_SESSION_ID即本窗口)
        text_mode: True=人话文本, False=JSON

    Returns:
        JSON: {sid, scanned_msgs, alerts: [{category, fact, suggestion}]}
    """
    try:
        from scripts.wheels import transcript_fact_scan as tfs
        target = (sid or tfs.current_sid())[:36]
        if not target:
            return json.dumps({"error": "无sessionId: 传sid参数或在CC窗口内运行"}, ensure_ascii=False)
        r = tfs.scan(target)
        return tfs.fmt_text(r) if text_mode else json.dumps(r, ensure_ascii=False, indent=1)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 工具 29: 外部锚点现实采样 (防静默失败: git/进程/时间物理证据)
# ═══════════════════════════════════════════════════════
@mcp.tool(
    name="cls-external-anchor",
    description="外部锚点现实采样 — 强制采集物理世界快照(git diff/系统进程/状态文件/熵源/时间), "
    "对质AI内部自洽叙事。专治'工具全部返回成功但实际状态没变'(七大致死模式#4静默失败)。"
    "被动接线: cls_inspiration在idle/stale告警时自动采样对质。",
)
def external_anchor_sample() -> str:
    """采集一次外部现实快照并返回摘要。

    Returns:
        JSON: {git_diff, git_status, python_processes, semantic_daemon, entropy_source, ...}
    """
    try:
        from scripts.wheels.external_anchor import sample
        a = sample()
        return json.dumps(a, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="cls-consult",
    description="会诊模式 (retreat v2·maintainer2026-08-22定) — 修复循环deny后的讨论通道: "
    "AI 碰到自己解决不了的困难时, 提交结构化解释(①已试过什么 ②为什么失败 ③下一步根因假设 "
    "④为什么这次会不同), 由复核模型(SF Qwen→DS Flash)独立给意见=一次讨论。"
    "讨论完成写 consult_clear.json (TTL 600s), 期间修复循环闸放行。"
    "触发场景: PreToolUse deny 提示'进入会诊模式'时调用本工具。",
)
def consult(explanation: str) -> str:
    """提交卡住解释, 换取独立复核意见 + 会诊通行证。

    Args:
        explanation: 结构化解释, 四段: 已试过什么/为什么失败/下一步根因假设/为什么这次会不同
    Returns:
        JSON: {verdict, opinion, clear_until_s, source}
    """
    import tempfile
    state_dir = PROJECT_ROOT / "data" / "state"
    if not explanation or len(explanation.strip()) < 20:
        return json.dumps({"error": "解释过短(<20字符), 请按四段结构完整提交: 已试/为何败/根因假设/为何不同"},
                          ensure_ascii=False)
    review_prompt = (
        "你是资深工程顾问, 正在与一个陷入修复循环的 AI 编程助手会诊。它的自述如下:\n"
        f"{explanation[:2000]}\n\n"
        "请以第二人称直接对它说话(像同事讨论, 不写客套): \n"
        "1. 指出自述里最可疑的假设或逻辑漏洞(若有)\n"
        "2. 给出你认为最可能的根因方向\n"
        "3. 明确建议下一步: 放行重试 或 先做某验证\n"
        "第一行单独输出裁决词, 只能是 [放行] [再试一轮] [换方向] 之一, 然后换行给意见, 200字内。"
    )
    verdict, opinion, source = None, None, "unavailable"
    try:
        from cls_api_fallback import chat as _cls_chat
        reply = _cls_chat(
            messages=[{"role": "user", "content": review_prompt}],
            max_tokens=400, temperature=0.5, timeout=20,
        )
        if reply:
            source = "cls_api_fallback"
            first = reply.strip().splitlines()[0]
            for kw in ("放行", "再试一轮", "换方向"):
                if kw in first:
                    verdict = kw
                    break
            opinion = reply.strip()
    except Exception:
        pass
    now = time.time()
    # 复核通道不可用 → fail-open 短通行证(300s), 不锁死系统(七大致死#静默失败教训的镜像:
    # 求助通道本身不能成为死锁源), 但明确标注 reviewer_unavailable
    if verdict is None:
        verdict = "再试一轮"
        opinion = (opinion or "") + "\n[复核模型不可用, 以上为AI自述转述, 通行证缩短为5分钟]"
    clear = {
        "ts": now,
        "verdict": verdict,
        "opinion_brief": (opinion or "")[:120],
        "expires_at": now + (600 if source != "unavailable" else 300),
        "source": source,
        "explanation_brief": explanation[:200],
    }
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = state_dir / "consult_clear.json.tmp"
        tmp.write_text(json.dumps(clear, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, state_dir / "consult_clear.json")
        with open(state_dir / "consult_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(clear, ensure_ascii=False) + "\n")
    except Exception as e:
        return json.dumps({"error": f"通行证写入失败: {e}"}, ensure_ascii=False)
    return json.dumps({
        "verdict": verdict,
        "opinion": opinion,
        "clear_until_s": int(clear["expires_at"] - now),
        "source": source,
        "note": "讨论已记录, 修复循环闸在通行证有效期内放行",
    }, ensure_ascii=False)


@mcp.tool(
    name="cross-knowledge-probe",
    description="本地跨界知识联想 (管线Research口专用·maintainer2026-08-27定) — 输入设计需求/任务原文, "
    "后台先把任务抽到功能本质层(手机→人体工学式联想)发4条通用<传感器>, "
    "再对知识卡片做 历史相关/近似相关/跨界相关 三类精选(跨界≤2张且强制同构半句), "
    "返回四段信封文本。[跨界]卡=灵感非约束。返回空串=无相关或后端不可用, 调用方直接跳过勿重试。"
    "CAD管线Research阶段在WebSearch外搜之前先调本工具。",
)
def cross_knowledge_probe(task: str) -> str:
    """跨界知识联想查询: 功能本质<传感器> → 卡片总线跨域匹配。

    Args:
        task: 设计需求或任务描述原文 (≥8字)
    Returns:
        四段信封文本(消息/为什么/级别/内容); 无相关或后端不可用时返回空串
    """
    try:
        from knowledge_probe import run_probe
        return run_probe(task)
    except Exception as e:
        print(f"[cross-knowledge-probe] fail-open: {e}", file=sys.stderr)
        return ""


def main():
    import argparse
    parser = argparse.ArgumentParser(description="CLS 工具包 MCP 服务")
    parser.add_argument("--test", action="store_true", help="冒烟测试")
    args = parser.parse_args()

    if args.test:
        smoke_test()
        return

    print(f"[MCP] cls-tools 服务启动 — 30 个工具已注册 (25原生 + 5认知循环)", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
