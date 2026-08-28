#!/usr/bin/env python3
"""
_run_hidden.py — 零窗口钩子启动器
================================
问题: PowerShell -WindowStyle Hidden 在 Win10 上仍可能闪烁窗口。
方案: Python subprocess(CREATE_NO_WINDOW | DETACHED_PROCESS) 真正零窗口。
用法: python _run_hidden.py <hook_script.ps1>
       stdin→hook→stdout/stderr 全转发，CC 钩子协议透明兼容。
"""
import subprocess, sys, os
from pathlib import Path

# ── CWD 漂移防御 ─────────────────────────
# hook 命令可能从任意 CWD 被调用（CC 的 CWD 会漂到子目录）。
# 用此脚本自身位置反推项目根，把所有相对路径解析为绝对路径。
_SELF = Path(__file__).resolve()
_PROJECT_ROOT = _SELF.parent.parent.parent  # .claude/hooks/_run_hidden.py → 项目根

HOOK_SCRIPT = sys.argv[1] if len(sys.argv) > 1 else None
if not HOOK_SCRIPT:
    sys.exit(1)

# 解析路径：绝对路径直接用，相对路径从项目根解析
_hook_path = Path(HOOK_SCRIPT)
if not _hook_path.is_absolute():
    _hook_path = (_PROJECT_ROOT / _hook_path).resolve()
HOOK_SCRIPT = str(_hook_path)

# 读 CC 传入的 stdin（钩子输入 JSON）
try:
    stdin_data = sys.stdin.buffer.read()
except Exception:
    stdin_data = b""

# 零窗口 spawn PowerShell
# ⚠️ 不能用 DETACHED_PROCESS — 会导致 PS 的 [Console]::OpenStandardInput() 失效
#   （PreToolUse.ps1 依赖此 API 读取 CC 传入的钩子输入 JSON）
# 0624: 添加 STARTUPINFO.wShowWindow=SW_HIDE 双保险。
#   Win11 上 CREATE_NO_WINDOW 单独偶有闪窗，加 STARTUPINFO 彻底消除。
startupinfo = None
if sys.platform == "win32":
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
proc = subprocess.Popen(
    [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle", "Hidden",
        "-ExecutionPolicy", "Bypass",
        "-File", HOOK_SCRIPT,
    ],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    startupinfo=startupinfo,
    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
)

# 转发 stdin → PS → 收集输出
try:
    stdout_data, stderr_data = proc.communicate(input=stdin_data, timeout=30)
except subprocess.TimeoutExpired:
    proc.kill()
    stdout_data, stderr_data = proc.communicate()

# 转发输出到 CC
# PowerShell on Windows outputs in system encoding (GBK on Chinese Windows).
# CC expects UTF-8 from hooks. Convert if system encoding is not UTF-8.
_SYS_ENC = "gbk" if sys.platform == "win32" else "utf-8"
if stdout_data:
    try:
        stdout_data = stdout_data.decode(_SYS_ENC).encode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass  # Already UTF-8 or unknown encoding, pass through as-is
    sys.stdout.buffer.write(stdout_data)
if stderr_data:
    try:
        stderr_data = stderr_data.decode(_SYS_ENC).encode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    sys.stderr.buffer.write(stderr_data)

sys.exit(proc.returncode)
