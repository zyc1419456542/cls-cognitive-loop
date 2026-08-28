// cls-cc-workflow — Claude Code workflow 桥接插件 v0.1 (phase6 全量共享)
// 功能: 注册 run_cc_workflow 工具, 直接执行 .claude/workflows/*.js(CC 唯一维护点, 零转换)
// 实现: AsyncFunction 沙箱注入 CC 钩子(phase/log/agent/parallel + args),
//       agent(prompt, opts) → ctx.subagents.start('spawn') 一次性子代理, 返回文本/结构化
// 支持面: 全部 26 个 CC workflow 的公共 API(agent/phase/log/parallel/return, 无 import/require)
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

export const name = 'cls-cc-workflow'

export const inject = ['tools']

const CC_WF_DIR = '<REPO_ROOT>/.claude/workflows'
// 卡死防线 (2026-08-14 maintainer批准#2): 单子代理 10min 超时, 单 workflow 并发 ≤4 (官方#131 子代理广度失控)
const AGENT_TIMEOUT_MS = 10 * 60 * 1000
const MAX_CONCURRENT_AGENTS = 4

function listWorkflows() {
  try {
    return readdirSync(CC_WF_DIR).filter((f) => f.endsWith('.js')).map((f) => f.replace(/\.js$/, ''))
  } catch (e) {
    return []
  }
}

/** 提取 meta(export const meta = {...} 顶格结束), 容错。 */
function extractMeta(source) {
  const m = source.match(/export\s+const\s+meta\s*=\s*(\{[\s\S]*?\n\})/)
  if (!m) return null
  try {
    // meta 是纯数据对象字面量, Function 求值
    return new Function('return (' + m[1] + ')')()
  } catch (e) {
    return null
  }
}

/** 从结果 output 提取纯文本。 */
function outputText(result) {
  if (result.structured !== undefined && result.structured !== null) {
    return typeof result.structured === 'string'
      ? result.structured
      : JSON.stringify(result.structured)
  }
  const blocks = result.output || []
  const text = blocks
    .filter((b) => b.type === 'text')
    .map((b) => b.text)
    .join('\n')
    .trim()
  return text || '(子代理无文本输出, stopReason=' + result.stopReason + ')'
}

export function apply(ctx) {
  ctx.tools.register({
    name: 'run_cc_workflow',
    description:
      '运行 Claude Code workflow(.claude/workflows/*.js, CC 侧唯一维护点): 传入 workflow 文件名与 input 参数对象, 在兼容沙箱中执行(agent 钩子映射为 harness 子代理)。可用: ' +
      listWorkflows().join(', '),
    parameters: {
      type: 'object',
      properties: {
        workflow: { type: 'string', description: 'workflow 文件名(不带 .js), 如 cad-validate / research-pipeline' },
        input: { type: 'object', description: '传给 workflow 的 args 参数对象(如 {nl: "调研主题"})' },
      },
      required: ['workflow'],
      additionalProperties: true,
    },
    output: {
      schema: { type: 'string' },
      render: (args, value) => [{ type: 'text', text: value }],
    },
    timeoutMs: 1800000,
    async execute(args, exec) {
      const wfName = String(args.workflow || '').replace(/\.js$/, '').trim()
      if (!wfName) return '❌ 缺少 workflow 名。可用: ' + listWorkflows().join(', ')
      const file = join(CC_WF_DIR, wfName + '.js')
      if (!existsSync(file)) return '❌ workflow 不存在: ' + wfName + '。可用: ' + listWorkflows().join(', ')

      const source = readFileSync(file, 'utf8')
      const meta = extractMeta(source)
      // 剥掉 meta 导出行, 剩余为脚本主体
      const body = source.replace(/export\s+const\s+meta\s*=\s*\{[\s\S]*?\n\}\s*\n?/, '')

      const logs = []
      const phases = []
      let inflight = 0

      // agent(prompt, opts?) → harness 子代理 (卡死防线: 10min 超时 + 并发≤4, 不再裸等 run.result)
      const fnAgent = async (prompt, opts) => {
        const subs = ctx.get('subagents')
        if (!subs) throw new Error('subagents 服务不可用')
        while (inflight >= MAX_CONCURRENT_AGENTS) {
          await new Promise((resolve) => setTimeout(resolve, 500))
          exec.signal?.throwIfAborted()
        }
        inflight += 1
        const label = String((opts && opts.label) || 'ccwf-' + wfName).slice(0, 60)
        if (opts && opts.phase) phases.push(String(opts.phase))
        const ctrl = new AbortController()
        const onParentAbort = () => ctrl.abort()
        try { exec.signal?.addEventListener('abort', onParentAbort, { once: true }) } catch { /* 信号未定义则忽略 */ }
        try {
          const run = await subs.start('spawn', {
            label,
            prompt: [{ type: 'text', text: String(prompt) }],
            parent: exec.agent,
            signal: ctrl.signal,
          })
          try {
            const r = await Promise.race([
              run.result,
              new Promise((_, reject) => {
                const timer = setTimeout(() => {
                  ctrl.abort()
                  reject(new Error(`子代理超时(${AGENT_TIMEOUT_MS / 60000}min): ${label}`))
                }, AGENT_TIMEOUT_MS)
                ctrl.signal.addEventListener('abort', () => clearTimeout(timer), { once: true })
              }),
            ])
            return outputText(r)
          } finally {
            try { run.dispose() } catch (e) { /* ignore */ }
          }
        } finally {
          inflight -= 1
          try { exec.signal?.removeEventListener('abort', onParentAbort) } catch { /* ignore */ }
        }
      }

      const fnParallel = (tasks) => Promise.all((tasks || []).map((t) => (typeof t === 'function' ? t() : t)))
      const fnPhase = (t) => { phases.push(String(t)); logs.push('[phase] ' + t) }
      const fnLog = (m) => { logs.push(String(m)) }

      try {
        const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
        const fn = new AsyncFunction('args', 'phase', 'log', 'agent', 'parallel', body)
        const result = await fn(args.input || {}, fnPhase, fnLog, fnAgent, fnParallel)
        return JSON.stringify(
          {
            workflow: wfName,
            result: result === undefined ? null : result,
            phases: phases.slice(-40),
            logs: logs.slice(-60),
          },
          null,
          2,
        )
      } catch (e) {
        return '❌ workflow 执行失败: ' + (e && e.message ? e.message : String(e))
      }
    },
  })
}
