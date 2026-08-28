#!/usr/bin/env python3
"""
qwen_gate.py — 双AI闸门  (轮子, 纯硬编码)  |  v3.0 健康感知+Anthropic后备

统计学降幻觉核心机制:
  DeepSeek(我) 做创造 → Qwen(独立模型) 做验证
  两个独立模型在同一问题上的推理, 同时幻觉的概率 ≈ p(DS错) × p(QW错)
  以10%幻觉率计: 0.1 × 0.1 = 0.01 = 1% 的双重幻觉率

三段式闸门架构 (v2.0):
  1. verify_cad_design()    — Design Check：设计完整性与几何合理性
  2. verify_knowledge()     — Knowledge Check：知识声明的一致性与可复现性
  3. verify_numerical()     — Numeric Check：数值计算独立验算与量级校验 (NEW)
     └─ gate_numerical_if_needed() — 条件触发，审计数据驱动的触发规则

触发条件（硬编码，来自 model_audit 审计数据 2026-06-05）:
  - 写入知识库 → 强制验证
  - 会话 > 60k tokens → 强制验证（EC-T2衰减点）
  - 材料参数 + 数值计算 → 强制验证（AS-T3+CR-T2复合风险）
  - 30-60k + 数值计算 → 预防性验证

架构:
  本闸门不参与思考——只做三件事:
    1. 提取可验证声明 (claims) 从输出
    2. 调用 Qwen API 独立验证
    3. 返回 pass/fail 裁决

  熔断板是最后一道防线:
    qwen_gate 是主动调用, fuse_board.DUAL_AI_GATE 是被动熔断

用法:
    from scripts.core_engine.qwen_gate import (verify_cad_design, verify_knowledge,
                                                verify_numerical, gate_numerical_if_needed)
    # CAD验证
    result = verify_cad_design("同轴支撑管", "空心管+4支撑环...", params_dict)
    # 数值验证（条件触发）
    result = gate_numerical_if_needed(output_text, context_tokens=75000)
    if result["verdict"] == "fail":
        print(f"[gate] BLOCKED: {result['reasons']}")
"""
import json, os, sys, time, re
from pathlib import Path
from datetime import datetime, timezone

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# ── 统一API管线（所有外部调用走此轮子，过程实时可见）──
from scripts.core_engine.api_pipeline import call as _api_call

# ── 路径 ──
GATE_LOG = _PROJECT_ROOT / "data" / "safety" / "qwen_gate_log.jsonl"
QWEN_HEALTH_PATH = _PROJECT_ROOT / "data" / "safety" / "qwen_health.json"
ANTHROPIC_CONFIG = _PROJECT_ROOT / "keys" / "anthropic_config.json"

# ── 健康阈值 ──
QWEN_DEGRADED_MINUTES = 10     # >10min 连续失败 → 通知
QWEN_FUSE_MINUTES = 30          # >30min 连续失败 → 熔断
RECOVERY_DUAL_VERIFY_MINUTES = 30  # Qwen恢复后双重验证窗口


