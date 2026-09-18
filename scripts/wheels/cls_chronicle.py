#!/usr/bin/env python3
"""
cls_chronicle.py — CLS 编年史: 时间+核心内容谱系压缩
=====================================================
@since 2026-07-28 | 硅基 Qwen2.5-7B 驱动

架构:
  首次全量: 扫描双轨进度+认知系统迭代大修/小修→生成压缩时间线
  增量更新: 每天8次(同knowledge_graph频率)→只扫描新增内容→合并
  输出: .cls_chronicle.json (≤2KB) → SessionStart注入

与assistant-node2知识图谱分工:
  chronicle: 时间维 ("什么时候发生了什么")
  knowledge_graph: 概念维 ("什么概念之间存在什么关系")
  两者互补, SessionStart 一起注入

用法:
  python scripts/wheels/cls_chronicle.py build    # 首次全量/增量更新
  python scripts/wheels/cls_chronicle.py inject   # 输出注入JSON
  python scripts/wheels/cls_chronicle.py status   # 遥测
"""

import json, os, sys, time, re, hashlib
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent
CHRONICLE_FILE = ROOT / "data" / "state" / ".cls_chronicle.json"
META_FILE = ROOT / "data" / "state" / ".cls_chronicle_meta.json"

# 扫描源
SCAN_SOURCES = [
    ("双轨进度", ROOT / "knowledge" / "进度文件", "progress_*.md"),
    ("大修", ROOT / "认知系统迭代" / "大修", "iter-*.md"),
    ("小修", ROOT / "认知系统迭代" / "小修", "iter-*"),
    ("capture", ROOT / "data" / "memory" / "on_demand", "auto_capture_*.md"),
]

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def call_model(prompt: str, timeout_s: int = 15) -> str | None:
    """硅基 Qwen2.5-7B"""
    try:
        import urllib.request
        api_key = os.environ.get("SILICONFLOW_API_KEY", "")
        if not api_key:
            kf = ROOT / "keys" / "siliconflow_key.txt"
            if kf.exists():
                api_key = kf.read_text(encoding="utf-8").strip()
        if api_key:
            req_data = json.dumps({
                "model": "Qwen/Qwen2.5-7B-Instruct",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 800, "temperature": 0.1,
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.siliconflow.cn/v1/chat/completions",
                data=req_data,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8")).get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception:
        pass
    return None

def collect_content(full_scan: bool = False) -> str:
    """收集扫描内容"""
    last_scan = 0
    if not full_scan and META_FILE.exists():
        try:
            last_scan = json.loads(META_FILE.read_text(encoding="utf-8")).get("last_scan_ts", 0)
        except: pass

    snippets = []
    # 只读人类写的摘要文件 (进度文件 + iter大修小修)
    # 7B模型没法从碎文件片段理解CLS, 但可以从已有的结构化摘要提取
    for src_type, src_dir, pattern in SCAN_SOURCES:
        if not src_dir.exists():
            continue
        for f in sorted(src_dir.rglob(pattern if "**" in pattern else pattern), key=lambda x: x.stat().st_mtime if x.is_file() else 0):
            if not f.is_file(): continue
            if f.suffix not in ('.md', '.json', '.txt'): continue
            try:
                mtime = f.stat().st_mtime
                if not full_scan and mtime < last_scan: continue
                size = f.stat().st_size
                if size > 50000: continue
                text = f.read_text(encoding="utf-8", errors="replace")
                # 取标题 + 前两段
                lines = [l.strip() for l in text.split('\n')]
                title = ""
                overview = []
                for l in lines:
                    if not title and (l.startswith('# ') or l.startswith('## ')):
                        title = l.lstrip('#').strip()[:80]
                    elif not l.startswith('#') and not l.startswith('>') and len(l) > 10:
                        overview.append(l[:150])
                    if title and len(overview) >= 3:
                        break
                preview = (title + ": " + ' '.join(overview))[:300]
                if preview:
                    ts = datetime.fromtimestamp(mtime).strftime("%m-%d")
                    snippets.append(f"[{src_type} {ts}] {f.name}: {preview}")
            except Exception:
                pass

    # 补充 CLAUDE.md 的核心信息 (CLS是什么)
    claude_md = ROOT / "CLAUDE.md"
    if claude_md.exists():
        try:
            text = claude_md.read_text(encoding="utf-8", errors="replace")
            # 取标题行
            title_line = ""
            for l in text.split('\n')[:3]:
                if l.startswith('# '):
                    title_line = l.lstrip('#').strip()
                    break
            snippets.insert(0, f"[SYSTEM] CLAUDE.md: {title_line}")
        except: pass

    return "\n---\n".join(snippets[:20])

def call_dsflash(prompt: str, timeout_s: int = 60) -> str | None:
    """opencode DS Flash 审核/追加 — opencode flash 推理量大(单次~30s), 默认超时放宽到60"""
    try:
        sys.path.insert(0, str(ROOT / "scripts" / "wheels"))
        from api_pipeline import call
        result = call("opencode", "deepseek-v4-flash",  # @fix 2026-09-10: 换自 mimo-v2.5(实测 33.9s/503字 → 15.1s/1473字)
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800, temperature=0.1, timeout_s=timeout_s, auto_route=False)  # 800: 推理模型需留 content 空间(thinking 已在 api_pipeline 集中关闭)
        if result and isinstance(result, dict):
            return result.get("text", "") or result.get("content", "")
    except Exception:
        pass
    return None

def build_chronicle(full_scan: bool = False) -> dict:
    """增量更新编年史 — 7B提取候选 → DS Flash审核 → 追加到对应领域"""
    default = {"overview": "", "domains": {}, "disclaimer": "近高远低,自行甄别", "updated": now_iso(), "version": 0}
    if CHRONICLE_FILE.exists() and not full_scan:
        try:
            chronicle = json.loads(CHRONICLE_FILE.read_text(encoding="utf-8"))
        except: chronicle = default
    else:
        chronicle = default

    content = collect_content(full_scan)
    if not content:
        return chronicle

    # 第一层: 7B 提取候选 (含领域分类)
    domains_str = ", ".join(chronicle.get("domains", {}).keys())
    extract_prompt = f"""扫描新增文件, 按领域分类提取候选里程碑。

已有领域: {domains_str}

新增文件:
{content[:2500]}

输出每行: DOMAIN | MM-DD 事件描述
领域用已有名称, 无法归类用"其他"。无新事件输出 SKIP。最多8条。"""

    candidates_raw = call_model(extract_prompt, timeout_s=20)
    if not candidates_raw or 'SKIP' in candidates_raw:
        return chronicle

    # 第二层: DS Flash 审核 + 分类
    judge_prompt = f"""从候选事件中筛选里程碑, 分配领域。

候选:
{candidates_raw[:1500]}

输出每行: DOMAIN | MM-DD 事件 (≤40字)
丢弃日常操作。无价值输出 SKIP。最多5条。"""

    judged_raw = call_dsflash(judge_prompt, timeout_s=15)
    if not judged_raw or 'SKIP' in judged_raw:
        return chronicle

    # 解析 → 追加到对应领域
    import re as _re
    domains = chronicle.setdefault("domains", {})
    for line in judged_raw.split('\n'):
        line = line.strip()
        parts = line.split('|', 1)
        if len(parts) == 2:
            domain_name = parts[0].strip()
            event_text = parts[1].strip()
            if _re.match(r'\d{2}-\d{2}', event_text) and len(event_text) > 5:
                m = _re.match(r'(\d{2}-\d{2})\s+(.+)', event_text)
                if m:
                    milestone = {"date": m.group(1), "event": m.group(2)}
                    if domain_name not in domains:
                        domains[domain_name] = {"summary": "", "milestones": []}
                    # 去重
                    existing = [e["event"] for e in domains[domain_name]["milestones"]]
                    if milestone["event"] not in existing:
                        domains[domain_name]["milestones"].append(milestone)

    # 每个领域最多保留8条里程碑, 旧的压缩到summary
    for dname, ddata in domains.items():
        milestones = ddata.get("milestones", [])
        if len(milestones) > 8:
            old_events = [f"{m['date']} {m['event']}" for m in milestones[:-8]]
            compress_prompt = f"将以下历史事件压缩为≤40字的一句话: {'; '.join(old_events[-5:])}\n输出: 一句话摘要"
            compressed = call_dsflash(compress_prompt, timeout_s=10)
            if compressed:
                ddata["summary"] = (ddata.get("summary", "") + " " + compressed.strip())[:120]
            ddata["milestones"] = milestones[-8:]

    chronicle["updated"] = now_iso()
    chronicle["version"] = chronicle.get("version", 0) + 1

    # 更新 overview
    domain_summaries = '; '.join(k + ':' + v.get('summary','')[:40] for k,v in list(domains.items())[:5])
    overview_prompt = "根据以下领域摘要, 写一句CLS项目概述(<=60字): " + domain_summaries
    overview_raw = call_dsflash(overview_prompt, timeout_s=10)
    if overview_raw:
        chronicle["overview"] = overview_raw.strip()[:80]

    CHRONICLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHRONICLE_FILE.write_text(json.dumps(chronicle, ensure_ascii=False, indent=2), encoding="utf-8")

    milestones_total = sum(len(d.get("milestones",[])) for d in domains.values())
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(json.dumps({"last_scan_ts": time.time(), "last_scan_iso": now_iso(), "domains": len(domains), "milestones": milestones_total, "version": chronicle["version"]}, ensure_ascii=False), encoding="utf-8")

    return chronicle

def inject() -> str | None:
    """生成注入JSON — 领域树形格式"""
    if not CHRONICLE_FILE.exists():
        return None
    try:
        c = json.loads(CHRONICLE_FILE.read_text(encoding="utf-8"))
    except: return None

    overview = c.get("overview", "")
    domains = c.get("domains", {})
    disclaimer = c.get("disclaimer", "近高远低,自行甄别")

    if not overview and not domains:
        return None

    # 构建树形注入
    lines = [overview] if overview else []
    for domain_name, domain_data in domains.items():
        summary = domain_data.get("summary", "")
        milestones = domain_data.get("milestones", [])
        recent = [f"{m['date']} {m['event']}" for m in milestones[-2:]]
        recent_str = "; ".join(recent) if recent else ""
        line = f"├ {domain_name}: {summary}"
        if recent_str:
            line += f" [{recent_str}]"
        lines.append(line)
        # 杂项展开子分类
        subsections = domain_data.get("subsections", {})
        for sub_name, sub_desc in subsections.items():
            lines.append(f"│  ├ {sub_name}: {sub_desc[:60]}")

    ctx = "\n".join(lines)
    ctx = ctx[:1500] + " [" + disclaimer + "]"

    # 注入计数器
    try:
        meta = {}
        if META_FILE.exists():
            meta = json.loads(META_FILE.read_text(encoding="utf-8"))
        meta["inject_count"] = meta.get("inject_count", 0) + 1
        meta["last_inject_iso"] = now_iso()
        META_FILE.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except: pass

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }, ensure_ascii=False))
    return ctx

