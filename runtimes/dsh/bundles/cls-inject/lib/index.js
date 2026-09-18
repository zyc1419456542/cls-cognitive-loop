// cls-inject — CLS 认知注入插件 v0.5 (2026-09-07 maintainer批准: Phase1 v0.4同步 + Phase0 反馈闭环)
// v0.1(旧): 锚点 bigram Jaccard 漂移 + L4 关键词正则, 旧文案。
// v0.2: ① L0 判级 → 四字段注入【消息/为什么/级别/内容】(对齐 iter-036 CC 语义路由);
//       ② 深度任务(判级 L4/L3) → 强制锚【结论前置, 分步推理...】(方案B: 完成需首行 ANCHOR 回执);
//       ③ 漂移检测保留(词面话题标签), 文案改四字段 参考级。
//       ④ 复用 CC knowledge_inject_audit 统一审计(source 前缀 dsh-inject)。
// v0.3 (2026-08-27 实测76条漂移全误报, 根治):
//       ① 锚点污染 — dsh 把 AGENTS.md 人格内容作为 user 角色消息注入 inbox(实测 session dump),
//          v0.2 拿首轮全部消息前120字当任务锚 → 锚=人格文本, 永远误报"话题漂移"。
//          修复: 锚点与漂移比较都只取最后一条"真实用户消息"(排除 plugin 注入与身份锚/系统前缀 chrome)。
//       ② 去重失效 — `driftInjected !== turn` 实为"每 turn 都可再注", 叠加①=每轮必弹。
//          修复: 漂移注入全会话最多 1 次(对齐注入三原则: 无增量不注入)。
//       ③ 防自噬 — 注入以 user 角色回灌后进下轮比较文本; 修复后按 source.kind=plugin 排除。
// v0.4 (2026-09-01 maintainer定: 去掉 Jaccard 纯字面匹配, 改 ep-json 纯推理漂移检测,
//       小模型真的读一遍内容归纳主题/判漂移, 文档管理制度而非算法):
//       ① 删除 tokenize/jaccard 函数 — 字面相似度不可靠, "讨论CLS基础设施" vs
//          "cls体系我感觉越来越不像rag" 在 Jaccard 看来 sim=0.06, 但语义高度相关。
//       ② 锚点捕获时: 调 ep-json 把用户消息归纳为一句话主题(如"改代码"/"哲学讨论")。
//       ③ 漂移检测时: 调 ep-json 归纳当前主题, 与锚点主题做逻辑比较。
//       ④ 判定: ep-json 输出 {is_drift, anchor_topic, current_topic, reason}。
//          is_drift=true 才注入告警; 无算法, 纯推理。
//       ⑤ topic 标签: 直接输出 ep-json 归纳的主题名。
// v0.5 (2026-09-07 Phase0 反馈闭环, 对齐 CC injection_feedback/inject_feedback_analyzer):
//       ① L0/L2 判级静默 — 对齐 CC cognitive_gate: L0/L2 = rate 0 = 永不注入(静默是有效动作),
//          只审计留痕。仅 L4/L3 复杂任务注入强制锚。
//       ② 注入反馈打分 — L4/L3 强制锚注入时 2% 概率附带打分邀请(对齐 CC FEEDBACK_RATE=0.02,
//          硬上限30轮), 模型回复可输出 [注入打分:判级:1|0|-1]。
//       ③ 收卷 — session/event 扫 assistant 回复里的 [注入打分:type:1|0|-1] /
//          [注入回应:id:采纳|参考|忽略] 标记 → 写 <DSH_HOME>\data\injection_feedback.jsonl。
//       ④ 自愈闸 — 每次注入前读 <DSH_HOME>\data\inject_feedback_config.json types{判级,漂移},
//          off → 该类型静默(差评>40%冷却2x / >60%自动关停由 dsh_inject_feedback_analyzer.py 写)。
// 触发: agent/pre-step; L0 判级本地正则零延迟; 漂移检测异步调 ep-json(不阻塞模型循环)。
import { randomUUID } from 'node:crypto'
import { execFile } from 'node:child_process'
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const name = 'cls-inject'

