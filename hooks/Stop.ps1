﻿# Stop.ps1 — 会话停止自动收尾  |  v1.0
# ===============================================
# CC会话停止时触发，自动保存状态/更新last_operation/推daemon。
# fail-open: 从不阻止CC的stop操作。
#
# 触发: CC的 Stop 事件
# 替代: AI手动调/save → 现在自动执行

$ErrorActionPreference = 'Continue'
$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $PROJECT_ROOT) { $PROJECT_ROOT = $env:CLS_ROOT }

# ── 0. 应急门快速绕过 ─────────────────────────────────────────
if (Test-Path "$PROJECT_ROOT\data\state\emergency_bypass.flag") {
    exit 0
}

# ── 0.5 自主任务检查: 有未完成任务时block退出 (@added 2026-07-29) ──
# @fix 2026-08-10 二号融合: 只 block 最近5分钟有工具活动的(防计时器误触发)
$autonomyFile = "$PROJECT_ROOT\data\state\autonomy_state.json"
if (Test-Path $autonomyFile) {
    try {
        $autoState = Get-Content $autonomyFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($autoState.active -eq $true) {
            $recent = $false
            $opsFile = "$PROJECT_ROOT\data\state\ops_freq.jsonl"
            if (Test-Path $opsFile) {
                $lastLine = Get-Content $opsFile -Tail 1
                if ($lastLine -match '"ts":\s*([0-9.]+)') {
                    $lastTs = [double]$matches[1]
                    $now = [double](Get-Date -UFormat %s)
                    if ($now - $lastTs -lt 300) { $recent = $true }
                }
            }
            if ($recent) {
                Write-Output '{"decision":"block","reason":"自主任务未完成。请手动确认退出。"}'
                exit 0
            } else { Write-Output "自主状态但无近期工具活动, 允许退出" }
        }
    } catch { }
}

$pythonExe = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) {
    $pythonExe = "pythonw"
}

# ── 1. 写 last_operation.json (会话停止记录) ─────────────────
$op = @{
    ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss.fffzzz")
    event = "session_stop"
    source = "CC_Stop_hook"
}
try {
    $op | ConvertTo-Json -Compress | Set-Content -Path "$PROJECT_ROOT\data\memory\last_operation.json" -Encoding UTF8
} catch { }

# ── 2. 更新跨窗口上下文 ──────────────────────────────────────
& $pythonExe "$PROJECT_ROOT\scripts\wheels\cross_window_hook.py" auto_update 2>&1 | Out-Null

# ── 3. 清理 .compact_flag (如果存在) ──────────────────────────
$flagFile = "$PROJECT_ROOT\.compact_flag"
if (Test-Path $flagFile) {
    try {
        Remove-Item $flagFile -Force
    } catch { }
}

# ── 4a. 清理认知循环僵尸锁 ─────────────────────────────────
try {
    $lockDir = "$PROJECT_ROOT\data\state\locks"
    if (Test-Path $lockDir) {
        $now = (Get-Date).ToUnixTimeSeconds()
        $maxAge = 600  # 同 mcp_cls_tools.py _COG_MAX_LOCK_AGE
        Get-ChildItem "$lockDir\*.lock" -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                $lockContent = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
                $lockTime = $lockContent.created_at
                if ($lockTime -and ($now - $lockTime) -gt $maxAge) {
                    Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
                }
            } catch { }
        }
    }
} catch { }

# ── 4a. ANCHOR 行强制落盘 (2026-08-20 maintainer定调) ──────────────
# 回复首行 ANCHOR: 声明 = 模型对自身行为的原生自述, 强制记录到
# data/state/cog_anchor_log.jsonl — 小模型行为分析语料 + 未来微调数据集。
# anchor_capture.py 从 stdin JSON 读 transcript_path, 解析最后一条 assistant 消息。
try {
    $anchorScript = "$PROJECT_ROOT\scripts\wheels\anchor_capture.py"
    if (Test-Path $anchorScript) {
        # @fix 2026-08-22 (incident-log#12/#17族): `$input | Out-String` 走GBK码页, UTF-8中文腐蚀。
        # anchor_capture.py 要读 transcript 中文锚点, 改字节流+UTF8显式解码。
        $_sr = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), [System.Text.Encoding]::UTF8)
        $rawIn = $_sr.ReadToEnd()
        $_sr.Close()
        if ($rawIn.Trim()) {
            $rawIn | & python $anchorScript 2>$null
        }
    }
} catch { }

