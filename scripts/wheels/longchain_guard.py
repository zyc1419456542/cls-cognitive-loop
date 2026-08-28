# longchain_guard.py — 长链推理笔记本守卫 v2 (2026-08-04)
# ======================================================
# 被 PreToolUse hook 调用。解决两个问题(maintainer定义, GitHub koi #505 自适应模式):
#   1. 长链漂移: 跑太久忘记初始目标/核心步骤
#   2. compact 后记忆丢失: (配合 PreCompact 快照 + PostCompact 注入)
#
# 核心: 周期注入目标锚定(koi 自适应间隔)——
#   - 每 N 轮注入 "⚠️无人状态, 目标X, 进度Y, 下一步Z"
#   - 连续 N 轮对齐 → 间隔翻倍(少打扰); 检测到漂移 → 重置回基础间隔(及时拉回)
#   - 措辞严厉: 明确告知无人状态, 要求谨慎防漂移
#
# 用法: python longchain_guard.py   (PreToolUse 调用, 有结论才输出 additionalContext)

import json, os, sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent
STATE = ROOT / "data" / "longchain" / "_state"
NOTEBOOK = ROOT / "data" / "longchain"
STATE_FILE = STATE / "guard_state.json"     # 自适应间隔状态
GOAL_FILE = STATE / "goal.txt"              # 当前任务目标+步骤进度(模型维护)
LAST_GOAL_FILE = STATE / "last_goal.txt"    # 上次注入时的目标(用于漂移检测)

BASE_INTERVAL = 5        # 基础间隔: 每5轮注入一次提醒
MAX_INTERVAL = 20        # 自适应上限: 最多20轮一次
ALIGN_THRESHOLD = 3      # 连续3次对齐 → 间隔翻倍
DRIFT_RESET = 1          # 检测到漂移 → 重置到基础间隔


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_state() -> dict:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"interval": BASE_INTERVAL, "rounds_since": 0, "aligned_streak": 0,
            "window_id": os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:12]}


def _save_state(s: dict):
    STATE.mkdir(parents=True, exist_ok=True)
    s["window_id"] = os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:12]
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


def _read_goal() -> str:
    """读当前目标(模型写在 goal.txt)。无则返回空(无法锚定, 不注入)。"""
    try:
        if GOAL_FILE.is_file():
            g = GOAL_FILE.read_text(encoding="utf-8").strip()
            return g[:200]
    except Exception:
        pass
    return ""


def _detect_drift(current_goal: str) -> bool:
    """漂移检测: 与上次注入时的目标对比。目标变化或工具序列偏离 → 视为漂移。"""
    try:
        if LAST_GOAL_FILE.is_file():
            last = LAST_GOAL_FILE.read_text(encoding="utf-8").strip()
            # 目标被重写(用户/模型改了目标) → 算对齐(新目标, 重置基线)
            if last and current_goal and last[:30] != current_goal[:30]:
                return True
    except Exception:
        pass
    return False


def main() -> None:
    s = _load_state()
    goal = _read_goal()
    if not goal:
        return  # 无目标文件(任务刚启动或非长任务) → 不注入

    # 轮次推进
    s["rounds_since"] = s.get("rounds_since", 0) + 1
    interval = s.get("interval", BASE_INTERVAL)

    # 未到间隔 → 不注入(但继续累计轮次)
    if s["rounds_since"] < interval:
        _save_state(s)
        return

    # 到间隔了 → 注入
    drift = _detect_drift(goal)

    if drift:
        # 漂移 → 重置基础间隔, 严厉提醒
        s["interval"] = BASE_INTERVAL
        s["aligned_streak"] = 0
        tip = (
            f"⚠️【无人状态·目标锚定】检测到你最近的目标或方向与任务基线不一致, 已重置提醒频率。"
            f"你现在处于无人值守状态, 推理必须谨慎防漂移。\n"
            f"当前目标: {goal}\n"
            f"请: ①确认没有偏离初始任务 ②若偏离, 回到正轨 ③把最新进度写入 data/longchain 笔记本"
        )
    else:
        # 对齐 → 连续 streak, 满 3 次间隔翻倍
        s["aligned_streak"] = s.get("aligned_streak", 0) + 1
        if s["aligned_streak"] >= ALIGN_THRESHOLD and interval < MAX_INTERVAL:
            s["interval"] = interval * 2
            s["aligned_streak"] = 0
        tip = (
            f"⚠️【无人状态·目标锚定】你已独自工作多轮, 请警惕长链漂移。"
            f"当前目标: {goal}\n"
            f"推理提示: ①复核这一步是否符合总目标 ②记录已推进的核心步骤 ③明确下一步。"
            f"将进度写入 data/longchain 笔记本, 防止 compact 丢失记忆。"
        )

    # 记录本次注入时的目标(供下次漂移检测)
    s["rounds_since"] = 0
    _save_state(s)
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        LAST_GOAL_FILE.write_text(goal, encoding="utf-8")
    except Exception:
        pass

    wid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:12]
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": tip + f" (窗口 {wid}, 间隔 {s['interval']} 轮)",
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open: 守卫失败不阻塞工具调用