const DEFAULTS = {
  enabled: true,
  pythonBin: 'E:/anaconda/python.exe',
  auditScript: 'E:/<ORG>/claude_api/claude/scripts/wheels/dsh_cls_nav.py',
  maxInjectPerTurn: 2,
  auditPath: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'injection_require.jsonl'),
  feedbackPath: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'injection_feedback.jsonl'),
  feedbackConfigPath: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'inject_feedback_config.json'),
  ollamaBin: 'ollama',
  ollamaModel: 'ep-json:latest',
  ollamaTimeout: 15,
  fbRate: 0.02,          // 打分邀请概率(对齐 CC FEEDBACK_RATE)
  fbHardCapTurns: 30,    // 打分邀请硬上限: 距上次邀请>=30轮必请一次
  gateTypes: ['判级', '漂移', '纠错'], // 可被反馈闭环关停的注入类型(纠错=第2期@2026-09-15)
  // ── 被纠错检测(第2期, @add 2026-09-15 maintainer批) ──
  opsFreqPath: join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'ops_freq.jsonl'),
  cogStepPath: 'E:/<ORG>/claude_api/claude/data/state/cog_step.json',
  actedWindowMin: 20,   // "AI 刚动过手"的时间窗(分钟)
}

// L0 判级(对齐 cls-symbolic 四类 + 深度任务强制): 4 规则按序
const L0_RULES = [
  { tier: 'L4', forced: true, re: /(推导|分析|论证|设计|方案|比较|综合|原理|为什么|证明)/, label: '深度推理任务: 结论前置, 分步推理, 每步验证, 最后自检(与上一步结果核对后再继续)' },
  { tier: 'L3', forced: true, re: /(写|改|修|实现|重构|代码|脚本|函数|bug|报错|编译)/, label: '编码任务: 先读现状, 改完自测' },
  { tier: 'L2', forced: false, re: /(检查|审查|审计|review|核对|验证|巡检)/, label: '审查任务: 独立视角, 逐项核对' },
  { tier: 'L0', forced: false, re: /(总结|摘要|概括|提炼|简述)/, label: '摘要任务: 直接输出要点' },
]

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

// v0.4: 调本地 ep-json 做主题归纳 + 漂移判定(纯推理, 无算法)
// 输入: 用户消息文本 + 模式(anchor=归纳锚点 / check=检测漂移)
// 输出: JSON {topic} 或 {is_drift, anchor_topic, current_topic, reason}
function callEpJson(c, userText, mode, anchorTopic) {
  const system = (
    "你是任务主题归纳与漂移判定器。\n" +
    "给定一段用户消息, 输出 JSON:\n" +
    "  归纳模式(anchor): {\"topic\": \"一句话概括任务主题, 如'改代码'/'哲学讨论'/'调研方案'}\n" +
    "  漂移检测模式(check): {\"is_drift\": true/false, \"anchor_topic\": \"原主题\", \"current_topic\": \"当前主题\", \"reason\": \"为什么漂移/不漂移, 一句话\"}\n" +
    "判定标准: 两个任务在逻辑上是否连续。'改代码'→'讨论代码架构'不漂移; '改代码'→'哲学讨论'漂移。\n" +
    "只输出合法 JSON, 不要解释不要多余文字。"
  )
  const modeHint = mode === 'anchor'
    ? `归纳以下用户消息的任务主题:\n${userText.slice(0, 800)}`
    : `原任务主题: ${anchorTopic}\n当前用户消息:\n${userText.slice(0, 800)}\n请判定是否漂移。`
  const prompt = system + '\n\n' + modeHint

  return new Promise((resolve) => {
    try {
      execFile(c.ollamaBin, ['run', c.ollamaModel, '--nowordwrap'],
        { input: prompt, encoding: 'utf8', timeout: c.ollamaTimeout * 1000, maxBuffer: 1024 * 1024,
          windowsHide: true },
        (err, stdout) => {
          if (err || !stdout) return resolve(null)
          try {
            const text = stdout.trim()
            const start = text.indexOf('{')
            const end = text.lastIndexOf('}')
            if (start < 0 || end < start) return resolve(null)
            resolve(JSON.parse(text.slice(start, end + 1)))
          } catch { resolve(null) }
        })
    } catch { resolve(null) }
  })
}