# ── 4b. 会话结束标记 → cog_step_end.json (不覆盖 cog_step.json) ──
# @fix 2026-08-19 二号交付: 原逻辑每轮 Stop 覆盖 cog_step.json, 把模型 ANCHOR 声明的真实任务锚点
# 抹成'会话结束'碎片 → unified_inject 锚点链失效(一号'注入无关知识'根因①)。
# 终止标记独立存 cog_step_end.json, cog_step.json 只归 cog-step-declare 管。
# @fix 2026-08-10 二号融合: 必须保留原 _meta.window_id (窗口隔离P0)。
$prevWindowId = $null
try {
    if (Test-Path "$PROJECT_ROOT\data\state\cog_step_end.json") {
        $prevCog = Get-Content "$PROJECT_ROOT\data\state\cog_step_end.json" -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($prevCog._meta -and $prevCog._meta.window_id) {
            $prevWindowId = $prevCog._meta.window_id
        }
    }
} catch { $prevWindowId = $null }

$cogStep = @{
    version = 1
    phase = 0
    label = "会话结束 (CC Stop hook)"
    declared_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")
    _meta = @{
        window_id = if ($prevWindowId) { $prevWindowId } else { $env:CLAUDE_CODE_SESSION_ID.Substring(0, [Math]::Min(16, $env:CLAUDE_CODE_SESSION_ID.Length)) }
    }
} | ConvertTo-Json -Compress
try {
    $cogStep | Set-Content -Path "$PROJECT_ROOT\data\state\cog_step_end.json" -Encoding UTF8
} catch { }

