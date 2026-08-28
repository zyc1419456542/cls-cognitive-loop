// cls-symbolic — 符号动力学接线插件 v0.1 (CLS 三大基础功能补全 B)
// 挂点: agent/pre-step
// 功能:
//   1. 禁用词闸门(只读共享 CC forbidden_words.json): p0 命中 → 强警告注入; p1 命中 → 提示注入
//   2. L0 路由(简化 4 类正则): 推理/编码/审查/摘要 → 注入 tier 标签(每 turn 一次, 低频)
//   3. 审计: <DSH_HOME>\data\symbolic_audit.jsonl
// 原则: CC 词表只读; harness 可挂 mcp__symbolic-dynamics__* 工具给模型做深度裁决
import { randomUUID } from 'node:crypto'
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const name = 'cls-symbolic'

const DEFAULTS = {
  enabled: true,
  forbiddenPath: '<REPO_ROOT>/data/symbolic_dynamics/forbidden_words.json',
  auditPath: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'symbolic_audit.jsonl'),
  refreshMs: 60 * 1000, // 词表缓存刷新
}

// L0 路由简化版(CC 的 12 正则精简为 4 类)
const L0_RULES = [
  { tier: 'L4-推理', re: /(推导|分析|论证|设计|方案|比较|综合|原理|为什么|证明)/, label: '深度推理任务: 结论前置, 分步验证' },
  { tier: 'L3-编码', re: /(写|改|修|实现|重构|代码|脚本|函数|bug|报错|编译)/, label: '编码任务: 先读现状, 改完自测' },
  { tier: 'L2-审查', re: /(检查|审查|审计|review|核对|验证|巡检)/, label: '审查任务: 独立视角, 逐项核对' },
  { tier: 'L0-摘要', re: /(总结|摘要|概括|提炼|简述)/, label: '摘要任务: 直接输出要点' },
]

let cache = { data: null, at: 0 }

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

function loadForbidden(c) {
  const now = Date.now()
  if (cache.data && now - cache.at < c.refreshMs) return cache.data
  try {
    if (!existsSync(c.forbiddenPath)) return null
    const data = JSON.parse(readFileSync(c.forbiddenPath, 'utf8'))
    cache = { data, at: now }
    return data
  } catch (e) {
    return null
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
    source: { kind: 'plugin', plugin: 'cls-symbolic' },
  }
}

export function apply(ctx, config) {
  const c = cfg(config)
  const state = new Map() // agentId -> { l0Done }

  ctx.root.on('agent/pre-step', async ({ agent, messages, turn }, next) => {
    const decision = await next()
    if (!c.enabled || decision.kind === 'reject') return decision
    const text = extractText(messages).trim()
    if (!text) return decision

    const id = agent.id
    let st = state.get(id)
    if (!st) {
      st = { l0Done: false }
      state.set(id, st)
    }
    const injections = []

    // 1) 禁用词闸门(只读 CC 词表)
    const fb = loadForbidden(c)
    if (fb) {
      const p0 = fb.p0_patterns || []
      const p1 = fb.p1_patterns || []
      let hitP0 = null
      for (const pat of p0) {
        try {
          if (new RegExp(pat).test(text)) { hitP0 = pat; break }
        } catch (e) { /* 非法正则跳过 */ }
      }
      if (hitP0) {
        injections.push('[cls-symbolic] 🔴 命中符号动力学 p0 禁用词(硬化层红线)。停止当前方向, 说明理由后由人工裁决。')
        audit(c, { ts: Date.now(), agent: id, turn, kind: 'p0_hit' })
      } else {
        for (const pat of p1) {
          try {
            if (new RegExp(pat).test(text)) {
              injections.push('[cls-symbolic] ⚠️ 命中符号动力学 p1 提示词。谨慎继续, 建议自查是否符合约束。')
              audit(c, { ts: Date.now(), agent: id, turn, kind: 'p1_hit' })
              break
            }
          } catch (e) { /* ignore */ }
        }
      }
    }

    // 2) L0 路由(每会话首次用户消息注入一次 tier 标签)
    if (!st.l0Done && text.length >= 4) {
      for (const rule of L0_RULES) {
        if (rule.re.test(text)) {
          st.l0Done = true
          injections.push(`[cls-symbolic] L0 路由: ${rule.tier} — ${rule.label}`)
          audit(c, { ts: Date.now(), agent: id, turn, kind: 'l0', tier: rule.tier })
          break
        }
      }
    }

    if (injections.length === 0) return decision
    return { kind: 'enter', messages: [...decision.messages, ...injections.map(makeMsg)] }
  }, 'cls-symbolic.pre-step')

  ctx.root.on('agent/disposed', ({ agent }) => {
    state.delete(agent.id)
  })
}
