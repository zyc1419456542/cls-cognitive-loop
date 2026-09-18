#!/usr/bin/env python3
"""
api_pipeline.py — 统一API调用管线（所有外部API调用的单一入口）
============================================================
设计目的: 所有API调用(远端/本地GPU)走这一个轮子, 调用过程实时可见。
每次调用三输出: ①控制台实时打印 ②结构化日志jsonl ③监视文件供watch面板

用法:
    # 作为轮子(代码调用)
    from scripts.wheels.api_pipeline import call, status
    result = call("qwen", "qwen-plus", messages=[...])
    result = call("anthropic", "claude-haiku-4-5", messages=[...])
    result = call("local_gpu", "qwen2.5-1.5b", prompt="...")

    # 监视面板(用户开一个终端盯着)
    python scripts/wheels/api_pipeline.py --watch

    # 查看统计
    python scripts/wheels/api_pipeline.py --status

架构:
    所有调用者 → api_pipeline.call() → ①控制台打印(实时可见)
                                     → ②data/pipeline/api_calls.jsonl(审计)
                                     → ③data/pipeline/api_monitor.json(watch面板)
                                     → ④实际API调用
                                     → ⑤返回结果

集成:
    qwen_gate.py 的 _call_qwen_api / _call_anthropic_verify / _call_local_qwen
    全部改走 api_pipeline.call(), 不再自己写socket/http。
"""

import json, os, sys, time, urllib.request, urllib.error, inspect, uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
PIPELINE_DIR = ROOT / "data" / "pipeline"
CALLS_LOG = PIPELINE_DIR / "api_calls.jsonl"
MONITOR_FILE = PIPELINE_DIR / "api_monitor.json"
PIPELINE_DIR.mkdir(parents=True, exist_ok=True)

# ── ANSI 颜色（Windows终端支持） ──
C = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[91m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "blue": "\033[94m",
    "magenta": "\033[95m",
    "cyan": "\033[96m",
    "white": "\033[97m",
}

PROVIDER_COLORS = {
    "qwen": C["blue"],
    "anthropic": C["green"],
    "local_gpu": C["cyan"],
    "ollama": C["yellow"],
    "deepseek": C["magenta"],
    "volc": C["cyan"],
    "openai": C["green"],
    "teio": C["magenta"],
    "opencode": C["green"],
    "kimi": C["green"],
    "zhipu": C["blue"],
    "unknown": C["yellow"],
}

PROVIDER_ICONS = {
    "qwen": "[QW]",
    "anthropic": "[AN]",
    "local_gpu": "[GPU]",
    "ollama": "[OL]",
    "deepseek": "[DS]",
    "volc": "[VC]",
    "openai": "[GPT]",
    "teio": "[TE]",
    "opencode": "[OC]",
    "kimi": "[KM]",
    "zhipu": "[ZP]",
    "unknown": "[?]",
}

# ── 国外精选模型 (国际 5 类任务速查) ──────────────────────────────
# @verified-by: opencode Zen 端点直连实测 @date: 2026-08-08
# 每类 1 个精选国外模型, 全部走 opencode provider。用法:
#     provider, model = pick_international("math")
#     call(provider, model, messages=[...])
# 实测不可用 (系统性问题非偶发): grok-4.5(HTTP 503) / gemini-3.5/3.6-flash(HTTP 500)
#   / claude-opus-5(空响应) / gpt-5.1-codex(HTTP 400)。已从精选剔除, 待端点恢复再评估。
INTERNATIONAL_PICKS = {
    "literature": ("opencode", "gpt-5.6-terra"),    # 文学/文笔描写
    "hardware":   ("opencode", "claude-opus-4-8"),  # 硬件/工程推理
    "code":       ("opencode", "gpt-5.3-codex"),    # 代码推理/生成
    "review":     ("opencode", "claude-sonnet-5"),  # 审稿/评审
    "math":       ("opencode", "gpt-5.6-sol"),      # 数学题
}


def pick_international(category: str):
    """按任务类别取精选国外模型 → (provider, model)。未知类别返回 None。

    Args:
        category: "literature" | "hardware" | "code" | "review" | "math"
    Returns:
        tuple[str, str] 或 None(未知类别)
    """
    return INTERNATIONAL_PICKS.get(category)

# ── caller 自检测 ─────────────────────────────────────────────

def _detect_caller() -> dict:
    """通过调用栈自动检测谁在调用 api_pipeline。

    向上遍历栈帧，跳过 api_pipeline.py 自身和标准库，
    返回第一个"业务代码"帧的信息。

    Returns:
        {"script": "qwen_gate.py", "function": "verify_cad", "line": 187}
    """
    try:
        pipeline_file = Path(__file__).name  # api_pipeline.py
        for frame_info in inspect.stack():
            fname = Path(frame_info.filename).name
            # 跳过 api_pipeline 自身
            if fname == pipeline_file:
                continue
            # 跳过标准库和 site-packages
            fpath = frame_info.filename
            if ('stdlib' in fpath or 'site-packages' in fpath or
                'lib/python' in fpath.replace('\\', '/') or
                'Lib/urllib' in fpath or 'Lib/http' in fpath):
                continue
            # 跳过 MCP 库封装
            if 'fastmcp' in fpath.lower() or 'mcp/server' in fpath.lower():
                continue
            # 找到第一个业务代码帧
            return {
                "script": fname,
                "function": frame_info.function,
                "line": frame_info.lineno,
                "path": fpath,
            }
    except Exception:
        pass
    return {"script": "unknown", "function": "unknown", "line": 0, "path": ""}


# ── 核心: 统一调用入口 ──────────────────────────────────────────

