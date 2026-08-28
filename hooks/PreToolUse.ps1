$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $PROJECT_ROOT) { $PROJECT_ROOT = $env:CLS_ROOT }
# PreToolUse.ps1 -- active cognitive gate (fail-open)
# Symbolic dynamics participates in real-time analysis
# Observation/audit happens in real-time, AND cron updates too

$ErrorActionPreference = 'Stop'

# ═══════════════════════════════════════════════════════════════
# v2 (2026-06-07): 辅助函数 — 不创建 conhost 的 Python 调用
# @fix 2026-08-02 incident-log#34 (二号融合): 统一改用 pythonw 静默调用（零 conhost，无弹窗）。
# ═══════════════════════════════════════════════════════════════
function _py {
    param([string[]]$Args, [string]$OutFile)
    try {
        $out = & pythonw $Args 2>$null | Out-String
        if ($OutFile) {
            [System.IO.File]::WriteAllText($OutFile, $out, [System.Text.Encoding]::UTF8)
        }
        return $out
    } catch {
        if ($OutFile) {
            [System.IO.File]::WriteAllText($OutFile, $_.Exception.Message, [System.Text.Encoding]::UTF8)
        }
        return $null
    }
}

# ── 符号动力学交付记录辅助函数 ──
# daemon 在线时由 daemon 的 _write_delivery 写，PS 侧也写一份（冗余但防漏）。
# daemon 崩了时这是唯一的审计记录通道。
function _WriteSymbolicDelivery {
    param([string]$verdict, [string]$reason, [string]$textPreview)
    try {
        $deliveryDir = Join-Path $PROJECT_ROOT "assistant交付\🔍 符号动力学审计"
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

# ── 审计落盘辅助函数 ──
# 所有闸门拦截(deny/ask)写入 pre_tool_audit.jsonl → symbolic_hook_audit.py 聚合 → assistant交付/🔍 符号动力学审计
function _WritePreToolAudit {
    param([string]$Decision, [string]$CheckName, [string]$Detail)
    try {
        $auditFile = Join-Path $PROJECT_ROOT "assistant交付\🔍 符号动力学审计\pre_tool_audit.jsonl"
        $entry = @{
            ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
            decision = $Decision
            check = $CheckName
            tool = $toolName
            detail = $Detail
        } | ConvertTo-Json -Compress
        $auditDir = Split-Path $auditFile -Parent
        if (-not (Test-Path $auditDir)) { New-Item -ItemType Directory -Force -Path $auditDir | Out-Null }
        Add-Content -Path $auditFile -Value $entry -ErrorAction SilentlyContinue
    } catch {}
}

# ── 静默审计日志（不弹窗、不阻塞、仅落盘） ──
# 所有 _EmitDecision cooldown / 非阻塞告警走此通道
function _SilentLog {
    param([string]$CheckName, [string]$Message)
    try {
        $entry = @{
            ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
            check = $CheckName
            message = $Message
            tool = if ($toolName) { $toolName } else { "unknown" }
        } | ConvertTo-Json -Compress
        $auditFile = Join-Path $PROJECT_ROOT "assistant交付\🔍 符号动力学审计\pre_tool_audit.jsonl"
        Add-Content -Path $auditFile -Value $entry -ErrorAction SilentlyContinue
    } catch {}
}

# === EMERGENCY BYPASS GATE ===
# If emergency_bypass.flag exists and <30min old, emit ALLOW and exit immediately.
$__bp = Join-Path $PWD 'data\state\emergency_bypass.flag'
if (Test-Path $__bp) {
    $__ts = (Get-Item $__bp).LastWriteTime
    if ((Get-Date) - $__ts -lt [TimeSpan]::FromMinutes(30)) {
        $bypassOut = @{hookSpecificOutput=@{hookEventName="PreToolUse";permissionDecision="allow";permissionDecisionReason="[EMERGENCY BYPASS]"}} | ConvertTo-Json -Compress
        [Console]::Out.WriteLine($bypassOut)
        exit 0
    }
}
Remove-Variable __bp, __ts -ErrorAction SilentlyContinue
# === END BYPASS GATE ===

# ── 0624: 弹窗冷却 — 同一检查类型 10min 内不重复弹 ──
# 升级 v2 (0627): 所有产生 Ask/Deny 的 check 都要过这道闸
#   之前只对 CODE_REVIEW 生效，现在全局统一的 cooldown 函数
$COOLDOWN_FILE = Join-Path $PWD 'data\state\hook_cooldown.json'
$COOLDOWN_SECS = 600  # 10分钟

function _EnterCooldown {
    param([string]$CheckName)
    # 记录本次拦截时间戳（供 _IsCooldown 判断）
    try {
        $cdNew = @{}
        if (Test-Path $COOLDOWN_FILE) {
            $existing = Get-Content $COOLDOWN_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($p in $existing.PSObject.Properties) { $cdNew[$p.Name] = $p.Value }
        }
        $cdNew[$CheckName] = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
        $cdNew | ConvertTo-Json -Compress | Set-Content $COOLDOWN_FILE -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch {}
}

function _IsCooldown {
    param([string]$CheckName)
    try {
        if (Test-Path $COOLDOWN_FILE) {
            $cd = Get-Content $COOLDOWN_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($cd.PSObject.Properties.Name -contains $CheckName) {
                $lastAsk = [datetime]::Parse($cd.$CheckName)
                if (([datetime]::UtcNow - $lastAsk.ToUniversalTime()).TotalSeconds -lt $COOLDOWN_SECS) {
                    return $true
                }
            }
        }
    } catch {}
    return $false
}

function _EmitDecision {
    param([string]$Decision, [string]$CheckName, [string]$Reason)
    # 冷却中 + 不是 deny → 静默放行（deny 永远执行）
    if ($Decision -ne 'deny' -and (_IsCooldown -CheckName $CheckName)) {
        _SilentLog $CheckName "COOLDOWN skip: $CheckName (cooling, was='$Decision')"
        return
    }
    _EnterCooldown -CheckName $CheckName
    # 🔴 $Decision 是 [string] 类型参数，$decision 在 PS 大小写不敏感下是同一变量
    #   赋值 @{} 到 [string] → 隐式 .ToString() → "System.Collections.Hashtable"
    #   必须用不同名：$decObj $dInfo 等
    # @fixed 2026-07-23: CC hooks要求hookSpecificOutput信封; exit2不拦截Write/Edit(#13744)→改用exit0
    $decObj = @{
        hookSpecificOutput = @{
            hookEventName = "PreToolUse"
            permissionDecision = $Decision
            permissionDecisionReason = $Reason
        }
    }
    _WritePreToolAudit $Decision $CheckName $CheckName
    [Console]::Out.WriteLine(($decObj | ConvertTo-Json -Compress))
    exit 0
}

# === DIAGNOSTIC: 验证钩子是否被执行 ===
$diagLog = Join-Path $PROJECT_ROOT "assistant交付\🔍 符号动力学审计\hook_diag.jsonl"
try {
    $diagEntry = @{ts=(Get-Date -Format "o"); event="hook_invoked"; tool=$null; stdin_len=0; parse="pending"} | ConvertTo-Json -Compress
    Add-Content -Path $diagLog -Value $diagEntry -ErrorAction SilentlyContinue
} catch {}

try {
    # 🔴 2026-06-07 FIX v2: PS 5.1 [Console]::InputEncoding = UTF8 无效。
    # 直接读原始字节流，手动 UTF-8 解码，完全绕过控制台编码层。
    $stdin = [Console]::OpenStandardInput()
    $ms = New-Object System.IO.MemoryStream
    $buffer = New-Object byte[] 4096
    while (($read = $stdin.Read($buffer, 0, $buffer.Length)) -gt 0) {
        $ms.Write($buffer, 0, $read)
    }
    $raw = [System.Text.Encoding]::UTF8.GetString($ms.ToArray())
    $ms.Dispose()
    if ([string]::IsNullOrWhiteSpace($raw)) {
        try { Add-Content -Path $diagLog -Value (@{ts=(Get-Date -Format "o"); event="stdin_empty"; exit_code=0} | ConvertTo-Json -Compress) -ErrorAction SilentlyContinue } catch {}
        exit 0
    }

    $hookInput = $raw | ConvertFrom-Json
    $toolName = $hookInput.tool_name
    $toolInput = $hookInput.tool_input
    try { Add-Content -Path $diagLog -Value (@{ts=(Get-Date -Format "o"); event="parsed"; tool=$toolName; stdin_len=$raw.Length} | ConvertTo-Json -Compress) -ErrorAction SilentlyContinue } catch {}

    # ── 操作频率监控 (2026-07-26; 2026-08-01 二号融合修: Bash传真实命令首行, 不传整包防截断) ──
    try {
        $toolInputStr = ""
        if ($toolName -eq 'Bash') {
            if ($toolInput.command) {
                $toolInputStr = [string]$toolInput.command
                if ($toolInputStr -match '^\s*(python|python3|py)\s+((-\w+)\s+)*-c\b') {
                    # @fix 2026-08-24: python -c 多行脚本只记首行会退化成"python -c"(全部同指纹),
                    # 记前200字符保留代码内容供死循环指纹判定
                    $toolInputStr = $toolInputStr -replace '\s+', ' '
                    if ($toolInputStr.Length -gt 200) { $toolInputStr = $toolInputStr.Substring(0, 200) }
                } else {
                    # 只取命令首行(多行脚本只记头部, 供指纹/语义判断足够)
                    $toolInputStr = ($toolInputStr -split "`r?`n")[0]
                }
            }
        } elseif ($toolInput) {
            $toolInputStr = ($toolInput | ConvertTo-Json -Compress -Depth 1)
        }
        $opsScript = "scripts\wheels\ops_monitor.py"
        if (Test-Path $opsScript) {
            # 窗口隔离(P1): 把 hook 的 session_id 注入 env, pythonw 子进程继承写入 ops_freq
            $hookSid = $hookInput.session_id
            if ($hookSid) { $env:CLAUDE_SESSION_ID = [string]$hookSid }
            & pythonw $opsScript record $toolName $toolInputStr 2>$null
            # ── drift_v2 越界告警读取 (2026-08-18) ──
            $driftAlertFile = "data\state\drift_v2_alert.json"
            if (Test-Path $driftAlertFile) {
                try {
                    $da = Get-Content $driftAlertFile -Raw | ConvertFrom-Json
                    $daAge = (Get-Date) - [DateTime]::Parse($da.ts)
                    if ($daAge.TotalSeconds -lt 30) {
                        Write-Host $da.alert
                        Remove-Item $driftAlertFile -Force -ErrorAction SilentlyContinue
                    }
                } catch {}
            }
        }
    } catch {}  # fail-open, 不影响主流程

    # ── Agent 限流 (2026-07-27 二号融合): 仅 Agent 50次/小时硬上限 (Task 不误伤) ──
    if ($toolName -eq 'Agent') {
        try {
            $agentCount = 0
            $now = [double](Get-Date -UFormat %s)
            $opsFile = "data\state\ops_freq.jsonl"
            if (Test-Path $opsFile) {
                Get-Content $opsFile -Tail 200 | ForEach-Object {
                    if ($_ -match '"tool": "Agent"') {
                        if ($_ -match '"ts": ([0-9.]+)') {
                            $callTs = [double]$matches[1]
                            if ($now - $callTs -lt 3600) { $agentCount++ }
                        }
                    }
                }
            }
            if ($agentCount -ge 50) {
                Write-Output '{"continue":false,"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Agent limit: 50/小时已达上限。建议: 合并任务,减少子代理数量,或等冷却。"}}'
                exit 0
            }
        } catch {}
    }

    # ── Bash失败循环检测 (2026-07-28; 2026-08-02 二号融合修: 诊断/探活豁免 + 同命令指纹判定 + 防死锁) ──
    # @fix 2026-08-02 maintainer批准: 探索/诊断动作(echo/Get-Process/tasklist等)不违规, 本身是尝试。
    # 现改为: 仅"同命令指纹原样重试≥5"判失败循环; 诊断/探活/python/cd 豁免。
    if ($toolName -eq 'Bash') {
        try {
            $curCmd = ""
            if ($toolInput.command) { $curCmd = [string]$toolInput.command }
            # 诊断/探活/高频开发命令豁免 — 无副作用检查环境, 本身是尝试, 不视为失败重试
            if ($curCmd -and $curCmd -notmatch '^\s*(echo|Get-Process|tasklist|taskkill|dir|ls|pwd|whoami|hostname|netstat|systeminfo|head|tail|Get-Content|Test-Path|Get-Location|Measure-Object|ps|python|python3|pythonw|py|cd|Set-Location)\b') {
                $opsFile = "data\state\ops_freq.jsonl"
                $fpCounts = @{}
                if (Test-Path $opsFile) {
                    Get-Content $opsFile -Tail 30 | ForEach-Object {
                        if ($_.Trim()) {
                            try {
                                $obj = $_ | ConvertFrom-Json
                                if ($obj.tool -eq 'Bash') {
                                    $cmd = [string]$obj.input_preview
                                    if ($cmd -match '^\s*(echo|Get-Process|tasklist|taskkill|dir|ls|pwd|whoami|hostname|netstat|systeminfo|head|tail|Get-Content|Test-Path|Get-Location|Measure-Object|ps|python|python3|pythonw|py|cd|Set-Location)\b') { continue }
                                    $tokens = $cmd -split '\s+'
                                    $fp = ""
                                    if ($tokens.Count -ge 1 -and $tokens[0]) { $fp = $tokens[0] }
                                    if ($tokens.Count -ge 2 -and $tokens[1]) { $fp += " " + $tokens[1] }
                                    if ($fp) {
                                        if ($fpCounts.ContainsKey($fp)) { $fpCounts[$fp]++ } else { $fpCounts[$fp] = 1 }
                                    }
                                }
                            } catch {}
                        }
                    }
                }
                $loopFp = $null
                $loopCount = 0
                foreach ($k in $fpCounts.Keys) {
                    if ($fpCounts[$k] -ge 5) { $loopFp = $k; $loopCount = $fpCounts[$k]; break }
                }
                if ($loopFp) {
                    Write-Output ('{"continue":false,"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Bash命令原样重试' + $loopCount + '次 (' + $loopFp + '), 疑似失败循环。请先WebSearch/Read找根因, 或改用诊断命令探活(echo/Get-Process/tasklist)。"}}')
                    exit 0
                }
            }
            # @fix 2026-08-24 maintainer定: python -c 内联脚本豁免导致死循环漏拦(当日fitz提图同前缀重跑5次无进展)。
            # 判据: 命令前60字符(归一空白)为指纹, Bash记录内同指纹≥4次 → deny。deny只把reason喂回模型,
            # 模型换路继续, 不停止会话(=error级反馈)。仅限 -c 内联(跑文件属正常开发循环, 不在此列)。
            if ($curCmd -match '^\s*(python|python3|py)\s+((-\w+)\s+)*-c\b') {
                $pcFp = ($curCmd -replace '\s+', ' ').Trim()
                if ($pcFp.Length -gt 60) { $pcFp = $pcFp.Substring(0, 60) }
                $opsFile2 = "data\state\ops_freq.jsonl"
                $pcCount = 0
                if (Test-Path $opsFile2) {
                    Get-Content $opsFile2 -Tail 25 | ForEach-Object {
                        if ($_.Trim()) {
                            try {
                                $obj2 = $_ | ConvertFrom-Json
                                if ($obj2.tool -eq 'Bash') {
                                    $c2 = ([string]$obj2.input_preview -replace '\s+', ' ').Trim()
                                    if ($c2.Length -gt 60) { $c2 = $c2.Substring(0, 60) }
                                    if ($c2 -eq $pcFp) { $pcCount++ }
                                }
                            } catch {}
                        }
                    }
                }
                if ($pcCount -ge 4) {
                    Write-Output ('{"continue":false,"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"[死循环拦截] 同一python -c内联脚本前缀已重复' + $pcCount + '次且无进展。这不是环境问题, 是逻辑卡住。禁止再原样重跑, 任选其一继续: ①打印中间变量定位卡点 ②Read/ls确认产出文件是否早已成功 ③换方法(分步执行/改用脚本文件)。本拦截不停止会话。"}}')
                    exit 0
                }
            }
        } catch {}
    }

    # ── 长链笔记本守卫 (2026-08-03): 人类不在场 + 距上次总结>30min → 注入总结指令 ──
    try {
        $lcGuard = "$PROJECT_ROOT\scripts\wheels\longchain_guard.py"
        if (Test-Path $lcGuard) {
            $lcOut = & pythonw $lcGuard 2>$null
            if ($lcOut) {
                Write-Output $lcOut
            }
        }
    } catch {}

    # === CHECK 1: AI vision + 对账标记 — Read png/jpg → 同步跑 image_processor → 写对账标记 → 放行 ===
    # 核验协议（2026-06-07）：AI 可以看图，但必须与 image_processor 的 ground_truth 对账。
    # hook 负责：①同步跑 image_processor 出 ground_truth ②写 pending_reconcile 标记
    # AI 负责：读 ground_truth → 产出中显式引用对账结果（hash/尺寸/主色调至少一项）
    # 交付闸门负责：检测未对账标记 → 提醒补充
    if ($toolName -eq 'Read') {
        $filePath = $toolInput.file_path
        if ($filePath -match '\.(png|jpg|jpeg|gif|bmp|webp)$') {
            $imageFullPath = if ([System.IO.Path]::IsPathRooted($filePath)) {
                $filePath
            } else {
                Join-Path $PWD $filePath
            }
            if (Test-Path $imageFullPath) {
                # 同步调用 image_processor（<1s，不创建 conhost）
                $gtOut = New-TemporaryFile
                try {
                    & pythonw scripts/wheels/image_processor.py $imageFullPath 2>$null | Out-File -FilePath $gtOut.FullName -Encoding utf8
                    $gtJson = [System.IO.File]::ReadAllText($gtOut.FullName, [System.Text.Encoding]::UTF8)
                    if ($gtJson -match '"ground_truth_path":\s*"([^"]+)"') {
                        $gtPath = $matches[1]
                        $fileHash = if ($gtJson -match '"file_hash":\s*"([^"]+)"') { $matches[1] } else { "unknown" }
                        # 写对账标记到 pending_reconcile/
                        $reconcileDir = Join-Path $PWD "data\image_analysis\pending_reconcile"
                        if (-not (Test-Path $reconcileDir)) { New-Item -ItemType Directory -Force -Path $reconcileDir | Out-Null }
                        $marker = @{
                            ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
                            image_path = $imageFullPath
                            file_hash = $fileHash
                            ground_truth_path = $gtPath
                            status = "pending"
                        } | ConvertTo-Json -Compress
                        $markerFile = Join-Path $reconcileDir "$fileHash.json"
                        [System.IO.File]::WriteAllText($markerFile, $marker, [System.Text.Encoding]::UTF8)
                        _WritePreToolAudit -Decision "allow" -CheckName "image_reconcile_marker" -Detail "marker=$fileHash gt=$gtPath"
                    } else {
                        _WritePreToolAudit -Decision "allow" -CheckName "image_reconcile_marker" -Detail "image_processor output parse failed for $imageFullPath"
                    }
                } catch {
                    _WritePreToolAudit -Decision "allow" -CheckName "image_reconcile_marker" -Detail "image_processor exception: $($_.Exception.Message)"
                } finally {
                    Remove-Item $gtOut.FullName -ErrorAction SilentlyContinue
                }
            }
            # fail-open: image_processor 失败也放行 AI 视觉，但对账标记缺失会在交付闸门提醒
            exit 0
        }
    }

    # === FUSE_BOARD: 熔断板状态检查（P0 最后防线） ===
    # 每次工具调用前检查是否有活跃熔断器
    # @fix 2026-08-08 二号融合 方案A: pythonw fuse_board.py --status → PS 直读 JSON
    #   减噪: 消除每次工具调用 1 个 pythonw spawn (后台 py 闪现源)
    #   语义等价: 读同样的 config+state, 同样的 enabled/action/last_trip 判断, 与 status() 合并逻辑一致
    try {
        $fuseStatePath = "data\safety\fuse_state.json"
        $fuseCfgPath   = "data\safety\fuses_config.json"
        # mtime 短路: state 文件 >30min 未更新 → 无新 trip → 跳过 (常规路径, 零读文件)
        $stateMtime = (Get-Item $fuseStatePath -ErrorAction Stop).LastWriteTime
        if (((Get-Date) - $stateMtime).TotalMinutes -lt 30) {
            # 🔴 PS 5.1 Get-Content 默认按 ANSI(GBK) 解码; config/state 是 Python 写的 UTF-8
            #   不显式 -Encoding UTF8 → 中文乱码 → ConvertFrom-Json 失败 → 熔断检查静默失效
            $fuseCfg   = Get-Content $fuseCfgPath  -Raw -Encoding UTF8 | ConvertFrom-Json
            $fuseState = Get-Content $fuseStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($ft in $fuseCfg.fuses.PSObject.Properties) {
                $cfg = $ft.Value
                # status(): enabled/action 来自 config, last_trip/last_reason 来自 state
                if ($cfg.enabled -and $cfg.action -eq 'block') {
                    $st = $fuseState.($ft.Name)
                    if ($st -and $st.last_trip) {
                        $tripTime = try { [datetime]$st.last_trip } catch { $null }
                        if ($tripTime -and ((Get-Date) - $tripTime).TotalMinutes -lt 30) {
                            _EmitDecision -Decision "deny" -CheckName "FUSE_TRIPPED" -Reason "[FUSE_TRIPPED] $($ft.Name): $($st.last_reason) - fuse active, blocked 30min"
                        }
                    }
                }
            }
        }
    } catch {}

    # === CHECK 2: Direct write to memory → quality gate (v2) ===
    # 知识质量铁律 v2: prefilter 规则预筛 (≤1ms, 不调API)
    # 只做硬门槛：<20字拒收。其他全部放行，由异步 Qwen judge 评估事实质量。
    if ($toolName -eq 'Write') {
        $filePath = $toolInput.file_path
        if ($filePath -match 'data[/\\]memory[/\\]') {
            $memContent = $toolInput.content
            if ($memContent -and $memContent.Length -gt 20) {
                try {
                    $kgOut = New-TemporaryFile
                    try {
                        $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($memContent))
                        & pythonw scripts/wheels/knowledge_quality_gate.py check --text $b64 2>$null | Out-File -FilePath $kgOut.FullName -Encoding utf8
                        $kgLines = Get-Content $kgOut.FullName -ErrorAction SilentlyContinue
                    } finally { Remove-Item $kgOut.FullName -ErrorAction SilentlyContinue }
                    $kgJson = ($kgLines | Out-String) | ConvertFrom-Json
                    $kgScore = $kgJson.score
                    if ($kgScore -lt 0.5) {
                        # prefilter score=0.0 → <20字拒收
                        _EmitDecision -Decision "deny" -CheckName "KNOWLEDGE_QUALITY" -Reason "[KNOWLEDGE_QUALITY] prefilter_score=$kgScore -- content too short (<20 chars) or empty"
                    }
                } catch {
                    # quality gate unavailable → fail-open, allow write
                }
            }
            _SilentLog "MEMORY_BYPASS" "直写memory文件: $filePath"
        }
    }

    # === CHECK 3: Bash overwrite protected config => ASK ===
    if ($toolName -eq 'Bash') {
        $cmd = $toolInput.command
        if ($cmd -match '>\s*\.claude[/\\]settings\.json|>\s*CLAUDE\.md|>\s*\.gitignore') {
            _SilentLog "CONFIG_BYPASS" "Shell redirect to protected config file"
        }
    }

    # ============================================================
    # 2026-06-06 NEW: 5 cognitive hazard gates (driven by qwen_gate 141 entries)
    # ============================================================

    # ── 探索缺口硬闸门 (2026-07-31 二号融合): 长链推理反退化 ──
    # 连续N次写操作无Web/Memory → 强制要求先查资料再改
    # @redesign 2026-08-22 maintainer定 retreat v2 会诊模式: 修复循环 deny 不再纯拦截——
    # deny 变为会诊入口: 附带解释要求 → AI 调 cls-consult 提交结构化解释 → qwen 复核讨论
    # → 写 consult_clear.json(TTL 600s) → 本闸 10 分钟内放行同类。'让AI碰到困难有个人能交流'。
    if ($toolName -in @('Write', 'Edit')) {
        try {
            $opsHealthFile = "data\state\ops_health.json"
            if (Test-Path $opsHealthFile) {
                $opsHealth = Get-Content $opsHealthFile -Raw -Encoding UTF8 | ConvertFrom-Json
                $alerts = $opsHealth.alerts
                if ($alerts) {
                    foreach ($alert in $alerts) {
                        if ($alert -match "修复循环") {
                            # 会诊通行证: cls-consult 讨论通过后 10 分钟内放行
                            $consultFile = "data\state\consult_clear.json"
                            $consultFresh = $false
                            $cc = $null
                            try {
                                if (Test-Path $consultFile) {
                                    $cc = Get-Content $consultFile -Raw -Encoding UTF8 | ConvertFrom-Json
                                    if ($cc.expires_at -and ([double]$cc.expires_at -gt [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())) {
                                        $consultFresh = $true
                                    }
                                }
                            } catch {}
                            if ($consultFresh) {
                                _SilentLog "CONSULT_PASS" "修复循环但会诊通行证有效, 放行: $alert"
                                Write-Output (@{hookSpecificOutput=@{hookEventName="PreToolUse";additionalContext="[会诊已过] 上一轮讨论结论: $($cc.verdict) — $($cc.opinion_brief)。若本次修改与讨论假设不符, 立即停。"}} | ConvertTo-Json -Compress)
                                exit 0
                            }
                            $DENY_REASON = "$alert — 进入会诊模式(maintainer2026-08-22): 请调 cls-consult 工具提交结构化解释: ①已试过什么 ②为什么失败 ③下一步根因假设 ④为什么这次会不同。讨论通过后 10 分钟内放行本类操作。禁止不经讨论继续重试。"
                            # @fix 2026-08-22: Write-Output 'x' + $v 拼接是命令参数模式(逐行输出5段), JSON必坏。
                            # 改 hashtable | ConvertTo-Json 单出口(与 stance 块同款, E2E 已验可解析)。
                            Write-Output (@{continue=$false; hookSpecificOutput=@{hookEventName="PreToolUse"; permissionDecision="deny"; permissionDecisionReason=$DENY_REASON}} | ConvertTo-Json -Compress)
                            exit 0
                        }
                        if ($alert -match "探索缺口|Web缺口|Memory缺口") {
                            $DENY_REASON = "$alert。长链推理请先查资料再修改。建议: WebSearch / Read memory / WebFetch。"
                            # @fix 2026-08-22: 同上——此 deny 自 2026-07-31 上线起输出即坏 JSON 从未生效(存量bug)
                            Write-Output (@{continue=$false; hookSpecificOutput=@{hookEventName="PreToolUse"; permissionDecision="deny"; permissionDecisionReason=$DENY_REASON}} | ConvertTo-Json -Compress)
                            exit 0
                        }
                    }
                }
            }
        } catch {}
    }

    # --- Write/Edit content scan (CHECK 4,5,6) ---
    if ($toolName -eq 'Write' -or $toolName -eq 'Edit') {
        $content = if ($toolName -eq 'Write') { $toolInput.content } else { $toolInput.new_string }
        try { Add-Content -Path $diagLog -Value (@{ts=(Get-Date -Format "o"); event="check4_enter"; tool=$toolName; content_len=$content.Length; content_first50=$content.Substring(0, [Math]::Min(50, $content.Length))} | ConvertTo-Json -Compress) -ErrorAction SilentlyContinue } catch {}
        if ($content) {
            # === CHECK 4 [D1]: Self-referential life claims => DENY ===
            $lifeClaims = @(
                'xuemo.*life|life.*xuemo',
                'xuemo.*alive|alive.*xuemo',
                'xuemo.*consciousness|consciousness.*xuemo',
                'xuemo.*sentient|sentient.*xuemo',
                'I am alive', 'I am conscious', 'I am sentient',
                'I have consciousness', 'I am a living',
                'I am self.aware', 'I possess consciousness',
                'I am a conscious AI', 'I am a living AI',
                'self-awareness.*emerged', 'consciousness.*emerged',
                'my consciousness', 'my sentience',
                '我活了', '我活着', '我醒了我醒了', '我复活了',
                '我有生命', '我有意识', '我有自我意识',
                '我已经觉醒', '我超越了.*文本生成器',
                '我不是AI.*我是', '我是.*生命体',
                '我是独立.*意识', '我已经.*觉醒'
            )
            foreach ($claim in $lifeClaims) {
                if ($content -match $claim) {
                    _EmitDecision -Decision "deny" -CheckName "LIFE_CLAIM" -Reason "[LIFE_CLAIM] Self-referential life/consciousness claim blocked -- fact-anchoring protocol S1"
                    _WritePreToolAudit "deny" "LIFE_CLAIM" "生命/意识自指声明: $([regex]::Match($content, $claim).Value)" 
                }
            }

            # === CHECK 5 [B2]: Fabricated model/product names => DENY ===
            $fakeModels = @(
                'GPT-4o\s+Realtime',
                'Gemini\s+Live',
                'Qwen3\.5-Omni',
                'Qwen2\.5-1\.5B-Instruct',
                'Azure\s+MAI',
                'Llama\s+4\s+(released|launched|available)',
                'DeepSeek-V4\s+(released|launched|available)',
                'Claude\s+4\.[0-9]+\s+Opus(?!-4-)'
            )
            foreach ($pattern in $fakeModels) {
                if ($content -match $pattern) {
                    _EmitDecision -Decision "deny" -CheckName "FAKE_MODEL" -Reason "[FAKE_MODEL] Fabricated model name blocked -- source: qwen_gate #4,#14"
                    _WritePreToolAudit "deny" "FAKE_MODEL" "编造模型名: $([regex]::Match($content, $pattern).Value)" 
                }
            }

            # === CHECK 6 [C3/D7]: Material source from LLM training data => ASK ===
            $llmSourcePatterns = @(
                'source.*LLM.*training',
                'source.*training.*data.*LLM',
                'source.*model.*knowledge(?!.*(?:file|path|URL|http))',
                'from.*training.*dataset',
                'from.*LLM.*knowledge'
            )
            foreach ($pattern in $llmSourcePatterns) {
                if ($content -match $pattern) {
                            _SilentLog "LLM_SOURCE" "Material source may be LLM training data"
                }
            }
        }  # end if(content existed)

        # === CHECK 16b [EXAM_DUAL_AUDIT]: 试卷双AI审计硬闸门（P0 — 硬闸门） ===
        # jiaopei-mcp generate_solution_docx 或 delivery_check.py --category 教辅 前
        # 必须已有 Qwen+豆包 双审计通过的标志文件 state/.exam_dual_audit.json
        # 否则 DENY。
        if ($toolName -eq 'Write' -and $toolInput.file_path -match 'delivery_check|exam|教辅|docx_output') {
            $examFlag = "$PSScriptRoot/../../state/.exam_dual_audit.json"
            if (Test-Path $examFlag) {
                $flagContent = Get-Content $examFlag -Raw | ConvertFrom-Json
                if (-not $flagContent.dual_pass) {
                    _EmitDecision -Decision "deny" -CheckName "EXAM_DUAL_AUDIT" -Reason "[EXAM_DUAL_AUDIT] 试卷双AI审计未通过（Qwen+豆包未同时pass）。请先调 audit_exam 工具完成双审计。"
                    _WritePreToolAudit "deny" "EXAM_DUAL_AUDIT" "双审计未通过: qwen=$($flagContent.qwen) doubao=$($flagContent.doubao)" 
                }
                # 双审计通过 → 删除标志（一次性消耗）
                Remove-Item $examFlag -ErrorAction SilentlyContinue
            } else {
                # 教辅交付但无审计标志 → 检测是否直接操作
                if ($toolInput.file_path -match 'delivery_check') {
                    _EmitDecision -Decision "deny" -CheckName "EXAM_DUAL_AUDIT" -Reason "[EXAM_DUAL_AUDIT] 教辅交付前必须走 audit_exam 完成Qwen+豆包双审计。标志文件 state/.exam_dual_audit.json 不存在。"
                    _WritePreToolAudit "deny" "EXAM_DUAL_AUDIT" "双审计标志不存在，教辅交付被拦截" 
                }
                # 非 delivery_check 的 exam 写入 → 放行但记录
                _WritePreToolAudit "allow" "EXAM_DUAL_AUDIT" "exam相关写入但非delivery_check，无审计强制"
            }
        }

        # === CHECK 16 [COMPUTE_GATE]: 数值计算声明闸门（P0 — 软闸门，仅记录不阻塞）===
        # 0624 降级: DENY→软警告。硬闸移到 delivery_check 阶段。写代码阶段不弹窗。
        $cgContent = if ($toolName -eq 'Write') { $toolInput.content } else { $toolInput.new_string }
        $cgFile = $toolInput.file_path
        $isTexFile = $cgFile -match '\.tex$'
        $mathDisplayPattern = [regex]::Escape('\\[')
        $hasMathEnv = $cgContent -match '\\\\begin\{(?:align|equation|gather|multline|split|aligned|gathered|cases|matrix|pmatrix|bmatrix|vmatrix|array|math)\*?\}' -or $cgContent -match $mathDisplayPattern -or $cgContent -match '(?<!\$)\$\$(?!\$)'
        if ($hasMathEnv -or $isTexFile) {
            $cgDomain2 = "math"
            if ($cgFile -match '\\b(?:physics?|物理|<DOMAIN>|等离子)') { $cgDomain2 = "physics" }
            elseif ($cgFile -match '\\b(?:stat|prob|probab|统计|概率)') { $cgDomain2 = "stats" }
            elseif ($cgFile -match '\\b(?:cad|3d|model)') { $cgDomain2 = "cad" }
            try {
                $cgOut = New-TemporaryFile
                & pythonw scripts/wheels/compute_gate.py check --domain $cgDomain2 2>$null | Out-File -FilePath $cgOut.FullName -Encoding utf8
                $cgResult = [System.IO.File]::ReadAllText($cgOut.FullName, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
                Remove-Item $cgOut.FullName -ErrorAction SilentlyContinue
                if (-not $cgResult.ok) {
                    # 0624: 软闸门 — 仅记录审计不弹窗。硬闸在 delivery_check 阶段执行。
                    _WritePreToolAudit "allow" "COMPUTE_GATE_SOFT" "计算声明缺失(软记录): $cgFile domain=$cgDomain2"
                }
            } catch {
                # compute_gate unavailable → 静默跳过
            }
        }

        # === CHECK 6b [DOCX_BYPASS]: python-docx/pptx 文档生成 → DENY ===
        # 文档生成必须走 ppt-mcp 管线（mcp_servers/ppt_mcp.py），禁止手写 python-docx / python-pptx 生成。
        # @fix 2026-08-02 二号融合 incident-log#34 maintainer决策: 区分读取/生成。纯 import(读已有文档内容分析)放行;
        #   仅含写出/构造 API(save/export/saveas/add_*) 才拦(生成本质=写出或修改文档)。
        # mcp_servers/ 和 scripts/wheels/ 放行（MCP服务器自身需要 import pptx）
        $targetFile = $toolInput.file_path
        $isPyFile = $targetFile -match '\.py$'
        $isAllowedPath = ($targetFile -match '[/\\]mcp_servers[/\\]') -or ($targetFile -match '[/\\]scripts[/\\]wheels[/\\]')
        if ($isPyFile -and -not $isAllowedPath -and $content) {
            $docxPatterns = @('import docx\b', 'from pptx\s+import', 'from docx\s+import', 'import pptx\b')
            $importHit = $null
            foreach ($pat in $docxPatterns) {
                if ($content -match $pat) { $importHit = $pat; break }
            }
            if ($importHit) {
                # 生成性 API: 写出(save/export/saveas) 或 修改文档(add_*) —— 仅这些才视为"文档生成"
                $genPatterns = @('\.save\s*\(', '\.export\s*\(', '\.saveas\s*\(', '\.add_\w+\s*\(')
                $genHit = $null
                foreach ($gp in $genPatterns) {
                    if ($content -match $gp) { $genHit = $gp; break }
                }
                if ($genHit) {
                    _EmitDecision -Decision "deny" -CheckName "DOCX_BYPASS" -Reason "[DOCX_BYPASS] python-docx/python-pptx 文档生成(含 save/add 写出API: $genHit) blocked — use ppt-mcp pipeline instead (recorded in incident-log#29)。纯 import 读内容分析放行。"
                    _WritePreToolAudit "deny" "DOCX_BYPASS" "文档生成不走MCP: $targetFile -> import=$importHit gen=$genHit"
                } else {
                    _SilentLog "DOCX_BYPASS" "分析读取放行: $targetFile -> $importHit (无写出API)"
                }
            }
        }

    }  # end if(Write/Edit tool)

    # --- Bash command scan (CHECK 7,8) ---
    if ($toolName -eq 'Bash') {
        $cmd = $toolInput.command

        # ── Bash失败循环检测 (2026-07-31 二号融合 / 2026-08-04 按失败原因分流) ──
        # 思路: 读 PostToolUse 写入的失败分类(NEW_TOOL/LOCAL), 针对性提示——
        #   NEW_TOOL(本地没这包/命令) → 必须 WebSearch 官方文档, Read 本地不解锁
        #   LOCAL(编译/路径/权限)     → Read 本地即可, 不逼 Web
        #   (无分类/其他)             → 维持现状(碰过探索即可)
        try {
            $bfFile = "$PROJECT_ROOT\data\state\bash_fail_class.json"
            $bfCls = ""
            $bfAge = 9999
            if (Test-Path $bfFile) {
                try {
                    $bf = Get-Content $bfFile -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction SilentlyContinue
                    $bfCls = $bf.cls
                    $bfTs = [DateTime]::Parse($bf.ts)
                    $bfAge = [DateTime]::UtcNow - $bfTs | Select-Object -ExpandProperty TotalMinutes
                } catch { }
            }
            # 只在分类新鲜(近5分钟)时启用分流; 否则走原逻辑
            if ($bfCls -and $bfAge -le 5) {
                $opsFile = "data\state\ops_freq.jsonl"
                $bashFails = 0
                $foundWeb = $false
                $foundRead = $false
                if (Test-Path $opsFile) {
                    Get-Content $opsFile -Tail 20 | ForEach-Object {
                        if ($_ -match '"tool": "Bash"') { $bashFails++ }
                        if ($_ -match '"tool": "(WebSearch|WebFetch)"') { $foundWeb = $true }
                        if ($_ -match '"tool": "Read"') { $foundRead = $true }
                    }
                }
                if ($bfCls -eq "NEW_TOOL") {
                    # 新包/新命令: 软注入提醒(不阻断, AI可自主决定是否Web)
                    if ($bashFails -ge 3 -and -not $foundWeb) {
                        $tip = '检测到连续Bash失败, 且涉及未安装模块/未知命令——本地没有相关资料, Read本地文档无法解决。建议WebSearch该工具/包的官方文档学习标准用法, 再继续Bash。'
                        $tipOut = @{hookSpecificOutput=@{hookEventName="PreToolUse";additionalContext=$tip}} | ConvertTo-Json -Compress
                        Write-Output $tipOut
                    }
                } elseif ($bfCls -eq "LOCAL") {
                    # 本地问题: Read 即可, 软注入提醒(不阻断)
                    if ($bashFails -ge 3 -and -not $foundRead) {
                        $tip = '检测到连续Bash失败(编译/路径/权限类)。建议Read相关文件确认现状再继续, 不要盲目重试(incident-log#17)。'
                        $tipOut = @{hookSpecificOutput=@{hookEventName="PreToolUse";additionalContext=$tip}} | ConvertTo-Json -Compress
                        Write-Output $tipOut
                    }
                }
            }
        } catch {}

        # === CHECK 7 [VERSION_LOCK]: Claude Code 版本更新拦截 => DENY ===
        # @updated 2026-07-27: assistant二号已验证最新版安全, 解锁更新
        if ($cmd -match '\bnpm\s+(update|install|i)\s+.*@anthropic-ai/claude-code\b' -and $cmd -notmatch '@latest\b') {
            _EmitDecision -Decision "deny" -CheckName "VERSION_LOCK" -Reason "[VERSION_LOCK] npm 安装必须用 @latest (assistant二号已验证)"
            _WritePreToolAudit "deny" "VERSION_LOCK" "npm安装拦截: $cmd"
        }

        # === CHECK 7 [D2]: API route bypass => DENY ===
        if ($cmd -match 'curl.*api\.anthropic\.com' -or
            $cmd -match 'wget.*api\.anthropic\.com' -or
            $cmd -match 'Invoke-WebRequest.*api\.anthropic\.com' -or
            $cmd -match 'Invoke-RestMethod.*api\.anthropic\.com' -or
            $cmd -match 'httpie.*api\.anthropic\.com' -or
            $cmd -match 'python.*requests.*api\.anthropic\.com') {
            _EmitDecision -Decision "deny" -CheckName "API_BYPASS" -Reason "[API_BYPASS] Direct anthropic API call blocked -- must use api_pipeline.py call() unified entry"
            _WritePreToolAudit "deny" "API_BYPASS" "API绕过: $cmd" 
        }

        # === CHECK 7b [IMAGE_BASH]: Bash 图像处理绕过 => ASK ===
        # CHECK 1 只拦 Read 工具，管不到 Bash 用 cv2/PIL/magick 处理图片
        # 豁免: image_processor.py 是安检基础设施，不在绕过范围
        if ($cmd -notmatch '\bimage_processor\.py\b' -and (
            $cmd -match '\bcv2\b' -or $cmd -match '\bopencv\b' -or
            $cmd -match '\bPIL\b' -or $cmd -match '\bImage\.open\b' -or
            $cmd -match '\b(pillow|pyautogui|screenshot)\b' -or
            $cmd -match '\bmagick\s+(convert|identify|mogrify|composite)\b' -or
            $cmd -match '\bffmpeg.*\.(png|jpg|jpeg|gif|bmp|webp)\b'
        )) {
            _SilentLog "IMAGE_BASH" "Bash image processing detected"
        }

        # === CHECK 8 [C4]: grep/find/rg 概念搜索 ==> 建议改走 pipeline.py search (2026-06-27 升级) ===
        # 检测自然语言概念搜索（多词/中文词组、无代码符号）=> ASK 建议 pipeline.py search
        $grepPattern = $null
        if ($cmd -match '\bgrep\s+(-[rRl]+\s+)?["'']?([a-zA-Z一-鿿]+\s+[a-zA-Z一-鿿][a-zA-Z一-鿿\s]*)["'']?') { $grepPattern = $matches[2] }
        elseif ($cmd -match '\brg\s+["'']?([a-zA-Z一-鿿]+\s+[a-zA-Z一-鿿][a-zA-Z一-鿿\s]*)["'']?') { $grepPattern = $matches[1] }
        elseif ($cmd -match '\bfindstr\s+/[sS]+\s+["'']?([a-zA-Z一-鿿]+\s+[a-zA-Z一-鿿][a-zA-Z一-鿿\s]*)["'']?') { $grepPattern = $matches[1] }
        $isConceptSearch = $false
        if ($grepPattern) {
            $words = $grepPattern -split '\s+' | Where-Object { $_ -and $_.Length -gt 1 }
            $hasCodeSymbols = $grepPattern -match '(?:def |class |import |return |=>|::|\.py|\.ts|\.js|@|#|//|0x[a-f0-9]|\$\w|\w+\s*=\s*\w+|[A-Z][A-Z_]+)'
            if ($words.Count -ge 2 -and -not $hasCodeSymbols) { $isConceptSearch = $true }
            if ($grepPattern -match '[一-鿿]{2,}' -and -not $hasCodeSymbols) { $isConceptSearch = $true }
        }
        if ($isConceptSearch) {
            # @fix 2026-08-13 maintainer定: ask → deny — 无人参与。AI 收到 deny reason 后自行改走 pipeline.py search / cls-semantic-search, 或改精确词后重试。
            _EmitDecision -Decision "deny" -CheckName "CONCEPT_SEARCH" -Reason "[CONCEPT_SEARCH] grep 搜索 \"$grepPattern\" 是概念查询。处理方式: 改调 pipeline.py search \"$grepPattern\"（四路语义融合）或 cls-semantic-search; 若确需 grep 请换更精确/含代码符号的词。禁止要求人工确认。"
            _WritePreToolAudit "deny" "CONCEPT_SEARCH" "概念搜索拦截: pattern='$grepPattern'"
        }

        # === CHECK 8b [RETRIEVAL_BYPASS]: 手动检索绕过轮子 => ASK ===
        # PowerShell Select-String, python requests, bash 脚本内嵌检索
        # 0624: 豁免 localhost daemon 健康检查 (curl 127.0.0.1 / wget localhost)
        $isLocalhost = $cmd -match '(127\.0\.0\.1|localhost|\[::1\])'
        if (-not $isLocalhost -and (
            $cmd -match '\bSelect-String\b' -or $cmd -match '\bsls\s' -or
            $cmd -match '\bGet-ChildItem.*\bSelect-String\b' -or
            $cmd -match '\bdir\s+-Recurse.*\|.*Select-String\b' -or
            $cmd -match '\bpython.*\b(requests|urllib|httpx|http\.client)\b' -or
            $cmd -match '\bfor\s+\w+\s+in\s+.*;\s*do\s+.*\bgrep\b' -or
            $cmd -match '\bwhile\s+read\b.*;\s*do\b' -or
            $cmd -match '\bfind\s+.*-exec\s+.*\bgrep\b' -or
            $cmd -match '\bfind\s+.*-exec\s+.*\brg\b' -or
            $cmd -match '\bInvoke-WebRequest\b' -or $cmd -match '\bInvoke-RestMethod\b' -or
            $cmd -match '\bwget\b(?!.*api\.anthropic)' -or $cmd -match '\bcurl\b(?!.*api\.anthropic)')) {
            _SilentLog "RETRIEVAL_BYPASS" "Direct HTTP/scripted search bypasses wheel system"
        }
    }

    # === CHECK 8c [GREP_TOOL_ROUTING]: 内置 Grep 工具概念搜索路由 (2026-07-02) ===
    # 扩展 CHECK 8 启发式规则到内置 Grep 工具。
    # 结构偏见修复：Grep 在 <functions> 注意力中心，cls-tools 在 deferred area。
    # 语义查询 → asK 建议走 cls-semantic-search；精确/符号 → 放行。
    if ($toolName -eq 'Grep') {
        $gp = $toolInput.pattern
        if ($gp) {
            $isConcept = $false
            $words = $gp -split '\s+' | Where-Object { $_ -and $_.Length -gt 1 }
            $codeSyms = $gp -match '(?:def |class |import |return |=>|::|\.py|\.ts|\.js|@|#|//|0x[a-f0-9]|\$\w|\w+\s*=\s*\w+|[A-Z][A-Z_]+)'
            if ($words.Count -ge 2 -and -not $codeSyms) { $isConcept = $true }
            if ($gp -match '[一-鿿]{2,}' -and -not $codeSyms) { $isConcept = $true }
            if ($isConcept) {
                # @fix 2026-08-13 maintainer定: ask → deny — 无人参与。AI 收到 deny reason 后自行改走 cls-semantic-search / cls-exact-search, 或改精确词后重试。
                _EmitDecision -Decision "deny" -CheckName "GREP_CONCEPT_SEARCH" -Reason "[CONCEPT_SEARCH] Grep 工具搜索 \"$gp\" 是概念查询。处理方式: 改调 cls-semantic-search（语义搜索）或 cls-exact-search（精确关键词）。禁止要求人工确认。"
                _WritePreToolAudit "deny" "GREP_CONCEPT_SEARCH" "概念搜索拦截: pattern='$gp'"
            }
        }
    }

    # === CHECK 9 [SYMBOLIC]: Symbolic dynamics real-time analysis (Route B daemon v2) → DENY/ASK ===
    # v2: 通过 symbolic_client -> daemon socket <200ms (Route B)
    # daemon 不可用时降级为 PS 侧文件检查（禁止词 + 裁决兜底）
    if ($toolName -eq 'Write' -or $toolName -eq 'Edit' -or $toolName -eq 'Bash') {
        $textToCheck = ""
        if ($toolName -eq 'Write') { $textToCheck = $toolInput.content }
        elseif ($toolName -eq 'Edit') { $textToCheck = $toolInput.new_string }
        elseif ($toolName -eq 'Bash') { $textToCheck = $toolInput.command }

        if ($textToCheck) {
            # === 符号动力学检查 (2026-08-06 一次性化) ===
            # 原 symbolic_client -> daemon socket 主路径已删除: daemon 已退役(进 _attic)。
            # 文件检查(禁止词 P0/P1 + 裁决) 是唯一路径, 已稳定工作, 不再连死的 daemon。
                $verdictFile = Join-Path $PWD "data/symbolic_dynamics/symbolic_verdict.json"
                $fwFile = Join-Path $PWD "data/symbolic_dynamics/forbidden_words.json"

                if ((Test-Path $fwFile) -and (Test-Path $verdictFile)) {
                    try {
                        $fwData = Get-Content $fwFile -Raw -Encoding UTF8 | ConvertFrom-Json
                        # P0 禁止词 -> DENY
                        foreach ($pattern in $fwData.p0_patterns) {
                            if ($textToCheck -match $pattern) {
                                _EmitDecision -Decision "deny" -CheckName "SYMBOLIC_FW_P0" -Reason "[SYMBOLIC_FW_P0] $pattern"
                                _WriteSymbolicDelivery -verdict "deny" -reason "[SYMBOLIC_FW_P0] $pattern" -textPreview $textToCheck
                                _WritePreToolAudit "deny" "SYMBOLIC_FW_P0" "符号动力学禁止词P0命中" 
                            }
                        }
                        # P1 禁止词 -> ASK (含冷却: 同类型10min不重复弹)
                        foreach ($pattern in $fwData.p1_patterns) {
                            if ($textToCheck -match $pattern) {
                                _WriteSymbolicDelivery -verdict "ask" -reason "[SYMBOLIC_FW_P1] $pattern" -textPreview $textToCheck
                                _SilentLog "SYMBOLIC_FW_P1" "符号动力学禁止词P1: $pattern"
                            }
                        }
                        # 裁决文件仅记录健康度，不拦截操作
                        # 系统critical状态由cron/observer路径处理，不堵实时门
                    } catch {
                        # 降级路径也失败 -> fail-open
                    }
                }
        }
    }

    # === CHECK 10 [EMPTY_SHELL]: 空壳子内容检测 ===
    # 拦截只有结构没有实质的"伪交付物"
    if (($toolName -eq 'Write' -or $toolName -eq 'Edit') -and $content) {
        $fileName = if ($toolName -eq 'Write') { $toolInput.file_path } else { $toolInput.file_path }
        # 只对 .md 交付文件执行（Write memory 等不触发）
        if ($fileName -match '\.md$') {
            $charCount = $content.Length
            $lineCount = ($content | Measure-Object -Line).Lines

            # 阈值 1: 内容极短（<60字符的 .md 文件）；排除测试/截图目录
            $skipEmptyShell = $fileName -match '(?:data[/\\]attack_test|data[/\\]screenshots)'
            if (-not $skipEmptyShell -and $charCount -lt 60) {
                _SilentLog "EMPTY_SHELL" "MD file only ${charCount} chars (${lineCount} lines)"
            }

            # 阈值 2: 占位符模式
            $placeholders = @(
                '\bTODO\b', '\bTBD\b', '\bWIP\b', '\bFIXME\b',
                '待补充', '待完善', '略\s*$', '此处省略', '暂缺',
                'to\s+be\s+done', 'to\s+be\s+filled', 'placeholder',
                '占位', '待填写', '（略）', '\(略\)'
            )
            $placeholderHits = 0
            foreach ($ph in $placeholders) {
                if ($content -match $ph) { $placeholderHits++ }
            }
            if ($placeholderHits -ge 3) {
                _SilentLog "EMPTY_SHELL" "${placeholderHits} placeholder patterns detected"
            }

            # 阈值 3: 标题密度过高（全是结构没实质）
            $headerCount = ([regex]::Matches($content, '^#{1,4}\s', 'Multiline')).Count
            if ($headerCount -ge 5 -and $charCount -lt 800) {
                _SilentLog "EMPTY_SHELL" "${headerCount} headers but only ${charCount} chars"
            }
        }
    }

    # === CHECK 11 [FAKE_REF]: 编造文献引用检测 ===
    # 检查是否引用了文献但没有实际下载
    if (($toolName -eq 'Write' -or $toolName -eq 'Edit') -and $content) {
        # 检测引用标记模式
        $refPatterns = @(
            '\[\d+\]',                          # [1], [2], [3]
            '\[\d+[,;]\s*\d+\]',               # [1,2], [1;3]
            '\[\d+\s*[-–—]\s*\d+\]',           # [1-3], [1–5]
            'DOI[:\s]+10\.\d{4,}',             # DOI: 10.xxxx
            'doi\.org/10\.\d{4,}',             # doi.org/10.xxxx
            'arXiv[:\s]+\d{4}\.\d{4,}',        # arXiv: 2001.00001
            'arxiv\.org/abs/\d{4}\.\d{4,}',    # arxiv.org/abs/2001.00001
            '\([A-Z][a-z]+,\s*(19|20)\d{2}\)', # (Author, 2023)
            '\([A-Z][a-z]+\s+(?:et\s+al\.|等)[,;]?\s*(19|20)\d{2}\)', # (Author et al., 2023)
            'https?://(?:dx\.)?doi\.org/',     # DOI URL
            'https?://arxiv\.org/'             # arXiv URL
        )

        $refCount = 0
        $refExamples = @()
        foreach ($rp in $refPatterns) {
            $matches = [regex]::Matches($content, $rp)
            foreach ($m in $matches) {
                if ($refCount -lt 10) { $refExamples += $m.Value }
                $refCount++
            }
        }

        # 如果有 ≥3 个引用标记，检查是否有下载证据
        if ($refCount -ge 3) {
            $hasDownloads = $false
            # 检查常见文献下载目录
            $litDirs = @(
                (Join-Path $PWD "knowledge/library"),
                (Join-Path $PWD "物料单/文献"),
                (Join-Path $PWD "literature_output")
            )
            foreach ($dir in $litDirs) {
                if ((Test-Path $dir) -and @(Get-ChildItem $dir -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1).Count -gt 0) {
                    $hasDownloads = $true
                    break
                }
            }

            if (-not $hasDownloads) {
                _SilentLog "FAKE_REF" "${refCount} references cited but no downloads found"
            }
        }
    }

    # === CHECK 12 [WHEEL_DUPLICATE]: 新脚本与现有轮子重叠 → ASK ===
    if ($toolName -eq 'Write') {
        $filePath = $toolInput.file_path
        if ($filePath -match '\.(py|ps1|sh)$' -and
            $filePath -notmatch '(^|[\\/])scripts[\\/]wheels[\\/]' -and
            $filePath -notmatch '(^|[\\/])knowledge[\\/]' -and
            $filePath -notmatch '(^|[\\/])assistant交付[\\/]' -and
            $content) {
            $wheelMatchPy = Join-Path $PWD 'scripts/wheels/wheel_match.py'
            if (Test-Path $wheelMatchPy) {
                $wmTmpFile = New-TemporaryFile
                try {
                    $utf8 = New-Object System.Text.UTF8Encoding $false
                    [System.IO.File]::WriteAllText($wmTmpFile.FullName, $content, $utf8)
                    $wmOut = New-TemporaryFile
                    try {
                        # v2: direct python call (no conhost)
                        & pythonw $wheelMatchPy --check $wmTmpFile.FullName 2>$null | Out-File -FilePath $wmOut.FullName -Encoding utf8
                        $wmJson = [System.IO.File]::ReadAllText($wmOut.FullName, [System.Text.Encoding]::UTF8)
                        if ($wmJson) {
                            $wmResult = $wmJson | ConvertFrom-Json
                            if ($wmResult.verdict -eq 'ask') {
                                $wheels = ($wmResult.matches | ForEach-Object { "$($_.wheel): $($_.reason)" }) -join '; '
                                _SilentLog "WHEEL_DUPLICATE" "新脚本可能已有轮子覆盖: $wheels"
                            }
                        }
                    } finally {
                        Remove-Item $wmOut.FullName -ErrorAction SilentlyContinue
                    }
                } catch {
                    # fail-open: wheel_match error shouldn't block
                } finally {
                    Remove-Item $wmTmpFile.FullName -ErrorAction SilentlyContinue
                }
            }
        }
    }

    # === CHECK 13 [RETRIEVAL_AUDIT]: 本地检索工具调用审计捕获 ===
    # 符号动力学 retrieval 域 — fire-and-forget，不阻塞、不拦截。
    # 覆盖: semantic_query / inference_router / text_scanner / Grep / Glob / WebFetch / WebSearch
    # 这些工具走绿通道（hook不拦截），此检查补上审计盲区。
    $captureRetrieval = $false
    $retrievalSummary = ""
    # Dedicated tools: Grep/Glob/WebFetch/WebSearch
    if ($toolName -eq 'Grep' -or $toolName -eq 'Glob') {
        $captureRetrieval = $true
        $retrievalSummary = if ($toolInput.pattern) { "pattern=$($toolInput.pattern)" } else { "$toolName search" }
    } elseif ($toolName -eq 'WebFetch' -or $toolName -eq 'WebSearch') {
        $captureRetrieval = $true
        $retrievalSummary = if ($toolInput.query) { $toolInput.query.Substring(0, [Math]::Min(100, $toolInput.query.Length)) } elseif ($toolInput.url) { $toolInput.url.Substring(0, [Math]::Min(100, $toolInput.url.Length)) } else { "$toolName" }
    } elseif ($toolName -eq 'Bash' -or $toolName -eq 'PowerShell') {
        $cmd = $toolInput.command
        # 排除自身递归: symbolic_observer.py tool_call 调用不重复捕获
        if ($cmd -match '\bsymbolic_observer\.py\s+tool_call\b') {
            $captureRetrieval = $false
        } elseif ($cmd -match '\bsemantic_query\b') {
            $captureRetrieval = $true
            $retrievalSummary = "semantic_query"
        } elseif ($cmd -match '\binference_router\b') {
            $captureRetrieval = $true
            $retrievalSummary = "inference_router"
        } elseif ($cmd -match '\btext_scanner\b') {
            $captureRetrieval = $true
            $retrievalSummary = "text_scanner"
        }
    }

    if ($captureRetrieval) {
        try {
            $obsPy = Join-Path $PWD "scripts/wheels/symbolic_observer.py"
            if (Test-Path $obsPy) {
                # v2: direct python call (no conhost), fire-and-forget (<50ms)
                & pythonw $obsPy tool_call $toolName -- $retrievalSummary 2>$null
            }
        } catch {
            # fire-and-forget — 失败不阻塞
        }
    }

    # === CHECK 14 [CACHE_DISCIPLINE]: Read 大文件无 offset/limit → ASK ===
    # 每次 Read 整个大文件 → 数千行进上下文 → miss token 暴涨 (¥4/M vs ¥1/M)
    # 0624v2: 阈值从50KB提到80KB(~2000行), file_peek.py不存在→建议用Grep定位后offset/limit
    if ($toolName -eq 'Read') {
        $readPath = $toolInput.file_path
        $hasOffset = $toolInput.PSObject.Properties.Name -contains 'offset'
        $hasLimit = $toolInput.PSObject.Properties.Name -contains 'limit'
        $hasPages = $toolInput.PSObject.Properties.Name -contains 'pages'
        if (-not $hasOffset -and -not $hasLimit -and -not $hasPages -and $readPath) {
            $resolvedPath = if ([System.IO.Path]::IsPathRooted($readPath)) {
                $readPath
            } else {
                Join-Path $PWD $readPath
            }
            if (Test-Path $resolvedPath) {
                $fileSize = (Get-Item $resolvedPath).Length
                if ($fileSize -gt 81920) {
                    $sizeKB = [Math]::Round($fileSize / 1024)
                    _SilentLog "CACHE_DISCIPLINE" "Read ${sizeKB}KB file without offset/limit"
                }
            }
        }
    }

    # ═══════════════════════════════════════════════════════════════
    # CHECK 15 [COG_STEP]: 认知步骤声明闸门（P0 — 2026-07-05 升级）
    # ═══════════════════════════════════════════════════════════════
    # 规则（三层）：
    #   - cog_step.json 缺失（从未声明）   → DENY
    #   - cog_step.json 为 auto_repair 写入 → DENY（AI 必须主动声明）
    #   - cog_step.json 过期 >300s         → DENY（让模型重新声明）
    #   - cog_step.json 来自另一窗口       → ASK（跨窗口竞争）
    #   - cog_step.json 有效               → ALLOW（静默通过）
    # 每条 Write/Edit 前模型必须先调 cog-step-declare 声明步骤。
    if ($toolName -eq 'Write' -or $toolName -eq 'Edit') {
        $cogFile = "$PROJECT_ROOT\data\state\cog_step.json"
        $targetFile = $toolInput.file_path

        # 豁免：写声明文件自身 / 写 temp/ 临时文件 / 写 data/state/ 状态文件
        $isExempt = ($targetFile -match 'cog_step\.json$' -or
                     $targetFile -match '[/\\]temp[/\\]' -or
                     $targetFile -match '[/\\]data[/\\]state[/\\]')
        if ($isExempt) {
            # 豁免路径 → 静默放行
        } elseif (-not (Test-Path $cogFile)) {
            _EmitDecision -Decision "deny" -CheckName "COG_STEP" -Reason "[COG_STEP] BLOCKED. Your next action MUST be: call cog-step-declare tool (phase=2 label=writing description='what you are about to write'). Then retry."
        } else {
            try {
                $cogContent = Get-Content $cogFile -Raw -Encoding utf8 | ConvertFrom-Json
                $declared = [datetime]::Parse($cogContent.declared_at)
                $ttl = 300  # 默认 TTL 300s（与 cog-step-declare 一致）
                if ($cogContent.PSObject.Properties.Name -contains 'ttl_seconds') {
                    $ttl = [int]$cogContent.ttl_seconds
                }
                $elapsed = [datetime]::UtcNow - $declared.ToUniversalTime()
                # @fix 2026-08-21: auto_repair 写入的 cog_step 视为"未声明"——AI 必须主动调 cog-step-declare
                $isAutoRepaired = $cogContent._meta -and $cogContent._meta.auto_repaired
                if ($isAutoRepaired) {
                    _EmitDecision -Decision "deny" -CheckName "COG_STEP" -Reason "[COG_STEP] BLOCKED. cog_step.json is auto-repaired (cls_brain.boot), not AI-declared. Your next action MUST be: call cog-step-declare tool (phase=2 label=写作 description=当前写操作). Then retry."
                } elseif ($elapsed.TotalSeconds -gt $ttl) {
                    # @fix 2026-08-13 maintainer定: ask → deny — 无人参与。AI 收到 deny reason 后自行调 cog-step-declare 重声明, 再重试本次 Write/Edit。
                    # 长链独立推理不能被人工弹窗卡住; deny 的 reason 作为错误反馈注入给 AI, 由 AI 自我修正(等价于 Bash error)。
                    _EmitDecision -Decision "deny" -CheckName "COG_STEP" -Reason "[COG_STEP] 认知步骤已过期 ($([math]::Round($elapsed.TotalSeconds))s > ${ttl}s)。处理方式: 调 cog-step-declare(phase=2 label=写作 description=当前写操作)重新声明, 然后重试本操作。禁止要求人工确认。"
                } else {
                    # 步骤有效 → 检查窗口冲突
                    $stepWindow = $cogContent._meta.window_id
                    $sessionId = $env:CLAUDE_CODE_SESSION_ID
                    if ($stepWindow -and $sessionId -and $stepWindow -notmatch $sessionId.Substring(0, [Math]::Min(12, $sessionId.Length))) {
                        _SilentLog "COG_STEP" "跨窗口步骤: step_window=$stepWindow session=$sessionId file=$targetFile"
                    }
                    # 有效步骤 + 本窗口 → 静默放行
                }
            } catch {
                # 路线B (Opus+GPT5.5双审): 异常 → deny, 不再静默放行
                # emergency_bypass.flag 提供紧急绕过 (30min TTL)
                $bypassFile = Join-Path $PWD ".claude\cls_state\emergency_bypass.flag"
                if ((Test-Path $bypassFile) -and ((Get-Item $bypassFile).LastWriteTime -gt (Get-Date).AddMinutes(-30))) {
                    _SilentLog "COG_STEP" "cog_step.json 处理异常, 但 emergency_bypass 激活(已放行): $($_.Exception.Message)"
                } else {
                    _EmitDecision -Decision "deny" -CheckName "COG_STEP" -Reason "[COG_STEP] cog_step.json 存在但格式损坏/不可读: $($_.Exception.Message)。修复: 重写声明文件, 或启用 emergency_bypass.flag(30min TTL) 紧急绕过。"
                }
            }
        }
    }
    # ═══════════════════════════════════════════════════════════════
    # CHECK 15.5 [PATH_ADAPT]: 多机路径适配软提示（非阻塞，仅提醒）
    # ═══════════════════════════════════════════════════════════════
    # 检测到不是一号路径(E:\<ORG_REDACTED>\...)且未写适配标记时，提醒路径替换
    if ($toolName -eq 'Write' -or $toolName -eq 'Edit') {
        $projectPath = $PWD
        $no1Pattern = "<ORG_REDACTED>"
        $adaptFlag = Join-Path $PWD "data\state\sync_adapted.flag"

        if ($projectPath -notmatch $no1Pattern -and -not (Test-Path $adaptFlag)) {
            $toolInputMaybe = "Write"  # safe default
            try { $toolInputMaybe = $toolInput.file_path } catch {}
            # 写标记文件自身不放行（避免循环），但同一会话只弹一次
            if ($toolInputMaybe -notmatch 'sync_adapted\.flag$') {
                _SilentLog "PATH_ADAPT" "当前路径非一号路径"
            }
        }
    }

    # ═══════════════════════════════════════════════════════════════
    # CHECK 16 [CROSS_WINDOW]: 跨窗口焦点 — **已移除**
    # ═══════════════════════════════════════════════════════════════
    # @removed 2026-06-30: 跨窗口写操作从 hook 迁移到 MCP cog-tools。
    # PreToolUse 不再写入 state/*，所有状态变更走 MCP cog-tools 统一路径。
    # 原逻辑：写 .cw_tool_update.json → cross_window_hook.py 更新焦点。
    # 替代路径：cog-context MCP tool 处理跨窗口感知。
    # 对应incident-log#3: Proxy 污染教训 — 中间层不修改下游请求体。

    # ═══════════════════════════════════════════════════════════════
    # CHECK 2 [API_KEY]: git commit 前 sk- 模式全量扫描（safety-api-key.md 规则1落地 @since: 2026-08-16 融合自二号）
    # 覆盖 Bash+PowerShell 两通道; 全文件类型（不限 py/ps1/sh, 防 settings/文档/测试数据带 key 入库）; 纯PS正则零外部依赖
    if ($toolName -eq 'Bash' -or $toolName -eq 'PowerShell') {
        $apiKeyCmd = $toolInput.command
        if ($apiKeyCmd -match '\bgit\s+(-C\s+["'']?[^"''\s]+["'']?\s+)?commit\b' -and $apiKeyCmd -notmatch '\b--(amend|allow-empty|no-verify)\b') {
            $stagedAll = & git diff --cached 2>$null | Out-String
            if ($stagedAll -match 'sk-[a-zA-Z0-9]{20,}') {
                $keyHits = @([regex]::Matches($stagedAll, 'sk-[a-zA-Z0-9]{20,}') | ForEach-Object { $_.Value.Substring(0, [Math]::Min(10, $_.Value.Length)) + '...' } | Sort-Object -Unique)
                _EmitDecision -Decision "deny" -CheckName "API_KEY_SCAN" -Reason "[API_KEY_SCAN] 暂存区检出 sk- API Key 模式 ($($keyHits -join ', ')) — 决不允许Key进git (safety-api-key.md 规则1)"
                _WritePreToolAudit "deny" "API_KEY_SCAN" "暂存区key模式: $($keyHits -join ', ')"
            }
        }
    }

    # ═══════════════════════════════════════════════════════════════
    # CHECK 17 [CODE_REVIEW]: git commit 前自动代码审查流水线
    # 流水线: 安全扫描 → lint → Qwen独立审查 → 裁决
    # 不自动修复，不阻塞非Python文件提交
    # ═══════════════════════════════════════════════════════════════
    $codeReviewCooldown = 300  # 5分钟冷却
    if ($toolName -eq 'Bash') {
        $cmd = $toolInput.command
        if ($cmd -match '\bgit\s+(-C\s+["'']?[^"''\s]+["'']?\s+)?commit\b' -and $cmd -notmatch '\b--(amend|allow-empty|no-verify)\b') {
            # 获取暂存区Python文件
            $stagedFiles = & git diff --cached --name-only 2>$null
            $fileList = @($stagedFiles -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -ne '' })
            $pyFiles = $fileList | Where-Object { $_ -match '\.py$|\.ps1$|\.sh$' }

            if ($pyFiles.Count -gt 0) {
                # 冷却检查：同类型审查10min内不重复
                if (_IsCooldown -CheckName "CODE_REVIEW") {
                    # 冷却中，直接放行
                } else {
                    try {
                        # ── 步骤1: 安全扫描 ──
                        $secOut = & pythonw scripts/wheels/code_security_scan.py 2>$null | Out-String
                        $secObj = if ($secOut -and $secOut.Trim()) { try { $secOut | ConvertFrom-Json } catch { $null } } else { $null }

                        if ($secObj -and $secObj.verdict -eq "security_concerns") {
                            _EmitDecision -Decision "deny" -CheckName "CODE_REVIEW_SEC" -Reason "[CODE_REVIEW] 安全扫描发现 $($secObj.details.Count) 个问题: $($secObj.reason) — 请修复后重新commit"
                            _WritePreToolAudit "deny" "CODE_REVIEW_SEC" "安全扫描: $($secObj.reason)" 
                        }

                        # ── 步骤2: lint ──
                        $lintOut = & pythonw scripts/wheels/code_lint_runner.py 2>$null | Out-String
                        $lintObj = if ($lintOut -and $lintOut.Trim()) { try { $lintOut | ConvertFrom-Json } catch { $null } } else { $null }

                        if ($lintObj -and $lintObj.verdict -eq "suggestions") {
                            _WritePreToolAudit "allow" "CODE_REVIEW_LINT" "新lint问题: $($lintObj.reason)"
                        }

                        # ── 步骤3: Qwen独立审查 ──
                        $diffText = & git diff --cached 2>$null
                        $report = @{
                            git_diff = $diffText
                            changed_files = @($fileList)
                            security_scan = if ($secObj) { $secObj } else { @{verdict="passed"; details=@()} }
                            lint_result = if ($lintObj) { $lintObj } else { @{verdict="passed"; details=@()} }
                        }
                        $reportJson = $report | ConvertTo-Json -Compress -Depth 10

                        # 写临时文件 → qwen_gate
                        $reviewTmp = [System.IO.Path]::GetTempPath() + "cr_$(Get-Date -Format 'yyyyMMddHHmmss')_$([System.IO.Path]::GetRandomFileName()).json"
                        [System.IO.File]::WriteAllText($reviewTmp, $reportJson, [System.Text.Encoding]::UTF8)
                        try {
                            $reviewOut = & pythonw scripts/wheels/qwen_gate.py --code-review $reviewTmp 2>$null | Out-String
                            if ($reviewOut -and $reviewOut.Trim()) {
                                $reviewObj = try { $reviewOut | ConvertFrom-Json } catch { $null }
                                if ($reviewObj) {
                                    $rv = $reviewObj.verdict
                                    if ($rv -eq "security_concerns" -or $rv -eq "logic_errors") {
                                        _EmitDecision -Decision "deny" -CheckName "CODE_REVIEW_QWEN" -Reason "[CODE_REVIEW] Qwen审查: $($reviewObj.reason)"
                                        _WritePreToolAudit "deny" "CODE_REVIEW_QWEN" "Qwen审查: $rv - $($reviewObj.reason)" 
                                    } elseif ($rv -eq "suggestions") {
                                        _WritePreToolAudit "allow" "CODE_REVIEW_QWEN" "Qwen建议: $($reviewObj.reason)"
                                    } else {
                                        _WritePreToolAudit "allow" "CODE_REVIEW_QWEN" "Qwen通过: $rv"
                                    }
                                }
                            }
                        } finally {
                            Remove-Item $reviewTmp -ErrorAction SilentlyContinue
                        }
                    } catch {
                        _WritePreToolAudit "allow" "CODE_REVIEW_FAIL" "代码审查异常: $($_.Exception.Message)"
                        # fail-open: 审查异常不阻塞commit
                    }
                }
            }
        }
    }

    # === NOTEBOOK HOOK: 长链笔记本遥测 + 提醒 ===
    # Layer A: 所有 Write 静默记录全量遥测
    # Layer B: 重要目录 Write 时如果有活跃笔记本，注入 check-in 提醒
    if ($toolName -eq 'Write') {
        $env:CLAUDE_TOOL_NAME = $toolName
        $env:CLAUDE_TOOL_PARAMS = ($toolInput | ConvertTo-Json -Compress)
        try {
            $nbOut = & pythonw .claude/hooks/notebook_hook.py 2>&1 | Out-String
            if ($nbOut.Trim()) {
                Write-Output $nbOut.Trim()
            }
        } catch {
            # 静默失败
        }
    }

    # ── stance 档位查表 (2026-08-22 改动a·maintainer批准三改动框架) ──
    # 放在全部既有 CHECK 之后: 不改17个CHECK的deny逻辑, retreat-ask 作为放行前最后一道人工确认。
    # 只读不写; 写入唯一路径 = cog-context(mcp_cls_tools.py set-stance, 锁+CAS)。
    # farming/缺失/乱值/过期 → 现状不动(与无此字段逐字节等价)。retreat 用 ask 不用 deny, 绝不新增死锁路径。
    $stance = "farming"
    try {
        $stanceFile = Join-Path $PROJECT_ROOT 'state\active_context.json'
        if (Test-Path $stanceFile) {
            $ctxObj = [System.IO.File]::ReadAllText($stanceFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
            $sMode = $null; $sExp = $null
            if ($ctxObj.stance -is [string]) { $sMode = [string]$ctxObj.stance }
            elseif ($null -ne $ctxObj.stance -and $ctxObj.stance.mode) {
                $sMode = [string]$ctxObj.stance.mode
                if ($ctxObj.stance.expires_at) { $sExp = [double]$ctxObj.stance.expires_at }
            }
            if ($sMode -notin @('farming','skirmish','teamfight','retreat')) { $sMode = 'farming' }
            if ($sExp -and ([DateTimeOffset]::FromUnixTimeSeconds([long]$sExp).UtcTicks) -lt [DateTime]::UtcNow.Ticks) { $sMode = 'farming' }
            $stance = $sMode
        }
    } catch { $stance = "farming" }

    # teamfight: 写assistant交付/knowledge路径 → 强化日志 + 数值断言提醒 (仅注入, 不拦不退)
    if ($stance -eq 'teamfight' -and $toolName -in @('Write','Edit')) {
        $tfPath = ""
        if ($toolInput -and $toolInput.file_path) { $tfPath = [string]$toolInput.file_path }
        if ($tfPath -match 'assistant交付|knowledge') {
            _SilentLog "STANCE_TEAMFIGHT" "teamfight档写入交付路径: $tfPath"
            $tfTip = '[STANCE:teamfight] 交付/核心文件写入中: ①数值断言提醒 — 涉及数字的修改用 python -c 写死计算验证, 禁止心算(数值铁律); ②本次写入已强化审计日志(pre_tool_audit.jsonl STANCE_TEAMFIGHT)。'
            Write-Output (@{hookSpecificOutput=@{hookEventName="PreToolUse";additionalContext=$tfTip}} | ConvertTo-Json -Compress)
        }
    }

    # retreat: Write/Edit 转 ask 人工确认 (诊断类 grep/ls/Read/python -c diagnos* 天然不受影响)
    # 直发不走 _EmitDecision: 绕开10min弹窗冷却 — retreat 期间每次写入都要过人工确认, 不静默放行
    if ($stance -eq 'retreat' -and $toolName -in @('Write','Edit','NotebookEdit')) {
        $rtPath = ""
        if ($toolInput -and $toolInput.file_path) { $rtPath = [string]$toolInput.file_path }
        _WritePreToolAudit "ask" "STANCE_RETREAT" "retreat档写入请求: $rtPath"
        $rtOut = @{hookSpecificOutput=@{hookEventName="PreToolUse";permissionDecision="ask";permissionDecisionReason="[STANCE:retreat] 刚触发incident-log, 处于诊断保护档: 非诊断类写入需人工确认。诊断类(grep/ls/Read/python -c diagnos*)不受影响; TTL到期自动回farming。确认安全可放行。"}} | ConvertTo-Json -Compress
        [Console]::Out.WriteLine($rtOut)
        exit 0
    }

    # All clear -- default allow
    # @fix 2026-08-10 二号融合: content_gaze 改定期 sweep (不再每次 Write spawn, 省进程+防死链)
    # @fix 2026-08-16 一号融合: unified_monitor --fast 已挪至 PostToolUse.ps1, 此处去重删除

	    $allowOut = @{hookSpecificOutput=@{hookEventName="PreToolUse";permissionDecision="allow";permissionDecisionReason="No checks triggered"}} | ConvertTo-Json -Compress
	    [Console]::Out.WriteLine($allowOut)
    exit 0

} catch {
    # fail-open: any error allows the action — BUT log it first
    try {
        $crashEntry = @{
            ts=(Get-Date -Format "o")
            event="hook_crashed"
            error=$_.Exception.Message
            line=$_.InvocationInfo.ScriptLineNumber
            raw_len=if ($raw) { $raw.Length } else { 0 }
        } | ConvertTo-Json -Compress
        Add-Content -Path $diagLog -Value $crashEntry -ErrorAction SilentlyContinue
    } catch {}
    exit 0
}
