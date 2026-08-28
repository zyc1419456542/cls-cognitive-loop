# PostToolUseFailure.ps1 — 工具失败自动学习  |  v1.0
# ===============================================
# CC工具调用失败时自动触发，提取错误信息调用failure_learner记录。
# fail-open: 从不阻止CC操作。
#
# 触发: CC的 PostToolUseFailure 事件
# 轮子: scripts/wheels/failure_learner.py

$ErrorActionPreference = 'Continue'
$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $PROJECT_ROOT) { $PROJECT_ROOT = $env:CLS_ROOT }

# ── 0. 应急门快速绕过 ─────────────────────────────────────────
if (Test-Path "$PROJECT_ROOT\data\state\emergency_bypass.flag") {
    exit 0
}

# ── 1. 读取 CC 传入的事件 JSON ─────────────────────────────────
$inputJson = ""
try {
    $inputJson = $input | Out-String
    if ([string]::IsNullOrWhiteSpace($inputJson)) {
        exit 0
    }
} catch {
    exit 0
}

try {
    $event = $inputJson | ConvertFrom-Json
} catch {
    exit 0
}

# ── 2. 提取失败信息 ───────────────────────────────────────────
$toolName = if ($event.tool_name) { $event.tool_name } else { "unknown_tool" }
$errorMsg = ""
if ($event.error) {
    $errorMsg = $event.error
} elseif ($event.error_message) {
    $errorMsg = $event.error_message
} elseif ($event.tool_result_error) {
    $errorMsg = $event.tool_result_error
} else {
    $errorMsg = "tool execution failed (no error detail)"
}

# 截断过长的错误信息 (防止命令行参数溢出)
$maxLen = 200
if ($errorMsg.Length -gt $maxLen) {
    $errorMsg = $errorMsg.Substring(0, $maxLen) + "..."
}

# 构造reason: 工具名+错误摘要
$reason = "$toolName`n$errorMsg" -replace '"', "'"
$cause = "CC PostToolUseFailure hook auto-capture: $toolName"
$lesson = "Auto-captured by PostToolUseFailure hook. Review error pattern."

# ── 3. 调用 failure_learner ───────────────────────────────────
$pythonExe = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) {
    $pythonExe = "pythonw"
}

$learnerScript = "$PROJECT_ROOT\scripts\wheels\failure_learner.py"
if (Test-Path $learnerScript) {
    $proc = Start-Process -FilePath $pythonExe -ArgumentList @(
        $learnerScript,
        "record",
        "--reason", $reason,
        "--cause", $cause,
        "--lesson", $lesson,
        "--type", "tool_error"
    ) -NoNewWindow -Wait -PassThru
}

exit 0
