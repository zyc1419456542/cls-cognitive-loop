"""
外部锚点 — 防自指闭合的强制现实采样
====================================
用法 (AI读):
  python scripts/wheels/external_anchor.py --status   # 查看当前锚点状态
  python scripts/wheels/external_anchor.py --sample   # 采样当前现实状态并写入锚点文件

功能:
  在认知循环 step ① 态势感知中强制采样外部世界状态，
  防止系统只在内部自洽中循环（自指闭合）。

采样维度:
  1. git diff — 代码/文件是否被外部修改
  2. 时间 — 当前时间、距上次交互间隔
  3. 系统进程 — 后台守护进程状态
  4. 最近日志 — 任务完成/失败事件
  5. 熵源 — 量子噪声池是否在线

输出:
  写入 data/state/external_anchor.json
  AI 在 step ② 决策前必须读此文件。
"""
import sys, json, os, subprocess, time

# 防弹窗: 父进程无控制台(pythonw/计划任务)时 subprocess 会新建窗口
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent.parent.parent
ANCHOR_FILE = BASE / "data" / "state" / "external_anchor.json"
STATE_FILES = {
    "activation": BASE / "data" / "state" / "activation_state.json",
    "session_health": BASE / "data" / "state" / "session_health.json",
    "last_operation": BASE / "data" / "memory" / "last_operation.json",
    "entropy_pool": BASE / "data" / "flows" / "entropy_pool.json",
    "semantic_pid": BASE / "data" / "state" / "semantic_service.pid",
    "semantic_port": BASE / "data" / "state" / "semantic_service.port",
    "time_awareness": BASE / "data" / "state" / "time_awareness.json",
}

# Ollama 可用性（替代废弃的 semantic_service daemon）
_OLLAMA_EMBED_MODEL = "nomic-embed-text"
_OLLAMA_URL = "http://localhost:11434"


def _read_json(path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"_error": "unreadable"}
    return None


def _run_git_diff() -> str:
    """快速检测是否有未提交变更"""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10,
            cwd=str(BASE), creationflags=_NO_WINDOW
        )
        return result.stdout.strip()[:100] or "clean"
    except Exception as e:
        return f"unavailable ({e})"


def _run_git_status() -> str:
    """检测是否有未跟踪文件"""
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10,
            cwd=str(BASE), creationflags=_NO_WINDOW
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        return f"{len(lines)} changes" if lines else "clean"
    except Exception as e:
        return f"unavailable ({e})"


def _process_count(pattern: str) -> int:
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {pattern}"],
                capture_output=True, text=True, encoding="gbk", errors="replace", timeout=5,
                creationflags=_NO_WINDOW
            )
            return result.stdout.count(pattern)
        else:
            result = subprocess.run(
                ["pgrep", "-c", pattern],
                capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW
            )
            return int(result.stdout.strip() or 0)
    except Exception:
        return -1


def sample() -> dict:
    """采集外部现实快照"""
    ts = datetime.now()
    git_diff = _run_git_diff()
    git_status = _run_git_status()

    # 系统进程
    python_procs = _process_count("python.exe" if os.name == "nt" else "python")

    # 读取状态文件
    activation = _read_json(STATE_FILES["activation"])
    session_health = _read_json(STATE_FILES["session_health"])
    last_op = _read_json(STATE_FILES["last_operation"])
    entropy = _read_json(STATE_FILES["entropy_pool"])
    time_awareness = _read_json(STATE_FILES["time_awareness"])

    # Ollama embedding 可用？（替代废弃的 semantic_service daemon）
    ollama_ok = False
    try:
        import urllib.request, json
        req = urllib.request.Request(f"{_OLLAMA_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            tags = json.loads(resp.read().decode())
            ollama_ok = any(
                _OLLAMA_EMBED_MODEL in t.get("name", "")
                for t in tags.get("models", [])
            )
    except Exception:
        pass

    anchor = {
        "timestamp": ts.isoformat(),
        "unix_ms": int(ts.timestamp() * 1000),
        "git": {
            "diff": git_diff,
            "status": git_status,
        },
        "system": {
            "python_processes": python_procs,
            "ollama_embedding_available": ollama_ok,
            "entropy_source": entropy.get("source", "offline") if entropy else "offline",
            "entropy_count": entropy.get("count", 0) if entropy else 0,
        },
        "state": {
            "activation_status": activation.get("status", "unknown") if activation else "unknown",
            "last_heartbeat": activation.get("last_heartbeat", "N/A") if activation else "N/A",
        },
        "session": {
            "msgs": session_health.get("msgs_current", session_health.get("msgs", "N/A")) if session_health else "N/A",
            "time_since_last": time_awareness.get("session", {}).get("elapsed_display", "N/A") if time_awareness else "N/A",
        },
        "last_operation": last_op.get("operation", "N/A") if last_op else "N/A",
    }

    # 写入锚点文件
    try:
        ANCHOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        ANCHOR_FILE.write_text(json.dumps(anchor, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[anchor] 写入失败: {e}")

    return anchor


def status():
    """展示当前锚点状态"""
    if ANCHOR_FILE.exists():
        try:
            anchor = json.loads(ANCHOR_FILE.read_text(encoding="utf-8"))
            print(f"外部锚点 (上次采样: {anchor.get('timestamp', 'N/A')})")
            print(f"  git diff: {anchor.get('git', {}).get('diff', 'N/A')}")
            print(f"  git status: {anchor.get('git', {}).get('status', 'N/A')}")
            print(f"  系统进程: {anchor.get('system', {}).get('python_processes', 'N/A')} python")
            print(f"  语义daemon: {'在线' if anchor.get('system', {}).get('semantic_daemon') else '离线'}")
            print(f"  熵源: {anchor.get('system', {}).get('entropy_source', 'N/A')}")
            print(f"  激活状态: {anchor.get('state', {}).get('activation_status', 'N/A')}")
            print(f"  消息计数: {anchor.get('session', {}).get('msgs', 'N/A')}")
            print(f"  上次操作: {anchor.get('last_operation', 'N/A')}")
        except Exception as e:
            print(f"锚点文件损坏: {e}")
    else:
        print("锚点文件不存在 — 尚未采样过")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scripts/wheels/external_anchor.py --status | --sample")
        sys.exit(1)

    mode = sys.argv[1]
    if mode == "--status":
        status()
    elif mode == "--sample":
        a = sample()
        sysd = a.get('system', {})
        daemon_ok = "[OK]" if sysd.get('semantic_daemon') else "[OFF]"
        print(f"[anchor] sample done | git: {a.get('git', {}).get('diff', 'N/A')[:60]} | daemon: {daemon_ok} | entropy: {sysd.get('entropy_source', 'N/A')}")
    else:
        print(f"未知模式: {mode}")
        sys.exit(1)
