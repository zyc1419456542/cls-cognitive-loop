# PostCompact.ps1 — compact 完成审计 (2026-08-15 修复: 零注入输出)
# ================================================================
# CC 2.1.220 schema 不认 hookSpecificOutput.hookEventName="PostCompact" →
# JSON 整体被拒 + 每次 compact 报 hook failed (2026-08-14 暴毙教训)。
# 注入内容已并入 PreCompact 写的 precompact_recovery.json, 由 SessionStart
# (compact 后 source=compact 重触发) 读取合并注入后删除。
# 本 hook 只留副作用: compact 完成审计记录。fail-open 不变。

$ErrorActionPreference = 'Continue'
$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $PROJECT_ROOT) { $PROJECT_ROOT = $env:CLS_ROOT }

# compact 完成审计 (与 PreCompact 的 pre_compact 配对, 供认知循环恢复参考)
try {
    $compactLog = "$PROJECT_ROOT\data\pipeline\compact_log.jsonl"
    $logEntry = @{
        ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss.fffzzz")
        event = "post_compact"
        source = "CC_PostCompact_hook"
    } | ConvertTo-Json -Compress
    Add-Content -Path $compactLog -Value $logEntry -Encoding UTF8
} catch { }

exit 0
