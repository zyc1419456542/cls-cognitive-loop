// cls-gate — CLS 工具硬闸插件 v0.3 (2026-08-27 maintainer批: UAC cog声明 + 符号观测 + 会诊入口)
// v0.1: 危险命令 deny / API Key 泄露 deny / 失败循环 deny / 应急旁路。
// v0.2: 修复循环提醒 — ①同一 Edit/Write 目标文件 edit≥3 次但未收敛 ②同命令失败连续≥3 次。
// v0.3 (2026-08-27, dsh 定位升格为与 CC 同级重活工具):
//        ① UAC 写前声明 (对齐 CC PreToolUse CHECK 15): write/edit 前校验共享 cog_step.json
//           (<REPO_ROOT>/data/state/cog_step.json, TTL 300s, 与 CC 同文件同协议),
//           过期/缺失 → deny + 指引 `dsh_cls_nav.py declare <phase> <label> <desc>` (复用 CC 函数)。
//           豁免: temp/ data/state/ .zcode/ cog_step.json 自身 (与 CC CHECK 15 豁免同源)。
//        ② 符号观测 (对齐 CC ops_monitor, dsh 本地日志防跨侧污染): tools/result 分类记账
//           explore/mutate/exec(+bash变异识别) → <DSH_HOME>/data/ops_freq.jsonl;
//           每 agent 滑窗12条: 同文件 mutate≥3 或 mutate→验证(Read/跑测试)对≥2 → 卡住提醒
//           (每 agent 每型一次, 对齐 CC skirmish v2 强制换策略文案)。
//        ③ 会诊入口: 修复循环/卡住提醒文案附 consult 命令 (复用 CC consult 纯函数, 写同通行证)。
// 挂点: tools/pre-execute(deny) + tools/result(计数/观测/提醒) + agent/pre-step(注入)。
import { randomUUID } from 'node:crypto'
import { appendFileSync, existsSync, mkdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const name = 'cls-gate'

const DEFAULTS = {
  enabled: true,
  auditPath: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'gate_audit.jsonl'),
  bypassFlag: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'emergency_bypass.flag'),
  bypassTtlMs: 30 * 60 * 1000,
  loopWindowMs: 5 * 60 * 1000,
  loopThreshold: 5,
  loopCooldownMs: 5 * 60 * 1000,
  editTargetCount: 3,
  cmdFailStreak: 3,
  // v0.3
  cogStepPath: '<REPO_ROOT>/data/state/cog_step.json',
  declareCmd: 'python <REPO_ROOT>/scripts/wheels/dsh_cls_nav.py declare',
  consultCmd: 'python <REPO_ROOT>/scripts/wheels/dsh_cls_nav.py consult',
  opsLogPath: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'ops_freq.jsonl'),
  stuckEditCount: 3,     // 同文件 mutate 次数 → 卡住
  stuckPairCount: 2,     // mutate→验证 对数 → 卡住前兆
}

