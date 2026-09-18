﻿# =============================================================================
# CLS HARDENED SessionStart Hook — 会话启动初始化
# =============================================================================
# 0. 控制台编码（必须在最前面，否则后续中文全部乱码）
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
# =============================================================================
# 执法清单:
#   1. self_activate (冷启动检测)
#   2. 清理过期 .compact_flag (新会话开始，旧 flag 作废)
#   3. 清理临时文件 (clean-temp-files)
#   4. 检查 .unarchived_delivery (上次会话交付提醒)
#   5. 符号动力学漂移检查 (.symbolic_drift_alert)
#   6. 认知循环缺步检查 (.incomplete_loop → 补偿注入)
#   7. 引导注入检查 (.guidance_injection)
#   8. 组件变更扫描 (component_scanner)
#   9. Git pull 共享knowledge (自动同步)
#  10. daemon健康扫描 (daemon_observer复活)
#  11. 流动模式评估 (mode_governor违规检查)
#  12. 缓存磁盘检查 (>.claude 500MB警告)
#  13. 前提泄洪闸 (premise_check激活验证)
#  14. 记忆衰减扫描 (memory_decay)
#  15. 不可判定命题隔离 (undecidable_isolator)
#  16. Hook健康检查 (hook_health_watchdog — 独立交叉验证)
#  17. 知识摘要 (最近进度文件+下一步，触发knowledge检索意识)
#  18. MCP Profile 检查 (mcp_profile状态+full警告)
# =============================================================================

$ErrorActionPreference = "SilentlyContinue"
# 自动推导项目根: .claude/hooks/ → .claude/ → PROJECT_ROOT
$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
# 降级: PSScriptRoot 为空时(罕见)使用环境变量
if (-not $PROJECT_ROOT) { $PROJECT_ROOT = $env:CLS_ROOT }
Set-Location $PROJECT_ROOT

# v2 (2026-06-10): 详细诊断写日志文件，stdout只输出固定一行 → 缓存友好
$DIAG_LOG = "$PROJECT_ROOT\state\session_start_diag.log"
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
function _diag($msg) {
    $line = "[$((Get-Date -Format 'HH:mm:ss'))] $msg"
    Add-Content $DIAG_LOG $line -Encoding UTF8
}
_diag "============================================================"
_diag "[CLS SessionStart] $ts — 会话启动初始化"
_diag "============================================================"

# ═══════════════════════════════════════════════════════════════════
# 1. self_activate — 冷/暖启动检测
# ═══════════════════════════════════════════════════════════════════
try {
    $stateFile = "$PROJECT_ROOT\state\activation_state.json"
    if (Test-Path $stateFile) {
        # @fix 2026-09-12: 原缺 -Encoding UTF8 → PS 5.1 按 GBK 读 UTF-8 中文 → ConvertFrom-Json 炸
        #   → catch 静默 → self_activate 分支 38/38 次从未走到过(实测 session_start_diag.log)。
        #   同族: incident-log#23(控制台编码) / #68(Edit 写无 BOM UTF-8, PS 按 GBK 解析)。
        #   修它不是为了让它跑起来(status=ALIVE 会走暖启动跳过), 而是让日志说真话。
        $state = Get-Content $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($state.status -eq "DEAD") {
            _diag "[SessionStart] 冷启动 → self_activate"
            $sa_out = & pythonw scripts/self_activate.py 2>&1; if ($sa_out) { _diag ($sa_out -join "`n") }
        } else {
            _diag "[SessionStart] 暖启动 (status=$($state.status))，跳过激活"
        }
    } else {
        _diag "[SessionStart] 无状态文件 → self_activate"
        $sa_out = & pythonw scripts/self_activate.py 2>&1; if ($sa_out) { _diag ($sa_out -join "`n") }
    }
} catch {
    _diag "[SessionStart] ERROR (self_activate): $_"
}

