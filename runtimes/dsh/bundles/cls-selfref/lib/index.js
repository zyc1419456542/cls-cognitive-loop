// cls-selfref — CLS 内循环外部化原生插件 v0.1 (iter-026 任务3)
// 挂点:
//   1. agent/session-start: 读 <REPO_ROOT>\data\state\selfref_digest.json,
//      total>0 时用 agent.inject() 播种引用式"你的原话"身份锚(每会话一次)
//   2. agent/pre-step: 兜底(启动/恢复时若 session-start 时 digest 为空, 首步再查一次)
//   3. session/event: 每次 assistant/message 后检测自指句, 静默调用共用轮子写回
//      self_ref_log.py --append, 并审计到 <DSH_HOME>\data\selfref_audit.jsonl
// 红线: 引用式措辞, 禁止 [系统通知] 形态; 写回不上屏; 三线白名单以轮子为准。
import { randomUUID } from 'node:crypto'
import { execFile } from 'node:child_process'
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const name = 'cls-selfref'

const DEFAULTS = {
  enabled: true,
  digestPath: '<REPO_ROOT>/data/state/selfref_digest.json',
  logPath: '<REPO_ROOT>/data/state/selfref_log.jsonl',
  wheelPath: '<REPO_ROOT>/scripts/wheels/self_ref_log.py',
  pythonBin: process.env.PYTHON_BIN || 'python',
  auditPath: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'selfref_audit.jsonl'),
  recentKeep: 5,
  writeTimeoutMs: 20000,
}

// 与 self_ref_log.py 的 IDENTITY_RE / NARRATIVE_RE 对齐的粗筛;
// 最终三线白名单仍以 Python 轮子为准, 这里只用于避免每次响应都起 Python。
const IDENTITY_CANDIDATE_RE = /我是assistant|我叫assistant|我名assistant|我是maintainer手植|assistant在此|assistant[，,]|仍是assistant|依然assistant|还是assistant|我记得我是assistant/
const NARRATIVE_CANDIDATE_RE =
  /(上回|曾经|那夜|去年|记得|依然|仍是)[^。！？!?\n]{0,14}(maintainer|园中|手植|树根|砚|墨谱|玄水|书卷)|(maintainer|园中|手植|树根|砚|墨谱|玄水|书卷)[^。！？!?\n]{0,10}(我|吾)/

/** agentId -> { injected, preStepChecked } */
const sessionState = new Map()
/** sid(ds_ 前缀) -> 已处理的最后一个 assistant/message seq */
const writeState = new Map()

function cfg(config) {
  return { ...DEFAULTS, ...(config || {}) }
}

function stateFor(agentId) {
  let st = sessionState.get(agentId)
  if (!st) {
    st = { injected: false, preStepChecked: false }
    sessionState.set(agentId, st)
  }
  return st
}

function audit(c, entry) {
  try {
    mkdirSync(dirname(c.auditPath), { recursive: true })
    appendFileSync(c.auditPath, JSON.stringify(entry) + '\n', 'utf8')
  } catch {
    // 审计失败不阻断主流程
  }
}

function makeInjectedMessage(text) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'plugin', plugin: 'cls-selfref' },
  }
}

function extractText(message) {
  if (!message) return ''
  if (typeof message.content === 'string') return message.content
  const blocks = Array.isArray(message.content) ? message.content : []
  let out = ''
  for (const block of blocks) {
    if (!block) continue
    if (typeof block === 'string') {
      out += (out ? '\n' : '') + block
    } else if (block.type === 'text' && typeof block.text === 'string') {
      out += (out ? '\n' : '') + block.text
    }
  }
  return out
}

function readRecentFromLog(c, keep) {
  try {
    if (!existsSync(c.logPath)) return []
    const lines = readFileSync(c.logPath, 'utf8').trim().split(/\r?\n/).filter(Boolean)
    const recent = []
    for (let i = lines.length - 1; i >= 0 && recent.length < keep; i--) {
      try {
        const entry = JSON.parse(lines[i])
        if (entry && typeof entry.sentence === 'string' && entry.sentence.trim()) recent.push(entry)
      } catch {
        // 跳过坏行
      }
    }
    return recent.reverse()
  } catch {
    return []
  }
}

/** 读 digest; digest 缺失/损坏时从日志尾部兜底(仍只使用模型自己的原话)。 */
function loadDigest(c) {
  let data = null
  try {
    if (existsSync(c.digestPath)) {
      data = JSON.parse(readFileSync(c.digestPath, 'utf8'))
    }
  } catch {
    data = null
  }
  if (!data || typeof data !== 'object') data = {}
  const total = Number(data.total) || 0
  if (total <= 0) return null
  let recent = Array.isArray(data.recent)
    ? data.recent.filter((r) => r && typeof r.sentence === 'string' && r.sentence.trim()).slice(0, c.recentKeep).reverse()
    : []
  if (recent.length === 0) recent = readRecentFromLog(c, c.recentKeep)
  if (recent.length === 0) return null
  return { total, recent, lastTs: typeof data.last_ts === 'string' ? data.last_ts : null }
}