const DANGEROUS = [
  { re: /\brm\s+(-[a-z]*r[a-z]*f?|[a-z]*f[r]?)\s+([\/\\~]|[A-Za-z]:[\\\/])(\s|$)/, reason: '危险删除: 系统根/家目录/盘符根' },
  { re: /\bRemove-Item\b[^\n]*\b(Recurse|Force)\b[^\n]*[A-Za-z]:[\\\/](\s|$)/i, reason: '危险删除: 盘符根' },
  { re: /\bformat\s+[A-Za-z]:/, reason: '格式化磁盘' },
  { re: /\bmkfs(\.\w+)?\b/, reason: '创建文件系统' },
  { re: /\bdd\b[^\n]*\bof=\/dev\//, reason: '写入裸设备' },
  { re: /Set-MpPreference\b[^\n]*(DisableRealtimeMonitoring|DisableBehaviorMonitoring)/i, reason: '关闭 Defender 防护' },
  { re: /\b(Remove-Item|rmdir|del|rd)\s+[^\n]*(Windows\\System32|\\System32)/i, reason: '修改系统目录' },
  { re: /\bdiskpart\b/, reason: '磁盘分区操作' },
]

const KEY_RE = /\bsk-[A-Za-z0-9_\-]{20,}\b/

const PROBE_PREFIXES = [
  'get-process', 'get-content', 'test-path', 'git status', 'git log', 'git diff', 'git -c status',
  'echo', 'date', 'dir', 'ls ', 'pwd', 'whoami', 'node --version', 'python --version', 'npm --version',
  'get-childitem', 'get-location', 'get-date', 'select-string', 'measure-object',
]

// UAC 豁免路径 (与 CC CHECK 15 同源 + dsh 特例): 声明文件自身/临时/状态目录/dsh内部记账不拦
const UAC_EXEMPT_RE = /[\/\\](temp|\.zcode|\.claude[\/\\]cls_state)[\/\\]|cog_step\.json$|E:[\/\\]dsh_home[\/\\]data[\/\\]/

// 符号观测: 工具分类 (对齐 CC ops_monitor TOOL_CATEGORY + bash 变异召回补丁)
const CAT_MUTATE_TOOLS = new Set(['write', 'edit', 'str_replace_editor'])
const CAT_EXPLORE_TOOLS = new Set(['read', 'glob', 'grep', 'web_search', 'web_fetch'])
const BASH_MUTATE_RE = /\bsed\b[^|;&]*\s-i|(?<![0-2>])>{1,2}\s*[^\s|>&]|\btee\b|\bSet-Content\b|\bAdd-Content\b|\bOut-File\b/

const failCounts = new Map()
const editCounts = new Map()
const warnedEdit = new Set()
const pendingReminder = new Map()
// v0.3 符号观测状态: agentId -> { ops: [{category,tool,cmd,file}], stuckFile: bool, stuckPair: bool }
const obsState = new Map()

function cfg(config) { return { ...DEFAULTS, ...(config || {}) } }
function audit(c, entry) { try { mkdirSync(dirname(c.auditPath), { recursive: true }); appendFileSync(c.auditPath, JSON.stringify(entry) + '\n', 'utf8') } catch {} }
function isBypassed(c) { try { if (!existsSync(c.bypassFlag)) return false; const st = statSync(c.bypassFlag); return Date.now() - st.mtimeMs < c.bypassTtlMs } catch { return false } }
function fingerprint(cmd) { if (typeof cmd !== 'string' || !cmd.trim()) return null; return cmd.trim().replace(/\s+/g, ' ').toLowerCase().slice(0, 120) }
function isProbe(fp) { return PROBE_PREFIXES.some((p) => fp.startsWith(p)) }
function getCommandText(exec) { const args = exec.arguments || {}; return typeof args.command === 'string' ? args.command : '' }
function getFilePath(exec) { const args = exec.arguments || {}; return String(args.file_path || args.path || '') }
function basename(p) { if (!p) return ''; const m = String(p).split(/[\\/]/); return m[m.length - 1] || '' }

// ── v0.3 UAC: cog_step.json 新鲜度 (与 CC CHECK 15 同判据: _meta.written_at + ttl_seconds) ──
function cogStepFresh(c) {
  try {
    if (!existsSync(c.cogStepPath)) return { ok: false, why: 'missing' }
    const d = JSON.parse(readFileSync(c.cogStepPath, 'utf8'))
    const written = d?._meta?.written_at
    const ttl = Number(d?.ttl_seconds) || 300
    if (!Number.isFinite(written)) return { ok: false, why: 'corrupt' }
    const age = (Date.now() / 1000) - written
    if (age < 0 || age > ttl) return { ok: false, why: 'expired' }
    return { ok: true, label: d.label }
  } catch { return { ok: false, why: 'corrupt' } }
}

// ── v0.3 符号观测: 单条 op 分类 + 文件线索 ──
function classifyOp(name, exec) {
  const cmd = getCommandText(exec)
  if (CAT_MUTATE_TOOLS.has(name)) return { category: 'mutate', file: basename(getFilePath(exec)), cmd: '' }
  if (CAT_EXPLORE_TOOLS.has(name)) return { category: 'explore', file: '', cmd: '' }
  if ((name === 'bash' || name === 'pwsh') && BASH_MUTATE_RE.test(cmd.slice(0, 400))) {
    const m = cmd.match(/(?:\bsed\b[^|;&]*?|\btee\s+\S+\s+|(?<![\w-])(?:mv|cp)\s+(?:-\w+\s+)*\S+\s+)(\S+\.\w+)/) || cmd.match(/(?<![0-2>])>{1,2}\s*([^\s|;&]+)/)
    return { category: 'mutate', file: basename(m ? m[1] : ''), cmd: cmd.slice(0, 100) }
  }
  if (name === 'bash' || name === 'pwsh' || name === 'run_code') return { category: 'exec', file: '', cmd: cmd.slice(0, 100) }
  return { category: 'other', file: '', cmd: '' }
}
function isVerifyOp(op, prevMutatedFile) {
  if (op.category === 'explore' && op.tool === 'read') return true
  if (op.category === 'exec' && /\bpython[\w.]*\b|\bpytest\b|\bnpm\s+test\b/.test(op.cmd)) {
    if (/test|pytest/.test(op.cmd.toLowerCase())) return true
    if (prevMutatedFile && op.cmd.includes(prevMutatedFile)) return true
  }
  return false
}

function remind(agentId, text) { pendingReminder.set(agentId, text) }

export function apply(ctx, config) {
  const c = cfg(config)

  ctx.root.on('tools/pre-execute', async (exec, next) => {
    const decision = await next()
    if (!c.enabled || decision.kind === 'deny') return decision
    if (isBypassed(c)) return decision
    const name = exec.name
    const args = exec.arguments || {}
    const text = JSON.stringify(args)

    // ── v0.3 UAC: write/edit 前须有新鲜 cog 声明 (与 CC 共用 cog_step.json) ──
    if (CAT_MUTATE_TOOLS.has(name)) {
      const path = getFilePath(exec)
      if (path && !UAC_EXEMPT_RE.test(path.replace(/\\/g, '/'))) {
        const cs = cogStepFresh(c)
        if (!cs.ok) {
          audit(c, { ts: Date.now(), kind: 'uac_deny', tool: name, path, why: cs.why })
          return {
            kind: 'deny',
            reason: `[cls-gate UAC] 写入前须声明认知步骤(当前声明${cs.why === 'missing' ? '缺失' : cs.why === 'expired' ? '已过期(>300s)' : '不可读'})。运行: ${c.declareCmd} <phase 1-6> <label> <一句话描述> 然后重试本写入。豁免: temp/ data/state/。与 CC CHECK 15 同协议同文件。`,
          }
        }
      }
    }

    if (name === 'bash' || name === 'pwsh' || name === 'run_code') {
      const cmd = getCommandText(exec)
      for (const { re, reason } of DANGEROUS) { if (re.test(cmd)) { audit(c, { ts: Date.now(), kind: 'danger', tool: name, reason, cmd: cmd.slice(0, 200) }); return { kind: 'deny', reason: `[cls-gate] ${reason}, 已拦截` } } }
    }
    if (CAT_MUTATE_TOOLS.has(name)) {
      const path = getFilePath(exec)
      if (!/[/\\](keys|\.git|persona|state)[/\\]/.test(path)) {
        const m = text.match(KEY_RE)
        if (m) { audit(c, { ts: Date.now(), kind: 'key_leak', tool: name, path }); return { kind: 'deny', reason: '[cls-gate] 检测到疑似 API Key 明文写入, 已拦截。请改走环境变量/凭据文件, 并轮换泄露的 Key' } }
      }
    }
    if (name === 'bash' || name === 'pwsh') {
      const cmd = getCommandText(exec)
      const fp = fingerprint(cmd)
      if (fp && !isProbe(fp) && (failCounts.get(fp)?.blockedUntil && Date.now() < failCounts.get(fp).blockedUntil)) {
        audit(c, { ts: Date.now(), kind: 'loop_deny', tool: name, fingerprint: fp })
        return { kind: 'deny', reason: '[cls-gate] 检测到失败循环(同命令 5 分钟内失败 5 次), 冷却 5 分钟。请先查明失败原因再重试' }
      }
    }
    return decision
  }, 'cls-gate.pre-execute')

  ctx.root.on('tools/result', (exec, result) => {
    if (!c.enabled || !result) return
    const name = exec.name
    const agentId = exec.agent?.id

    // ── v0.3 符号观测: 分类记账 + 卡住检测 (dsh 本地日志, 不污染 CC ops_freq) ──
    if (agentId) {
      try {
        const op = { ...classifyOp(name, exec), tool: name, ts: Date.now() }
        appendFileSync(c.opsLogPath, JSON.stringify({ ts: Date.now(), agent: agentId, tool: name, category: op.category, file: op.file || undefined }) + '\n', 'utf8')
        let st = obsState.get(agentId)
        if (!st) { st = { ops: [], stuckFile: false, stuckPair: false }; obsState.set(agentId, st) }
        const prev = st.ops[st.ops.length - 1]
        st.ops.push(op)
        if (st.ops.length > 12) st.ops.shift()
        // ① 同文件 mutate ≥3 (含 bash 变异)
        if (!st.stuckFile) {
          const cnt = {}
          for (const o of st.ops) if (o.category === 'mutate' && o.file) cnt[o.file] = (cnt[o.file] || 0) + 1
          const worst = Object.entries(cnt).find(([, n]) => n >= c.stuckEditCount)
          if (worst) {
            st.stuckFile = true
            audit(c, { ts: Date.now(), kind: 'stuck_file', agent: agentId, file: worst[0], count: worst[1] })
            remind(agentId, `【消息】卡住模式(CLS): 文件「${worst[0].slice(0, 50)}」已被修改 ${worst[1]} 次仍未通过。\n【为什么】同一目标重复尝试无进展 = 卡住, 继续莽不会收敛。\n【级别】强制 — 停止继续改代码, 先执行: ①列≥2个根因假设 ②每假设写验证方法 ③挑最便宜先验。验证通过才再改。\n【内容】若已卡住3轮以上: ${c.consultCmd} "①已试过什么 ②为何失败 ③根因假设 ④为何这次会不同" 获取复核讨论与通行证。`)
          }
        }
        // ② mutate→验证 摇摆 ≥2 对 (验证=Read 或 跑测试/运行被改文件)
        if (!st.stuckPair && prev && prev.category === 'mutate') {
          const pairs = st.ops.slice(0, -1).filter((o, i) => o.category === 'mutate' && isVerifyOp(st.ops[i + 1], o.file)).length
          if (pairs >= c.stuckPairCount) {
            st.stuckPair = true
            audit(c, { ts: Date.now(), kind: 'stuck_pair', agent: agentId, pairs })
            remind(agentId, `【消息】卡住前兆(CLS): ${pairs} 轮 改→验证 摇摆, 再恶化将要求会诊。\n【为什么】写后立即验证循环 = 对修改不确定, 摇摆升级即修复循环。\n【级别】参考 — 本会话仅提示此一次。换策略: 列根因假设清单再动手。`)
          }
        }
      } catch { /* 观测失败不阻塞 */ }
    }

    if (result.isError && (name === 'bash' || name === 'pwsh')) {
      const fp = fingerprint(getCommandText(exec))
      if (fp && !isProbe(fp)) {
        const now = Date.now()
        let rec = failCounts.get(fp)
        if (!rec || now - rec.firstTs > c.loopWindowMs) { rec = { count: 1, firstTs: now, blockedUntil: 0, streak: 1, warned: false }; failCounts.set(fp, rec) }
        else {
          rec.count += 1; rec.streak += 1
          if (rec.count >= c.loopThreshold && !rec.blockedUntil) rec.blockedUntil = now + c.loopCooldownMs
          if (rec.streak >= c.cmdFailStreak && !rec.warned && agentId) {
            rec.warned = true
            const msg = `【消息】修复循环警告(CLS): 同命令连续失败 ${rec.streak} 次仍未收敛。\n【为什么】连续修复>${rec.streak}轮不会收敛(incident-log#17)。\n【级别】强制 — 先 Read/搜索确认根因, 换策略, 别原地改。\n【内容】命令: ${fp.slice(0, 60)}。若再不收敛: ${c.consultCmd} "①已试过什么 ②为何失败 ③根因假设 ④为何这次会不同"`
            remind(agentId, msg)
            audit(c, { ts: Date.now(), kind: 'fix_loop_reminder', fp: fp.slice(0, 80), streak: rec.streak })
          }
        }
      }
    }

    if (CAT_MUTATE_TOOLS.has(name) && agentId && !warnedEdit.has(getFilePath(exec))) {
      const p = getFilePath(exec)
      if (p) { const cnt = (editCounts.get(p) || 0) + 1; editCounts.set(p, cnt); if (cnt >= c.editTargetCount) { warnedEdit.add(p); remind(agentId, `【消息】修复循环警告(CLS): 文件「${p.slice(0, 60)}」已被 Edit ${cnt} 次仍未收敛。\n【为什么】连续修复>${c.editTargetCount}轮不会收敛(incident-log#17)。\n【级别】强制 — 先 Read/搜索确认根因, 换策略, 别原地改。\n【内容】会诊通道: ${c.consultCmd} "①已试过什么 ②为何失败 ③根因假设 ④为何这次会不同"`); audit(c, { ts: Date.now(), kind: 'fix_loop_edit', path: p, count: cnt }) } }
    }
  }, 'cls-gate.result')

  ctx.root.on('agent/pre-step', async ({ agent, messages, turn }, next) => {
    const decision = await next()
    if (!c.enabled || decision.kind === 'reject') return decision
    const txt = pendingReminder.get(agent.id)
    if (!txt) return decision
    pendingReminder.delete(agent.id)
    return { kind: 'enter', messages: [...decision.messages, { id: randomUUID(), role: 'user', content: [{ type: 'text', text: txt }], source: { kind: 'plugin', plugin: 'cls-gate' } }] }
  }, 'cls-gate.pre-step')

  ctx.root.on('agent/disposed', ({ agent }) => { obsState.delete(agent.id) }, 'cls-gate.disposed')
}