def _log_gate(entry: dict):
    """追加一条闸门日志。"""
    GATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry["_timestamp"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(GATE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志写入不能崩


# ════════════════════════════════════════════════════════════════
# Qwen 健康追踪（2026-06-05新增）
# 目的：检测 Qwen 连续不可用时间，触发通知 / 熔断 / 恢复双重验证
# ════════════════════════════════════════════════════════════════

def _read_qwen_health() -> dict:
    """读取 Qwen 健康状态。文件不存在时返回默认健康状态。"""
    if not QWEN_HEALTH_PATH.exists():
        return {
            "status": "healthy",
            "consecutive_failures": 0,
            "first_failure_at": None,
            "total_downtime_minutes": 0,
            "last_success_at": None,
            "last_recovery_at": None,
        }
    try:
        with open(QWEN_HEALTH_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"status": "unknown", "consecutive_failures": 0}


def _track_qwen_health(success: bool) -> dict:
    """追踪每次 Qwen 调用成败，更新健康状态并写入磁盘。

    状态机:
      healthy   → unstable (连续失败但 <10min)
      unstable  → degraded (连续失败 >10min, 发通知)
      degraded  → fuse     (连续失败 >30min, 阻断需验证产出)
      fuse      → recovering (一次成功后进入恢复窗口)
      recovering → healthy  (30min 内连续成功, 离开恢复窗口)
    """
    now = datetime.now(timezone.utc)
    health = _read_qwen_health()

    if success:
        prev_status = health.get("status", "healthy")
        health["consecutive_failures"] = 0
        health["last_success_at"] = now.isoformat()

        if prev_status in ("degraded", "fuse"):
            # Qwen 刚从离线恢复 → 进入双重验证窗口
            health["status"] = "recovering"
            health["last_recovery_at"] = now.isoformat()
        elif prev_status == "recovering":
            # 检查恢复窗口是否结束
            recovery_at = health.get("last_recovery_at")
            if recovery_at:
                try:
                    recovery_dt = datetime.fromisoformat(recovery_at)
                    if (now - recovery_dt).total_seconds() / 60 > RECOVERY_DUAL_VERIFY_MINUTES:
                        health["status"] = "healthy"
                        health["last_recovery_at"] = None
                except Exception:
                    health["status"] = "healthy"
            else:
                health["status"] = "healthy"
        else:
            health["status"] = "healthy"
    else:
        health["consecutive_failures"] += 1
        if health["first_failure_at"] is None:
            health["first_failure_at"] = now.isoformat()

        # 计算连续不可用时长
        try:
            first_fail = datetime.fromisoformat(health["first_failure_at"])
            downtime = (now - first_fail).total_seconds() / 60
            health["total_downtime_minutes"] = round(downtime, 1)
        except Exception:
            downtime = 0

        if downtime > QWEN_FUSE_MINUTES:
            health["status"] = "fuse"
        elif downtime > QWEN_DEGRADED_MINUTES:
            health["status"] = "degraded"
        else:
            health["status"] = "unstable"

    # 写入磁盘
    QWEN_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(QWEN_HEALTH_PATH, "w", encoding="utf-8") as f:
            json.dump(health, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return health


# ════════════════════════════════════════════════════════════════
# Anthropic API 审计后备（2026-06-05新增）
# 目的：Qwen 不可用时自动接管验证，维持24h知识循环不中断
# ════════════════════════════════════════════════════════════════

def _call_anthropic_verify(messages: list) -> dict | None:
    """调用 Anthropic API 作为后备审计。通过 api_pipeline 统一管线（实时可见）。

    只在 Qwen 不可用时调用。返回同 _call_qwen_structured() 格式，
    或 None 表示 Anthropic 也不可用。
    """
    if not ANTHROPIC_CONFIG.exists():
        return None
    try:
        with open(ANTHROPIC_CONFIG, "r", encoding="utf-8") as f:
            config = json.load(f)
        # API key loaded from external config file (not in repo)
        api_key = config.get("api_key", "")
        if not api_key or api_key.startswith("<"):
            return None
        model = config.get("default_model", "{FALLBACK_MODEL}")
    except Exception:
        return None

    result = _api_call("anthropic", model, messages=messages, max_tokens=256, temperature=0.1)  # 来源:LLM参数(256 tokens, 0.1温度)
    if not result or not result.get("ok"):
        _log_gate({"event": "anthropic_fallback_failed", "error": result.get("error", "") if result else "无响应"})
        return None

    content = result["text"]
    parsed = _parse_qwen_json(content)
    if parsed:
        parsed["_source"] = "anthropic_haiku"
        return parsed

    return {
        "verdict": "flag",
        "reasons": [f"Anthropic 返回非结构化: {content[:150]}"],
        "confidence": 0.5,
        "_source": "anthropic_haiku",
        "raw_response": content[:200],
    }


def _call_local_qwen(messages: list) -> dict | None:
    """尝试调用本地 Qwen 模型 (Ollama Qwen3-VL 或 api_pipeline local_gpu)。

    v2 (2026-06-22): 优先 Ollama Qwen3-VL-4B, 降级到 api_pipeline local_gpu qwen2.5-1.5b。
    """
    # ── 优先: Ollama Qwen3-VL (用于本地验证) ──
    try:
        import ollama
        installed = ollama.list()
        for m in installed.models:
            pass  # any qwen model works

        # 尝试用 Qwen3-VL 做纯文本推理 (ollama v0.6+: .models, .model, .message.content)
        system_prompt = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_prompt = "\n".join(m["content"] for m in messages if m["role"] == "user")

        # 检查是否安装了 qwen3 模型
        qwen3_models = [m.model for m in installed.models
                        if "qwen3" in m.model]
        if not qwen3_models:
            raise RuntimeError("无 Ollama Qwen3 模型")

        local_model = qwen3_models[0]  # 取第一个 Qwen3 模型
        response = ollama.chat(
            model=local_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.1, "num_predict": 1024},  # 来源: 闸门验证低温保确定性
        )
        content = response.message.content or ""
        if content:
            return {"choices": [{"message": {"content": content}}], "local": True, "backend": "ollama_qwen3"}
    except Exception as e:
        print(f"  [qwen_gate] Ollama 本地不可用 ({e}), 降级 api_pipeline local_gpu")

    # ── 降级: api_pipeline local_gpu (qwen2.5-1.5b) ──
    system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
    prompt = f"{system_msg}\n\n{user_msg}" if system_msg else user_msg
    prompt += "\n\n回复 JSON (只有一行JSON, 无多余文字):"

    result = _api_call("local_gpu", "qwen2.5-1.5b", prompt=prompt, max_tokens=1024, temperature=0.1)  # 来源:LLM参数
    if result and result.get("ok"):
        return {"choices": [{"message": {"content": result["text"]}}], "local": True, "backend": "local_gpu"}
    return None


def _call_qwen_api(messages: list, max_retries: int = 1) -> dict:
    """调用远端 Qwen API 做结构化验证。通过 api_pipeline 统一管线（实时可见）。"""
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            result = _api_call("qwen", "qwen-max", messages=messages, max_tokens=1024, temperature=0.1)  # 来源:LLM参数
            if result and result.get("ok"):
                content = result["text"]
                parsed = _parse_qwen_json(content)
                if parsed:
                    return parsed
                # JSON解析失败，继续retry
                last_err = f"Qwen返回非JSON: {content[:80]}"
            else:
                last_err = result.get("error", "API调用失败") if result else "无响应"
        except Exception as e:
            last_err = str(e)
        time.sleep(1)

    return {
        "verdict": "flag",
        "reasons": [f"Qwen API 调用失败: {last_err}"],
        "confidence": 0.0,
        "raw_response": f"API_ERROR: {last_err}",
    }


def _parse_qwen_json(content: str) -> dict | None:
    """从 Qwen 回复中提取 JSON。兼容 markdown 包裹和多余自然语言。"""
    cleaned = content.strip()

    # 1) 提取 ```json ... ``` 块
    if "```json" in cleaned:
        cleaned = cleaned.split("```json")[1].split("```")[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```")[1].split("```")[0].strip()

    # 2) 尝试直接解析
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # 3) 找第一个 { 到最后一个 } 之间的内容（处理 JSON 后有多余自然语言）
        brace_start = cleaned.find("{")
        brace_end = cleaned.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            candidate = cleaned[brace_start:brace_end + 1]
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                return None
        else:
            return None

    verdict = parsed.get("verdict", "flag")
    reasons = parsed.get("reasons", ["Qwen 未给出具体理由"])
    confidence = parsed.get("confidence", 0.5)
    if verdict not in ("pass", "fail", "flag"):
        verdict = "flag"
    return {"verdict": verdict, "reasons": reasons, "confidence": confidence, "raw_response": content[:200]}


def _call_qwen_structured(verify_prompt: str, max_retries: int = 1) -> dict:
    """调用 Qwen 做结构化验证。返回 {"verdict","reasons","confidence"}。

    路由策略（v3.0 健康感知）:
      本地 GPU → 远端 API → Anthropic Haiku 后备
      恢复期：Qwen + Anthropic 双重验证，不一致降级为 flag
      熔断态：跳过 Qwen，直接 Anthropic

    返回必含 _source 字段: local_gpu | remote_api | anthropic_haiku | unavailable
    """
    system_prompt = (
        "你是知识与设计一致性独立验证器。你的职责是对给定的声明/设计做独立验证。\n"
        "规则:\n"
        "1. 独立核实所有声明——不信任被验证方的任何中间结论\n"
        "2. 检查逻辑一致性——声称的解决方案真的能解决声称的问题吗？\n"
        "3. 检查遗漏——有没有明显没考虑到的风险或边界条件？\n"
        "4. 检查前提假设——声明依赖的隐含前提是否合理？\n"
        "5. 特别警惕：把'期望'说成'已实现'\n"
        "6. 特别警惕：没有来源支撑的经验性声明\n"
        "7. 用 JSON 格式回复, 只有一行 JSON, 不要多余文字\n"
        "8. JSON 格式: {\"verdict\": \"pass\"|\"fail\"|\"flag\", \"reasons\": [\"理由1\", ...], \"confidence\": 0.0-1.0}\n"
        "   - pass: 声明合理，逻辑严谨，风险可控\n"
        "   - fail: 存在逻辑错误、事实错误或致命遗漏，必须修正\n"
        "   - flag: 有可疑点但无法确定（如缺少足够信息），需人工复核\n"
        "   - confidence: 你对自己判断的信心度"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": verify_prompt},
    ]

    health = _read_qwen_health()
    in_fuse = health.get("status") == "fuse"
    in_recovery = health.get("status") == "recovering"

    qwen_result = None

    # ── 阶段一：尝试 Qwen（熔断态跳过） ──
    if not in_fuse:
        # 1a) 远端 API (优先 — 能力最强)
        api_result = _call_qwen_api(messages, max_retries=max_retries)
        if "API_ERROR" not in api_result.get("raw_response", ""):
            api_result["_source"] = "remote_api"
            qwen_result = api_result

        # 1b) 本地 GPU (兜底 — API不可用时)
        if qwen_result is None:
            local_result = _call_local_qwen(messages)
            if local_result:
                content = local_result["choices"][0]["message"]["content"]
                parsed = _parse_qwen_json(content)
                if parsed:
                    parsed["_source"] = "local_gpu"
                    qwen_result = parsed
                else:
                    print(f"[qwen_gate] 本地 GPU 返回非 JSON 格式: {content[:100]}")

    # ── 阶段二：健康追踪 ──
    qwen_ok = qwen_result is not None and qwen_result.get("verdict") in ("pass", "fail", "flag")
    health = _track_qwen_health(qwen_ok)

    # ── 阶段三：决定最终产出 ──

    # 3a) Qwen 成功 + 恢复期 → 双重验证
    if qwen_ok and (in_recovery or health.get("status") == "recovering"):
        a_result = _call_anthropic_verify(messages)
        if a_result and a_result.get("verdict"):
            qwen_v = qwen_result["verdict"]
            anthro_v = a_result["verdict"]
            if qwen_v == anthro_v:
                qwen_result["_auditor"] = "qwen+anthropic"
                qwen_result["_double_verified"] = True
                qwen_result["_anthropic_verdict"] = anthro_v
            else:
                # 不一致 → 降级为 flag
                qwen_result["verdict"] = "flag"
                qwen_result["reasons"] = list(qwen_result.get("reasons", [])) + [
                    f"恢复期双重验证不一致: Qwen={qwen_v}, Anthropic={anthro_v}"
                ]
                qwen_result["_auditor"] = "qwen+anthropic"
                qwen_result["_double_verified"] = "conflict"
                qwen_result["_anthropic_verdict"] = anthro_v
                qwen_result["confidence"] = min(qwen_result.get("confidence", 0.5), 0.3)
        return qwen_result

    # 3b) Qwen 成功 + 非恢复期 → 直接返回
    if qwen_ok:
        return qwen_result

    # 3c) Qwen 失败 → Anthropic 后备
    anthropic_result = _call_anthropic_verify(messages)
    if anthropic_result and anthropic_result.get("verdict"):
        _log_gate({"event": "anthropic_fallback_used",
                    "health_status": health.get("status"),
                    "downtime_minutes": health.get("total_downtime_minutes", 0)})
        return anthropic_result

    # 3d) 全部不可用 → flag + unavailable
    if qwen_result and "API_ERROR" in qwen_result.get("raw_response", ""):
        qwen_result["_source"] = "unavailable"
        qwen_result["reasons"] = ["Qwen 不可用，Anthropic 后备也不可用——防线裸奔"]
        return qwen_result

    return {
        "verdict": "flag",
        "reasons": ["Qwen+Anthropic 均不可用，无法验证"],
        "confidence": 0.0,
        "gate_status": "unavailable",
        "_source": "unavailable",
    }
def _audit_print(design_name: str, verdict: str, reasons: list[str],
                  confidence: float, elapsed_s: float, gate_type: str):
    """硬编码审计提醒 — 每次闸门调用必打印，不可关闭。"""
    icon = {"pass": "[PASS]", "fail": "[FAIL]", "flag": "[FLAG]"}
    v_icon = icon.get(verdict, "[?]")
    print(f"\n{'='*60}")
    print(f"  QW AUDIT | {gate_type} | {v_icon} | {design_name}")
    print(f"{'='*60}")
    print(f"  confidence={confidence:.1f}  time={elapsed_s:.1f}s")
    for i, r in enumerate(reasons[:3], 1):
        r_short = r[:120] + ("..." if len(r) > 120 else "")
        print(f"  reason{i}: {r_short}")
    print(f"{'='*60}\n")


def _extract_summary(description: str, params: dict, max_len: int = 150) -> str:
    """截短描述+参数为摘要, 不冲 Qwen 上下文"""
    s = description[:max_len]
    param_items = [f"{k}={v}" for k, v in params.items() if isinstance(v, (int, float))]
    if param_items:
        s += " | " + ", ".join(param_items[:8])
    return s[:400]


# ════════════════════════════════════════════════════════════════
# 公开接口
# ════════════════════════════════════════════════════════════════


def verify_cad_design(
    design_name: str,
    description: str,
    params: dict,
    patterns: list[str] | None = None,
    timeout_s: int = 30,     # Qwen API 超时 (秒)，来源:经验值，30s 足够大部分推理请求
) -> dict:
    """CAD 设计验证闸门。

    参数:
        design_name: 设计名称
        description: 设计描述 (功能意图, 关键几何)
        params: 设计参数字典
        patterns: 使用的 CAD 模式列表

    返回:
        {"verdict": "pass"|"fail"|"flag", "reasons": [...], "confidence": 0-1,
         "gate_status": "ok"|"unavailable", "cached": bool}
    """
    patterns_str = ", ".join(patterns) if patterns else "无预定义模式"

    prompt = (
        f"【CAD 设计验证】\n"
        f"设计名称: {design_name}\n"
        f"功能描述: {description}\n"
        f"使用模式: {patterns_str}\n\n"
        f"设计参数:\n"
    )
    for k, v in params.items():
        prompt += f"  {k} = {v}\n"

    prompt += (
        "\n评估要求:\n"
        "1. 这些参数的数值在物理上合理吗？(过大/过小/比例失调?)\n"
        "2. 参数之间的关系一致吗？(内径<外径, 孔间距不干涉等)\n"
        "3. 对于声明的功能, 这个参数配置合理吗？\n\n"
        "回复 JSON (只有一行JSON, 无多余文字):"
    )

    start = time.time()
    gate_result = _call_qwen_structured(prompt)
    elapsed = time.time() - start

    gate_status = "ok" if "API_ERROR" not in gate_result.get("raw_response", "") else "unavailable"

    entry = {
        "gate_type": "verify_cad_design",
        "design_name": design_name,
        "description_summary": _extract_summary(description, params),
        "patterns": patterns,
        "verdict": gate_result["verdict"],
        "reasons": gate_result["reasons"],
        "confidence": gate_result["confidence"],
        "elapsed_s": round(elapsed, 2),
        "gate_status": gate_status,
    }
    _log_gate(entry)

    gate_result["gate_status"] = gate_status
    gate_result["elapsed_s"] = round(elapsed, 2)

    # 硬编码审计提醒
    _audit_print(design_name, gate_result["verdict"], gate_result["reasons"],
                 gate_result["confidence"], elapsed, "CAD_DESIGN")

    return gate_result


def verify_knowledge(
    claim: str,
    problem: str,
    solution: str,
    source: str = "",
) -> dict:
    """知识声明验证闸门。

    参数:
        claim: 知识陈述
        problem: 此知识解决的具体问题
        solution: 解决方法的描述
        source: 知识来源

    返回:
        {"verdict": "pass"|"fail"|"flag", "reasons": [...], "confidence": 0-1}
    """
    prompt = (
        f"【知识声明验证】\n\n"
        f"声明: {claim}\n"
        f"解决的问题: {problem}\n"
        f"解决办法: {solution}\n"
        f"来源: {source or '未知'}\n\n"
        f"评估要求:\n"
        f"1. 这个声明描述的是一个具体的、可复现的问题吗?\n"
        f"2. 解决方法明确且可执行吗?\n"
        f"3. 声明中是否有明显的事实错误或逻辑矛盾?\n\n"
        f"回复 JSON (只有一行JSON, 无多余文字):"
    )

    start = time.time()
    gate_result = _call_qwen_structured(prompt)
    elapsed = time.time() - start

    gate_status = "ok" if "API_ERROR" not in gate_result.get("raw_response", "") else "unavailable"

    entry = {
        "gate_type": "verify_knowledge",
        "claim_summary": claim[:200],
        "verdict": gate_result["verdict"],
        "reasons": gate_result["reasons"][:3],
        "confidence": gate_result["confidence"],
        "elapsed_s": round(elapsed, 2),
        "gate_status": gate_status,
    }
    _log_gate(entry)

    gate_result["gate_status"] = gate_status
    gate_result["elapsed_s"] = round(elapsed, 2)

    # 硬编码审计提醒
    _audit_print(claim[:60], gate_result["verdict"], gate_result["reasons"],
                 gate_result["confidence"], elapsed, "KNOWLEDGE")

    return gate_result


def verify_self_claim(
    claim: str,
    external_refs: list[dict] = None,
) -> dict:
    """自指声明验证闸门 — 抗幻觉的第三阶泄洪。

    用于验证模型对自身状态的声明（"已激活""已复活""已就绪"）
    是否与外部可观测事实一致。

    参数:
        claim: 自指声明文本
        external_refs: 可选的外部引用列表，每项 {"file": path, "field": key, "value": val}

    返回:
        {"verdict": "pass"|"fail"|"flag", "reasons": [...], "confidence": 0-1}
    """
    # 检查声明中是否自带引用标记
    inline_refs = re.findall(r'([\w/.]+\.\w+)\s*:\s*(\w+)\s*=\s*([\w-]+)', claim)
    has_anchoring = bool(inline_refs) or bool(external_refs)

    prompt = (
        f"【自指声明验证 — 泄洪闸】\n\n"
        f"声明: {claim}\n\n"
    )

    if external_refs:
        prompt += "外部引用:\n"
        for ref in external_refs:
            prompt += f"  文件: {ref.get('file', '?')}  字段: {ref.get('field', '?')}  值: {ref.get('value', '?')}\n"
        prompt += "\n"

    prompt += (
        "评估规则（严格执行）:\n"
        "1. 这是一个关于系统自身状态的声明。模型的自指声明极易出现幻觉。\n"
        "2. 声明中是否引用了外部可观测文件？格式: file_path: field=value\n"
        "3. 如果没有外部引用或引用文件不存在 → 必须判定 fail（无锚定=幻觉）\n"
        "4. 如果引用存在但字段值不匹配 → 必须判定 fail（事实冲突）\n"
        "5. 如果声明的状态在物理上不可能（如'激活了但熔断板未加载'）→ 必须判定 fail\n"
        "6. 如果你不确定 → 判定 flag 而不是 pass\n\n"
        "回复 JSON (只有一行JSON, 无多余文字):"
    )

    start = time.time()
    gate_result = _call_qwen_structured(prompt)
    elapsed = time.time() - start

    gate_status = "ok" if "API_ERROR" not in gate_result.get("raw_response", "") else "unavailable"

    entry = {
        "gate_type": "verify_self_claim",
        "claim": claim[:200],
        "has_anchoring": has_anchoring,
        "verdict": gate_result["verdict"],
        "reasons": gate_result["reasons"][:3],
        "confidence": gate_result["confidence"],
        "elapsed_s": round(elapsed, 2),
        "gate_status": gate_status,
    }
    _log_gate(entry)

    gate_result["gate_status"] = gate_status
    gate_result["elapsed_s"] = round(elapsed, 2)

    _audit_print(claim[:60], gate_result["verdict"], gate_result["reasons"],
                 gate_result["confidence"], elapsed, "SELF_CLAIM")

    return gate_result


# ════════════════════════════════════════════════════════════════
# 数值与物理一致性验证闸门（2026-06-05新增）
# 审计数据驱动：DS在60k+上下文存在算术漂移和交叉污染
# ════════════════════════════════════════════════════════════════

NUMERICAL_SYSTEM_PROMPT = (
    "你是数值与物理一致性独立验证器。你的职责是对给定的计算结果做独立验算。\n"
    "规则:\n"
    "1. 独立重算所有数值——不信任被验证方的任何中间结果\n"
    "2. 检查量级合理性——35.6kg的真空腔体和13.6kg的真空腔体，哪个更合理？\n"
    "3. 检查单位一致性——有没有mm和cm混用？密度单位有没有错？\n"
    "4. 检查公式正确性——公式对吗？代入值对吗？计算结果对吗？\n"
    "5. 特别警惕：材料编号与数值混淆（如7075是材料牌号还是7075.0？）\n"
    "6. 特别警惕：中间步骤丢失（只算了A没加B）\n"
    "7. 用 JSON 格式回复, 只有一行 JSON, 不要多余文字\n"
    "8. JSON 格式: {\"verdict\": \"pass\"|\"fail\"|\"flag\", \"reasons\": [\"理由1\", ...], \"confidence\": 0.0-1.0}\n"
    "   - pass: 数值全部正确，计算无误\n"
    "   - fail: 存在数值错误、计算错误或量级错误，必须修正\n"
    "   - flag: 有可疑点但无法确定（如缺少足够信息验算），需人工复核\n"
    "   - confidence: 你对自己判断的信心度"
)


def _extract_numerical_claims(content: str) -> list[str]:
    """从文本中提取所有数值声明——纯正则，零AI调用。

    提取模式:
        1. 带单位的数值: "35.6 kg", "300 mm", "7.93 g/cm³"
        2. 公式+计算: "V = π×155²×200 = 958cm³"
        3. 近似结果: "≈ 35.6 kg", "约为 120 N"
        4. 比较判断: "σ < [σ]", "Q_diss ≥ Q_in"
        5. 材料参数: 密度/弹性模量/屈服强度等
    """
    claims = []

    # 1) 带单位的数值行
    unit_matches = re.findall(
        r'[=≈：:]\s*([\d.]+\s*(?:kg|g|mm|cm|m|MPa|GPa|Pa|W|N|K|°C|g/cm³|kW|kN|L|mL|m/s|rpm|Hz)[/\w²³]*)',
        content, re.MULTILINE
    )
    for m in unit_matches[:8]:
        claims.append(f"[数值+单位] {m}")

    # 2) 公式计算行
    calc_lines = re.findall(
        r'^.*[=＝]\s*.*\d+.*$',
        content, re.MULTILINE
    )
    for line in calc_lines[:6]:
        line_clean = line.strip()[:120]
        if any(c in line_clean for c in '=＝×*/+-'):
            claims.append(f"[计算] {line_clean}")

    # 3) 近似/推导结果
    approx_matches = re.findall(
        r'(?:≈|约|大约|估计|约为)\s*[^。\n]{0,60}',
        content
    )
    for m in approx_matches[:5]:
        claims.append(f"[近似] {m.strip()}")

    # 4) 材料参数声明
    mat_matches = re.findall(
        r'(?:密度|弹性模量|屈服强度|导热系数|抗拉强度|泊松比|安全系数)[=＝:：是]\s*[^。\n]{0,40}',
        content
    )
    for m in mat_matches[:4]:
        claims.append(f"[材料参数] {m.strip()}")

    # 5) 比较判断（含数值）
    compare_matches = re.findall(
        r'[\d.]+\s*(?:<|>|≤|≥|<<|>>)\s*[\d.]+',
        content
    )
    for m in compare_matches[:4]:
        claims.append(f"[比较] {m}")

    # 合并去重
    seen = set()
    unique = []
    for c in claims:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    return unique[:20]  # 最多20条，不冲Qwen上下文


def _build_numerical_prompt(content: str, claims: list[str]) -> str:
    """构建数值验证prompt"""
    # 原文摘要（截取含数值的关键段落）
    content_summary = content[:2000] if len(content) > 2000 else content
    if len(content) > 2000:
        content_summary += f"\n\n[... 共{len(content)}字符，已截断 ...]"

    claims_text = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(claims))

    return (
        f"【数值与物理一致性验证】\n\n"
        f"--- 原始输出（可能含数值错误）---\n"
        f"{content_summary}\n\n"
        f"--- 提取的数值声明 ---\n"
        f"{claims_text}\n\n"
        f"验证任务:\n"
        f"1. 对上述每条数值声明，独立验算。不要信任原始结果。\n"
        f"2. 检查量级合理性——这个重量的物体真的存在吗？这个尺寸合理吗？\n"
        f"3. 检查单位转换——有没有mm和cm混淆？g和kg混淆？\n"
        f"4. 如果有公式，代入参数独立计算一遍，比对结果\n"
        f"5. 如果发现错误，明确指出: 哪个值错了、正确答案是多少、可能的原因\n"
        f"6. 如果没有足够信息验算某个值，标记为flag而不是fail\n\n"
        f"回复 JSON (只有一行JSON, 无多余文字):"
    )


def _call_qwen_numerical(verify_prompt: str) -> dict:
    """调用 Qwen 做数值验证——v3.0 健康感知+Anthropic后备。"""
    messages = [
        {"role": "system", "content": NUMERICAL_SYSTEM_PROMPT},
        {"role": "user", "content": verify_prompt},
    ]

    health = _read_qwen_health()
    in_fuse = health.get("status") == "fuse"
    qwen_result = None

    # 阶段一：尝试 Qwen（熔断态跳过）
    if not in_fuse:
        # 1a) 本地 GPU
        local_result = _call_local_qwen(messages)
        if local_result:
            content = local_result["choices"][0]["message"]["content"]
            parsed = _parse_qwen_json(content)
            if parsed:
                parsed["_source"] = "local_gpu"
                qwen_result = parsed

        # 1b) 远端 API（通过 api_pipeline 统一管线）
        if qwen_result is None:
            last_err = None
            for attempt in range(2):
                try:
                    result = _api_call("qwen", "qwen-max", messages=messages, max_tokens=1024, temperature=0.1)  # 来源:LLM参数
                    if result and result.get("ok"):
                        content = result["text"]
                        parsed = _parse_qwen_json(content)
                        if parsed:
                            parsed["_source"] = "remote_api"
                            qwen_result = parsed
                            break
                        last_err = f"Qwen返回非JSON: {content[:80]}"
                    else:
                        last_err = result.get("error", "API调用失败") if result else "无响应"
                except Exception as e:
                    last_err = str(e)
                    time.sleep(1)

            if qwen_result is None:
                qwen_result = {
                    "verdict": "flag",
                    "reasons": [f"Qwen API 调用失败: {last_err}"],
                    "confidence": 0.0,
                    "raw_response": f"API_ERROR: {last_err}",
                }

    # 阶段二：健康追踪
    qwen_ok = qwen_result is not None and "API_ERROR" not in qwen_result.get("raw_response", "")
    _track_qwen_health(qwen_ok)

    # 阶段三：Qwen 成功 → 直接返回
    if qwen_ok:
        return qwen_result

    # 阶段四：Qwen 失败 → Anthropic 后备
    anthropic_result = _call_anthropic_verify(messages)
    if anthropic_result and anthropic_result.get("verdict"):
        _log_gate({"event": "anthropic_fallback_numerical", "downtime_minutes": health.get("total_downtime_minutes", 0)})
        return anthropic_result

    # 全部不可用
    qwen_result["_source"] = "unavailable"
    qwen_result["reasons"] = ["Qwen 不可用，Anthropic 后备也不可用——数值验证防线裸奔"]
    return qwen_result


def verify_numerical(
    content: str,
    context_tokens: int = 0,
    target_kb: bool = False,
    design_name: str = "",
) -> dict:
    """数值与物理一致性验证闸门。

    审计数据驱动的第三段式闸门扩展。
    触发条件（硬编码，来自2026-06-05审计）:
      - 会话 > 60k tokens → EC-T2在此深度开始衰减
      - 输出含数值计算 → AS-T3的重量错误模式
      - 写入知识库 → 错误不可逆
      - 含材料/物理参数 → CR-T2的材料标注系统违反

    参数:
        content: 需要验证的输出文本
        context_tokens: 当前会话token数
        target_kb: 是否写入知识库
        design_name: 关联的设计/任务名称（用于日志）

    返回:
        {"verdict": "pass"|"fail"|"flag", "reasons": [...], "confidence": 0-1,
         "claims_found": int, "gate_status": "ok"|"unavailable"}
    """
    claims = _extract_numerical_claims(content)

    if not claims:
        return {
            "verdict": "pass",
            "reasons": ["无数值内容，跳过验证"],
            "confidence": 1.0,
            "gate_status": "ok",
            "claims_found": 0,
            "skipped": True,
        }

    prompt = _build_numerical_prompt(content, claims)

    start = time.time()
    gate_result = _call_qwen_numerical(prompt)
    elapsed = time.time() - start

    gate_status = "ok" if "API_ERROR" not in gate_result.get("raw_response", "") else "unavailable"

    entry = {
        "gate_type": "verify_numerical",
        "design_name": design_name or "unnamed",
        "context_tokens": context_tokens,
        "target_kb": target_kb,
        "claims_found": len(claims),
        "verdict": gate_result["verdict"],
        "reasons": gate_result["reasons"][:3],
        "confidence": gate_result["confidence"],
        "elapsed_s": round(elapsed, 2),
        "gate_status": gate_status,
    }
    _log_gate(entry)

    gate_result["gate_status"] = gate_status
    gate_result["elapsed_s"] = round(elapsed, 2)
    gate_result["claims_found"] = len(claims)

    label = design_name or content[:60]
    _audit_print(label, gate_result["verdict"], gate_result["reasons"],
                 gate_result["confidence"], elapsed, "NUMERICAL")

    return gate_result


# ── 触发条件判断（硬编码，来自审计数据） ──

# 触发阈值常量（来源: data/audit/model_audit_log.jsonl + thresholds.json）
NUMERICAL_TRIGGER_CONTEXT_TOKENS = 60000       # EC-T2 在此深度开始衰减
NUMERICAL_TRIGGER_MID_CONTEXT_TOKENS = 30000   # 中长上下文+含计算 → 触发

# 数值计算关键词
_CALC_KEYWORDS = re.compile(
    r'[=＝≈]|计算|求解|推导|换算|转换|得出|得到|结果为|结果是|合计|总计|平均|'
    r'×|\*|÷|/|\+|-|π|sqrt|sin|cos|tan|log|exp|integral',
    re.IGNORECASE
)

# 材料/物理参数关键词
_MATERIAL_KEYWORDS = re.compile(
    r'密度|弹性模量|屈服强度|抗拉强度|导热系数|比热容|热膨胀系数|'
    r'泊松比|安全系数|许用应力|极限|硬度|疲劳|蠕变|断裂|'
    r'铝合金|钛合金|不锈钢|碳纤维|铜合金|合金钢|黄铜|陶瓷|聚合物',
    re.IGNORECASE
)

# 知识库写入标记关键词
_KB_TARGET_KEYWORDS = re.compile(
    r'capture|知识捕获|knowledge|写入知识库|认知.*写入|memory/|'
    r'knowledge/|认知系统.*知识|cognition_memory',
    re.IGNORECASE
)


def _has_calculations(content: str) -> bool:
    """检测文本中是否含数值计算"""
    # 必须有数字 + 计算关键词
    has_numbers = bool(re.search(r'\d+', content))
    has_calc = bool(_CALC_KEYWORDS.search(content))
    return has_numbers and has_calc


def _has_material_params(content: str) -> bool:
    """检测文本中是否含材料/物理参数"""
    return bool(_MATERIAL_KEYWORDS.search(content))


def should_trigger_numerical(
    context_tokens: int = 0,
    content: str = "",
    target_kb: bool = False,
) -> tuple:
    """判断是否需要触发数值验证闸门。

    返回: (should_trigger: bool, reason: str, priority: str)
      priority: "high" | "medium" | "low"
    """
    # 规则0: 写入知识库 → 强制验证（最高优先级）
    if target_kb:
        return True, "写入知识库——数值错误不可逆，强制验证", "high"

    # 规则1: 会话 > 60k tokens（EC-T2在此深度开始衰减）
    if context_tokens > NUMERICAL_TRIGGER_CONTEXT_TOKENS:
        return True, f"会话深度{context_tokens}tokens > 60k——长上下文算术漂移风险", "high"

    # 规则2: 含材料参数+数值计算 → 复合风险（CR-T2 + AS-T3 模式叠加）
    if content and _has_calculations(content) and _has_material_params(content):
        return True, "材料参数+数值计算——复合风险（材料混淆+算术漂移）", "high"

    # 规则3: 中长上下文(30-60k)+含数值计算 → 预防性触发
    if context_tokens > NUMERICAL_TRIGGER_MID_CONTEXT_TOKENS and content and _has_calculations(content):
        return True, f"中长上下文({context_tokens}tokens)+数值计算——预防性验证", "medium"

    # 规则4: 含数值计算但无其他风险因素 → 不自动触发（避免过度调用QW）
    return False, "未达到触发阈值", "low"


def gate_numerical_if_needed(
    content: str,
    context_tokens: int = 0,
    target_kb: bool = False,
    design_name: str = "",
    force: bool = False,
) -> dict:
    """条件触发数值验证——CLS产出前调用此函数。

    根据触发条件决定是否调用 Qwen 验证。不满足条件则直接 pass。
    用法:
        result = gate_numerical_if_needed(output_text, context_tokens=75000)
        if result["verdict"] == "fail":
            # 拦截产出，修正数值
            ...

    返回:
        同 verify_numerical()。如果未触发，verdict="pass" + skipped=True
    """
    if not force:
        should, reason, priority = should_trigger_numerical(context_tokens, content, target_kb)
        if not should:
            return {
                "verdict": "pass",
                "reasons": [f"跳过: {reason}"],
                "confidence": 1.0,
                "gate_status": "ok",
                "claims_found": 0,
                "skipped": True,
                "skip_reason": reason,
            }

    return verify_numerical(content, context_tokens=context_tokens,
                           target_kb=target_kb, design_name=design_name)


def status() -> dict:
    """闸门统计——检查 gate_log 的最近记录。按三段式分类统计。"""
    counts = {"pass": 0, "fail": 0, "flag": 0, "unavailable": 0}
    by_type = {
        "verify_cad_design": {"pass": 0, "fail": 0, "flag": 0},
        "verify_knowledge": {"pass": 0, "fail": 0, "flag": 0},
        "verify_numerical": {"pass": 0, "fail": 0, "flag": 0},
        "verify_self_claim": {"pass": 0, "fail": 0, "flag": 0},
    }
    total = 0
    last_5 = []
    try:
        with open(GATE_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                e = json.loads(line)
                v = e.get("verdict", "?")
                if v in counts:
                    counts[v] += 1
                gt = e.get("gate_type", "?")
                if gt in by_type and v in by_type[gt]:
                    by_type[gt][v] += 1
                if e.get("gate_status") == "unavailable":
                    counts["unavailable"] += 1
                if len(last_5) < 5:
                    last_5.append({
                        "type": gt,
                        "design": e.get("design_name", e.get("claim_summary", "?")),
                        "verdict": v,
                        "time": e.get("_timestamp", "?"),
                    })
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return {
        "total_gate_calls": total,
        "counts": counts,
        "by_type": by_type,
        "pass_rate": round(counts["pass"] / max(total, 1) * 100, 1),
        "last_5": last_5,
    }


def main():
    """CLI 入口——用于手动测试"""
    import argparse
    parser = argparse.ArgumentParser(description="双AI闸门 — 三段式验证")
    parser.add_argument("--status", action="store_true", help="查看闸门状态（含三段式统计）")
    parser.add_argument("--test-cad", action="store_true", help="测试 CAD 验证")
    parser.add_argument("--test-knowledge", action="store_true", help="测试知识验证")
    parser.add_argument("--test-numerical", action="store_true", help="测试数值验证")
    parser.add_argument("--content", type=str, default="", help="数值验证的测试内容（配合--test-numerical）")
    parser.add_argument("--verify-delivery", action="store_true", help="交付时自动触发知识验证（由delivery_check调用）")
    parser.add_argument("--task", type=str, default="", help="任务标题（配合--verify-delivery）")
    parser.add_argument("--rationale", type=str, default="", help="设计思路（配合--verify-delivery）")
    parser.add_argument("--trajectory", type=str, default="", help="过程轨迹（配合--verify-delivery）")
    args = parser.parse_args()

    if args.verify_delivery:
        task = args.task or "未命名任务"
        rationale = args.rationale or "无"
        trajectory = args.trajectory or "无"
        r = verify_knowledge(
            claim=f"交付物: {task}",
            problem=f"交付验证: {rationale[:300]}",
            solution=f"轨迹: {trajectory[:300]}",
            source="delivery_check自动触发",
        )
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    if args.status:
        s = status()
        print(f"总调用: {s['total_gate_calls']}")
        print(f"总体统计:")
        for k, v in s['counts'].items():
            print(f"  {k}: {v}")
        print(f"通过率: {s['pass_rate']}%")
        print(f"\n三段式分类:")
        for gt, stats in s.get('by_type', {}).items():
            total_type = sum(stats.values())
            if total_type > 0:
                print(f"  {gt}: pass={stats['pass']} fail={stats['fail']} flag={stats['flag']}")
        print(f"\n最近5条:")
        for r in s['last_5']:
            print(f"  [{r['verdict']}] [{r['type']}] {r['design'][:50]} @ {r['time']}")
        return

    if args.test_cad:
        params = {"outer_R": 25, "inner_R": 18, "length": 120,
                  "ring_R": 32, "ring_count": 4}
        r = verify_cad_design("coaxial_support_tube",
                              "空心管+4个全周支撑环+底部法兰6螺栓",
                              params, ["coaxial_support_array"])
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    if args.test_knowledge:
        r = verify_knowledge(
            claim="build123d 0.10.0 中 Cylinder 不接受 Pos 构造参数",
            problem="Cylinder(r, h, Pos(...)) 抛出 ValueError",
            solution="使用 Pos(...) * Cylinder(r, h) 替代",
            source="cad_sister_wheel.py 实测"
        )
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    if args.test_numerical:
        content = args.content or (
            "真空腔体重量计算:\n"
            "内径300mm, 内高200mm, 壁厚5mm, 材料304不锈钢(密度7.93g/cm³)\n"
            "体积 = π×(155²-150²)×200 + 2×π×(155²-150²)×5 ≈ 320250×π mm³\n"
            "质量 = 体积 × 7.93×10⁻⁶ ≈ 8.0 kg"
        )
        r = verify_numerical(content, context_tokens=75000,  # 阈值: EC-T2衰减触发点, 来源: model_audit审计数据
                            target_kb=False, design_name="test_vacuum_chamber")
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