def status():
    print("=== CLS 编年史 ===")
    if CHRONICLE_FILE.exists():
        try:
            c = json.loads(CHRONICLE_FILE.read_text(encoding="utf-8"))
            sz = CHRONICLE_FILE.stat().st_size
            domains = c.get("domains", {})
            total_ms = sum(len(d.get("milestones",[])) for d in domains.values())
            print(f"  概述: {c.get('overview','')[:100]}")
            print(f"  领域: {len(domains)} | 里程碑: {total_ms} | 大小: {sz}B")
            print(f"  版本: v{c.get('version',0)}")
            for dname, ddata in domains.items():
                ms = ddata.get("milestones", [])
                print(f"  ├ {dname}: {ddata.get('summary','')[:60]}")
                for m in ms[-3:]:
                    print(f"  │  {m['date']} {m['event'][:50]}")
        except Exception as e:
            print(f"  解析错误: {e}")
    else:
        print("  编年史不存在")

    if META_FILE.exists():
        m = json.loads(META_FILE.read_text(encoding="utf-8"))
        print(f"  上次扫描: {m.get('last_scan_iso','?')[:19]}")
        print(f"  注入次数: {m.get('inject_count', 0)}")
        print(f"  上次注入: {m.get('last_inject_iso','?')[:19]}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        full = "--full" in sys.argv
        c = build_chronicle(full_scan=full)
        print(f"Built: {len(c.get('timeline',[]))} entries, v{c.get('version',0)}")
    elif len(sys.argv) > 1 and sys.argv[1] == "inject":
        inject()
    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        status()
    else:
        print("用法: cls_chronicle.py build|build --full|inject|status")
