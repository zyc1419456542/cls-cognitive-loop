// cls-inject — CLS 认知注入插件 v0.3 (2026-08-27 三bug修复, maintainer批准)
// v0.1(旧): 锚点 bigram Jaccard 漂移 + L4 关键词正则, 旧文案。
// v0.2: ① L0 判级 → 四字段注入【消息/为什么/级别/内容】(对齐 iter-036 CC 语义路由);
//       ② 深度任务(判级 L4/L3) → 强制锚【结论前置, 分步推理...】(方案B: 完成需首行 ANCHOR 回执);
//       ③ 漂移检测保留(词面话题标签), 文案改四字段 参考级。
//       ④ 复用 CC knowledge_inject_audit 统一审计(source 前缀 dsh-inject)。
// v0.3 (2026-08-27 实测76条漂移全误报, 根治):
//       ① 锚点污染 — v0.2 拿首轮全部消息前120字当任务锚, 而 dsh 把 AGENTS.md 人格内容
//          作为 user 角色消息注入 inbox(实测 session dump: 首条 user/message 即"身份锚…"),
//          → 锚点=人格文本, 工作内容 vs 人格锚 sim 恒 0.01-0.04, 永远误报"话题漂移"。
//          修复: 锚点与漂移比较都只取最后一条"真实用户消息"(排除 plugin 注入与
//          身份锚/系统前缀 chrome)。
//       ② 去重失效 — `driftInjected !== turn` 实为"每 turn 都可再注", 叠加①=每轮必弹。
//          修复: 漂移注入全会话最多 1 次(对齐注入三原则: 无增量不注入; CD清点同教训)。
//       ③ 防自噬 — 注入以 user 角色回灌后进下轮比较文本; 修复后按 source.kind=plugin 排除。
// 触发: agent/pre-step; 全部本地正则零延迟(不阻塞模型循环), 深度任务强制锚每 turn 最多 1 次。
import { randomUUID } from 'node:crypto'
import { execFile } from 'node:child_process'
import { appendFileSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const name = 'cls-inject'

const DEFAULTS = {
  enabled: true,
  pythonBin: 'E:/anaconda/python.exe',
  auditScript: '<REPO_ROOT>/scripts/wheels/dsh_cls_nav.py',
  driftThreshold: 0.12,
  maxInjectPerTurn: 2,
  auditPath: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'injection_require.jsonl'),
}

// L0 判级(对齐 cls-symbolic 四类 + 深度任务强制): 4 规则按序
const L0_RULES = [
  { tier: 'L4', forced: true, re: /(推导|分析|论证|设计|方案|比较|综合|原理|为什么|证明)/, label: '深度推理任务: 结论前置, 分步推理, 每步验证, 最后自检(与上一步结果核对后再继续)' },
  { tier: 'L3', forced: true, re: /(写|改|修|实现|重构|代码|脚本|函数|bug|报错|编译)/, label: '编码任务: 先读现状, 改完自测' },
  { tier: 'L2', forced: false, re: /(检查|审查|审计|review|核对|验证|巡检)/, label: '审查任务: 独立视角, 逐项核对' },
  { tier: 'L0', forced: false, re: /(总结|摘要|概括|提炼|简述)/, label: '摘要任务: 直接输出要点' },
]

const STOPWORDS = new Set([])

function cfg(config) {
  return { ...DEFAULTS, ...(config || {}) }
}

function audit(c, entry) {
  try {
    mkdirSync(dirname(c.auditPath), { recursive: true })
    appendFileSync(c.auditPath, JSON.stringify(entry) + '\n', 'utf8')
  } catch {
    // 审计失败不阻断
  }
}

function extractText(messages) {
  return messages
    .flatMap((m) => (m.content || []).filter((b) => b.type === 'text').map((b) => b.text))
    .join('\n')
}

// v0.3: 真实用户文本 — 只取最后一条非 chrome 的 user 消息。
// chrome 判据: plugin 自注入(source.kind) + dsh 把 AGENTS.md 人格内容作为 user 消息
// 注入 inbox(前缀"身份锚…"/"现在CLS会更换模型…"), 以及本插件四字段注入前缀【消息】。
const CHROME_PREFIX_RE = /^(身份锚|现在CLS会更换模型|【消息】)/
function textOf(m) {
  return (m.content || []).filter((b) => b.type === 'text').map((b) => b.text).join('\n')
}
function realUserText(messages) {
  const real = messages.filter((m) => {
    if (m.role !== 'user') return false
    if (m.source?.kind === 'plugin') return false
    const t = textOf(m).trim()
    return t && !CHROME_PREFIX_RE.test(t)
  })
  return real.length ? textOf(real[real.length - 1]).trim() : ''
}

function makeInjectedMessage(text) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'plugin', plugin: 'cls-inject' },
  }
}