def call(
    provider: str,
    model: str = "",
    messages: list = None,
    prompt: str = "",
    max_tokens: int = 1024,   # 标准LLM API max_tokens默认值(OpenAI/Anthropic惯例)
    temperature: float = 0.1,  # 低温度默认值:优先确定性/事实性输出
    timeout_s: int = 180,      # HTTP超时:180s(含Ollama首次加载20-30s,旧60s不够)
    task_type: str = "generate",
    auto_route: bool = True,   # 🆕 自动六层路由: 默认启用丘脑路由(2026-07-21)
    endpoint: str = "base_url", # opencode 专属: base_url(云Zen国际池) | go_base_url(云Go国内池) | local_base_url(本地网关)
    quiet: bool = False,       # 🆕 2026-08-18: True → 不打印实时提示词框图(供后台批调, 防刷屏)
    extra_body: dict = None,   # 🆕 2026-08-19 二号交付: 透传附加请求体字段(如 thinking disabled)
) -> dict | None:
    """统一API调用入口 — 所有外部API(远端+本地GPU)必走此函数。

    参数:
        provider: "qwen" | "anthropic" | "local_gpu" | "deepseek" | "ollama" | "volc" | "openai" | "teio" | "opencode" | "kimi" | "zhipu"
        model: 模型名
        messages: OpenAI格式消息列表 [{"role":"system","content":"..."}, ...]
        prompt: 纯文本prompt（local_gpu使用; messages存在时忽略）
        max_tokens: 最大生成token数
        temperature: 温度
        timeout_s: 超时秒数
        task_type: local_gpu的cmd类型 ("generate"|"ping")
        auto_route: 🆕 True→调用 tier_router 自动选最便宜的层级, 覆盖 provider/model
        endpoint: opencode 专属端点选择 (base_url/go_base_url/local_base_url)

    返回:
        {"ok": bool, "text": str, "tokens": {"prompt": int, "completion": int},
         "elapsed_ms": int, "provider": str, "model": str}
        或 None (完全不可用)
    """
    # 🆕 自动路由: 正则闸门→小模型分类→决定provider/model
    if auto_route:
        try:
            from scripts.wheels.tier_router import route
            user_prompt = prompt or _msgs_to_prompt(messages) or ""
            decision = route(user_prompt)
            provider = decision["provider"]
            model = decision["model"]
        except Exception:
            pass  # 路由失败 → 用原始 provider/model

    call_id = _short_id()
    started = time.time()

    # ── ① 控制台实时打印（蓝色框图）—— quiet=True 时静默(后台批调不刷屏)
    icon = PROVIDER_ICONS.get(provider, "❓")
    color = PROVIDER_COLORS.get(provider, C["yellow"])
    prompt_full = prompt or _msgs_to_prompt(messages) or "(空)"
    prompt_preview = _preview(prompt_full, 80)
    if not quiet:
        _print_prompt_block(provider, model, prompt_full, icon, color)

    # ── ② 写监视文件（调用中状态）──
    _update_monitor({
        "status": "calling",
        "call_id": call_id,
        "provider": provider,
        "model": model,
        "prompt_preview": prompt_preview,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    # ── 2.5 丘脑过滤 (2026-07-19): 上下文完整性预检 → 自动注入 ──
    if prompt and len(prompt) > 10:
        try:
            from scripts.wheels.tier_router import _thalamus_check
            thalamus = _thalamus_check(prompt[:500])
            if thalamus.get("ok") and thalamus.get("enriched") != prompt:
                prompt = thalamus["enriched"]
        except:
            pass  # 丘脑不可用 → 不影响主线

    # ── ③ 实际调用──
    result = None
    error = None
    try:
        if provider == "local_gpu":
            result = _call_local_gpu(prompt or _msgs_to_prompt(messages), max_tokens, temperature, task_type, timeout_s)
        elif provider == "qwen":
            result = _call_qwen_api(messages or [{"role": "user", "content": prompt}], model, max_tokens, timeout_s)
        elif provider == "anthropic":
            result = _call_anthropic_api(messages or [{"role": "user", "content": prompt}], model, max_tokens, timeout_s)
        elif provider == "deepseek":
            result = _call_deepseek_api(messages or [{"role": "user", "content": prompt}], model, max_tokens, timeout_s)
        elif provider == "ollama":
            result = _call_ollama_api(prompt, messages, model, max_tokens, temperature, timeout_s)
        elif provider == "volc":
            result = _call_volc_api(messages or [{"role": "user", "content": prompt}], model, max_tokens, timeout_s)
        elif provider == "openai":
            result = _call_openai_api(messages or [{"role": "user", "content": prompt}], model, max_tokens, timeout_s)
        elif provider == "teio":
            result = _call_teio_api(messages or [{"role": "user", "content": prompt}], model, max_tokens, timeout_s)
        elif provider == "kimi":
            result = _call_kimi_api(messages or [{"role": "user", "content": prompt}], model, max_tokens, timeout_s)
        elif provider == "zhipu":
            result = _call_zhipu_api(messages or [{"role": "user", "content": prompt}], model, max_tokens, timeout_s)
        elif provider == "opencode":
            result = _call_opencode_api(messages or [{"role": "user", "content": prompt}], model, max_tokens, timeout_s, endpoint=endpoint, extra_body=extra_body)
        elif provider == "mimo":
            result = _call_mimo_api(messages or [{"role": "user", "content": prompt}], model, max_tokens, timeout_s)
        else:
            error = f"未知 provider: {provider}"
    except Exception as e:
        error = str(e)[:200]

    elapsed_ms = int((time.time() - started) * 1000)

    # ── ④ 结果打印──
    import shutil
    term_width = shutil.get_terminal_size((100, 20)).columns
    box_w = min(term_width - 2, 120)
    if result and result.get("ok"):
        text_preview = result.get("text", "")
        tok_info = result.get("tokens", {})
        if isinstance(tok_info, dict):
            tok_str = f"in:{tok_info.get('prompt', '?')} out:{tok_info.get('completion', '?')}"
        else:
            tok_str = f"tokens:{tok_info}"
        # 结果框（绿色）
        result_color = C["green"]
        status_line = f" ✓ [{elapsed_ms}ms] {tok_str}"
        print(f"{result_color}╔{'═' * (box_w - 2)}╗{C['reset']}")
        print(f"{result_color}║{C['bold']}{status_line}{' ' * (box_w - 4 - len(status_line))}{C['reset']}{result_color}║{C['reset']}")
        # 结果预览前几行
        resp_lines = text_preview.split('\n')[:5]
        for line in resp_lines:
            if len(line) > box_w - 4:
                line = line[:box_w - 7] + "..."
            print(f"{result_color}║ {C['dim']}{line}{' ' * (box_w - 4 - len(line))}{C['reset']}{result_color}║{C['reset']}")
        if len(text_preview.split('\n')) > 5:
            print(f"{result_color}║ {C['dim']}...（共{len(text_preview.split(chr(10)))}行）{C['reset']}{result_color} ║{C['reset']}")
        print(f"{result_color}╚{'═' * (box_w - 2)}╝{C['reset']}\n", flush=True)
    else:
        err_msg = error or (result.get("error", "") if isinstance(result, dict) else "") if result else error or ""
        fail_color = C["red"]
        err_line = f" ✗ [{elapsed_ms}ms] {err_msg[:100]}"
        print(f"{fail_color}╔{'═' * (box_w - 2)}╗{C['reset']}")
        print(f"{fail_color}║{C['bold']}{err_line}{' ' * (box_w - 4 - len(err_line))}{C['reset']}{fail_color}║{C['reset']}")
        print(f"{fail_color}╚{'═' * (box_w - 2)}╝{C['reset']}\n", flush=True)
        print(f"{color}'--{'--'*25}{C['reset']}\n", flush=True)

    # ── ⑤ 结构化日志──
    caller = _detect_caller()
    log_entry = {
        "_timestamp": datetime.now(timezone.utc).isoformat(),
        "call_id": call_id,
        "provider": provider,
        "model": model,
        "caller": caller,
        "prompt_preview": prompt_preview,
        "ok": result.get("ok", False) if isinstance(result, dict) else False,
        "elapsed_ms": elapsed_ms,
        "tokens": result.get("tokens", {}) if isinstance(result, dict) else {},
        "response_preview": _preview(result.get("text", ""), 150) if isinstance(result, dict) else "",
        "error": error or "",
    }
    _append_log(log_entry)

    # ── ⑥ 更新监视文件（完成状态）──
    _update_monitor({
        "status": "done" if (result and result.get("ok")) else "failed",
        "call_id": call_id,
        "provider": provider,
        "model": model,
        "prompt_preview": prompt_preview,
        "elapsed_ms": elapsed_ms,
        "ok": result.get("ok", False) if result else False,
        "error": error or "",
        "finished_at": datetime.now(timezone.utc).isoformat(),
    })

    if result is None:
        return {"ok": False, "error": error or "无响应", "provider": provider, "elapsed_ms": elapsed_ms}
    return result


def call_dual(
    model: str = "",
    messages: list = None,
    prompt: str = "",
    max_tokens: int = 1024,
    temperature: float = 0.1,
    timeout_s: int = 180,
    provider_primary: str = "teio",
    provider_fallback: str = "opencode",
    endpoint: str = "base_url",
    quiet: bool = False,
) -> dict | None:
    """双通道调用 — 先中转站(teio)再opencode，哪个能用用哪个。

    张maintainer定调 2026-08-08: teio 是中转站（不可信），opencode 是新 API（可信）。
    先试 primary，失败自动 fallback 到 fallback，返回第一个成功结果。
    teio 配额耗尽后 → 从管线全面删除，本函数退化为单通道（只走 opencode）。

    参数:
        model: 模型名（两个通道共用同一个模型）
        messages/prompt/max_tokens/temperature/timeout_s: 透传给 call()
        provider_primary: 首选通道（默认 teio 中转站）
        provider_fallback: 备选通道（默认 opencode）
        endpoint: opencode 专属端点 (base_url/go_base_url/local_base_url)，透传给 call()
        quiet: True → 不打印通道切换提示（供脚本内部调用）

    返回:
        第一个成功通道的 result（含 provider/model 字段标明实际走的是谁）
        两通道都失败 → 返回 {"ok": False, "error": 两通道错误拼接, "provider": f"{primary}->{fallback}"}
    """
    errors = []
    for p in (provider_primary, provider_fallback):
        if not quiet and errors:
            print(f"{C['yellow']}[dual] {provider_primary} 失败 → 尝试 {p}...{C['reset']}", flush=True)
        r = call(p, model=model, messages=messages, prompt=prompt,
                 max_tokens=max_tokens, temperature=temperature, timeout_s=timeout_s,
                 endpoint=endpoint, auto_route=False)
        if r and r.get("ok"):
            if not quiet:
                print(f"{C['green']}[dual] ✓ 通道 {p} 可用（model={model}）{C['reset']}", flush=True)
            return r
        errors.append(f"{p}:{r.get('error','无响应') if isinstance(r,dict) else '无响应'}")
    return {"ok": False, "error": " 双通道全失败 → ".join(errors),
            "provider": f"{provider_primary}->{provider_fallback}",
            "model": model}


# ── 内部: 各provider的实际调用实现 ─────────────────────────────────

def _call_local_gpu(prompt: str, max_tokens: int, temperature: float, task_type: str, timeout_s: int) -> dict | None:
    """本地GPU推理 (Ollama HTTP API) — 用完即走，不占VRAM"""
# ⚠️【本机】Ollama 地址 —— 与 _call_ollama_api 的 OLLAMA_REMOTE_URL(二号机 Tailscale) 是两条不同的路。
#    本 provider(local_gpu) 走本机; provider="ollama" 走二号机。
    OLLAMA_URL = "http://localhost:11434"
    OLLAMA_MODEL = model if model else "qwen2.5:1.5b"  # 优先用传入model,兜底1.5B

    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
        "keep_alive": "0s",  # 🔴 用完即走，不占VRAM
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("done"):
            return {
                "ok": True,
                "text": data.get("response", ""),
                "provider": "local_gpu",
                "model": OLLAMA_MODEL,
                "tokens": {
                    "prompt": data.get("prompt_eval_count", 0),
                    "completion": data.get("eval_count", 0),
                }
            }
        return {"ok": False, "error": "Ollama返回未完成", "provider": "local_gpu"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"Ollama HTTP {e.code}: {e.reason}", "provider": "local_gpu"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"Ollama不可达: {e.reason}", "provider": "local_gpu"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "local_gpu"}


def _call_qwen_api(messages: list, model: str, max_tokens: int, timeout_s: int) -> dict | None:
    """远端 Qwen API (DashScope)"""
    from scripts.qwen_api import call_qwen
    try:
        result = call_qwen(messages, model=model or "qwen-plus")
        content = result["choices"][0]["message"]["content"]
        usage = result.get("usage", {})
        return {
            "ok": True,
            "text": content,
            "tokens": {
                "prompt": usage.get("prompt_tokens", 0),
                "completion": usage.get("completion_tokens", 0),
            },
            "provider": "qwen",
            "model": model or "qwen-plus",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "qwen"}


def _call_anthropic_api(messages: list, model: str, max_tokens: int, timeout_s: int) -> dict | None:
    """远端 Anthropic API"""
    config_file = ROOT / "keys" / "anthropic_config.json"
    if not config_file.exists():
        return {"ok": False, "error": "anthropic_config.json 不存在", "provider": "anthropic"}
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        api_key = config.get("api_key", "")
        if not api_key or api_key == "<your-anthropic-api-key>":
            return {"ok": False, "error": "Anthropic API key 未配置", "provider": "anthropic"}
        base_url = config.get("base_url", "https://api.anthropic.com")
        model = model or config.get("default_model", "claude-haiku-4-5-20251001")

        # 转换 messages 格式: OpenAI → Anthropic
        system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_msgs = [m for m in messages if m["role"] != "system"]
        anthropic_msgs = []
        for m in user_msgs:
            role = "user" if m["role"] in ("user", "system") else "assistant"
            anthropic_msgs.append({"role": role, "content": m["content"]})

        import urllib.request
        api_url = f"{base_url.rstrip('/')}/v1/messages"
        req_data = json.dumps({
            "model": model,
            "max_tokens": max_tokens,
            "messages": anthropic_msgs,
            "system": system_msg if system_msg else None,
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(api_url, data=req_data, method="POST")
        req.add_header("x-api-key", api_key)
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("content-type", "application/json")

        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        # 安全提取 text block（可能混有 thinking 类型的 block）
        content = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                content = block.get("text", "")
                break
        usage = result.get("usage", {})
        return {
            "ok": True,
            "text": content,
            "tokens": {
                "prompt": usage.get("input_tokens", 0),
                "completion": usage.get("output_tokens", 0),
            },
            "provider": "anthropic",
            "model": model,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "anthropic"}


def _call_deepseek_api(messages: list, model: str, max_tokens: int, timeout_s: int) -> dict | None:
    """远端 DeepSeek V4 Pro API — 裸调用，不走 /anthropic 兼容层

    直连 DeepSeek 原生 API (OpenAI 兼容)，绕过 CC 的模型路由。
    模型默认 deepseek-v4-flash，适用于复杂数学/物理/科学推理。
    API Key 优先用 DEEPSEEK_PRO_KEY，兜底 ANTHROPIC_API_KEY。
    """
    import urllib.request

    # 优先级: 官网key文件 → 环境变量 → 兜底
    _official_cfg = ROOT / "keys" / "deepseek_official.json"
    if _official_cfg.exists():
        try:
            _oc = json.loads(_official_cfg.read_text(encoding="utf-8"))
            api_key = _oc.get("api_key", "")
        except Exception:
            api_key = ""
    else:
        api_key = ""
    if not api_key:
        api_key = os.environ.get("DEEPSEEK_PRO_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "未设置 DEEPSEEK_PRO_KEY 或 ANTHROPIC_API_KEY", "provider": "deepseek"}

    model = model or "deepseek-v4-flash"
    base_url = "https://api.deepseek.com/v1/chat/completions"
    req_data = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        base_url, data=req_data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout_s)
        body = json.loads(resp.read().decode("utf-8"))
        text = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = body.get("usage", {})
        tokens = {"prompt": usage.get("prompt_tokens", 0), "completion": usage.get("completion_tokens", 0)}
        return {"ok": True, "text": text, "tokens": tokens, "provider": "deepseek", "model": model}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:300]
        return {"ok": False, "error": f"HTTP {e.code}: {err_body}", "provider": "deepseek"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "deepseek"}


def _call_ollama_api(prompt: str, messages: list, model: str, max_tokens: int,
                     temperature: float, timeout_s: int) -> dict | None:
    """Ollama 推理 → assistant-node2 GPU (Tailscale)

    通过 HTTP POST 调用二号 Ollama 服务 (100.101.150.25:11434)，支持:
      - messages 格式 → /api/chat
      - 纯文本 prompt → /api/generate
    环境变量 OLLAMA_REMOTE_URL 可覆盖地址。
    """
    import urllib.request
    model = model or "qwen3:32b"  # assistant-node2主力 (20GB)
    base_url = os.environ.get("OLLAMA_REMOTE_URL", "http://100.101.150.25:11434")
    # ⚠️ 这是【二号机 Tailscale】地址 —— 二号机不在线就连接超时。
    #    本机 Ollama(跑着 ep 全系列) 请用 provider="local_gpu"(同文件 OLLAMA_URL) 或直连 127.0.0.1。
    #    实测 2026-09-14: 本机 12 个模型可用, 但本 provider 默认走二号机 → 看起来像"Ollama 不可靠", 实为地址指错机器。

    try:
        if messages:
            url = f"{base_url}/api/chat"
            req_data = json.dumps({
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {
                    "num_ctx": 2048,
                    "temperature": temperature,
                },
            }, ensure_ascii=False).encode("utf-8")
        else:
            url = f"{base_url}/api/generate"
            req_data = json.dumps({
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_ctx": 2048,
                    "temperature": temperature,
                },
            }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url, data=req_data, method="POST")
        req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        if messages:
            text = result.get("message", {}).get("content", "")
        else:
            text = result.get("response", "")

        eval_count = result.get("eval_count", 0)
        return {
            "ok": bool(text),
            "text": text,
            "tokens": {"prompt": 0, "completion": eval_count},
            "provider": "ollama",
            "model": model,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "ollama"}


def _call_volc_api(messages: list, model: str, max_tokens: int, timeout_s: int) -> dict | None:
    """远端火山引擎 Ark API (OpenAI 兼容协议)

    支持豆包系列模型(代码/通用/视觉)和第三方模型(GLM/DeepSeek)。
    Key 存储在 keys/volc_config.json 中。
    """
    config_file = ROOT / "keys" / "volc_config.json"
    if not config_file.exists():
        return {"ok": False, "error": "volc_config.json 不存在", "provider": "volc"}
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        api_key = config.get("api_key", "")
        if not api_key:
            return {"ok": False, "error": "Volcengine API key 未配置", "provider": "volc"}
        base_url = config.get("base_url", "https://ark.cn-beijing.volces.com/api/v3")
        # 2026-08-03: 兜底对齐 volc_config.json 的 default_code_model(doubao-seed-2-1-pro-260628), 弃用旧的 2-0-code-preview
        model = model or config.get("default_code_model", "doubao-seed-2-1-pro-260628")

        api_url = f"{base_url.rstrip('/')}/chat/completions"
        req_data = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(api_url, data=req_data, method="POST")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        usage = result.get("usage", {})
        return {
            "ok": True,
            "text": content,
            "tokens": {
                "prompt": usage.get("prompt_tokens", 0),
                "completion": usage.get("completion_tokens", 0),
            },
            "provider": "volc",
            "model": model,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "volc"}




# ── Kimi K2 (Moonshot, api.moonshot.cn, OpenAI-compatible) ──────────

def _call_kimi_api(messages: list, model: str, max_tokens: int, timeout_s: int) -> dict | None:
    """Kimi K2 API (Moonshot), OpenAI 兼容协议.
    Key 存储在 keys/kimi.key 中。默认模型 kimi-k2-0719。
    价格约为 Fable5 的 1/3-1/5。"""
    key_file = ROOT / "keys" / "kimi.key"
    if not key_file.exists():
        return {"ok": False, "error": "kimi.key 不存在", "provider": "kimi"}
    try:
        with open(key_file, "r", encoding="utf-8") as f:
            api_key = f.read().strip()
        if not api_key:
            return {"ok": False, "error": "Kimi API key 未配置", "provider": "kimi"}
        model = model or "kimi-k3"  # kimi3 ~Fable5, 1/3-1/5 price
        api_url = "https://api.moonshot.cn/v1/chat/completions"
        req_data = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(api_url, data=req_data, method="POST")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        choice = result["choices"][0]
        msg = choice.get("message", {})
        content = msg.get("content") or ""
        # Kimi K2/K3 thinking 模型: content 为空时回退 reasoning_content(实际答案在思考字段)
        reasoning = msg.get("reasoning_content") or ""
        if not content.strip() and reasoning:
            content = reasoning
        usage = result.get("usage", {})
        return {
            "ok": True,
            "text": content,
            "tokens": {
                "prompt": usage.get("prompt_tokens", 0),
                "completion": usage.get("completion_tokens", 0),
                "reasoning": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
            },
            "provider": "kimi",
            "model": model,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "kimi"}


# ── 智谱 GLM (open.bigmodel.cn) ──────────────────────────

def _call_zhipu_api(messages: list, model: str, max_tokens: int, timeout_s: int) -> dict | None:
    """智谱 GLM-5.2 API, OpenAI 兼容协议.
    Key 存储在 keys/zhipu_key.txt 中。默认模型 glm-4.5。
    GLM-5.2 适合复杂代码逻辑解析。"""
    key_file = ROOT / "keys" / "zhipu_key.txt"
    if not key_file.exists():
        return {"ok": False, "error": "zhipu_key.txt 不存在", "provider": "zhipu"}
    try:
        with open(key_file, "r", encoding="utf-8") as f:
            api_key = f.read().strip()
        if not api_key:
            return {"ok": False, "error": "Zhipu API key 未配置", "provider": "zhipu"}
        model = model or "glm-4.5"
        api_url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        req_data = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(api_url, data=req_data, method="POST")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        usage = result.get("usage", {})
        return {
            "ok": True,
            "text": content,
            "tokens": {"prompt": usage.get("prompt_tokens", 0), "completion": usage.get("completion_tokens", 0)},
            "provider": "zhipu",
            "model": model,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "zhipu"}


# ── OpenAI GPT (opencode Zen 端点 — OpenAI 协议通道) ─────────

def _call_openai_api(messages: list, model: str, max_tokens: int, timeout_s: int) -> dict | None:
    """OpenAI 兼容协议 GPT 调用 (opencode Zen 端点)

    sublyx 已退役并全面移除, openai 通道重定向到 opencode Zen。支持 GPT-5.x 全系列。
    Key 存储在 keys/openai_config.json 中。

    2026-08-16: 委托给 _call_opencode_api — 旧实现走 /chat/completions 但 zen 上
    gpt 系需 /responses 协议(2026-08-15 实测修复), 直接复用三协议路由避免重复 bug。
    """
    config_file = ROOT / "keys" / "openai_config.json"
    if not config_file.exists():
        return {"ok": False, "error": "openai_config.json 不存在", "provider": "openai"}
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        model = model or config.get("default_chat_model", "gpt-5.6-luna")
        return _call_opencode_api(messages, model, max_tokens, timeout_s, endpoint="base_url")
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "openai"}


# ── Teio Anthropic 中转（复杂代码处理辅助通道） ─────────────

def _call_teio_api(messages: list, model: str, max_tokens: int, timeout_s: int) -> dict | None:
    """Teio Anthropic 中转 API（复杂代码处理的辅助通道，不参与 CLS 核心）

    与 Anthropic 协议兼容: x-api-key + /v1/messages。
    Key 存储在 keys/teio_config.json 中。
    """
    config_file = ROOT / "keys" / "teio_config.json"
    if not config_file.exists():
        return {"ok": False, "error": "teio_config.json 不存在", "provider": "teio"}
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        api_key = config.get("api_key", "")
        if not api_key or api_key == "<your-api-key>":
            return {"ok": False, "error": "Teio API key 未配置", "provider": "teio"}
        base_url = config.get("base_url", "https://teio.me")
        model = model or config.get("default_model", "claude-sonnet-4-6")

        # 转换 messages: OpenAI → Anthropic
        system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_msgs = [m for m in messages if m["role"] != "system"]
        anthropic_msgs = []
        for m in user_msgs:
            role = "user" if m["role"] in ("user", "system") else "assistant"
            anthropic_msgs.append({"role": role, "content": m["content"]})

        api_url = f"{base_url.rstrip('/')}/v1/messages"
        req_data = json.dumps({
            "model": model,
            "max_tokens": max_tokens,
            "messages": anthropic_msgs,
            "system": system_msg if system_msg else None,
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(api_url, data=req_data, method="POST")
        req.add_header("x-api-key", api_key)
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("content-type", "application/json")
        req.add_header("User-Agent", "ClaudeCode/1.0")  # teio.me blocks Python urllib UA

        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        # Anthropic 响应可能有多个 content block (thinking + text)
        content = ""
        thinking = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                content = block.get("text", "")
            elif block.get("type") == "thinking":
                thinking = block.get("thinking", "")
        usage = result.get("usage", {})
        return {
            "ok": bool(content) or bool(thinking),
            "text": content,
            "thinking": thinking if thinking else None,
            "tokens": {
                "prompt": usage.get("input_tokens", 0),
                "completion": usage.get("output_tokens", 0),
            },
            "provider": "teio",
            "model": model,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "teio"}


# ── MiMo 小米多模态（视觉外挂: PPT前端/科研绘图/坐标图识别/论文绘图） ────────────

def _call_mimo_api(messages: list, model: str, max_tokens: int, timeout_s: int) -> dict | None:
    """小米 MiMo API — OpenAI 兼容协议

    mimo-v2.5 = 原生全模态模型(文本+图像+视频+音频输入, 文本输出)。
    官方文档锚定 (mimo.mi.com, 2026-08-16 查证):
      - base_url: https://api.xiaomimimo.com/v1 (按量付费 sk- key, Bearer 认证)
      - 图像: JPEG/PNG/GIF/WebP/BMP ≤50MB, URL 或 base64 data URL, 多图同传
      - content 支持 list 多模态 part ([{"type":"text"},{"type":"image_url",...}])
      - ⚠️ mimo-v2.5-Pro 是纯文本(Agent/编程), 视觉必须用 mimo-v2.5
    Key 在 keys/mimo_config.json。
    """
    config_file = ROOT / "keys" / "mimo_config.json"
    if not config_file.exists():
        return {"ok": False, "error": "mimo_config.json 不存在", "provider": "mimo"}
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        api_key = config.get("api_key", "")
        if not api_key or api_key == "<your-api-key>":
            return {"ok": False, "error": "MiMo API key 未配置", "provider": "mimo"}
        base_url = config.get("base_url", "https://api.xiaomimimo.com/v1")
        model = model or config.get("default_model", "mimo-v2.5")

        api_url = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        req_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(api_url, data=req_data, method="POST")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "ClaudeCode/1.0")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        msg = data["choices"][0]["message"]
        content = msg.get("content", "")
        # mimo-v2.5 是推理模型: max_tokens 太小时思考占满, content 为空 → 回退 reasoning_content
        if not content:
            content = msg.get("reasoning_content", "") or "(思考被截断: max_tokens 过小)"
        usage = data.get("usage", {})
        return {
            "ok": True,
            "text": content if isinstance(content, str) else str(content),
            "tokens": {
                "prompt": usage.get("prompt_tokens", 0),
                "completion": usage.get("completion_tokens", 0),
            },
            "provider": "mimo",
            "model": model,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "mimo"}


# ── Opencode（新 API — 全模型池双协议入口） ────────────

def _call_opencode_api(messages: list, model: str, max_tokens: int, timeout_s: int,
                       # @fix 2026-08-19 二号交付: 默认 local_base_url — zen 云端对 opencode key 401, bridge(go端点)实测 200
                       endpoint: str = "local_base_url",
                       extra_body: dict = None) -> dict | None:
    """Opencode Zen API（本地网关 127.0.0.1:8787 + 云 Zen 双协议入口）

    Zen 端点同时支持 Anthropic + OpenAI 协议，全模型池:
      - claude-* / fable-* → Anthropic 协议 (/v1/messages, x-api-key)
      - gpt-* / gemini-* / grok-* / 其余 → OpenAI 协议 (/v1/chat/completions, Bearer)

    模型优先级按调用方传参，本函数只负责协议路由。Key 在 keys/opencode_config.json。

    Fable 5 refusal 处理: 安全分类器误判时(HTTP 200 + content=[] + stop_reason=refusal),
    自动用自然语气重写提示词重试1次。仍失败则返回错误说明,不静默降级。

    endpoint: "base_url"(云 Zen) | "go_base_url"(云 Go 国内池) | "local_base_url"(本地网关)
    """
    # 2026-08-17: MiMo 模型自动走 Go 池 (Go 池支持 mimo, Zen 不支持)
    if "mimo" in model.lower():
        endpoint = "go_base_url"
    # 2026-08-16: go 池默认禁止(maintainer定), 强制走 zen (MiMo 除外)
    elif endpoint == "go_base_url":
        endpoint = "base_url"
    config_file = ROOT / "keys" / "opencode_config.json"
    if not config_file.exists():
        return {"ok": False, "error": "opencode_config.json 不存在", "provider": "opencode"}
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        base_url = config.get(endpoint) or config.get("base_url", "https://opencode.ai/zen/v1")
        api_key = config.get("api_key", "")
        model = model or config.get("default_model", "claude-fable-5")

        # ── 三 key 简单轮换 (2026-08-16): 三 key 等价 zen 池, 每次调用轮换防限流 ──
        _op_keys = config.get("api_keys") or ([api_key] if api_key else [])
        _op_rotor = [0]

        # ── x-opencode-session (2026-09-07 对齐 cls_api_fallback): zen/go 09/05 起缺头报错 ──
        # 来源优先级: CC会话env → dsh会话env → cog_step窗口ID → 进程uuid
        # @fix 2026-09-10: 原第②级只读 _meta.window_id, 而 cog_step.json 存在顶层 window_id 的
        #   旧 schema(无 _meta) → 该级永远落空 → 每次调用都落到 uuid 兜底, 从而引爆 L961 的
        #   NameError(uuid 当时只在嵌套函数内 import)。改为双 schema 双读 + 补 DSH_SESSION_ID。
        _oc_session = (os.environ.get("CLAUDE_CODE_SESSION_ID")
                       or os.environ.get("CLAUDE_SESSION_ID")
                       or os.environ.get("DSH_SESSION_ID") or "")[:64] or None
        if not _oc_session:
            try:
                _csp = ROOT / "data" / "state" / "cog_step.json"
                _cs = json.loads(_csp.read_text(encoding="utf-8"))
                _wid = (_cs.get("_meta") or {}).get("window_id") or _cs.get("window_id")
                _oc_session = str(_wid)[:64] if _wid else None
            except Exception:
                _oc_session = None
        if not _oc_session:
            _oc_session = uuid.uuid4().hex

        def _next_op_key() -> str:
            if not _op_keys:
                return ""
            k = _op_keys[_op_rotor[0] % len(_op_keys)]
            _op_rotor[0] += 1
            return k

        # ── 路由：Zen 三协议 (2026-08-15 修复, opencode官方文档) ──
        #   gpt-*/grok-*          → /responses      (OpenAI Responses API)
        #   claude-*/fable-*/qwen3* → /messages      (Anthropic Messages API)
        #   deepseek-*/glm-*/kimi-*/minimax-*/*-free → /chat/completions (OpenAI Chat API)
        #   gemini-* → /chat/completions 兜底(本机暂不用, per-model path 未实现)
        lower_model = model.lower()
        is_anthropic = lower_model.startswith(("claude-", "fable-", "qwen3"))
        is_responses = lower_model.startswith(("gpt-", "grok-"))

        def _do_request(req_messages: list) -> dict:
            """执行一次实际请求,返回原始响应 dict"""
            # @fix 2026-09-10: 原此处 `import uuid` 局部导入, 但 _oc_session 兜底在函数之外
            #   (上方 provider 初始化段), 局部导入救不了它 → NameError。uuid 已提到模块顶层 L33。
            key = _next_op_key()
            if not key:
                return {"_error": "opencode api_key 未配置"}
            def _v1_base(url: str) -> str:
                """base_url 可能已含 /v1(Zen端点)也可能不含(本地网关),统一补 /v1 段。"""
                b = url.rstrip("/")
                return b if b.endswith("/v1") else b + "/v1"

            def _post(api_url: str, payload: dict, anthropic_auth: bool = False) -> dict:
                """POST + UA (Cloudflare 指纹拦截 error 1010 防护)。
                anthropic_auth=True → /messages 用 x-api-key (Zen Anthropic 协议要求)"""
                req_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(api_url, data=req_data, method="POST")
                if anthropic_auth:
                    req.add_header("x-api-key", key)
                    req.add_header("anthropic-version", "2023-06-01")
                else:
                    req.add_header("Authorization", f"Bearer {key}")
                req.add_header("Content-Type", "application/json")
                req.add_header("User-Agent", "ClaudeCode/1.0")
                # x-opencode-session (2026-09-07): zen/go 缺头报 MissingSessionID — 对齐 cls_api_fallback
                req.add_header("x-opencode-session", _oc_session)
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            if is_responses:
                # OpenAI Responses API: input 是字符串(文档实测), 响应在 output[] 里
                user_text = "\n".join(
                    m["content"] for m in req_messages if m.get("role") == "user"
                )
                api_url = f"{_v1_base(base_url)}/responses"
                return _post(api_url, {
                    "model": model,
                    "input": user_text,
                    "max_output_tokens": max_tokens,
                })
            elif is_anthropic:
                system_msg = next((m["content"] for m in req_messages if m["role"] == "system"), "")
                user_msgs = [m for m in req_messages if m["role"] != "system"]
                anthropic_msgs = []
                for m in user_msgs:
                    role = "user" if m["role"] in ("user", "system") else "assistant"
                    anthropic_msgs.append({"role": role, "content": m["content"]})
                api_url = f"{_v1_base(base_url)}/messages"
                return _post(api_url, {
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": anthropic_msgs,
                    "system": system_msg if system_msg else None,
                }, anthropic_auth=True)
            else:
                api_url = f"{_v1_base(base_url)}/chat/completions"
                payload = {
                    "model": model,
                    "messages": req_messages,
                    "max_tokens": max_tokens,
                }
                # 推理型模型: 禁用 thinking mode 防推理链吃光 token (2026-08-17 MiMo / 2026-09-10 扩至 deepseek)
                # @fix 2026-09-10: 9 处调用点换用 deepseek-v4-flash 后实测 —— 不关 thinking 时
                #   always_injector(max_tokens=1200) 的 completion_tokens 恰为 1200, JSON 尾部断在
                #   对象中间(解析必失败); 关掉后同一 payload 输出 254 字合法 JSON。
                #   extra_body 紧随其后 update, 故确需 thinking 的调用方可显式覆盖。
                if "mimo" in model.lower() or lower_model.startswith("deepseek"):
                    payload["thinking"] = {"type": "disabled"}
                if extra_body:
                    payload.update(extra_body)  # 2026-08-19 二号交付: thinking disabled 等透传
                return _post(api_url, payload)

        # ── 文本提取 helper (三协议统一) ──
        def _extract_text(resp: dict) -> str:
            """从三种 Zen 协议响应中提取文本。"""
            if is_responses:
                # Responses API: output[] → message.content[].text (type=output_text)
                parts = []
                for out in resp.get("output", []) or []:
                    if out.get("type") != "message":
                        continue
                    for blk in out.get("content", []) or []:
                        if blk.get("type") in ("output_text", "text"):
                            parts.append(blk.get("text", ""))
                return "\n".join(parts).strip()
            elif is_anthropic:
                return "".join(
                    b.get("text", "") for b in resp.get("content", []) or []
                    if b.get("type") == "text"
                ).strip()
            else:
                msg = resp.get("choices", [{}])[0].get("message", {})
                # opencode/deepseek-v4-flash 是推理模型: max_tokens 小→content 空, 兜底 reasoning_content
                return (msg.get("content") or msg.get("reasoning_content") or "").strip()

        def _extract_usage(resp: dict) -> dict:
            if is_responses:
                u = resp.get("usage", {})
                return {"prompt": u.get("input_tokens", 0), "completion": u.get("output_tokens", 0)}
            elif is_anthropic:
                u = resp.get("usage", {})
                return {
                    "prompt": u.get("input_tokens", 0),
                    "completion": u.get("output_tokens", 0),
                    "cache_read": u.get("cache_read_input_tokens", 0),
                }
            else:
                u = resp.get("usage", {})
                return {"prompt": u.get("prompt_tokens", 0), "completion": u.get("completion_tokens", 0)}

        # ── 首次请求 ──
        result = _do_request(messages)

        # ── refusal 检测 + 自动重试 (安全分类器误判时软化提示重试1次) ──
        is_refusal = False
        if is_responses:
            # Responses API: error.refusal 或 output 为空但 status=incomplete
            is_refusal = bool(result.get("error")) or (
                not _extract_text(result) and result.get("status") == "incomplete"
            )
        elif is_anthropic:
            is_refusal = not _extract_text(result) and result.get("stop_reason") == "refusal"
        else:
            choice = result.get("choices", [{}])[0] if result.get("choices") else {}
            finish_reason = choice.get("finish_reason", "")
            is_refusal = finish_reason == "refusal" or (
                finish_reason == "stop" and not choice.get("message", {}).get("content", "").strip()
            )

        if is_refusal:
            import warnings as _w
            _w.warn(f"[opencode] Fable 5 safety classifier triggered (refusal). Retrying with natural prompt...")
            retry_msg = {
                "role": "user",
                "content": "I'm working on a technical project and would like your help. "
                           "Please provide a helpful response to the following:\n\n"
                           + (messages[-1]["content"] if messages else "")
            }
            retry_messages = messages[:-1] + [retry_msg] if len(messages) > 1 else [retry_msg]
            try:
                result = _do_request(retry_messages)
                if _extract_text(result):
                    _w.warn("[opencode] ✅ Fable 5 retry succeeded with natural prompt.")
                else:
                    _w.warn("[opencode] ❌ Fable 5 retry still refused. Consider using gpt-5.5 or opus-4.8.")
            except Exception:
                _w.warn("[opencode] Fable 5 retry also failed.")

        # ── 解析响应 ──
        content = _extract_text(result)
        if is_anthropic:
            thinking = ""
            for block in result.get("content", []):
                if block.get("type") == "thinking":
                    thinking = block.get("thinking", "")
        else:
            thinking = ""
        return {
            "ok": bool(content),
            "text": content,
            "thinking": thinking,
            "refusal_detected": is_refusal,
            "tokens": _extract_usage(result),
            "provider": "opencode",
            "model": model,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "opencode"}




# ── DeepSeek-R1 深度推理 ─────────────────────────────────────

def _call_ollama_reasoning(prompt: str, model: str = "deepseek-r1:8b", timeout_s: int = 120) -> dict | None:
    """调用 DeepSeek-R1 做链式推理（CoT）。

    R1 通过 <think> 标签输出推理过程，适合数学/逻辑/多步分析。
    专有参数（来自 DeepSeek 官方推荐）:
      - temperature=0.6（R1 专用，非默认值）
      - top_p=0.95
      - 不传 system prompt（R1 不需要）
      - 不给 few-shot（降低 R1 推理质量）
      - num_ctx=2048（8GB VRAM 最优值）
    """
    import urllib.request, re

    model = model or "deepseek-r1:8b"
    base_url = os.environ.get("OLLAMA_REMOTE_URL", "http://100.101.150.25:11434")
    # ⚠️ 这是【二号机 Tailscale】地址 —— 二号机不在线就连接超时。
    #    本机 Ollama(跑着 ep 全系列) 请用 provider="local_gpu"(同文件 OLLAMA_URL) 或直连 127.0.0.1。
    #    实测 2026-09-14: 本机 12 个模型可用, 但本 provider 默认走二号机 → 看起来像"Ollama 不可靠", 实为地址指错机器。

    try:
        url = f"{base_url}/api/generate"
        req_data = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.6,
                "top_p": 0.95,
                "num_ctx": 2048,
            },
            "keep_alive": "0s",  # 🔴 用完即走，不占VRAM
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url, data=req_data, method="POST")
        req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        text = result.get("response", "")
        eval_count = result.get("eval_count", 0)

        # 提取 <think> 块中的推理过程（旧版 R1 格式）
        think_match = re.search(r'<think>(.*?)</think>', text, re.DOTALL)
        if think_match:
            thinking = think_match.group(1).strip()
            final_answer = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
        else:
            # 新版 R1/Hybrid: 不使用 <think> 标签，全文本即推理+答案
            thinking = text
            final_answer = text

        return {
            "ok": bool(text),
            "text": text,
            "thinking": thinking,
            "final_answer": final_answer or text,
            "tokens": {"prompt": 0, "completion": eval_count},
            "provider": "ollama",
            "model": model,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "provider": "ollama", "model": model}


