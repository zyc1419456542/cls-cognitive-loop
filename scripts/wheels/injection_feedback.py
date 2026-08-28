# injection_feedback.py — 注入问卷+打分收卷器 (A+B, 2026-08-03; 丙方案合一, 2026-08-21 maintainer定)
# ======================================================
# 在 Stop hook 调用: 扫描最近 transcript 中 Claude 回复里的
#   [注入回应:<id>:采纳|参考|忽略] 标记 (问卷路, semantic_inject/process_inject 注入端带标记)
#   [注入打分:<类型>:<1|0|-1>] 标记 (打分路, semantic_inject 质量反馈段要求输出)
# 统一写入 data/state/injection_feedback.jsonl (与 inject_feedback_analyzer.py 消费路径一致)。
# @fix 2026-08-21 路径统一: 原 LOG=data/injection_feedback.jsonl 与 analyzer 读的
#   data/state/injection_feedback.jsonl 不一致 → 两路数据19天永不互通(考古结论)。
#   问卷行无 score 字段, analyzer load_feedback(type+score双检查)自动跳过, 同文件安全共存。
# 目的: 让"注入质量"可观测 — 知道哪些注入器/类型被采纳, 哪些是噪音。
# 用法: python injection_feedback.py   (Stop hook 无参调用) | summary (手动统计)

import json, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOG = ROOT / "data" / "state" / "injection_feedback.jsonl"
GI_FILE = ROOT / ".claude" / "cls_state" / ".guidance_injection"

# 标记格式1: [注入回应:<id>:采纳|参考|忽略]
# @fix 2026-08-15: id 放宽为非冒号任意串(模型按屏幕标签回应如"语义路由L4认知循环", 原 8hex 永不匹配 → 收卷永远0)
_PAT = re.compile(r"\[注入回应:([^:\]]+):(采纳|参考|忽略)\](.*)$", re.MULTILINE)
# 标记格式2 (丙方案 2026-08-21): [注入打分:<类型>:<1|0|-1>]
_SCORE_PAT = re.compile(r"\[注入打分:([^:\]]+):([\-01]+)\]")


def _find_transcripts():
    """定位本会话 transcript(从 CLAUDE_CODE_SESSION_ID env 推导)。"""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid:
        return []
    # 形如: <project_dir>/<session_id>.jsonl
    # 项目目录由会话哈希决定, 扫 ~/.claude/projects/ 下所有匹配 sid 的文件
    base = Path(os.environ.get("USERPROFILE", "<HOME>")) / ".claude" / "projects"
    if not base.is_dir():
        return []
    hits = []
    for d in base.iterdir():
        f = d / f"{sid}.jsonl"
        if f.is_file():
            hits.append(f)
    return hits


_FRESH_SECONDS = 120  # 注入后 2 分钟内视为"新鲜"(Stop 每轮跑, 只在有新鲜注入时才要求回应)


def _collect_pending() -> dict:
    """从 .guidance_injection 拿到最近注入的 id/source/type(用于关联回应)。

    2026-08-04(二号建议): 只认"新鲜"注入(ts 在 _FRESH_SECONDS 内) —
    否则 Stop 每轮都拿着旧注入 id 要求回应, 过度打扰。注入类型为"无"时返回空。
    """
    try:
        if GI_FILE.is_file():
            g = json.loads(GI_FILE.read_text(encoding="utf-8"))
            iid = g.get("injection_id", "")
            ts = g.get("ts", "")
            # 注入类型显式"无" → 不要求回应
            if g.get("type") in ("none", "无", ""):
                return {}
            # ts 新鲜度检查
            if iid and ts:
                try:
                    from datetime import datetime, timezone
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    if (now - t).total_seconds() > _FRESH_SECONDS:
                        return {}
                except Exception:
                    pass  # ts 解析失败 → 保守视为新鲜(宁可不打扰过度, 不遗漏真实注入)
            if iid:
                return {"id": iid, "source": g.get("source", "?"), "type": g.get("type", "?")}
    except Exception:
        pass
    return {}