// 统一审计走 CC knowledge_inject_audit (异步 spawn, 不阻塞模型循环)
function auditKi(c, label, trigger, text) {
  try {
    execFile(c.pythonBin, [c.auditScript, 'audit', 'dsh-inject', label, trigger, text],
      { encoding: 'utf8', windowsHide: true, timeout: 30000, maxBuffer: 1024 * 1024 }, () => {})
  } catch {
    // ignore
  }
}

export function apply(ctx, config) {
  const c = cfg(config)
  const state = new Map() // agentId -> { anchorText, tokens, driftDone, l0DoneTurn, maxL4Turn }

  function tokenize(text) {
    const out = []
    for (const m of text.matchAll(/[a-zA-Z0-9]+/g)) { const t = m[0].toLowerCase(); if (t.length >= 2 && !STOPWORDS.has(t)) out.push(t) }
    const han = text.replace(/[^\u4e00-\u9fff]+/g, '')
    for (let i = 0; i < han.length - 1; i++) out.push(han.slice(i, i + 2))
    return out
  }
  function jaccard(a, b) {
    const sa = new Set(a), sb = new Set(b)
    if (sa.size === 0 || sb.size === 0) return 0
    let inter = 0
    for (const t of sa) if (sb.has(t)) inter++
    return inter / (sa.size + sb.size - inter)
  }

  const classify = (text) => {
    for (const r of L0_RULES) if (r.re.test(text)) return r
    return { tier: 'L0', forced: false, label: '常规任务: 直接输出要点', re: null }
  }

  ctx.root.on('agent/pre-step', async ({ agent, messages, turn }, next) => {
    const decision = await next()
    if (!c.enabled || decision.kind === 'reject') return decision
    const text = extractText(messages).trim()
    if (!text) return decision
    const id = agent.id
    const injections = []

    let st = state.get(id)
    // ① L0 判级 + 深度任务强制锚(会话首次)
    if (!st) {
      // v0.3: 锚点 = 最后一条真实用户消息(非人格chrome), 拿不到则本会话禁用漂移检测
      const userText = realUserText(messages)
      st = { anchorText: userText.slice(0, 120), tokens: tokenize(userText), driftDone: false, l0DoneTurn: null, maxL4Turn: null, driftEnabled: userText.length > 0 }
      state.set(id, st)
      const rule = classify(userText || text)
      if (rule.tier === 'L4' || rule.tier === 'L3') {
        st.maxL4Turn = turn
        injections.push(`【消息】任务判级(CLS): ${rule.tier}级 — ${rule.label}。\n【为什么】本轮深度/编码任务, 保证推理链条不漂移。\n【级别】强制 — 完成需首行 ANCHOR 回执, 否则视认知约束失效。\n【内容】${rule.label}。`)
        auditKi(c, '任务判级', `${rule.tier}:${text.slice(0, 30)}`, injections[0])
      } else {
        st.l0DoneTurn = turn
        injections.push(`【消息】任务判级(CLS): ${rule.tier} — ${rule.label}。\n【为什么】识别本轮任务类型。\n【级别】参考。\n【内容】${rule.label}。`)
        auditKi(c, '任务判级', `${rule.tier}:${text.slice(0, 30)}`, injections[0])
      }
      audit(c, { agent: id, turn, kind: 'tier', tier: rule.tier })
      return { kind: 'enter', messages: [...decision.messages, ...injections.map(makeInjectedMessage)] }
    }

    // ② 漂移检测(v0.3: 真实用户消息 vs 任务锚, 全会话最多注入 1 次)
    if (st.driftEnabled && !st.driftDone) {
      const curText = realUserText(messages)
      if (curText && curText.slice(0, 120) !== st.anchorText) {
        const tokens = tokenize(curText)
        const sim = jaccard(st.tokens, tokens)
        if (sim < c.driftThreshold && tokens.length > 0) {
          st.driftDone = true // 全会话一次 — 注入三原则: 同一漂移重复提醒无增量
          const topic = [...new Set(tokens)].slice(0, 4).join('/')
          const msg = `【消息】话题漂移(CLS): 相似度 ${sim.toFixed(2)} — 原任务「${st.anchorText}」→ 当前话题[${topic}]。\n【为什么】已偏离本轮目标(本会话仅提示此一次)。\n【级别】参考 — 确需继续当前方向请明确说明理由, 否则回归原任务。\n【内容】如继续请说明; 否则回到「${st.anchorText.slice(0, 40)}」。`
          injections.push(msg)
          auditKi(c, '话题漂移', `sim:${sim.toFixed(2)} t:${topic}`, msg)
        }
      }
    }
    if (injections.length === 0) return decision
    return { kind: 'enter', messages: [...decision.messages, ...injections.map(makeInjectedMessage)] }
  }, 'cls-inject.pre-step')

  ctx.root.on('agent/disposed', ({ agent }) => {
    state.delete(agent.id)
  })
}