def call_reasoning_chain(prompt: str, max_tokens: int = 2048) -> dict | None:
    """双阶段推理链：DeepSeek-R1 慢思考 → Qwen3 验证与格式化。

    用于复杂问题（数学/代码/逻辑/多步分析）:
      Stage 1: R1 产生 CoT 推理链 (<think>...)
      Stage 2: 推理链 + 原始问题 → Qwen3:14b → 最终简洁答案

    VRAM 约束：R1(~5GB) 和 Qwen3(~7GB) 不同时驻显存，
    Ollama 自动顺序加载，每次交换约 2-5s。

    注意: R1 与标准 LLM 不同 — 不给 system prompt, 不给 few-shot, temperature=0.6。
    """
    print(f"\n{'='*50}")
    print(f"  REASONING CHAIN: deepseek-r1:8b -> qwen3:32b")
    print(f"{'='*50}")

    # ── Stage 1: R1 深度推理 ──
    # 注意: R1 会自动对复杂问题进行链式推理，无需显式 CoT 提示
    # 不给 system prompt, 不给 few-shot — 这两项反而会降低 R1 的推理质量
    print(f"  [Stage 1] R1 推理中...")
    r1 = _call_ollama_reasoning(prompt)
    if not r1 or not r1.get("ok"):
        err = r1.get("error", "R1 不可用") if r1 else "R1 无响应"
        print(f"  [Stage 1] R1 FAIL: {err}")
        print(f"  [Stage 1] 降级到 Qwen3 直接推理")
        return call("ollama", "qwen3:32b", prompt=prompt, max_tokens=max_tokens, temperature=0.2)

    # 打印推理摘要
    think_len = len(r1.get("thinking", ""))
    print(f"  [Stage 1] R1 OK — 推理链 {think_len} 字符")

    # ── Stage 2: Qwen3 验证与格式化成最终答案 ──
    print(f"  [Stage 2] Qwen3 验证中...")
    thinking_trace = r1.get("thinking", r1["text"])
    verify_prompt = (
        f"## 原始问题\n{prompt}\n\n"
        f"## 推理过程\n{thinking_trace}\n\n"
        f"基于以上推理，给出简洁准确的最终答案。"
    )
    qwen = call("ollama", "qwen3:32b", prompt=verify_prompt,
                max_tokens=max_tokens, temperature=0.2, timeout_s=180)  # 来源: DeepSeek推理链默认温度(官方SDK建议)

    if qwen and qwen.get("ok"):
        print(f"  [Stage 2] Qwen3 OK")
    else:
        err = qwen.get("error", "") if qwen else "Qwen3 无响应"
        print(f"  [Stage 2] Qwen3 FAIL: {err}")
        # 降级：直接返回 R1 的最终答案
        return {
            "ok": r1["ok"],
            "text": r1.get("final_answer", r1["text"]),
            "reasoning_trace": r1.get("thinking", ""),
            "tokens": r1.get("tokens", {"completion": 0}),
            "provider": "reasoning_chain",
            "model": "deepseek-r1:8b -> (qwen3:32b FAIL)",
        }

    return {
        "ok": True,
        "text": qwen["text"],
        "reasoning_trace": thinking_trace,
        "tokens": {
            "reasoning": r1.get("tokens", {}).get("completion", 0),
            "verification": qwen.get("tokens", {}).get("completion", 0),
        },
        "provider": "reasoning_chain",
        "model": "deepseek-r1:8b -> qwen3:32b",
    }