def maybe_prompt() -> str:
    """Stop hook 调用: 若本轮有新鲜注入, 输出注入提醒要求模型回应问卷。

    输出 Stop 事件的 additionalContext(对话继续, 模型可回应)。
    无新鲜注入 → 返回空(不打扰)。
    """
    pending = _collect_pending()
    if not pending:
        return ""
    return (
        f"📌 注入问卷(必答, 一行即可): 本轮收到的注入(#{pending['id']}, "
        f"{pending['source']}/{pending['type']}) 你【采纳|参考|忽略】? "
        f"请在回复中输出 [注入回应:{pending['id']}:采纳] 或 "
        f"[注入回应:{pending['id']}:参考] 或 [注入回应:{pending['id']}:忽略], 附一句话理由。"
    )


def scan_and_record():
    """扫 transcript 尾部(最近 ~3000 行)找回应标记, 记录。"""
    pending = _collect_pending()
    records = []
    for tf in _find_transcripts():
        tf = Path(tf)  # 2026-08-03: 字符串路径无 read_text → 静默吞错, 收卷永远 0
        try:
            lines = tf.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        # 2026-08-03: 只扫最后一条 assistant 消息 — 避免误收"对话中讨论问卷格式"的引用文本。
        # Stop 触发时本轮回复刚写完, 是最后一条 assistant。
        last_assistant = None
        for line in lines[-3000:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = entry.get("message") or {}
            if msg.get("role") == "assistant":
                last_assistant = entry
        if not last_assistant:
            continue
        content = (last_assistant.get("message") or {}).get("content")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    texts.append(c.get("text", ""))
        for t in texts:
            for m in _PAT.finditer(t):
                iid, verdict, reason = m.group(1), m.group(2), m.group(3).strip()[:200]
                records.append({
                    "ts": last_assistant.get("timestamp", ""),
                    "injection_id": iid,
                    "verdict": verdict,
                    "reason": reason,
                    "source": pending.get("source", ""),
                    "type": pending.get("type", ""),
                    "transcript": tf.name,
                })
            # 丙方案(2026-08-21): 打分标记 → analyzer 消费格式 {kind:"score", type, score}
            for m in _SCORE_PAT.finditer(t):
                fb_type, score = m.group(1).strip(), int(m.group(2))
                records.append({
                    "ts": last_assistant.get("timestamp", ""),
                    "kind": "score",
                    "type": fb_type,
                    "score": max(-1, min(1, score)),
                    "transcript": tf.name,
                })
    if not records:
        return 0
    # 去重: 对比已存在的日志(Stop 每轮跑, 跨调用不能重复追加)
    # 问卷行 key=(injection_id, verdict); 打分行 key=("score", type, score, ts到分钟) —
    # 同分钟同类型同分数视为同一次打分(Stop每轮重扫同一回复)。
    existing = set()
    if LOG.is_file():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                existing.add(_dedup_key(d))
            except json.JSONDecodeError:
                pass
    new_count = 0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    for r in records:
        key = _dedup_key(r)
        if key in existing:
            continue
        existing.add(key)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        new_count += 1
    return new_count


def _dedup_key(d: dict) -> tuple:
    """问卷行按 (injection_id, verdict) 去重; 打分行按 (kind, type, score, ts到分钟) 去重。"""
    if d.get("kind") == "score":
        return ("score", d.get("type", ""), d.get("score"), (d.get("ts") or "")[:16])
    return (d.get("injection_id", ""), d.get("verdict", ""))


def summary():
    """汇总统计(手动查看用)。问卷(采纳/参考/忽略)与打分(1/0/-1)分开统计。"""
    from collections import Counter
    survey = Counter()
    score = Counter()
    if LOG.is_file():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("kind") == "score":
                score[(d.get("type", "?"), d.get("score", "?"))] += 1
            else:
                survey[(d.get("source", "?") or d.get("type", "?"), d.get("verdict", "?"))] += 1
    print(f"注入反馈统计 ({LOG}):")
    if not survey and not score:
        print("  (暂无回应记录)")
    if survey:
        print("  ── 问卷 ──")
        for (src, v), n in sorted(survey.items()):
            print(f"  {src} → {v}: {n}")
    if score:
        print("  ── 打分 ──")
        for (t, s), n in sorted(score.items()):
            print(f"  {t} → {s}: {n}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        summary()
    elif len(sys.argv) > 1 and sys.argv[1] == "maybe_prompt":
        # Stop hook 调用: 有新鲜注入才输出问卷提醒(供 additionalContext 注入)
        print(maybe_prompt())
    else:
        n = scan_and_record()
        # 有回应就静默退出; 无回应也静默(Stop hook 不应输出 noise)
        sys.exit(0)
