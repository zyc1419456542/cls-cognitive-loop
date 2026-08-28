// cls-memory — CLS 知识导航插件 v0.3 (2026-08-17 maintainer建议2: 锚点改读状态文件, 弃人类消息记录器)
// v0.1(旧): kg_index 实体名匹配 — 被 iter-036 证伪。
// v0.2: 薄壳 spawn dsh_cls_nav.py nav, 锚点取自首条用户消息/人类记录器。
// v0.3(本版): 锚点不再取"人类消息"/记录器, 而是调 `dsh_cls_nav.py navstate` — 该轮子读状态文件
//           (cog_step带window_id / goal.txt带sid / active_context) 取锚点, 与 CC unified_inject 同源。
//           从根上消除"后台脉冲被当人类锚点" bug。首条/compaction/每10轮均走 navstate。
// 全部异步 fire-and-forget, 结果缓存到下一 pre-step 注入, 不阻塞模型循环。
import { randomUUID } from 'node:crypto'
import { execFile } from 'node:child_process'
import { appendFileSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const name = 'cls-memory'

const DEFAULTS = {
  enabled: true,
  pythonBin: 'E:/anaconda/python.exe',
  navScript: '<REPO_ROOT>/scripts/wheels/dsh_cls_nav.py',
  auditPath: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'memory_audit.jsonl'),
  rerunTurns: 10,
  spawnTimeoutMs: 100000,
}

function cfg(config) { return { ...DEFAULTS, ...(config || {}) } }
function audit(c, entry) { try { mkdirSync(dirname(c.auditPath), { recursive: true }); appendFileSync(c.auditPath, JSON.stringify(entry) + '\n', 'utf8') } catch {} }
function makeInjectedMessage(text) { return { id: randomUUID(), role: 'user', content: [{ type: 'text', text }], source: { kind: 'plugin', plugin: 'cls-memory' } } }

export function apply(ctx, config) {
  const c = cfg(config)
  const state = new Map() // agentId -> { pendingText, turns, running }

  const spawnNav = (agent) => {
    const st = state.get(agent.id)
    if (!c.enabled || !st || st.running) return
    st.running = true
    // v0.4: navstate 传 agent.id 作 session_id 去重 key — 多窗口各记各的, 不串不误杀
    execFile(
      c.pythonBin, [c.navScript, 'navstate', String(agent.id)],
      { encoding: 'utf8', windowsHide: true, timeout: c.spawnTimeoutMs, maxBuffer: 1024 * 1024 },
      (error, stdout) => {
        st.running = false
        const text = (stdout || '').trim()
        if (!error && text.length > 0) {
          st.pendingText = text
          audit(c, { ts: Date.now(), agent: agent.id, kind: 'nav_result', len: text.length })
        } else {
          audit(c, { ts: Date.now(), agent: agent.id, kind: 'nav_skip_or_fail', err: String(error?.message || 'empty').slice(0, 160) })
        }
      },
    )
  }

  ctx.root.on('agent/pre-step', async ({ agent, messages, turn }, next) => {
    const decision = await next()
    if (!c.enabled || decision.kind === 'reject') return decision
    const id = agent.id
    let st = state.get(id)
    if (!st) {
      st = { pendingText: '', turns: 0, running: false }
      state.set(id, st)
      spawnNav(agent) // 会话首次
      return decision
    }
    if (st.pendingText) {
      const text = st.pendingText
      st.pendingText = ''
      return { kind: 'enter', messages: [...decision.messages, makeInjectedMessage(text)] }
    }
    st.turns += 1
    if (st.turns % c.rerunTurns === 0) spawnNav(agent)
    return decision
  }, 'cls-memory.pre-step')

  ctx.root.on('session/event', (session, event) => {
    if (!c.enabled || event?.type !== 'compaction/end') return
    const st = state.get(session?.id)
    if (st) spawnNav({ id: session.id })
  }, 'cls-memory.compaction')

  ctx.root.on('agent/disposed', ({ agent }) => { state.delete(agent.id) })
}
