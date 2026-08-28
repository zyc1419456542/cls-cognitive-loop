// cls-gaze — 内容凝视插件 v0.1 (CLS 符号动力学 B2 降级实现)
// 挂点: tools/result(统计) + agent/pre-step(趋势告警)
// v0.1 统计启发式: 连续 3 次极短产出(<200 字符) → 注入告警
// 外部模型三维评分(信息密度/策略变化/逻辑自洽)留待 B3 统一接线
import { randomUUID } from 'node:crypto'
import { appendFileSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const name = 'cls-gaze'

const DEFAULTS = {
  enabled: true,
  auditPath: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'gaze_audit.jsonl'),
  shortThreshold: 200,  // 极短输出阈值(字符)
  shortStreak: 3,       // 连续极短次数触发告警
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

/** 产出长度: 优先 write/edit 的 content 参数(write 结果 value 不含内容)。 */
function resultTextLen(exec, result) {
  try {
    const args = exec.arguments || {}
    if (typeof args.content === 'string' && args.content.length > 0) return args.content.length
    if (typeof args.new_string === 'string' && args.new_string.length > 0) return args.new_string.length
    const content = result?.value?.content || result?.content || []
    if (typeof content === 'string') return content.length
    if (Array.isArray(content)) {
      return content.reduce((sum, b) => {
        if (b && typeof b.text === 'string') return sum + b.text.length
        if (b && typeof b.content === 'string') return sum + b.content.length
        return sum
      }, 0)
    }
    return 0
  } catch (e) {
    return 0
  }
}

function makeMsg(text) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'plugin', plugin: 'cls-gaze' },
  }
}

export function apply(ctx, config) {
  const c = cfg(config)
  const state = new Map() // agentId -> { shortStreak, warnedTurn }

  // 统计: write/edit 成功产出长度
  ctx.root.on('tools/result', (exec, result) => {
    if (!c.enabled) return
    if (exec.name !== 'write' && exec.name !== 'edit') return
    if (result.isError) return
    const id = exec.agent?.id
    if (!id) return
    const len = resultTextLen(exec, result)
    let st = state.get(id)
    if (!st) {
      st = { shortStreak: 0, warnedTurn: -1 }
      state.set(id, st)
    }
    const isShort = len < c.shortThreshold
    st.shortStreak = isShort ? st.shortStreak + 1 : 0
    audit(c, { ts: Date.now(), agent: id, kind: 'write_stat', len, short: isShort, streak: st.shortStreak })
  }, 'cls-gaze.stat')

  // 趋势告警: 连续极短 → pre-step 注入
  ctx.root.on('agent/pre-step', async ({ agent, messages, turn }, next) => {
    const decision = await next()
    if (!c.enabled || decision.kind === 'reject') return decision
    const id = agent.id
    const st = state.get(id)
    if (!st) return decision
    if (st.shortStreak >= c.shortStreak && st.warnedTurn !== turn) {
      st.warnedTurn = turn
      const text = `[cls-gaze] ⚠️ 内容凝视: 连续 ${st.shortStreak} 次产出过短(<${c.shortThreshold} 字符)。自查: 是否在偷懒/敷衍? 若有合理理由(命令类操作)可忽略。`
      audit(c, { ts: Date.now(), agent: id, turn, kind: 'short_streak_alert' })
      return { kind: 'enter', messages: [...decision.messages, makeMsg(text)] }
    }
    return decision
  }, 'cls-gaze.pre-step')

  ctx.root.on('agent/disposed', ({ agent }) => {
    state.delete(agent.id)
  })
}