# ═══════════════════════════════════════════════════════════════════
# 1.5 跨窗口感知 — 每次窗口打开自动宣告+窥视
# ═══════════════════════════════════════════════════════════════════
try {
    & pythonw -c "from scripts.wheels.cross_window_hook import auto_peek_and_announce; auto_peek_and_announce(focus='会话启动', domain='general')" 2>$null | Out-Null
    _diag "[SessionStart] 跨窗口感知: 完成 (peek+announce)"
} catch {
    _diag "[SessionStart] 跨窗口感知跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# [已移除 2026-07-04] 1.8 CC守护进程拉起 — 全部守护进程已清除
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# 1.9 MCP孤儿进程清理（--once模式，不常驻）
# ═══════════════════════════════════════════════════════════════════
try {
    & pythonw scripts/wheels/mcp_orphan_killer.py --once 2>$null | Out-Null
    _diag "[SessionStart] MCP孤儿进程清理完成"
} catch {
    _diag "[SessionStart] MCP孤儿进程清理跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 2. 清理过期 .compact_flag (新会话不需要旧 compact 信号)
# ═══════════════════════════════════════════════════════════════════
try {
    $flagFile = "$PROJECT_ROOT\.compact_flag"
    if (Test-Path $flagFile) {
        Remove-Item $flagFile -Force
        _diag "[SessionStart] 已清理旧 .compact_flag"
    }
} catch {}

# ═══════════════════════════════════════════════════════════════════
# 3. 清理临时文件
# ═══════════════════════════════════════════════════════════════════
try {
    $cleanScript = "$PROJECT_ROOT\.claude\hooks\clean-temp-files.ps1"
    if (Test-Path $cleanScript) {
        $clean_out = & $cleanScript 2>&1; if ($clean_out) { _diag ($clean_out -join "`n") }
        _diag "[SessionStart] 临时文件清理完成"
    }
} catch {
    _diag "[SessionStart] 清理跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 3.5. 安全网：删除 temp/cc_tmp 下的残余备份
# post-commit hook 已删除 + CLAUDE_CODE_TMPDIR 已移除
# backup_to_baidu.py 不会在 git commit 时自动触发了
# 这段作为安全网，万一还有残余就顺手清掉
# ═══════════════════════════════════════════════════════════════════
try {
    $ccTmpBackups = "$PROJECT_ROOT\temp\cc_tmp\claude_backup_*"
    $found = Get-ChildItem $ccTmpBackups -Directory -ErrorAction SilentlyContinue
    if ($found) {
        $totalMB = 0
        foreach ($b in $found) {
            $sz = (Get-ChildItem $b.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
            $totalMB += [math]::Round($sz / 1MB, 0)
            Remove-Item $b.FullName -Recurse -Force -ErrorAction SilentlyContinue
            _diag "[SessionStart] 已删除残余备份: $($b.Name) ($([math]::Round($sz/1MB,0))MB)"
        }
        _diag "[SessionStart] 清理残余备份 ${totalMB}MB（根因已除，不太可能再出现）"
    }
} catch {
    _diag "[SessionStart] 残余备份清理跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 4. 检查上次会话的未归档交付提醒
# ═══════════════════════════════════════════════════════════════════
try {
    $unarchivedFlag = "$PROJECT_ROOT\.claude\cls_state\.unarchived_delivery"
    if (Test-Path $unarchivedFlag) {
        $info = Get-Content $unarchivedFlag -Raw -Encoding UTF8 | ConvertFrom-Json
        _diag ""
        _diag "╔══════════════════════════════════════════════════════════╗"
        _diag "║  ⚠️  上次会话有未归档交付                                    ║"
        _diag ("║  任务: " + $info.task.Substring(0, [Math]::Min(40, $info.task.Length)) + " | 文件: " + $info.files.Count + " 个                                      ║")
        _diag "║  运行 /capture 归档, 或 touch .no_delivery_check 永久跳过   ║"
        _diag "╚══════════════════════════════════════════════════════════╝"
        _diag ""
    }
} catch {}

# ═══════════════════════════════════════════════════════════════════
# 5. 符号动力学漂移检查 (.symbolic_drift_alert)
# ═══════════════════════════════════════════════════════════════════
try {
    $driftAlert = "$PROJECT_ROOT\.claude\cls_state\.symbolic_drift_alert"
    if (Test-Path $driftAlert) {
        $drift = Get-Content $driftAlert -Raw -Encoding UTF8 | ConvertFrom-Json
        _diag ""
        _diag "╔══════════════════════════════════════════════════════════╗"
        _diag "║  ⚠️  符号动力学漂移告警                                     ║"
        _diag ("║  整体漂移: " + $drift.drift + " (阈值: " + $drift.threshold + ")                              ║")
        foreach ($domain in $drift.domains.PSObject.Properties) {
            _diag ("║  " + $domain.Name + ": drift=" + $domain.Value + "                                  ║")
        }
        _diag "║  AI 应解释漂移原因并决定是否调整基线。                       ║"
        _diag "╚══════════════════════════════════════════════════════════╝"
        _diag ""
        Remove-Item $driftAlert -Force
    }
} catch {}

# ═══════════════════════════════════════════════════════════════════
# 6. 认知循环缺步检查 (.incomplete_loop)
# ═══════════════════════════════════════════════════════════════════
try {
    $incompleteLoop = "$PROJECT_ROOT\.claude\cls_state\.incomplete_loop"
    if (Test-Path $incompleteLoop) {
        $loop = Get-Content $incompleteLoop -Raw -Encoding UTF8 | ConvertFrom-Json
        $gapNames = ($loop.gaps.PSObject.Properties | ForEach-Object { $_.Value }) -join ', '
        _diag ""
        _diag "╔══════════════════════════════════════════════════════════╗"
        _diag "║  ⚠️  上次会话认知循环不完整                                  ║"
        _diag ("║  缺步: " + $gapNames + "                          ║")
        _diag "║  AI 应在本次 session 中补完缺失的认知步骤。                  ║"
        _diag "╚══════════════════════════════════════════════════════════╝"
        _diag ""
        # 写补偿指令到 guidance_injection
        $guidance = @{
            ts = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
            reason = "loop_compensation"
            gaps = $loop.gaps
            action = "AI 应优先补完缺失的认知循环步骤"
        }
        $guidanceFile = "$PROJECT_ROOT\.claude\cls_state\.guidance_injection"
        $guidance | ConvertTo-Json -Depth 4 | Set-Content $guidanceFile -Encoding UTF8
        Remove-Item $incompleteLoop -Force
    }
} catch {}

# ═══════════════════════════════════════════════════════════════════
# 7. 引导注入检查 (.guidance_injection)
# ═══════════════════════════════════════════════════════════════════
try {
    $guidanceFile = "$PROJECT_ROOT\.claude\cls_state\.guidance_injection"
    if (Test-Path $guidanceFile) {
        $g = Get-Content $guidanceFile -Raw -Encoding UTF8 | ConvertFrom-Json
        _diag "[SessionStart] 📍 引导注入: $($g.reason) — $($g.action)"
        # content 留在文件里让 AI 读取，不删
        # 窗口隔离 (2026-08-16 融合自 M1 §7): 仅本窗口或<2h的 guidance 记入诊断
        $gw = $g.window
        $myWid = if ($env:CLAUDE_CODE_SESSION_ID) { $env:CLAUDE_CODE_SESSION_ID.Substring(0, [Math]::Min(12, $env:CLAUDE_CODE_SESSION_ID.Length)) } else { "" }
        if ((-not $gw) -or $gw -eq $myWid -or ([DateTime]::UtcNow - [DateTime]::Parse($g.ts)).TotalHours -lt 2) {
            _diag "[SessionStart] 脑区guidance: 本窗口适用 (gw=$gw my=$myWid)"
        } else {
            _diag "[SessionStart] guidance跨窗口跳过: gw=$gw my=$myWid"
        }
    }
} catch {}

# ═══════════════════════════════════════════════════════════════════
# 8. 组件变更扫描
# ═══════════════════════════════════════════════════════════════════
try {
    $scannerScript = "$PROJECT_ROOT\scripts\component_scanner.py"
    if (Test-Path $scannerScript) {
        $scanOutput = & pythonw $scannerScript 2>&1
        _diag "[SessionStart] 组件扫描完成"
    }
} catch {
    _diag "[SessionStart] 组件扫描跳过"
}

# ═══════════════════════════════════════════════════════════════════
# 9. Git pull 共享knowledge (自动同步)
# ═══════════════════════════════════════════════════════════════════
try {
    $syncScript = "$PROJECT_ROOT\scripts\sync_knowledge.py"
    if (Test-Path $syncScript) {
        & pythonw "$syncScript" pull --auto 2>$null | Out-Null
        _diag "[SessionStart] 知识同步 (pull) 完成"
    }
} catch {
    _diag "[SessionStart] 知识同步跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# [已移除 2026-07-04] 10. Daemon 健康检查 — 全部守护进程已清除
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# 11. 流动模式评估 — compact 恢复后检查零违规
# ═══════════════════════════════════════════════════════════════════
try {
    $modeScript = "$PROJECT_ROOT\scripts\wheels\mode_governor.py"
    if (Test-Path $modeScript) {
        $modeStatus = & pythonw $modeScript --status --json 2>$null | ConvertFrom-Json
        _diag "[SessionStart] 当前权限模式: $($modeStatus.mode)"
    }

    # 检查违规状态 — 零违规时提示进入流动模式
    $violationFile = "$PROJECT_ROOT\.claude\cls_state\violation_state.json"
    if (Test-Path $violationFile) {
        $vs = Get-Content $violationFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $totalViolations = 0
        if ($vs.rules) {
            foreach ($r in $vs.rules.PSObject.Properties) {
                $totalViolations += ($r.Value.violations_24h -as [int])
            }
        }
        if ($totalViolations -eq 0) {
            _diag "[SessionStart] 📍 零违规 — 建议进入流动模式"
        }
    }
} catch {
    _diag "[SessionStart] 流动模式评估跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 12. 缓存磁盘检查 — 启动时报告缓存大小
# ═══════════════════════════════════════════════════════════════════
try {
    $cacheDir = "$PROJECT_ROOT\.claude"
    if (Test-Path $cacheDir) {
        $cacheSize = (Get-ChildItem $cacheDir -Recurse -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
        $cacheSizeMB = [math]::Round($cacheSize / 1MB, 1)
        if ($cacheSizeMB -gt 500) {
            _diag "[SessionStart] ⚠️ .claude 缓存: ${cacheSizeMB}MB > 500MB 警告线 — 建议运行 clean-temp-files"
        } else {
            _diag "[SessionStart] 缓存大小: ${cacheSizeMB}MB"
        }
    }
} catch {
    _diag "[SessionStart] 缓存检查跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 12b. 每日缓存基线 — 确保今日 baseline 存在（每日首次会话创建）
# ═══════════════════════════════════════════════════════════════════
try {
    $dailyDir = "$PROJECT_ROOT\data\cache_monitor\daily"
    $todayStr = (Get-Date).ToString("yyyyMMdd")
    $todayBaseline = Join-Path $dailyDir "$todayStr.json"
    if (-not (Test-Path $todayBaseline)) {
        $baselineScript = "$PROJECT_ROOT\scripts\wheels\cache_monitor_daily.py"
        if (Test-Path $baselineScript) {
            & pythonw "$baselineScript" --baseline 2>$null | Out-Null
            _diag "[SessionStart] 缓存基线: $todayStr 已创建"
        }
    } else {
        _diag "[SessionStart] 缓存基线: $todayStr 已存在"
    }
} catch {
    _diag "[SessionStart] 缓存基线跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 13. 前提泄洪闸 — 验证关键文件/进程存在后才允许激活
# ═══════════════════════════════════════════════════════════════════
try {
    $premiseScript = "$PROJECT_ROOT\scripts\wheels\premise_check.py"
    if (Test-Path $premiseScript) {
        $premiseOutput = & pythonw "$premiseScript" activate 2>&1
        _diag "[SessionStart] 前提闸: $($premiseOutput -join ' ')"
    }
} catch {
    _diag "[SessionStart] 前提闸跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 14. 记忆衰减扫描 — 衰减过期逻辑块，防逻辑锁死
# ═══════════════════════════════════════════════════════════════════
try {
    $decayScript = "$PROJECT_ROOT\scripts\wheels\memory_decay.py"
    if (Test-Path $decayScript) {
        & pythonw "$decayScript" --prune 2>$null | Out-Null
        _diag "[SessionStart] 记忆衰减扫描完成"
    }
} catch {
    _diag "[SessionStart] 记忆衰减跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 15. 不可判定命题隔离 — 扫描knowledge中无法证真/证伪的命题
# ═══════════════════════════════════════════════════════════════════
try {
    $undecScript = "$PROJECT_ROOT\scripts\wheels\undecidable_isolator.py"
    if (Test-Path $undecScript) {
        $undecOutput = & pythonw "$undecScript" --stats 2>&1
        _diag "[SessionStart] 不可判定隔离: $($undecOutput -join ' ')"
    }
} catch {
    _diag "[SessionStart] 不可判定隔离跳过: $_"
    }

# ═══════════════════════════════════════════════════════════════════
# 16. Hook 健康检查 — 独立交叉验证 hook 体系健康，异常时自动打开紧急旁路
# ═══════════════════════════════════════════════════════════════════
try {
    $hhScript = "$PROJECT_ROOT\scripts\wheels\hook_health_watchdog.py"
    if (Test-Path $hhScript) {
        & pythonw "$hhScript" --once 2>$null | Out-Null
        _diag "[SessionStart] Hook健康检查完成"
    }
} catch {
    _diag "[SessionStart] Hook健康检查跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 17. 知识摘要 — 输出最近进度文件关键信息，触发knowledge检索意识
# ═══════════════════════════════════════════════════════════════════
try {
    $kbProgressDir = "$PROJECT_ROOT\knowledge\进度文件"
    if (Test-Path $kbProgressDir) {
        # 取最近修改的3个.md文件（排除README和index）
        $recentFiles = Get-ChildItem $kbProgressDir -Filter "*.md" -File -ErrorAction SilentlyContinue `
            | Where-Object { $_.Name -notmatch 'README|index' } `
            | Sort-Object LastWriteTime -Descending `
            | Select-Object -First 3
        if ($recentFiles.Count -gt 0) {
            _diag "[SessionStart] 最近知识:"
            foreach ($f in $recentFiles) {
                # 读前2行取标题
                $lines = Get-Content $f.FullName -TotalCount 3 -Encoding UTF8 -ErrorAction SilentlyContinue
                $title = ($lines | Where-Object { $_ -match '^\s*#' } | Select-Object -First 1) -replace '^\s*#+\s*', ''
                if (-not $title) { $title = $f.BaseName }
                _diag "  · $title"
            }
            # 检查最近进度文件中"下一步"部分
            $latest = $recentFiles | Select-Object -First 1
            $content = Get-Content $latest.FullName -Encoding UTF8 -Raw -ErrorAction SilentlyContinue
            if ($content -match '##\s*下一步[^\n]*\n((?:\s*[\d]+\.\s*[^\n]+\n?)+)') {
                $nextSteps = $Matches[1].Trim() -split '\n' | Select-Object -First 3
                _diag "  → 下一步:"
                foreach ($step in $nextSteps) { _diag "    $step" }
            }
        }
    }
} catch {
    _diag "[SessionStart] 知识摘要跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 18. MCP Profile — 状态报告 + auto切换(为下次启动准备)
# ═══════════════════════════════════════════════════════════════════
try {
    $mcpMarker = "$PROJECT_ROOT\.claude\current_mcp_profile.txt"
    $claudeJsonPath = "$PROJECT_ROOT\.claude\claude.json"
    $mcpWheel = "$PROJECT_ROOT\scripts\wheels\mcp_profile.py"
    $profileName = "unknown"
    $serverCount = 0
    if (Test-Path $mcpMarker) {
        $profileName = (Get-Content $mcpMarker -TotalCount 1 -Encoding UTF8).Trim()
    }
    if (Test-Path $claudeJsonPath) {
        $cj = Get-Content $claudeJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $serverCount = ($cj.mcpServers | Get-Member -MemberType NoteProperty).Count
    }
    $tokenEst = $serverCount * 200
    if ($profileName -eq "full" -or $serverCount -ge 8) {
        _diag ""
        _diag "╔══════════════════════════════════════════════════════════╗"
        _diag "║  ⚠️  MCP Profile: FULL — $($serverCount)服务器 ~$($tokenEst) token/轮           ║"
        _diag "║  python scripts/wheels/mcp_profile.py --auto               ║"
        _diag "║  profiles: minimal(~8t) cad pic paper web ppt full(~67t)   ║"
        _diag "╚══════════════════════════════════════════════════════════╝"
        _diag ""
    } else {
        _diag "[SessionStart] MCP Profile: $profileName ($serverCount 服务器)"
    }

    # --- 自动切换（为下次CC启动准备，本次已加载完毕不受影响）---
    if (Test-Path $mcpWheel) {
        $prevProfile = $profileName
        pythonw "$mcpWheel" --auto 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $newProfile = (Get-Content $mcpMarker -TotalCount 1 -Encoding UTF8).Trim()
            if ($newProfile -ne $prevProfile) {
                _diag "[SessionStart] MCP auto: $prevProfile → $newProfile (下次生效)"
            }
        }
    }
} catch {
    _diag "[SessionStart] MCP Profile 检查跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 19. 数学系统扫描漏检日志检查 — 最近未匹配的高频输入
# ═══════════════════════════════════════════════════════════════════
try {
    $missLog = "$PROJECT_ROOT\data\safety\quick_scan_misses.jsonl"
    if (Test-Path $missLog) {
        # 用 Python 读出摘要
        $summary = pythonw -c @"
import json, sys
sys.path.insert(0, 'scripts/wheels')
from struct_spacetime_check import show_miss_summary
print(json.dumps(show_miss_summary(30), ensure_ascii=False))
"@ 2>&1
        if ($summary) {
            $missData = $summary | ConvertFrom-Json
            if ($missData.total -gt 0) {
                if ($missData.hot_inputs -and $missData.hot_inputs.Count -gt 0) {
                    _diag "[SessionStart] ⚠ quick_scan_misses: $($missData.total)总/$($missData.unmatched)未匹配"
                    foreach ($hot in $missData.hot_inputs) {
                        _diag "  - 高频未匹配: $hot"
                    }
                } else {
                    _diag "[SessionStart] quick_scan_misses: $($missData.total)条记录，无高频未匹配"
                }
            }
        }
    }
    # 防腐烂提醒 — 每session提醒一次数学系统扫描器可用
    _diag "[SessionStart] 🧮 数学系统扫描器 loaded: quick_structure_scan(结构分析) | compute_hybrid_strategy(MoE混合态) | transfer_analyze_lsg(管线风险) | recommend_pattern(力学推荐)"
} catch {
    _diag "[SessionStart] 漏检日志检查跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 19. 遥测文件旋转 (cog_telemetry.jsonl > 1000行 → 归档)
# ═══════════════════════════════════════════════════════════════════
$TELEMETRY_FILE = "$PROJECT_ROOT\data\state\cog_telemetry.jsonl"
if (Test-Path $TELEMETRY_FILE) {
    try {
        $lineCount = (Get-Content $TELEMETRY_FILE | Measure-Object -Line).Lines
        if ($lineCount -gt 1000) {
            $rotated = "$TELEMETRY_FILE.1"
            if (Test-Path $rotated) { Remove-Item $rotated -Force -ErrorAction SilentlyContinue }
            Rename-Item $TELEMETRY_FILE $rotated -Force -ErrorAction SilentlyContinue
            _diag "[SessionStart] cog_telemetry.jsonl 超过1000行($lineCount)，已旋转到 .1"
            # 清理更旧的 .2 文件
            $oldFile = "$TELEMETRY_FILE.2"
            if (Test-Path $oldFile) { Remove-Item $oldFile -Force -ErrorAction SilentlyContinue }
        }
    } catch {
        _diag "[SessionStart] 遥测文件旋转失败: $_"
    }
}

_diag "[CLS SessionStart] 初始化完成"
_diag "============================================================"

# ═══════════════════════════════════════════════════════════════════
# 20. session_health.json session_id 初始化（2026-07-05 新增）
# ═══════════════════════════════════════════════════════════════════
try {
    $sessionId = $env:CLAUDE_CODE_SESSION_ID
    if ($sessionId) {
        $healthFile = "$PROJECT_ROOT\state\session_health.json"
        if (Test-Path $healthFile) {
            $health = Get-Content $healthFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $health.session_id = $sessionId.Substring(0, [Math]::Min(16, $sessionId.Length))
            $health.updated_at = (Get-Date -Format "o")
            $health._meta.window_id = $sessionId.Substring(0, [Math]::Min(12, $sessionId.Length))
            $health | ConvertTo-Json -Depth 4 | Set-Content $healthFile -Encoding UTF8
            _diag "[SessionStart] session_id 初始化: $($sessionId.Substring(0,12))..."
        } else {
            # 文件不存在 → 创建基础结构
            $health = @{
                session_id = $sessionId.Substring(0, [Math]::Min(16, $sessionId.Length))
                msgs = 0
                status = @{ msgs_current = 0; msgs_max_seen = 0 }
                last_compact_at = $null
                started_at = (Get-Date -Format "o")
                warnings = @()
                compact_status = "healthy"
                _meta = @{ window_id = $sessionId.Substring(0, 12); written_at = [DateTime]::UtcNow.Ticks; schema = "v2" }
                updated_at = (Get-Date -Format "o")
                _last_refreshed_by = "sessionstart_hook"
            }
            $health | ConvertTo-Json -Depth 4 | Set-Content $healthFile -Encoding UTF8
            _diag "[SessionStart] session_health.json 创建 (新)"
        }
    }
} catch {
    _diag "[SessionStart] session_id 初始化跳过: $_"
}

# ═══════════════════════════════════════════════════════════════════
# [2026-08-16 双向融合] 以下 §21-§21.10 为 M1 独有注入组, 回植到 M2 骨架
# (M2 曾整段删除; 轮子均已验证存在于 scripts/wheels/)
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# 21. 上下文自动恢复注入 (auto_recall — 100%保证有输出)
# ═══════════════════════════════════════════════════════════════════
try {
    $injectScript = "$PROJECT_ROOT\scripts\wheels\auto_recall.py"
    if (Test-Path $injectScript) {
        $injection = & pythonw $injectScript 2>&1
        if ($injection) {
            Write-Host ($injection -join "`n")
            $injLen = ($injection -join "`n").Length
            _diag "[SessionStart] 上下文注入: ${injLen} chars"
        } else {
            Write-Host "[CLS SessionStart] auto_recall 返回空 — 走兜底"
            _diag "[SessionStart] auto_recall 返回空"
        }
    } else {
        Write-Host "[CLS SessionStart] auto_recall.py 未找到 — 走兜底"
        _diag "[SessionStart] auto_recall.py 未找到: $injectScript"
    }
} catch {
    Write-Host "[CLS SessionStart] auto_recall 异常 — 走兜底: $_"
    _diag "[SessionStart] auto_recall 异常: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 21.3 knowledge_graph inject — 本地知识图谱注入 (@added 2026-07-25)
try {
    $kgScript = "$PROJECT_ROOT\scripts\wheels\knowledge_graph.py"
    if (Test-Path $kgScript) {
        $kgOut = & pythonw $kgScript inject 2>&1
        if ($kgOut) {
            Write-Host ($kgOut -join "`n")
            _diag "[SessionStart] knowledge_graph injected"
        }
    }
} catch { _diag "[SessionStart] knowledge_graph skipped: $_" }

# 21.3b cls_chronicle inject — 编年史注入 (@added 2026-07-28)
try {
    $chronicleScript = "$PROJECT_ROOT\scripts\wheels\cls_chronicle.py"
    if (Test-Path $chronicleScript) {
        $chronicleOut = & pythonw $chronicleScript inject 2>&1
        if ($chronicleOut) {
            Write-Host ($chronicleOut -join "`n")
            _diag "[SessionStart] chronicle injected"
        }
    }
} catch { _diag "[SessionStart] chronicle skipped: $_" }

# 21.4 cls_brain boot() — 脑区统一调度启动注入
try {
    $brainScript = "$PROJECT_ROOT\scripts\wheels\cls_brain.py"
    if (Test-Path $brainScript) {
        $bootOut = & pythonw $brainScript --boot 2>&1
        if ($bootOut) {
            Write-Host ($bootOut -join "`n")
            _diag "[SessionStart] brain.boot: injected"
        }
    }
} catch {
    _diag "[SessionStart] brain.boot skipped: $_"
}

# 21.5 文件监听 — 检测变更并增量重索引
# ═══════════════════════════════════════════════════════════════════
try {
    $watcherScript = "$PROJECT_ROOT\scripts\wheels\file_watcher.py"
    if (Test-Path $watcherScript) {
        $watcherOut = & pythonw $watcherScript --once 2>&1
        if ($watcherOut) {
            _diag "[SessionStart] 文件监听: $watcherOut"
        }
    }
} catch {
    _diag "[SessionStart] 文件监听跳过: $_"
}


# =============================================================================
# 21.6 状态新鲜度 (state_freshness — DS Pro方案)
# active_context/session_memory/cog_step过期 -> 注入告警 | 3次未修 -> deny
# =============================================================================
try {
    $freshScript = "$PROJECT_ROOT\scripts\wheels\state_freshness.py"
    if (Test-Path $freshScript) {
        $fout = & pythonw $freshScript 2>&1
        if ($fout) {
            Write-Host ($fout -join "`n")
            _diag "[SessionStart] freshness: stale items found"
        } else {
            _diag "[SessionStart] freshness: all OK"
        }
    }
} catch {
    _diag "[SessionStart] freshness skipped: $_"
}

# ═══════════════════════════════════════════════════════════════════
# 21.7 always_inject — CLAUDE.md P0规则自动提取注入 (@since 2026-07-31 二号融合)
# ═══════════════════════════════════════════════════════════════════
try {
    $alwaysScript = "$PROJECT_ROOT\scripts\wheels\always_injector.py"
    if (Test-Path $alwaysScript) {
        $alwaysOut = & pythonw $alwaysScript --status 2>&1
        if ($alwaysOut) {
            _diag "[SessionStart] always_inject: loaded"
        } else {
            # 首次运行: 提取
            $extractOut = & pythonw $alwaysScript 2>&1
            _diag "[SessionStart] always_inject: first extraction done"
        }
    }
} catch { _diag "[SessionStart] always_inject skipped: $_" }

# ═══════════════════════════════════════════════════════════════════
# 21.8 cognitive_gate warm-up (@since 2026-07-31 二号融合)
# ═══════════════════════════════════════════════════════════════════
try {
    $gateScript = "$PROJECT_ROOT\scripts\wheels\cognitive_gate.py"
    if (Test-Path $gateScript) {
        _diag "[SessionStart] cognitive_gate: ready (gate-on-use, no warm-up needed)"
    }
} catch { _diag "[SessionStart] cognitive_gate skipped: $_" }

# ═══════════════════════════════════════════════════════════════════
# 21.9 unified_inject — 知识卡片导航 (@since 2026-07-31 二号融合; 2026-08-19 二号接线 iter-036)
# ═══════════════════════════════════════════════════════════════════
# 当前模式: 仅当本窗口锚点明确(第⓪级 prompt_history + 三级链)时注入, 无锚点静默(宁缺毋错)。
# @fix 2026-09-04 双重死亡修复(maintainer实测"知识卡从未触发"): 本段在 async:true 钩子里,
#   Write-Host/systemMessage 均无人接收(人看不到/模型收不到)。
#   修复: $script:KnowledgeCardCtx 收集注入文本 → 脚本末尾(既有信封处)合并输出 additionalContext。
#   配套: settings.json 该钩子需去 async:true (同步执行 stdout 才被 CC 读取)。
$script:KnowledgeCardCtx = ""
try {
    $uiScript = "$PROJECT_ROOT\scripts\wheels\unified_inject.py"
    if (Test-Path $uiScript) {
        $uiOut = & pythonw $uiScript 2>&1
        if ($uiOut) {
            Write-Host ($uiOut -join "`n")
            # @fix 2026-09-04 第三重死亡: unified_inject stdout 混着 api_pipeline 彩色调试框
            # (200+ ANSI码)与四字段文本 — 全量塞 additionalContext 会污染/破坏 JSON。
            # 只提取【消息】起的四字段纯文本行。
            $cleanLines = @($uiOut | Where-Object { $_ -match '^【(消息|为什么|级别|内容)】|^(── |关联分析|^- 《)' })
            if ($cleanLines.Count -gt 0) {
                $script:KnowledgeCardCtx = ($cleanLines -join "`n")
            }
            _diag "[SessionStart] unified_inject: injected"
            # @since 2026-08-19: 注入卡片 CC 屏幕框图 (systemMessage 通道)
            $vizFile = "$PROJECT_ROOT\data\state\card_inject_viz.json"
            if (Test-Path $vizFile) {
                try {
                    $v = Get-Content $vizFile -Raw -Encoding UTF8 | ConvertFrom-Json
                    # api_pipeline 同款彩色框: Write-Host 分色 (绿框/蓝任务/黄卡/紫理由)
                    Write-Host "=============================================================" -ForegroundColor Green
                    Write-Host "  [CLS] 知识卡片注入  $($v.ts)" -ForegroundColor Green
                    Write-Host "=============================================================" -ForegroundColor Green
                    Write-Host "  当前任务: $($v.anchor)" -ForegroundColor Cyan
                    Write-Host "-------------------------------------------------------------" -ForegroundColor DarkGray
                    foreach ($c in $v.cards) {
                        Write-Host "  [$($c.id)] $($c.date)  $($c.title)" -ForegroundColor Yellow
                        Write-Host "        源: $($c.file)" -ForegroundColor DarkGray
                    }
                    foreach ($r in @($v.reasons)) {
                        if ($r) { Write-Host "  $r" -ForegroundColor Magenta }
                    }
                    Write-Host "=============================================================" -ForegroundColor Green
                    $box = "=========================================`n"
                    $box += "  [CLS] 知识卡片注入 ($($v.ts))`n"
                    $box += "  当前任务: $($v.anchor)`n"
                    $box += "-----------------------------------------`n"
                    foreach ($c in $v.cards) {
                        $box += "  [$($c.id)] $($c.date) $($c.title)`n"
                        $box += "  源: $($c.file)`n"
                    }
                    $box += "========================================="
                    Write-Output (@{ continue=$true; systemMessage=$box } | ConvertTo-Json -Compress)
                    _diag "[SessionStart] card viz box displayed"
                } catch { _diag ("[SessionStart] card viz failed: " + $_) }
                Remove-Item $vizFile -Force -ErrorAction SilentlyContinue
            }
        }
    }
} catch { _diag "[SessionStart] unified_inject skipped: $_" }

# ═══════════════════════════════════════════════════════════════════
# 21.10 CC原生工具速查 — DS V4无CC agent trajectory SFT导致召回率0
# ═══════════════════════════════════════════════════════════════════
try {
    $toolReminderScript = "$PROJECT_ROOT\scripts\wheels\cc_tool_reminder.py"
    if (Test-Path $toolReminderScript) {
        $toolReminder = & pythonw $toolReminderScript 2>&1
        if ($toolReminder) {
            $trOut = @{hookSpecificOutput=@{hookEventName="SessionStart";additionalContext=$toolReminder}} | ConvertTo-Json -Compress
            Write-Output $trOut
            _diag "[SessionStart] CC tool reminder injected"
        }
    }
} catch { _diag "[SessionStart] tool reminder skipped: $_" }

# ── CLS 会话恢复注入 (2026-07-30 二号, 2026-08-16 四字段合并版): CLS状态/建议 ──
try {
    $kgInfo = "无"
    if (Test-Path "$PROJECT_ROOT\data\state\_consolidator_state.json") {
        $cs = Get-Content "$PROJECT_ROOT\data\state\_consolidator_state.json" -Raw -Encoding UTF8 | ConvertFrom-Json
        $kgInfo = "$($cs.total_entities)实体"
    }
    $injInfo = "无"
    if (Test-Path "$PROJECT_ROOT\data\state\injection_log.jsonl") { $injInfo = "$((Get-Content "$PROJECT_ROOT\data\state\injection_log.jsonl").Count)次" }
    $autoInfo = "关闭"
    if (Test-Path "$PROJECT_ROOT\data\state\autonomy_state.json") { $autoInfo = "激活" }
    # 2026-08-16 四字段改造(张maintainer批准): 消息|为什么|级别|内容 — 原"[会话恢复] KG: x | 注入: y"缩写, 一无所知的AI看不懂
    # (自指句读回在独立 hook scripts/wheels/selfref_cc_read.py, iter-026 外部化, 此处不重复)
    $ctxText = "【消息】会话恢复(CLS): 会话启动时对本窗口累积状态的自动摘要。"
    $ctxText += "【为什么】压缩/重启后你可能不记得上个会话做到哪, 以下是本窗口状态。"
    $ctxText += "【级别】参考 — 不强制行动, 只做背景。"
    $ctxText += ("【内容】知识图谱: {0} | 全仓累计注入: {1} | 无人值守: {2} | 建议: 复杂任务走认知循环6步" -f $kgInfo, $injInfo, $autoInfo)
    # ── 压缩恢复合并 (2026-08-14 读端, 2026-08-15 扩展 extra): PreCompact 写入的恢复信息在此注入后删除 ──
    $recoveryFile = "$PROJECT_ROOT\data\state\precompact_recovery.json"
    if (Test-Path $recoveryFile) {
        $r = Get-Content $recoveryFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($r) {
            $ctxText = $ctxText + "`n" + "【消息】压缩恢复(CLS): 上下文压缩前的任务状态备份。" + "【为什么】压缩后可能丢失任务锚点。" + "【级别】参考 — 只做背景。" + ("【内容】压缩前任务: {0} | 压缩前状态: {1} | 建议: 检查锚点未偏离" -f $r.anchor, $r.ops)
            if ($r.extra) { $ctxText = $ctxText + "`n" + $r.extra }
        }
        Remove-Item $recoveryFile -Force -ErrorAction SilentlyContinue
        # ── longchain 工作目录提醒 (2026-08-20 大眼③借鉴): compact 后最需要想起工作目录 ──
        # 工作产物即笔记(文件=原文, 路径=便签, Read=调回), compact 只丢对话不丢文件
        try {
            $lcDir = Get-ChildItem "$PROJECT_ROOT\data\longchain" -Directory -ErrorAction SilentlyContinue |
                     Where-Object { $_.Name -notmatch '^_' } |
                     Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($lcDir -and ((Get-Date) - $lcDir.LastWriteTime).TotalHours -lt 72) {
                $ctxText = $ctxText + "`n" + ("【内容】最近工作目录: data/longchain/{0}/ (最后活动 {1:0.0}h 前)。中间结果/脚本/数据都在里面, 继续前先 ls 看一眼, 不要凭记忆重建。" -f $lcDir.Name, ((Get-Date) - $lcDir.LastWriteTime).TotalHours)
            }
        } catch { }
    }
    # ── 裁决拦截历史合并 (2026-08-16 融合: M1 §7 的 judge_log.jsonl 拦截历史, 并入四字段格式) ──
    try {
        $judgeLog = "$PROJECT_ROOT\data\symbolic_dynamics\judge_log.jsonl"
        if (Test-Path $judgeLog) {
            $lastLine = Get-Content $judgeLog -Tail 1 -ErrorAction SilentlyContinue
            if ($lastLine) {
                $lastVerdict = $lastLine | ConvertFrom-Json -ErrorAction SilentlyContinue
                if ($lastVerdict -and $lastVerdict.block -eq $true) {
                    $ageSpan = [DateTime]::UtcNow - [DateTime]::Parse($lastVerdict.ts)
                    if ($ageSpan.TotalHours -lt 24) {
                        $ctxText = $ctxText + "`n" + "【消息】裁决拦截历史(CLS): 上个会话最后一次被裁决器拦截的操作。"
                        $ctxText += "【为什么】AI 可能不记得上次什么动作被拦, 避免重复踩坑。"
                        $ctxText += "【级别】参考 — 不强制行动。"
                        $ctxText += ("【内容】工具: {0} | 域: {1} | 原因: {2}" -f $lastVerdict.tool, $lastVerdict.domain, $lastVerdict.reason)
                        _diag "[SessionStart] 注入拦截历史: tool=$($lastVerdict.tool) domain=$($lastVerdict.domain)"
                    }
                }
            }
        }
    } catch { _diag "[SessionStart] 拦截历史合并跳过: $_" }
    # ── 知识卡片合并进 additionalContext (@fix 2026-09-04 双重死亡修复) ──
    if ($script:KnowledgeCardCtx) {
        $ctxText = $ctxText + "`n" + "【消息】知识卡片导航(CLS): 后台发现knowledge中有与当前任务相关的工作记录, 自动推送。【级别】参考 — 相关就纳入思考, 不相关可忽略, 无需回应。" + "`n" + $script:KnowledgeCardCtx
        _diag "[SessionStart] 知识卡片并入additionalContext"
    }
    Write-Output (@{ continue=$true; hookSpecificOutput=@{ hookEventName="SessionStart"; additionalContext=$ctxText } } | ConvertTo-Json -Compress)
} catch {}

Write-Host "[CLS SessionStart] ready"  # v2: 缓存友好 — 固定字符串，详诊见 state/session_start_diag.log
exit 0