# ── 工具函数 ─────────────────────────────────────────────────────

_seq = 0

def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def _short_id() -> str:
    import random
    return ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=6))


def _print_prompt_block(provider: str, model: str, prompt_full: str, icon: str, color: str, max_chars: int = 1500):
    """打印完整的蓝色提示词框图，替代原截断预览"""
    import shutil
    term_width = shutil.get_terminal_size((100, 20)).columns
    box_w = min(term_width - 2, 120)
    title = f" API CALL #{_next_seq() - 1} {icon} {provider}/{model or 'default'} "
    # ── 框顶 ──
    print(f"\n{color}╔{'═' * (box_w - 2)}╗{C['reset']}")
    # ── 标题行，居中对齐 ──
    header = title.center(box_w - 2)
    print(f"{color}║{C['bold']}{header}{C['reset']}{color}║{C['reset']}")
    print(f"{color}╠{'═' * (box_w - 2)}╣{C['reset']}")
    # ── 提示词内容 ──
    lines = prompt_full.split('\n')
    shown = 0
    for line in lines:
        # 每行换行显示，避免自动换行破坏框
        while len(line) > (box_w - 4):
            print(f"{color}║ {C['dim']}{line[:box_w - 4]}{C['reset']}{color} ║{C['reset']}")
            line = line[box_w - 4:]
            shown += box_w - 4
            if shown > max_chars:
                print(f"{color}║ {C['dim']}...（提示词过长，截断显示）{C['reset']}{color} ║{C['reset']}")
                break
        if shown > max_chars:
            break
        print(f"{color}║ {C['dim']}{line}{' ' * (box_w - 3 - len(line))}{C['reset']}{color}║{C['reset']}")
        shown += len(line)
    # ── 框底 ──
    print(f"{color}╚{'═' * (box_w - 2)}╝{C['reset']}", flush=True)