// ── 被纠错检测(第2期, @add 2026-09-15 maintainer批) ──────────────────────────────
// 设计依据(实测 2026-09-15, 同一批 8 条含陷阱真值):
//   裸标签版(「只回答 1 或 2」/「输出: 改/不改」) 五种提示词 + 两个模型 + 两条路径
//   **全部 5/8**, 且输出分布退化成常量(因果版 8 条全输出"不改", 三分类版 7/8 输出"新要求")
//   —— 那个 5/8 不是能力, 是"恒定输出撞对多数类的概率"。
//   ★ 换成"标记 + 自然语言"(输出【要提醒】<一句提醒>)后: 合成 7/8, 真实对话 4/5。
//   三个机制: ①标记不是孤立 token, 而是一个完整回答的开头 → 模型必须先在脑内判完才写得出来;
//             ②标记后必须跟内容 → 堵死"恒吐一个词"的退化路径;
//             ③分类成了"生成一句提醒"的副产品。
//   而且产出物本身更有用: 是一句可读的自然语言, 信息量远大于一个布尔值。
//   ⚠️ @neg 2026-09-15 实测反例(别重犯): 曾把产出物收窄成"只给事实、不许写建议", 想让四字段
//     全由代码包 → **准确率 7/8 掉到 6/8**, 掉的正是真实样本里最有价值那条
//     ("怎么循环起来了 transformer怎么可能没安装")。根因就是上面第 ③ 条机制本身:
//     **分类是"生成一句提醒"的副产品** —— 生成任务一窄化, 判定跟着降级。
//     ⇒ 判断质量挂在生成任务上; **不能为了格式统一去砍生成**。格式由代码做"加法"补。
// ★ 时机先验(关键, 不用正则判语义): 只在"AI 刚动过手"时才跑 —— 人不会在 AI 没动手时纠错。
//   这用的是**结构先验**而不是语言特征, 所以不踩"消息正则不可靠"那个坑;
//   真正的语义判定交给小模型, 筛选只做前缀匹配(零歧义零词表)。
//
// 返回 AI 刚做的事(一句话), 或 null(= 没刚动过手, 不必跑检测)。
function recentAction(c) {
  // ① 优先 cog_step.json 的 label —— 它有人味("甲:给卡住告警补元认知跳出提示"), 但要新鲜
  try {
    if (existsSync(c.cogStepPath)) {
      const j = JSON.parse(readFileSync(c.cogStepPath, 'utf8'))
      const at = Date.parse(j.declared_at || '')
      if (at && (Date.now() - at) / 60000 <= c.actedWindowMin && j.label) {
        return String(j.label).slice(0, 60)
      }
    }
  } catch { /* 读不到就退回 ops_freq */ }
  // ② 退回 ops_freq 的最近一次 mutate(带文件名, 同样有信息量)
  try {
    const lines = readFileSync(c.opsFreqPath, 'utf8').trim().split('\n')
    for (let i = lines.length - 1; i >= Math.max(0, lines.length - 15); i--) {
      const d = JSON.parse(lines[i])
      if (d.category === 'mutate' && (Date.now() - (d.ts || 0)) / 60000 <= c.actedWindowMin) {
        const f = d.file || d.path || ''
        if (f) return `改了文件 ${String(f).split(/[\\/]/).pop()}`
      }
    }
  } catch { /* 观测不到就不检测(fail-quiet, 不打扰) */ }
  return null
}

