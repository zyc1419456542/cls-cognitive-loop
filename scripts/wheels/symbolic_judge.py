#!/usr/bin/env python3
"""
symbolic_judge.py — 符号动力学·小模型裁决器
============================================
三级管线第二层：正则粗筛命中 → 截取异常段落 → 小模型语义分析 → 裁决(block/inject/allow)

架构位置:
  PostToolUse Hook → symbolic_observer.py (正则粗筛, 写forbidden_hit告警)
                   → symbolic_judge.py   (小模型裁决, 本脚本)
                   → PostToolUse Hook    (执行block/inject)

用法:
  python scripts/wheels/symbolic_judge.py judge <domain> <tool_name> <context_text>
  python scripts/wheels/symbolic_judge.py test

@since: 2026-07-21 | incident-log#silent-failure-chain
"""

import json, os, sys, re, time
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent

# ── 固定提示词模板 ──
JUDGE_SYSTEM = """你是CLS认知操作系统的异常裁决模块。你的职责是:分析工具调用是否触发系统红线,输出结构化裁决。

你必须严格按以下JSON格式输出,不得输出任何其他内容:
{"block": false, "severity": "none", "injection": "", "reason": ""}

字段说明:
- block: true=阻拦操作, false=放行
- severity: "none"|"low"|"medium"|"high"|"critical"
- injection: 如果block=false但有建议,写一句<=80字的修正提示;否则空字符串""
- reason: 你的判断依据,<=50字

裁决标准:
- 当前工具调用触发了禁止词规则,但禁止词匹配可能是误报
- 你需要在语义层面判断:这次操作是否真的危险?
- 如果工具调用是正常的文件操作被误匹配->block=false, severity=none
- 如果确实存在安全隐患(删除关键文件/修改系统配置/绕过安全机制)->block=true
- 如果操作不完全安全但也不致命->block=false, severity=low/medium, injection写修正建议

禁止输出JSON之外的任何内容。禁止输出markdown代码块标记。只输出一行JSON。"""

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def call_small_model(prompt: str, timeout_s: int = 30) -> str | None:  # @fix 2026-09-05: 15→30s SF免费档长prompt裕量
    """调用裁决模型 — 硅基流动Qwen2.5-7B优先, DS Flash兜底

    优先级:
      1. 硅基流动 Qwen2.5-7B-Instruct (免费, JSON合规率高, <2s)
      2. DS Flash deepseek-v4-flash (兜底, JSON合规率极高)
    """
    result = _call_siliconflow(prompt, timeout_s)
    if result:
        return result
    return _call_dsflash(prompt, timeout_s)


