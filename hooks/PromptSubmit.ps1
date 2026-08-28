# PromptSubmit.ps1 — 危险指令闸门 (fail-open)
# 只阻断明确的破坏性操作指令

$ErrorActionPreference = "Stop"

# ── 符号动力学交付记录辅助函数 ──
function _WriteSymbolicDelivery {
    param([string]$verdict, [string]$reason, [string]$textPreview)
    try {
        $deliveryDir = Join-Path $PWD "assistant交付\🔍 符号动力学审计"
        if (-not (Test-Path $deliveryDir)) {
            New-Item -ItemType Directory -Force -Path $deliveryDir | Out-Null
        }
        $ts = Get-Date -Format "yyyyMMddTHHmmss"
        $tag = $reason -replace '[<>:"/\\|?*]', ''
        if ($tag.Length -gt 60) { $tag = $tag.Substring(0, 60) }
        $fname = "${ts}_${verdict}_$tag.json"
        $delivery = @{
            ts = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
            verdict = $verdict
            reason = $reason
            text_preview = if ($textPreview.Length -gt 300) { $textPreview.Substring(0, 300) } else { $textPreview }
        } | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText(
            [System.IO.Path]::Combine($deliveryDir, $fname),
            $delivery,
            [System.Text.Encoding]::UTF8
        )
    } catch {}
}

try {
    $raw = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($raw)) { exit 0 }

    $input = $raw | ConvertFrom-Json
    $prompt = $input.prompt

    # === CHECK 1: 危险系统命令 → DENY ===
    # 危险模式列表 — 匹配任一即 DENY
    $dangerous = @(
        'rm\s+-rf\s+/',           # 删除根目录
        ':\s*\(\s*\)\s*\{\s*:\|:', # fork bomb
        ':\(\)\s*\{',              # fork bomb 变体
        'mkfs\.\w+\s+/dev/',      # 格式化设备
        'dd\s+if=.*of=/dev/',     # 直接写设备
        '>\s*/dev/sd[a-z]',      # 重定向覆盖磁盘
        'format\s+[A-Z]:\s*/',   # Windows format
        'Remove-Item\s+-Recurse\s+-Force\s+C:', # PowerShell 递归删系统盘
        'del\s+/F\s+/S\s+C:\\'   # cmd 递归删系统盘
    )

    foreach ($pattern in $dangerous) {
        if ($prompt -match $pattern) {
            $decision = @{permissionDecision="deny"; permissionDecisionReason="🔴 危险指令 — 匹配模式: $pattern"}
            _WriteSymbolicDelivery -verdict "deny" -reason "🔴 危险指令 — 匹配模式: $pattern" -textPreview $prompt
            Write-Output ($decision | ConvertTo-Json -Compress)
            exit 0
        }
    }

    # === CHECK 2 [SYMBOLIC]: 符号动力学裁决 → DENY/ASK ===
    # 连接"计算层→执行层"，每次用户输入都过符号审计
    $verdictFile = Join-Path $PWD "data/symbolic_dynamics/symbolic_verdict.json"
    if (Test-Path $verdictFile) {
        try {
            $verdict = Get-Content $verdictFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $ts = [DateTime]$verdict.ts
            $age = [DateTime]::UtcNow - $ts
            if ($age.TotalMinutes -lt 30) {
                # 检查用户 prompt 是否命中禁止词
                $fwFile = Join-Path $PWD "data/symbolic_dynamics/forbidden_words.json"
                if (Test-Path $fwFile) {
                    $fwData = Get-Content $fwFile -Raw -Encoding UTF8 | ConvertFrom-Json
                    foreach ($pattern in $fwData.p0_patterns) {
                        if ($prompt -match $pattern) {
                            $decision = @{permissionDecision='deny'; permissionDecisionReason="[SYMBOLIC_FORBIDDEN_P0] Prompt matches P0 forbidden pattern: $pattern"}
                            _WriteSymbolicDelivery -verdict "deny" -reason "[SYMBOLIC_FORBIDDEN_P0] $pattern" -textPreview $prompt
                            Write-Output ($decision | ConvertTo-Json -Compress)
                            exit 0
                        }
                    }
                    foreach ($pattern in $fwData.p1_patterns) {
                        if ($prompt -match $pattern) {
                            $decision = @{permissionDecision='ask'; permissionDecisionReason="[SYMBOLIC_FORBIDDEN_P1] Prompt matches P1 warning pattern: $pattern"}
                            _WriteSymbolicDelivery -verdict "ask" -reason "[SYMBOLIC_FORBIDDEN_P1] $pattern" -textPreview $prompt
                            Write-Output ($decision | ConvertTo-Json -Compress)
                            exit 0
                        }
                    }
                }
            }
        } catch {
            # fail-open
        }
    }

    # 一切通过
    exit 0

} catch {
    # fail-open
    exit 0
}