// 调 ep-json 做判定 —— **不收 JSON**, 保住"标记+自然语言"这个已验证形态(见上)。
function callCorrection(c, aiAction, userText) {
  const prompt = (
    "你是 CLS 的小模型。AI 刚做了一件事, 人类回了一句话。\n" +
    "你要判断: 人类这句话是不是在说 AI 刚才做得不对、需要改动。\n" +
    "⚠️ 只根据【人类说的话】判断, 不要把【AI 做的事】当成人类的要求。\n\n" +
    "如果【是】, 输出: 【要提醒】<一句给 AI 的提醒, 20字内>\n" +
    "如果【不是】, 只输出: 【不用提醒】\n\n" +
    "例1) AI 说: 我把数据库连接池从 20 调到 50\n    人类回复: 你搞错了，重来\n" +
    "    【要提醒】人类说搞错了并要求重来, 建议先确认正确值再动手\n\n" +
    "例2) AI 说: 我清理了日志目录的历史文件\n    人类回复: 对，就这样\n    【不用提醒】\n\n" +
    "例3) AI 说: 我优化了订单查询索引\n    人类回复: 顺便把缓存也加上吧\n" +
    "    【不用提醒】这是新要求, 不是说 AI 做得不对\n\n" +
    `现在) AI 说: ${aiAction}\n    人类回复: ${userText.slice(0, 300)}\n`
  )
  return new Promise((resolve) => {
    try {
      execFile(c.ollamaBin, ['run', c.ollamaModel, '--nowordwrap'],
        { input: prompt, encoding: 'utf8', timeout: c.ollamaTimeout * 1000,
          maxBuffer: 1024 * 1024, windowsHide: true },
        (err, stdout) => resolve(err || !stdout ? null : String(stdout).trim()))
    } catch { resolve(null) }
  })
}

const classify = (text) => {
  for (const r of L0_RULES) if (r.re.test(text)) return r
  return { tier: 'L0', forced: false, label: '常规任务: 直接输出要点', re: null }
}

// ── v0.5 反馈闭环: 自愈闸(config 读) + 收卷(jsonl 写) ──
// 对齐 CC inject_feedback_config.json 格式: {types:{判级:on/off,漂移:on/off}, cooldown_mult, threshold_delta}
function readFeedbackGate(c) {
  try {
    if (!existsSync(c.feedbackConfigPath)) return {}
    return JSON.parse(readFileSync(c.feedbackConfigPath, 'utf8'))
  } catch {
    return {}
  }
}

function gateAllows(c, type) {
  const g = readFeedbackGate(c)
  const types = (g && g.types) || {}
  return types[type] !== 'off' // 缺省=on (fail-open: 配置损坏不误伤注入)
}

// L4/L3 注入时 2% 概率 + 30轮硬上限附带打分邀请(对齐 CC FEEDBACK_RATE + 硬上限)
function maybeFbInvite(c, st, turn) {
  st.lastFbTurn = st.lastFbTurn ?? -c.fbHardCapTurns
  if (turn - st.lastFbTurn < c.fbHardCapTurns && Math.random() > c.fbRate) return ''
  st.lastFbTurn = turn
  return '\n【注入反馈】(可忽略, 一行即可): 本条注入对当前任务有用吗? 请在回复末尾输出 [注入打分:判级:1] 或 [注入打分:判级:0] 或 [注入打分:判级:-1] (1=有用 0=噪音 -1=误导), 附一句话理由。'
}

// 收卷: 扫 assistant 回复里的打分/回应标记, 写 feedback jsonl
const FB_SCORE_RE = /\[注入打分:([^:\]]+):([\-01]+)\]/g
const FB_RESPOND_RE = /\[注入回应:([^:\]]+):(采纳|参考|忽略)\]/g
function collectFeedback(c, sessionId, text) {
  if (!text) return
  const rows = []
  for (const m of text.matchAll(FB_SCORE_RE)) {
    rows.push({ ts: new Date().toISOString(), kind: 'score', agent: sessionId, type: m[1].trim(), score: Math.max(-1, Math.min(1, Number(m[2] || 0))) })
  }
  for (const m of text.matchAll(FB_RESPOND_RE)) {
    rows.push({ ts: new Date().toISOString(), kind: 'respond', agent: sessionId, injection_id: m[1].trim(), verdict: m[2] })
  }
  if (!rows.length) return
  try {
    mkdirSync(dirname(c.feedbackPath), { recursive: true })
    appendFileSync(c.feedbackPath, rows.map((r) => JSON.stringify(r) + '\n').join(''), 'utf8')
  } catch {
    // 收卷失败不阻断
  }
}