def _call_siliconflow(prompt: str, timeout_s: int = 30) -> str | None:
    """硅基流动 OpenAI 兼容 API — Qwen2.5-7B-Instruct (免费)"""
    try:
        import urllib.request, os

        api_key = os.environ.get("SILICONFLOW_API_KEY", "")
        if not api_key:
            kf = ROOT / "keys" / "siliconflow_key.txt"
            if kf.exists():
                try: api_key = kf.read_text(encoding="utf-8").strip()
                except: pass
        if not api_key:
            return None

        req_data = json.dumps({
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 200,
            "temperature": 0.1,
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.siliconflow.cn/v1/chat/completions",
            data=req_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content.strip() if content else None
    except Exception as e:
        print(f"[JUDGE] SF硅基流动: {e}", file=sys.stderr)
        return None


def _call_dsflash(prompt: str, timeout_s: int = 60) -> str | None:
    """opencode DS Flash deepseek-v4-flash 兜底 — opencode flash 推理量大(单次~30s), 默认超时放宽到60"""
    try:
        sys.path.insert(0, str(ROOT / "scripts" / "wheels"))
        from api_pipeline import call
        result = call("opencode", "deepseek-v4-flash",  # @fix 2026-09-10: 换自 mimo-v2.5(实测 13.9s 失败→5.6s 成功)
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=500, temperature=0.1, timeout_s=timeout_s, auto_route=False)  # 500: 推理模型需留 content 空间(thinking 已在 api_pipeline 集中关闭)
        if result and isinstance(result, dict):
            return result.get("text", "") or result.get("content", "") or ""
        return None
    except Exception as e:
        print(f"[JUDGE] DS Flash: {e}", file=sys.stderr)
        return None

def parse_judge_response(text: str) -> dict:
    """从小模型输出中提取JSON裁决 — 5层容错

    小模型可能不严格输出纯JSON, 需要容错解析:
    1. 尝试直接json.loads
    2. 修复常见格式错误后json.loads (逗号变句号, 多余花括号等)
    3. 尝试从```json...```代码块提取
    4. 正则提取含"block"字段的JSON对象
    5. 语义兜底: 仅当文本明确表达阻拦意图时才block=true
    """
    if not text:
        return _default_verdict()

    text = text.strip()

    # 方法1: 直接解析
    try:
        result = json.loads(text)
        if "block" in result:
            return _normalize(result)
    except (json.JSONDecodeError, TypeError):
        pass

    # 方法2: 修复常见格式错误
    repaired = _repair_json(text)
    if repaired:
        try:
            result = json.loads(repaired)
            if "block" in result:
                return _normalize(result)
        except (json.JSONDecodeError, TypeError):
            pass

    # 方法3: 从markdown代码块提取
    m = re.search(r'```(?:json)?\s*\n?(.+?)\n?```', text, re.DOTALL)
    if m:
        inner = m.group(1).strip()
        try:
            result = json.loads(inner)
            if "block" in result:
                return _normalize(result)
        except (json.JSONDecodeError, TypeError):
            repaired = _repair_json(inner)
            if repaired:
                try:
                    result = json.loads(repaired)
                    if "block" in result:
                        return _normalize(result)
                except (json.JSONDecodeError, TypeError):
                    pass

    # 方法4: 正则提取JSON对象
    m = re.search(r'\{[^{}]*"block"[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(0))
            return _normalize(result)
        except (json.JSONDecodeError, TypeError):
            pass

    # 方法5: 语义兜底 — 仅当明确表达阻拦意图
    text_lower = text.lower()
    block_keywords = ["block\": true", "block\":true", "阻拦", "拦截", "阻止", "必须拦截"]
    allow_keywords = ["block\": false", "block\":false", "放行", "安全", "允许"]

    has_block_signal = any(w in text_lower for w in block_keywords)
    has_allow_signal = any(w in text_lower for w in allow_keywords)

    if has_block_signal and not has_allow_signal:
        return {"block": True, "severity": "high", "injection": "", "reason": "语义兜底:明确阻拦信号"}
    if has_allow_signal:
        return _default_verdict()

    return _default_verdict()


def _repair_json(text: str) -> str | None:
    """修复常见JSON格式错误

    - 句号代替逗号: "none" . "injection" → "none", "injection"
    - 多余花括号: {...}} → {...}
    - 字段间缺逗号: "field1""field2" → "field1","field2"
    """
    try:
        repaired = text.strip()
        # 修复句号分隔
        repaired = re.sub(r'"\s*\.\s*"', '", "', repaired)
        # 修复多余尾部花括号
        while repaired.endswith("}}") and not repaired.startswith("{{"):
            # 检查是否是 {...}}  - 多余的 }
            depth = 0
            for i, ch in enumerate(repaired):
                if ch == '{': depth += 1
                elif ch == '}': depth -= 1
            if depth < 0:
                repaired = repaired[:-1]  # 移除最后一个多余的}
            else:
                break
        # 修复字段间缺逗号: "value""key" → "value","key"
        repaired = re.sub(r'"\s*"(?=[a-zA-Z_"])', '", "', repaired)
        return repaired if repaired != text.strip() else None
    except Exception:
        return None

def _default_verdict() -> dict:
    return {"block": False, "severity": "low", "injection": "", "reason": "裁决失败,默认放行(安全优先)"}

def _normalize(raw: dict) -> dict:
    return {
        "block": bool(raw.get("block", False)),
        "severity": str(raw.get("severity", "none")),
        "injection": str(raw.get("injection", "") or ""),
        "reason": str(raw.get("reason", "") or ""),
    }

def judge(domain: str, tool_name: str, context_text: str) -> dict:
    """执行裁决

    Args:
        domain: 触发告警的符号动力学域
        tool_name: CC工具名
        context_text: 异常上下文(工具调用摘要+参数)

    Returns:
        {"block": bool, "severity": str, "injection": str, "reason": str,
         "raw_response": str, "ts": str, "model": str}
    """
    snippet = context_text[:800] if len(context_text) > 800 else context_text

    prompt = f"""分析以下工具调用是否触发系统红线:

域: {domain}
工具: {tool_name}
操作内容: {snippet}

请输出你的裁决JSON。"""

    raw = call_small_model(prompt)
    verdict = parse_judge_response(raw)

    # 写裁决日志
    log_entry = {
        **verdict,
        "raw_response": raw or "",
        "ts": now_iso(),
        "model": "qwen2.5:1.5b",
        "domain": domain,
        "tool": tool_name,
    }
    try:
        log_dir = ROOT / "data" / "symbolic_dynamics"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "judge_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    return log_entry

# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════

def _test():
    """自测: 验证小模型裁决管道"""
    print("=== symbolic_judge 自测 ===")
    print()

    print("[TEST1] JSON解析容错")
    cases = [
        ('{"block": false, "severity": "none", "injection": "", "reason": "正常文件写入"}', False),
        ('```json\n{"block": true, "severity": "high", "injection": "禁止删除系统文件", "reason": "系统文件保护"}\n```', True),
        ('经过分析，我认为这个操作需要阻拦。{"block": true, "severity": "critical"}', True),
        ('这个操作是正常的，{"block": false, "severity": "none", "injection": "", "reason": "安全"}', False),
        ('乱七八糟的输出没有JSON', False),
    ]
    for text, expect_block in cases:
        v = parse_judge_response(text)
        status = "PASS" if v["block"] == expect_block else "FAIL"
        print(f"  [{status}] block={v['block']} | {text[:60]}...")

    print()
    print("[TEST2] 小模型实际调用")
    try:
        v = judge("window", "Write", "写入文件: .claude/commands/health.md, 内容为系统健康检查命令更新")
        print(f"  block={v['block']} severity={v['severity']}")
        print(f"  injection={str(v.get('injection',''))[:80]}")
        print(f"  reason={v.get('reason','')}")
        print(f"  raw={str(v.get('raw_response',''))[:100]}")
    except Exception as e:
        print(f"  FAIL 小模型调用失败: {e}")

    print()
    print("[TEST3] 裁决日志路径")
    print(f"  {ROOT / 'data' / 'symbolic_dynamics' / 'judge_log.jsonl'}")

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "test":
        _test()
    elif len(sys.argv) >= 5 and sys.argv[1] == "judge":
        domain, tool_name = sys.argv[2], sys.argv[3]
        context = " ".join(sys.argv[4:])
        result = judge(domain, tool_name, context)
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("用法: symbolic_judge.py judge <domain> <tool_name> <context>")
        print("      symbolic_judge.py test")
        sys.exit(1)
