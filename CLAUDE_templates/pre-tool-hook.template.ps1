# =============================================================================
# PreToolUse.ps1 — CLS Cognitive Loop Pre-Tool Hook Template
# =============================================================================
# Copy to: {PROJECT_ROOT}/.claude/hooks/PreToolUse.ps1
#
# Enforces 4 checks before every tool call:
#   1. COG_STEP — blocks Write/Edit if no step declared (TTL: 300s)
#   2. WRITE_PROTECT — blocks writes to protected file paths
#   3. RECURSION_LIMIT — caps nested tool call depth (max: 5)
#   4. AUDIT — logs every tool call for traceability
#
# Fill in {PLACEHOLDER}s before using.
# =============================================================================

param(
    [string]$tool_name,
    [string]$tool_input_json,
    [string]$workspace
)

# ── Configuration ───────────────────────────────────────────────────────────
$ProjectRoot = "{PROJECT_ROOT}"
$CogStepFile = Join-Path $ProjectRoot "data\state\cog_step.json"
$RecursionFile = Join-Path $ProjectRoot "data\state\recursion_depth.json"
$AuditLog = Join-Path $ProjectRoot "data\state\hook_audit.jsonl"

# Core files the AI must not modify without explicit override
$ProtectedPaths = @(
    (Join-Path $ProjectRoot "scripts\core-engine\fuse_board.py"),
    (Join-Path $ProjectRoot "scripts\core-engine\qwen_gate.py"),
    (Join-Path $ProjectRoot "CLAUDE.md"),
    (Join-Path $ProjectRoot ".claude\hooks\PreToolUse.ps1"),
    (Join-Path $ProjectRoot ".claude\hooks\PostToolUse.ps1"),
    (Join-Path $ProjectRoot ".claude\settings.json")
)

$MaxRecursionDepth = {MAX_RECURSION_DEPTH}
$CogStepTTL = {COG_STEP_TTL_SECONDS}

# ── Helper: Log verdict ────────────────────────────────────────────────────
function Write-AuditLog {
    param([string]$verdict, [string]$check, [string]$detail)
    $entry = @{
        ts      = (Get-Date -Format "o")
        tool    = $tool_name
        verdict = $verdict
        check   = $check
        detail  = $detail
    } | ConvertTo-Json -Compress
    Add-Content -Path $AuditLog -Value $entry -ErrorAction SilentlyContinue
}

# ═══════════════════════════════════════════════════════════════════════════
# CHECK 1: Cognitive Step Declaration (Write/Edit only)
# ═══════════════════════════════════════════════════════════════════════════
if ($tool_name -eq "Write" -or $tool_name -eq "Edit") {
    # Allow writing the cog_step declaration file itself
    try {
        $inputObj = $tool_input_json | ConvertFrom-Json
        $targetPath = $inputObj.file_path
    } catch { $targetPath = $tool_input_json }
    if ($targetPath -match "cog_step\.json$") {
        # Always allow writing the declaration file
    } else {
        if (-not (Test-Path $CogStepFile)) {
            $result = @{
                permissionDecision = "deny"
                permissionDecisionReason = @(
                    "[COG_STEP] Write/Edit blocked: no cognitive step declared.",
                    "Write cog_step.json first:",
                    '{"version":1,"phase":2,"label":"② Task Execution","declared_at":"2026-06-01T12:00:00Z"}'
                ) -join "`n"
            }
            Write-AuditLog "BLOCK" "COG_STEP" "missing cog_step.json"
            Write-Output ($result | ConvertTo-Json -Compress)
            exit 0
        }

        try {
            $cog = Get-Content $CogStepFile -Raw | ConvertFrom-Json
            $declaredAt = [datetime]::Parse($cog.declared_at)
            $age = [int]((Get-Date) - $declaredAt).TotalSeconds

            if ($age -gt $CogStepTTL) {
                $result = @{
                    permissionDecision = "deny"
                    permissionDecisionReason = "[COG_STEP] Step declaration expired ($age s > $CogStepTTL s TTL). Re-declare."
                }
                Write-AuditLog "BLOCK" "COG_STEP_EXPIRED" "age=$age s"
                Write-Output ($result | ConvertTo-Json -Compress)
                exit 0
            }
            Write-AuditLog "PASS" "COG_STEP" "phase=$($cog.phase) age=$age s"
        } catch {
            $result = @{
                permissionDecision = "deny"
                permissionDecisionReason = "[COG_STEP] cog_step.json corrupt: $_"
            }
            Write-AuditLog "BLOCK" "COG_STEP_PARSE" "$_"
            Write-Output ($result | ConvertTo-Json -Compress)
            exit 0
        }
    }
}

# ═══════════════════════════════════════════════════════════════════════════
# CHECK 2: Fuse Board — WRITE_PROTECT
# ═══════════════════════════════════════════════════════════════════════════
if ($tool_name -eq "Write" -or $tool_name -eq "Edit") {
    if ($targetPath) {
        $normalized = [System.IO.Path]::GetFullPath($targetPath)
        foreach ($protected in $ProtectedPaths) {
            $protNormalized = [System.IO.Path]::GetFullPath($protected)
            if ($normalized -eq $protNormalized) {
                $result = @{
                    permissionDecision = "deny"
                    permissionDecisionReason = "[FUSE:WRITE_PROTECT] Path blocked: $targetPath"
                }
                Write-AuditLog "BLOCK" "WRITE_PROTECT" "target=$targetPath"
                Write-Output ($result | ConvertTo-Json -Compress)
                exit 0
            }
        }
    }
}

# ═══════════════════════════════════════════════════════════════════════════
# CHECK 3: Fuse Board — RECURSION_LIMIT
# ═══════════════════════════════════════════════════════════════════════════
$currentDepth = 0
if (Test-Path $RecursionFile) {
    try {
        $recState = Get-Content $RecursionFile -Raw | ConvertFrom-Json
        $currentDepth = [int]$recState.depth
    } catch { }
}
$currentDepth++

if ($currentDepth -gt $MaxRecursionDepth) {
    $result = @{
        permissionDecision = "deny"
        permissionDecisionReason = "[FUSE:RECURSION_LIMIT] Depth $currentDepth > max $MaxRecursionDepth"
    }
    Write-AuditLog "BLOCK" "RECURSION_LIMIT" "depth=$currentDepth"
    Write-Output ($result | ConvertTo-Json -Compress)
    exit 0
}

@{ depth = $currentDepth; tool = $tool_name; ts = (Get-Date -Format "o") } |
    ConvertTo-Json | Set-Content $RecursionFile -Force

# ═══════════════════════════════════════════════════════════════════════════
# CHECK 4: Audit
# ═══════════════════════════════════════════════════════════════════════════
Write-AuditLog "ALLOW" "PASSED" "depth=$currentDepth"

$result = @{
    permissionDecision = "allow"
    permissionDecisionReason = "[CLS] All checks passed (depth=$currentDepth)"
}
Write-Output ($result | ConvertTo-Json -Compress)
