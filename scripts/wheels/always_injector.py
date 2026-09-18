#!/usr/bin/env python3
"""always_injector.py - CLAUDE.md -> 本地ep-json -> 规则卡片注入
Windows Task Scheduler, batch-extract, SHA256 change detection.
@fix 2026-08-16 maintainer定: ①模型 SF Qwen → DS Flash(SF结构化实验三连崩) ②规则卡片化:
每条规则带 为什么+触发场景 — "注入一定要详细, 不然会被忽略"。
@fix 2026-08-16 夜: ③换轨对齐: 结构化提取实测 MiMo 失败(推理链非JSON, 正则0规则) → opencode DS Flash(实测合法JSON)。轮子走 api_pipeline 可换。
@fix 2026-08-21: ④切换本地ep-json (1.5B微调, JSON合法率100%, 免费零延迟)。失败→api_pipeline兜底。
"""
import json, re, hashlib, sys, time, subprocess, urllib.request
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent.parent
CLAUDE_MD = ROOT / "CLAUDE.md"
INJECT_FILE = ROOT / "data" / "state" / "always_inject.json"
STATE_FILE = ROOT / "data" / "state" / "_always_injector_state.json"
LOCAL_MODEL = "ep-json:latest"  # 本地微调 (2026-08-21 切换)
PROVIDER = "opencode"  # 远端兜底
DS_MODEL = "deepseek-v4-flash"  # @fix 2026-09-10: 换自 mimo-v2.5(实测 P0 提取 payload 上 mimo 28.7s vs DSF 7.0s)
MAX_SECTIONS = 12


def _hash(path):
    return hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()[:16] if path.exists() else ""


def _needs_update():
    if not STATE_FILE.exists():
        return True
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("hash") != _hash(CLAUDE_MD)
    except Exception:
        return True


def _call_local(prompt, timeout=30):
    """调本地Ollama ep-json。失败返回None。"""
    try:
        r = subprocess.run(
            ["ollama", "run", LOCAL_MODEL, "--nowordwrap"],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)  # 防闪窗
        )
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def _extract_batch(text):
    system = (
        "从给定的项目规则文档中提取 P0 级(生死线级)行为规则, 输出 JSON 数组。\n"
        "每条规则输出一个对象, 字段:\n"
        '  "rule": 规则本体, 一行, ≤30字\n'
        '  "why": 为什么这条规则存在(违反的后果), ≤40字\n'
        '  "trigger": 什么场景触发这条规则(如: 交付时/写代码前/git提交前), ≤30字\n'
        "要求: 只提取明确标注 P0/生死线/禁止 级别的规则, 普通建议不要; "
        "输出合法 JSON 数组, 不要解释不要多余文字。"
    )
    full_prompt = system + "\n\n" + text[:3000]

    # 优先本地ep-json
    for _ in range(2):
        response = _call_local(full_prompt, timeout=30)
        if response:
            m = re.search(r"\[.*\]", response, re.DOTALL)
            arr = json.loads(m.group()) if m else []
            if isinstance(arr, list) and len(arr) > 0:
                return [a for a in arr if isinstance(a, dict) and a.get("rule")]

    # 兜底: api_pipeline (opencode MiMo)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.wheels.api_pipeline import call
    for _ in range(3):
        try:
            r = call(PROVIDER, DS_MODEL,
                     messages=[{"role": "system", "content": system},
                               {"role": "user", "content": text[:3000]}],
                     max_tokens=1200, auto_route=False, timeout_s=120)
            if not (r and r.get("ok")):
                time.sleep(3)
                continue
            response = r.get("text") or ""
            m = re.search(r"\[.*\]", response, re.DOTALL)
            arr = json.loads(m.group()) if m else []
            if isinstance(arr, list):
                return [a for a in arr if isinstance(a, dict) and a.get("rule")]
            return []
        except Exception:
            time.sleep(3)
    return []


def extract():
    if not _needs_update():
        return {"status": "unchanged"}
    text = CLAUDE_MD.read_text(encoding="utf-8")
    sections = [s for s in re.split(r"\n(?=#{1,3} )", text) if len(s) > 100][:MAX_SECTIONS]
    cards = {}
    for batch in sections:
        for r in _extract_batch(batch):
            key = hashlib.md5((r.get("rule") or "").encode()).hexdigest()[:8]
            if key not in cards:
                cards[key] = {
                    "rule": (r.get("rule") or "").strip(),
                    "why": (r.get("why") or "无").strip(),
                    "trigger": (r.get("trigger") or "无").strip(),
                }
    card_list = list(cards.values())[:15]
    lines = ["[P0规则卡片]"]
    for c in card_list:
        lines.append(f"- {c['rule']} | 为什么: {c['why']} | 触发: {c['trigger']}")
    injection = "\n".join(lines)
    try:  # P7 统一审计
        from knowledge_inject_audit import log as _audit_ki
        _audit_ki("always_injector", injection, "P0规则卡片", f"{len(card_list)}条")
    except Exception:
        pass
    output = {"text": injection, "cards": card_list, "extracted_at": datetime.now().isoformat(),
              "hash": _hash(CLAUDE_MD)}
    INJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
    INJECT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"hash": output["hash"], "last_run": output["extracted_at"],
                                      "total": len(card_list)}, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return {"status": "ok", "rules": len(card_list)}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        if INJECT_FILE.exists():
            d = json.loads(INJECT_FILE.read_text(encoding="utf-8"))
            print(f"Last: {d['extracted_at']}\n{d['text']}")
        else:
            print("No injection yet")
    else:
        print(json.dumps(extract(), ensure_ascii=False, indent=2))