def _preview(text: str, max_len: int = 80) -> str:
    """截短文本用于显示"""
    if not text:
        return "(空)"
    t = text.replace("\n", " ").replace("\r", "").strip()
    if len(t) > max_len:
        return t[:max_len] + "…"
    return t


def _msgs_preview(messages: list) -> str:
    """从messages提取预览"""
    if not messages:
        return "(空)"
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if user_msgs:
        return _preview(user_msgs[-1].get("content", ""), 80)
    return _preview(messages[-1].get("content", ""), 80)


def _msgs_to_prompt(messages: list) -> str:
    """messages列表→拼接为prompt字符串（支持多模态list content）。"""
    if not messages:
        return ""
    parts = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            # 多模态消息: 提取所有text段合并
            text_segments = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_segments.append(item.get("text", ""))
                elif isinstance(item, dict) and item.get("type") == "image_url":
                    text_segments.append("[图片]")
                else:
                    text_segments.append(str(item))
            parts.append(" | ".join(text_segments))
        else:
            parts.append(str(content))
    return "\n\n".join(parts)


def _append_log(entry: dict):
    """追加结构化日志。内部函数，外部请用 append_log()。"""
    try:
        with open(CALLS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def append_log(entry: dict):
    """公开接口：追加一条日志到 api_calls.jsonl。

    供 small_model / inference_router 等外部模块调用，
    确保所有写入走同一路径，避免并发竞争。
    """
    _append_log(entry)


def _update_monitor(data: dict):
    """更新监视文件（watch面板读取）"""
    try:
        MONITOR_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── 公开接口 ──────────────────────────────────────────────────────

def status() -> dict:
    """统计最近API调用"""
    calls = []
    try:
        if CALLS_LOG.exists():
            with open(CALLS_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        calls.append(json.loads(line))
    except Exception:
        pass

    recent = calls[-20:]
    by_provider = {}
    for c in recent:
        p = c.get("provider", "unknown")
        by_provider.setdefault(p, {"total": 0, "ok": 0, "fail": 0, "total_ms": 0})
        by_provider[p]["total"] += 1
        if c.get("ok"):
            by_provider[p]["ok"] += 1
        else:
            by_provider[p]["fail"] += 1
        by_provider[p]["total_ms"] += c.get("elapsed_ms", 0)

    return {
        "total_calls": len(calls),
        "recent_20": len(recent),
        "by_provider": {
            p: {
                "total": d["total"],
                "ok": d["ok"],
                "fail": d["fail"],
                "avg_ms": round(d["total_ms"] / max(d["total"], 1)),
            }
            for p, d in sorted(by_provider.items())
        },
        "last_call": recent[-1] if recent else None,
    }


def watch(refresh_s: float = 1.0):
    """监视面板 — 实时显示API调用（用户开终端盯着）。Ctrl+C退出。"""
    import shutil
    print(f"{C['bold']}{C['cyan']}╔══════════════════════════════════════════════╗{C['reset']}")
    print(f"{C['bold']}{C['cyan']}║   🔍 API Pipeline 实时监视                    ║{C['reset']}")
    print(f"{C['bold']}{C['cyan']}║   所有外部API调用在此可见 | Ctrl+C 退出        ║{C['reset']}")
    print(f"{C['bold']}{C['cyan']}╚══════════════════════════════════════════════╝{C['reset']}")
    print()

    last_seen_seq = 0
    last_mtime = 0

    try:
        while True:
            # 检查监视文件
            if MONITOR_FILE.exists():
                mtime = MONITOR_FILE.stat().st_mtime
                if mtime > last_mtime:
                    last_mtime = mtime
                    try:
                        data = json.loads(MONITOR_FILE.read_text(encoding="utf-8"))
                        status_val = data.get("status", "?")
                        provider = data.get("provider", "?")
                        color = PROVIDER_COLORS.get(provider, C["white"])
                        icon = PROVIDER_ICONS.get(provider, "?")

                        if status_val == "calling":
                            print(f"{color}{icon} [{provider}] 调用中... {data.get('prompt_preview', '')[:80]}{C['reset']}")
                        elif status_val == "done":
                            print(f"{C['green']}✓{C['reset']} {icon} [{provider}] {data.get('elapsed_ms', '?')}ms — {data.get('prompt_preview', '')[:80]}")
                        elif status_val == "failed":
                            print(f"{C['red']}x{C['reset']} {icon} [{provider}] FAIL {data.get('elapsed_ms', '?')}ms — {data.get('error', '?')[:80]}")
                    except Exception:
                        pass

            # 检查日志文件尾部
            if CALLS_LOG.exists():
                try:
                    with open(CALLS_LOG, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    for line in lines[last_seen_seq:]:
                        line = line.strip()
                        if line:
                            last_seen_seq = max(last_seen_seq, len(lines))
                except Exception:
                    pass

            time.sleep(refresh_s)

            # 终端宽度自适应显示
            term_w = shutil.get_terminal_size((120, 40)).columns
            if term_w < 80:
                print(f"{C['dim']}── 窗口太窄({term_w}列), 建议>=80列 ──{C['reset']}")

    except KeyboardInterrupt:
        print(f"\n{C['dim']}监视结束。共记录 {last_seen_seq} 条API调用。{C['reset']}")


# ── CLI ──────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="统一API管线 — 单一入口, 实时可见")
    parser.add_argument("--watch", action="store_true", help="实时监视面板（用户开终端盯着）")
    parser.add_argument("--status", action="store_true", help="查看API调用统计")
    parser.add_argument("--prompt", type=str, default="", help="直接调用 DeepSeek V4 Pro 做深度推理（prompt 文本或留空从 stdin 读）")
    parser.add_argument("--system", type=str, default="", help="--prompt 模式下附加 system prompt")
    parser.add_argument("--test", type=str, default="", choices=["local", "qwen", "anthropic", "ollama", "teio", "deepseek"],
                        help="测试调用")
    args = parser.parse_args()

    if args.watch:
        watch()
        return

    if args.status:
        s = status()
        print(f"总调用: {s['total_calls']} | 最近20条: {s['recent_20']}")
        for p, d in s["by_provider"].items():
            ok_rate = round(d["ok"] / max(d["total"], 1) * 100)
            print(f"  {PROVIDER_ICONS.get(p, '?')} {p}: {d['total']}次 | OK:{d['ok']} FAIL:{d['fail']} | 平均{d['avg_ms']}ms | 成功率{ok_rate}%")
        if s["last_call"]:
            lc = s["last_call"]
            print(f"\n最近: [{lc.get('provider')}] {lc.get('ok','?')} | {lc.get('elapsed_ms')}ms | {lc.get('prompt_preview','')[:60]}")
        return

    if args.test:
        if args.test == "local":
            r = call("local_gpu", "qwen2.5-1.5b", prompt="用一句话回答: 1+1等于几?")
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif args.test == "qwen":
            r = call("qwen", "qwen-plus", messages=[{"role":"user","content":"用一句话回答: 1+1等于几?"}])
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif args.test == "anthropic":
            r = call("anthropic", "", messages=[{"role":"user","content":"用一句话回答: hello world"}])
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif args.test == "teio":
            r = call("teio", "", messages=[{"role":"user","content":"用一句话回答: 1+1等于几?"}])
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif args.test == "deepseek":
            r = call("opencode", "deepseek-v4-flash", messages=[{"role":"user","content":"用一句话解释量子隧穿效应"}], auto_route=False)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    if args.prompt or not sys.stdin.isatty():
        prompt = args.prompt or sys.stdin.read().strip()
        if prompt:
            messages = []
            if args.system:
                messages.append({"role": "system", "content": args.system})
            messages.append({"role": "user", "content": prompt})
            result = call("opencode", "deepseek-v4-flash", messages=messages, max_tokens=32000, auto_route=False)
            if result and result.get("ok"):
                print(result["text"])
            else:
                err = result.get("error", "调用失败") if result else "无返回"
                print(f"\n错误: {err}", file=sys.stderr)
            return

    parser.print_help()


if __name__ == "__main__":
    main()