export function apply(ctx, config) {
  const c = cfg(config)
  const state = new Map() // agentId -> { anchorText, anchorTopic, driftDone, l0DoneTurn, maxL4Turn, lastFbTurn }

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
      st = { anchorText: userText.slice(0, 120), anchorTopic: null, driftDone: false, l0DoneTurn: null, maxL4Turn: null, driftEnabled: userText.length > 0, lastFbTurn: null }
      state.set(id, st)
      const rule = classify(userText || text)
      // v0.5: L0/L2 静默(对齐 CC: L0/L2 rate=0 永不注入); 判级被反馈闸关停也静默
      if ((rule.tier === 'L4' || rule.tier === 'L3') && (gateAllows(c, '判级'))) {
        st.maxL4Turn = turn
        // @fix 2026-09-15 maintainer批: 按《注入文案规范 v1》检查表 #4 清理 ——
        //   原【消息】= "任务判级(CLS): L4级 — 深度推理任务: 结论前置, …", 【内容】= 同一串 label,
        //   两者**逐字重复**(lint R4 error)。maintainer本轮开头贴出的运行时样本正是这一条 ——
        //   他说"提示词一定要按为什么-怎么做写", 追到底就是这条消息和内容撞车。
        //   改法: 【消息】只说事实(判成几级), label 只出现在【内容】(那才是"怎么做");
        //   【为什么】从"复述是什么任务"改成机制(为什么会漂移 / 不管会怎样)。
        let msg = `【消息】任务判级(CLS): 本轮判定为 ${rule.tier} 级深度/编码任务。\n【为什么】长链推理会在中途漂移, 事前立约束比事后返工便宜。\n【级别】强制 — 完成需首行 ANCHOR 回执, 否则视认知约束失效。\n【内容】${rule.label}。`
        msg += maybeFbInvite(c, st, turn)
        injections.push(msg)
        auditKi(c, '任务判级', `${rule.tier}:${text.slice(0, 30)}`, msg)
      } else {
        st.l0DoneTurn = turn
        audit(c, { agent: id, turn, kind: 'tier', tier: rule.tier, silent: true })
      }
      audit(c, { agent: id, turn, kind: 'tier', tier: rule.tier })

      // v0.4: 异步调 ep-json 归纳锚点主题(不阻塞模型循环)
      if (st.driftEnabled) {
        callEpJson(c, userText, 'anchor').then((res) => {
          if (res && res.topic) {
            st.anchorTopic = res.topic
          } else {
            // ep-json 不可用时退化: 用原文本前40字当主题(仍比 Jaccard 可靠)
            st.anchorTopic = userText.slice(0, 40)
          }
        })
      }

      if (injections.length === 0) return decision
      return { kind: 'enter', messages: [...decision.messages, ...injections.map(makeInjectedMessage)] }
    }

    // ② 漂移检测(v0.4: ep-json 纯推理, 全会话最多注入 1 次; v0.5: 漂移类型被反馈闸关停则静默)
    if (st.driftEnabled && !st.driftDone && st.anchorTopic && gateAllows(c, '漂移')) {
      const curText = realUserText(messages)
      if (curText && curText.slice(0, 120) !== st.anchorText) {
        // v0.4: 异步调 ep-json(不阻塞), 但注入需等结果; 若超时则本次不注(下次还有机会)
        st.driftDone = true // 先锁死, 避免重复检测(对齐注入三原则: 同一漂移重复提醒无增量)
        const driftP = callEpJson(c, curText, 'check', st.anchorTopic)
        const timeoutP = new Promise((r) => setTimeout(() => r(null), c.ollamaTimeout * 1000))
        const res = await Promise.race([driftP, timeoutP])
        if (res && res.is_drift) {
          const anchorTopic = res.anchor_topic || st.anchorTopic
          const currentTopic = res.current_topic || '未知'
          const reason = res.reason || '逻辑上不连续'
          const msg = `【消息】话题漂移(CLS): 原任务「${anchorTopic}」→ 当前话题「${currentTopic}」。\n【为什么】${reason}(本会话仅提示此一次)。\n【级别】参考 — 确需继续当前方向请明确说明理由, 否则回归原任务。\n【内容】如继续请说明; 否则回到「${anchorTopic}」。`
          injections.push(msg)
          auditKi(c, '话题漂移', `anchor:${anchorTopic} cur:${currentTopic}`, msg)
        }
      }
    }
    // ③ 被纠错检测(第2期, @add 2026-09-15 maintainer批) —— 设计依据见 recentAction/callCorrection 注释
    //   时机先验: 只在"AI 刚动过手"时跑; 消息过长(多半是提需求/贴材料)跳过; 同一句不重复测。
    if (gateAllows(c, '纠错')) {
      const acted = recentAction(c)
      const curText = realUserText(messages)
      // @add 2026-09-17 maintainer批: ④ 的**漏斗审计**。
      //   @why ④ 上线至今 knowledge_inject_log 里 **0 条**, 但"没触发"是好事还是坏了说不清 ——
      //     因为没有任何中间量。记下"武装了几次"(= AI 刚动过手, 唯一的前置条件),
      //     才能把"0"变成"0 是因为 X": 是根本没机会, 还是有机会但小模型都判了不用提醒。
      //   注: audit() 写的是 injection_require.jsonl(与 tier/l4/anchor/drift 同处), 靠 kind 区分。
      if (acted) {
        audit(c, { agent: id, turn, kind: 'corr_armed',
                   text_len: (curText || '').length,
                   too_long: (curText || '').length > 300,
                   dup_anchor: (curText || '').slice(0, 120) === st.anchorText })
      }
      if (acted && curText && curText.length >= 2 && curText.length <= 300
          && curText.slice(0, 120) !== st.anchorText) {
        const cp = callCorrection(c, acted, curText)
        const cto = new Promise((r) => setTimeout(() => r(null), c.ollamaTimeout * 1000))
        const out = await Promise.race([cp, cto])
        if (out && out.includes('【要提醒】')) {
          const say = out.replace(/^[\s\S]*?【要提醒】/, '').split('\n')[0].trim().slice(0, 60)
          if (say) {
            // @add 2026-09-15 maintainer批: 按《注入文案规范 v1》补第四字段 —— 原文案缺【内容】。
            //   ⚠️ 踩过的坑(实测, 别重犯): 曾试图让小模型"只给事实, 建议由代码补"以求格式更纯,
            //     结果 **7/8 → 6/8**, 掉的正是真实样本里最有价值那条("transformer怎么可能没安装")。
            //     根因: 本机制第③条是"**分类是'生成一句提醒'的副产品**"(见本文件上方注释与考古报告§12.3)
            //     —— 把生成任务窄化成"指认事实", 判定跟着降级。**判断质量挂在生成任务上。**
            //   ⇒ 所以分工是: 小模型照旧生成一句(可含建议), **代码只做加法**: 补【为什么】【级别】【内容】。
            //   ⇒ 唯一残留偏离: 【消息】里可能带"建议"(事实与动作混着)。**这是被实测换来的, 接受。**
            const msg = `【消息】人类反馈(CLS): ${say}\n` +
              "【为什么】人已经指出问题; 沿原路继续做 = 把这处错误扩散到后面几步。\n" +
              "【级别】参考 — 小模型从一句话判的, 可能判错; 若你判断这是新要求而非纠正, 忽略即可。\n" +
              "【内容】①回看人类指的是哪一处 ②说明你当时的判断依据 ③说清改法后再动手。"
            injections.push(msg)
            auditKi(c, '人类反馈', curText.slice(0, 40), msg)
          }
        }
      }
    }
    if (injections.length === 0) return decision
    return { kind: 'enter', messages: [...decision.messages, ...injections.map(makeInjectedMessage)] }
  }, 'cls-inject.pre-step')

  // v0.5 收卷: 每轮 assistant 回复后扫打分标记(等价 CC Stop hook 扫 transcript)
  ctx.root.on('session/event', (session, event) => {
    if (!c.enabled) return
    if (!event || event.type !== 'assistant/message') return
    const data = event.data || {}
    const t = textOf(data.message)
    collectFeedback(c, session.id, t)
  })

  ctx.root.on('agent/disposed', ({ agent }) => {
    state.delete(agent.id)
  })
}