// cls-cycle — 认知核心循环接线插件 v0.1 (CLS 三大基础功能补全 A)
// 挂点: agent/pre-step
// 功能:
//   1. cog_step 态势感知(只读共享 CC 状态): 缺失/过期(>300s) → 注入 ANCHOR 步骤声明提醒
//   2. 任务收尾提醒: 消息含收尾信号 → 注入 双轨进度 + 经验捕获(/capture 流程)提醒
//   3. 审计: <DSH_HOME>\data\cycle_audit.jsonl
// 原则: CC 状态只读(不写 cog_step), harness 写自己的审计
import { randomUUID } from 'node:crypto'
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const name = 'cls-cycle'

export const inject = ['agents']

const DEFAULTS = {
  enabled: true,
  cogStepPath: '<REPO_ROOT>/data/state/cog_step.json',
  ttlMs: 300 * 1000,           // cog_step TTL(与 CC CHECK 15 一致)
  auditPath: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'cycle_audit.jsonl'),
  wrapSignals: ['完成', '交付', '收尾', '搞定', 'done', '总结', '汇报'],
}

function cfg(config) {
  return { ...DEFAULTS, ...(config || {}) }
}

function audit(c, entry) {
  try {
    mkdirSync(dirname(c.auditPath), { recursive: true })
    appendFileSync(c.auditPath, JSON.stringify(entry) + '\n', 'utf8')
  } catch (e) {
    // ignore
  }
}

/** 读 CC cog_step(只读): 返回 {ok, phase, label, ageMs}
 *  B4 修复: CC 侧 cog_step.json 带 UTF-8 BOM, 需 strip 后 JSON.parse */
function readCogStep(c) {
  try {
    if (!existsSync(c.cogStepPath)) return { ok: false, missing: true }
    const raw = readFileSync(c.cogStepPath, 'utf8').replace(/^\uFEFF/, '')
    const data = JSON.parse(raw)
    const declared = Date.parse(data.declared_at)
    if (Number.isNaN(declared)) return { ok: false, missing: true }
    return { ok: true, phase: data.phase, label: data.label, ageMs: Date.now() - declared }
  } catch (e) {
    return { ok: false, missing: true }
  }
}

function extractText(messages) {
  return messages
    .flatMap((m) => (m.content || []).filter((b) => b.type === 'text').map((b) => b.text))
    .join('\n')
}

function makeMsg(text) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'plugin', plugin: 'cls-cycle' },
  }
}

export function apply(ctx, config) {
  const c = cfg(config)
  const injected = new Map() // agentId -> { stepWarned, wrapWarned }

  // C2 硬闸(maintainer裁决 2026-08-15): 写操作前 cog_step 校验, 对齐 CC CHECK 15
  // 缺失 → deny; 过期 → 注入提醒(不 deny, 防 CC 不活跃时 harness 完全卡死)
  // 豁免: cog_step 自身 / <DSH_HOME> / temp / data\state / 交付目录之外的临时区
  ctx.root.on('tools/pre-execute', async (exec, next) => {
    const decision = await next()
    if (!c.enabled || decision.kind === 'deny') return decision
    const name = exec.name
    if (name !== 'write' && name !== 'edit' && name !== 'str_replace_editor') return decision
    const args = exec.arguments || {}
    const path = String(args.file_path || args.path || '')
    const lower = path.toLowerCase()
    if (
      lower.includes('cog_step.json') ||
      lower.includes('e:/dsh_home') ||
      lower.includes('/temp') ||
      lower.includes('\\temp') ||
      lower.includes('data/state') ||
      lower.includes('data\\state')
    ) {
      return decision // 豁免
    }
    const cog = readCogStep(c)
    if (cog.missing) {
      audit(c, { ts: Date.now(), agent: exec.agent?.id, kind: 'gate_deny_write', tool: name, path })
      return { kind: 'deny', reason: '[cls-cycle] 🔴 硬闸: CC 侧 cog_step.json 缺失。写操作前请先声明认知步骤(回复首行 ANCHOR: <步骤标签>), 或在 CC 侧补声明。' }
    }
    return decision
  }, 'cls-cycle.gate')

  ctx.root.on('agent/pre-step', async ({ agent, messages, turn }, next) => {
    const decision = await next()
    if (!c.enabled || decision.kind === 'reject') return decision
    const text = extractText(messages).trim()
    if (!text) return decision

    const id = agent.id
    let st = injected.get(id)
    if (!st) {
      st = { stepWarned: false, wrapWarned: false }
      injected.set(id, st)
    }
    const injections = []

    // 1) cog_step 态势感知(只读 CC 状态)
    const cog = readCogStep(c)
    if (cog.missing) {
      if (!st.stepWarned) {
        st.stepWarned = true
        injections.push('[cls-cycle] ⚠️ CC 侧 cog_step.json 缺失。写操作前请声明认知步骤(回复首行 ANCHOR: <步骤标签>), 保证任务执行可回溯。')
        audit(c, { ts: Date.now(), agent: id, turn, kind: 'cog_step_missing' })
      }
    } else if (cog.ageMs > c.ttlMs) {
      if (!st.stepWarned) {
        st.stepWarned = true
        injections.push(`[cls-cycle] ⚠️ CC 侧认知步骤声明已过期(${Math.round(cog.ageMs / 1000)}s):「${cog.label}」。继续前请重新声明 ANCHOR。`)
        audit(c, { ts: Date.now(), agent: id, turn, kind: 'cog_step_expired', label: cog.label })
      }
    }

    // 2) 任务收尾提醒
    if (!st.wrapWarned && c.wrapSignals.some((s) => text.includes(s))) {
      st.wrapWarned = true
      injections.push('[cls-cycle] 📋 任务收尾三件事: ①双轨进度(/progress 或 progress_file_filer.py) ②经验捕获(mcp__learn-mcp__learn_capture) ③交付区归档(assistant交付/)。')
      audit(c, { ts: Date.now(), agent: id, turn, kind: 'wrap_reminder' })
    }

    if (injections.length === 0) return decision
    return { kind: 'enter', messages: [...decision.messages, ...injections.map(makeMsg)] }
  }, 'cls-cycle.pre-step')

  ctx.root.on('agent/disposed', ({ agent }) => {
    injected.delete(agent.id)
  })
}