# ── 4c. 开团前CD清点 (@added 2026-08-22 框架改动b·maintainer批准, 顺序c→b→a) ──────
# 开团(下一轮任务)前清点未转好的复核债(CD): 只读扫描三来源, 有命中生成一句话清单,
# 经 §5 合并出口以 Stop additionalContext 信封注入。铁律: 只亮牌不拦截 —
# 全程无 decision/block/deny 字段, 不改变 exit code, 决定权留给模型(三层记忆架构: 荐而不决)。
# 约束: 清单≤5条防淹没 | 单源失败静默跳过+写 hook_diag.jsonl | 显式UTF8(incident-log#12/#17) |
#       时间戳解析失败跳过不当0 | 纯只读无锁 | 同步<2s 零spawn零API。
# 注: §0.5 自主block分支提前exit时本段不执行 — 不许退出的轮次无需开团清点。
$cdItems = New-Object System.Collections.Generic.List[string]
$cdMsg = ""
function _CdDiag([string]$Src, [string]$Err) {
    # 扫描失败诊断 — 与 PreToolUse 同一 hook_diag.jsonl 同格式; 写日志自身失败再静默(fail-open)
    try {
        if ($Err.Length -gt 200) { $Err = $Err.Substring(0, 200) }
        $d = @{ts=(Get-Date -Format "o"); event="cd_scan_fail"; src=$Src; err=$err}
        Add-Content -Path "$PROJECT_ROOT\assistant交付\🔍 符号动力学审计\hook_diag.jsonl" -Value ($d | ConvertTo-Json -Compress) -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch { }
}
# 来源③ state_freshness.py 已报过期的数据源 (直接读其落盘 escalation 文件, 免spawn python;
# 排最前 — 系统级过期影响认知循环本身, 优先亮牌)
try {
    $feFile = Join-Path $PROJECT_ROOT "data\state\freshness_escalation.json"
    if (Test-Path $feFile) {
        $fe = Get-Content $feFile -Raw -Encoding UTF8 | ConvertFrom-Json
        # @backport 2026-09-14 二号v2: cog_step 的 TTL 过期是自然行为(离开>5min必过期),
        #   列进CD=慢性常驻项只刷屏不催行动(maintainer8-29反馈"不太准/每次都刷"), 剔除
        $feStale = @($fe.stale | Where-Object { $_ -notmatch 'cog_step' })
        if ($feStale.Count -gt 0) {
            $cdItems.Add("state过期:" + ($feStale -join "/"))
        }
    }
} catch { _CdDiag "freshness_escalation" "$_" }
# 来源① KG结论库: anchor_level=model_authored 且超7天未复核 (百行级小文件, 流式读)
try {
    $kgFile = Join-Path $PROJECT_ROOT "knowledge\知识图谱\kg_conclusions.jsonl"
    if (Test-Path $kgFile) {
        $cutoff = (Get-Date).ToUniversalTime().AddDays(-7)
        $kgStaleCount = 0
        $kgDisputedCount = 0
        foreach ($line in [System.IO.File]::ReadLines($kgFile, [System.Text.Encoding]::UTF8)) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            try {
                $e = $line | ConvertFrom-Json
                # @backport 2026-09-14 二号v2: disputed 分歧项不限7天直接亮牌(maintainer必看)
                if ($e.verdict -eq "disputed") { $kgDisputedCount++; continue }
                if ($e.anchor_level -ne "model_authored") { continue }
                # @backport 2026-09-14 二号9-13定稿: unverifiable 是终态不是债 — 机检缺证据链
                #   本身就是裁决结论, 无任何机制能消化它, 原实现数字只增不减
                if ($e.verdict -in @("verified_multi_ai", "rejected", "verified", "human_confirmed", "unverifiable")) { continue }
                # @fix 2026-09-07 已复核豁免(maintainer批准): 有 reviewed_at/review_note 的条目视为已复核
                #   否则复核动作无法消除CD项(判定只看创建时间, 复核了照样计数) — 修前ma超期20条
                if ($e.reviewed_at -or $e.review_note) { continue }
                if (-not $e.ts) { continue }               # 无时间戳 → 跳过, 不当0误报超期
                $ts = [DateTimeOffset]::Parse($e.ts)       # 解析异常 → catch 跳过该条
                if ($ts.UtcDateTime -lt $cutoff) { $kgStaleCount++ }
            } catch { }                                    # 坏行/坏时间戳跳过, 不计数不当0
        }
        if ($kgStaleCount -gt 0) { $cdItems.Add("KG model_authored结论超7天未复核×$kgStaleCount") }
        if ($kgDisputedCount -gt 0) { $cdItems.Add("KG多AI审核分歧待maintainer裁决×$kgDisputedCount") }  # @backport 二号v2
    }
} catch { _CdDiag "kg_conclusions" "$_" }
# 来源② 双轨进度: 正文标 @unverified 的未验证结论 (mtime倒序上限120个 — 学习进度目录已有近千文件,
# 全量扫会拖垮2s预算; 近期交付的未验证项才是开团前需清点的复核债, 超限部分放弃并在此注明)
try {
    $progDir = Join-Path $PROJECT_ROOT "assistant交付\📚 学习资料\学习进度"
    if (Test-Path $progDir) {
        $recent = Get-ChildItem $progDir -Filter "*双轨进度*" -File -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTime -Descending | Select-Object -First 120
        foreach ($pf in $recent) {
            try {
                $txt = [System.IO.File]::ReadAllText($pf.FullName, [System.Text.Encoding]::UTF8)
                # @fix 2026-08-22 实地事故: 原正文任意位置含'@unverified'即命中 → 描述性提及也进清单(今日3条全误报)。
                # 改行首标记匹配: 只有独立成行的 @unverified 才算复核债声明。
                if ($txt -match '(?m)^\s*@unverified\b') {
                    $nm = $pf.BaseName -replace '^\d{4}-?\d{2}-?\d{2}_?\d{0,4}_双轨进度_[A-Za-z0-9]*_?', ''
                    if (-not $nm -or $nm.Length -lt 2) { $nm = $pf.BaseName }
                    if ($nm.Length -gt 24) { $nm = $nm.Substring(0, 24) + "…" }
                    $cdItems.Add("@unverified:$nm")
                }
            } catch { }   # 单文件读取失败跳过该文件
        }
    }
} catch { _CdDiag "dual_track_progress" "$_" }
# 汇总: ≤5条硬上限, 溢出时附总数(不静默截断)
if ($cdItems.Count -gt 0) {
    $shown = ($cdItems | Select-Object -First 5) -join "、"
    if ($cdItems.Count -gt 5) { $shown = "$shown(共$($cdItems.Count)项, 列前5)" }
    $cdMsg = "[CD清点] 以下CD未转好: $shown, 本次结论含未复核项"
}
# @hotfix 2026-08-22 实地事故(maintainer报告'一上来就循环刷屏'): CD清单含慢性常驻项(trajectory过期等),
# 每轮 Stop 都弹同一清单 → nagging 循环 → 注入疲劳(违反注入三原则: 无增量/不抗过时)。
# 修复: 去重+冷却 — 内容 hash 不变且 30min 内 → 静默; 首轮(无基线)只建基线不注入。
$cdLastFile = Join-Path $PROJECT_ROOT "data\state\cd_last.json"
try {
    $listHash = ""
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($cdItems -join "|"))
    $md5 = [System.Security.Cryptography.MD5]::Create()
    $listHash = [BitConverter]::ToString($md5.ComputeHash($bytes)) -replace '-', ''
    $lastH = ""; $lastT = 0.0
    if (Test-Path $cdLastFile) {
        try {
            $cl = Get-Content $cdLastFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $lastH = [string]$cl.hash
            $lastT = [double]$cl.ts
        } catch {}
    }
    $nowEp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $suppress = $true
    if ($lastH -eq "") {
        $suppress = $true            # 首轮: 只建基线, 不注入(冷启动静默)
    } elseif ($listHash -ne $lastH) {
        $suppress = $false           # 清单变化 = 有新CD转好/新增 → 值得说一次
    } elseif (($nowEp - $lastT) -ge 7200) {  # @backport 2026-09-14 二号v2: 30→120min(maintainer8-29反馈每次都刷)
        $suppress = $false           # 同清单 120min 提醒一次
    }
    if ($suppress) { $cdMsg = "" }
    if ($listHash -ne $lastH -or (-not $suppress)) {
        $cdState = @{ts=$nowEp; hash=$listHash; items=$cdItems.Count}
        $cdState | ConvertTo-Json -Compress | Set-Content -Path $cdLastFile -Encoding UTF8 -ErrorAction SilentlyContinue
    }
} catch { }

