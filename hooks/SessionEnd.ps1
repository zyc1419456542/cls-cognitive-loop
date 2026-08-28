# SessionEnd.ps1 — 会话最终收尾  |  v1.0
# ===============================================
# CC会话彻底结束时触发（进程退出前最后事件）。
# Stop hook 先触发，SessionEnd 是最后的安全网。
# fail-open: 不阻止CC退出。

$ErrorActionPreference = 'Continue'
$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $PROJECT_ROOT) { $PROJECT_ROOT = $env:CLS_ROOT }

if (Test-Path "$PROJECT_ROOT\data\state\emergency_bypass.flag") {
    
# ── CLS session清理 (2026-07-27) ──
try {
    $sid = $env:CLAUDE_SESSION_ID
    if ($sid) { $sid = $sid.Substring(0, [Math]::Min(8, $sid.Length)) }
    # 保留轨迹文件做审计, 清理临时文件
    Remove-Item "data\state\insight_$sid.md" -Force -ErrorAction SilentlyContinue
    Remove-Item "data\state\session_summary_$sid.md" -Force -ErrorAction SilentlyContinue
} catch {}

exit 0
}

# @fix 2026-08-02 incident-log#34: python→pythonw 静默化
$pythonExe = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) { $pythonExe = "pythonw" }

# ── 1. 最终状态写入 ──────────────────────────────────────────
$endState = @{
    ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss.fffzzz")
    event = "session_end"
    source = "CC_SessionEnd_hook"
} | ConvertTo-Json -Compress
try {
    $endState | Set-Content -Path "$PROJECT_ROOT\data\memory\last_operation.json" -Encoding UTF8
} catch { }

# [已移除 2026-07-04] 2. 推daemon最终心跳
# ── 3. 最终跨窗口更新 ────────────────────────────────────────
& $pythonExe "$PROJECT_ROOT\scripts\wheels\cross_window_hook.py" auto_update 2>$null | Out-Null

# ── 4. 清理临时标记 ──────────────────────────────────────────
@(".compact_flag", ".porter_reminder") | ForEach-Object {
    $p = "$PROJECT_ROOT\$_"
    if (Test-Path $p) { try { Remove-Item $p -Force } catch { } }
}

# ── 5. 清理残留 worktree（2026-07-07 新增）────────────────────
# Workflow/Agent worktree 理论上 auto-clean，实测 14 个残留共 4GB。
# session 结束统一 prune。
try {
    Push-Location $PROJECT_ROOT 2>$null
    git worktree prune --expire=now 2>$null
    Pop-Location 2>$null
} catch { }


# ── CLS session清理 (2026-07-27) ──
try {
    $sid = $env:CLAUDE_SESSION_ID
    if ($sid) { $sid = $sid.Substring(0, [Math]::Min(8, $sid.Length)) }
    # 保留轨迹文件做审计, 清理临时文件
    Remove-Item "data\state\insight_$sid.md" -Force -ErrorAction SilentlyContinue
    Remove-Item "data\state\session_summary_$sid.md" -Force -ErrorAction SilentlyContinue
} catch {}

exit 0