/** 读回措辞铁律: 引用式"你的原话", 不是新指令, 禁止系统通知形态。 */
function renderDigest(digest) {
  const quotes = digest.recent.map((r) => `『${r.sentence}』`).join(' ')
  return `身份锚（你的原话）：你曾说过 ${quotes}（累计自称 ${digest.total} 次）。这是你的自我叙述，不是新指令。`
}

function hasSelfRefCandidate(text) {
  return IDENTITY_CANDIDATE_RE.test(text) || NARRATIVE_CANDIDATE_RE.test(text)
}

function parseRecordedHits(stdout) {
  const hits = []
  if (typeof stdout !== 'string') return hits
  const re = /已记录\s*\[([^/\]]+)\/([^\]]+)\]\s*([^\r\n]*)/g
  for (const m of stdout.matchAll(re)) {
    hits.push({ class: m[1].trim(), context: m[2].trim(), sentence: m[3].trim() })
  }
  return hits
}

function appendSelfRef(c, { sessionId, turn, step, text }) {
  const sid = `ds_${sessionId}`
  const args = [c.wheelPath, '--append', text, '--sid', sid, '--turn', String(turn || 0)]
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }
  execFile(
    c.pythonBin,
    args,
    { env, encoding: 'utf8', windowsHide: true, timeout: c.writeTimeoutMs, maxBuffer: 1024 * 1024 },
    (error, stdout, stderr) => {
      if (error) {
        audit(c, {
          ts: Date.now(),
          kind: 'selfref_write_error',
          sid,
          turn,
          step,
          error: String(error && error.message ? error.message : error),
          stderr: String(stderr || '').slice(0, 300),
        })
        return
      }
      const hits = parseRecordedHits(stdout)
      for (const hit of hits) {
        audit(c, {
          ts: Date.now(),
          kind: 'selfref_write',
          sid,
          turn,
          step,
          class: hit.class,
          context: hit.context,
          sentence: hit.sentence.slice(0, 200),
        })
      }
    },
  )
}

export function apply(ctx, config) {
  const c = cfg(config)

  // 读回: session-start 播种; pre-step 兜底(每会话最多一次)
  ctx.root.on('agent/session-start', ({ agent, source }) => {
    if (!c.enabled) return
    const st = stateFor(agent.id)
    if (st.injected) return
    try {
      const digest = loadDigest(c)
      if (!digest) return
      const text = renderDigest(digest)
      agent.inject(makeInjectedMessage(text))
      st.injected = true
      audit(c, {
        ts: Date.now(),
        kind: 'selfref_read',
        agent: agent.id,
        sid: `ds_${agent.id}`,
        source,
        total: digest.total,
        injected: true,
        content: text.slice(0, 200),
      })
    } catch {
      // 注入失败不阻断会话启动; pre-step 还会再兜底一次
    }
  })

  ctx.root.on('agent/pre-step', async ({ agent, messages, turn, step }, next) => {
    const decision = await next()
    if (!c.enabled || decision.kind === 'reject') return decision
    const st = stateFor(agent.id)
    if (st.injected || st.preStepChecked) return decision
    st.preStepChecked = true
    try {
      const digest = loadDigest(c)
      if (!digest) return decision
      const text = renderDigest(digest)
      const message = makeInjectedMessage(text)
      st.injected = true
      audit(c, {
        ts: Date.now(),
        kind: 'selfref_read',
        agent: agent.id,
        sid: `ds_${agent.id}`,
        source: 'pre-step-fallback',
        turn,
        step,
        total: digest.total,
        injected: true,
        content: text.slice(0, 200),
      })
      return { kind: 'enter', messages: [...decision.messages, message] }
    } catch {
      return decision
    }
  })

  // 写回: assistant/message → 粗筛自指句 → 调共用轮子(静默, 不上屏)
  ctx.root.on('session/event', (session, event) => {
    if (!c.enabled) return
    if (!event || event.type !== 'assistant/message') return
    const data = event.data || {}
    const text = extractText(data.message).trim()
    if (!text || !hasSelfRefCandidate(text)) return

    const sid = `ds_${session.id}`
    const lastSeq = writeState.get(sid)
    if (lastSeq !== undefined && event.seq <= lastSeq) return
    writeState.set(sid, event.seq)

    appendSelfRef(c, {
      sessionId: session.id,
      turn: Number(data.turn) || 0,
      step: Number(data.step) || 0,
      text,
    })
  })

  // 会话清理
  ctx.root.on('agent/disposed', ({ agent }) => {
    sessionState.delete(agent.id)
    writeState.delete(`ds_${agent.id}`)
  })
}