# ── 5. 注入问卷收卷 (2026-09-01 改异步: 不阻塞 Stop 出口) ──
$fbScript = "$PROJECT_ROOT\scripts\wheels\injection_feedback.py"
if (Test-Path $fbScript) {
    try { & $pythonExe $fbScript 2>$null | Out-Null } catch { }
    $fbPendingFile = "$PROJECT_ROOT\data\stateb_pending.txt"
    try {
        $promptOut = & $pythonExe $fbScript maybe_prompt 2>$null
        $promptOut = ($promptOut | Out-String).Trim()
        if ($promptOut) { $promptOut | Set-Content -Path $fbPendingFile -Encoding UTF8 -ErrorAction SilentlyContinue }
    } catch { }
}
$fbPendingFile = "$PROJECT_ROOT\data\stateb_pending.txt"
$promptOut = ""
if (Test-Path $fbPendingFile) {
    try {
        $promptOut = (Get-Content $fbPendingFile -Raw -Encoding UTF8).Trim()
        Remove-Item $fbPendingFile -Force -ErrorAction SilentlyContinue
    } catch { }
}
$combinedCtx = (@($promptOut, $cdMsg) | Where-Object { $_ -and $_.ToString().Trim() }) -join "`n"
if ($combinedCtx.Length -gt 0) {
    $fbOut = @{hookSpecificOutput=@{hookEventName="Stop";additionalContext=$combinedCtx}} | ConvertTo-Json -Compress
    Write-Output $fbOut
}

exit 0
