// cls-cc-commands — Claude Code 命令桥接插件 v0.2 (phase6 全量共享)
// v0.2: 动态重注册 — fs.watch 监听 .claude/commands 目录, CC 新增/修改命令 md 即时生效
// 功能: 扫描 .claude/commands/*.md(CC 唯一维护点), 每个 md 注册为 harness 命令
// handler: 读取 md 内容 + 用户输入 → agent.followup(提交为模型可见消息, 模型按 md 指示执行)
import { existsSync, readdirSync, readFileSync, watch } from 'node:fs'
import { join } from 'node:path'

export const name = 'cls-cc-commands'

// 2026-08-14 止血: cordis loader 对 apply 内 ctx 服务访问做 inject 校验,
// 缺失时 boot 崩 ("cannot get property commands without inject")。
// 对照 cc-session-persistence(inject: [sessionPersistence, sessions]) / cls-cc-workflow(inject: [tools])。
export const inject = ['commands']

const CC_CMD_DIR = '<REPO_ROOT>/.claude/commands'

function collectCommands() {
  const out = []
  try {
    for (const f of readdirSync(CC_CMD_DIR)) {
      if (f.endsWith('.md') && !f.startsWith('_')) {
        out.push({ name: f.replace(/\.md$/, ''), file: join(CC_CMD_DIR, f) })
      }
    }
    const agentsDir = join(CC_CMD_DIR, 'agents')
    if (existsSync(agentsDir)) {
      for (const f of readdirSync(agentsDir)) {
        if (f.endsWith('.md')) {
          out.push({ name: 'agents-' + f.replace(/\.md$/, ''), file: join(agentsDir, f) })
        }
      }
    }
  } catch (e) {
    // ignore
  }
  return out
}

/** 从 md 首行标题提取描述。 */
function extractTitle(content) {
  const first = content.split('\n').find((l) => l.trim().startsWith('#'))
  if (first) return first.replace(/^#+\s*/, '').trim().slice(0, 80)
  return ''
}

export function apply(ctx) {
  const disposers = new Map() // name -> disposer

  const rescan = () => {
    const current = collectCommands()
    const known = new Set()
    for (const { name, file } of current) {
      known.add(name)
      if (disposers.has(name)) continue // 已注册(内容变更由 handler 每次读文件保证最新)
      if (ctx.commands.find?.(undefined, name)) continue // 手动版优先
      try {
        const disposer = ctx.commands.register({
          name,
          description: `[CC桥接] ${extractTitle(readFileSync(file, 'utf8'))}`,
          recordInput: true,
          handler: async (invocation) => {
            const { agent, rawInput } = invocation
            try {
              const content = readFileSync(file, 'utf8')
              const full = content + '\n\n---\n用户输入: ' + (rawInput || '(无)')
              agent.followup({
                id: crypto.randomUUID(),
                role: 'user',
                content: [{ type: 'text', text: full }],
                source: { kind: 'plugin', plugin: 'cls-cc-commands' },
              })
              return { kind: 'success', text: `已提交执行: /${name}(CC 命令桥接, 模型将按命令内容执行)` }
            } catch (e) {
              return { kind: 'error', text: `读取命令失败: ${e.message}` }
            }
          },
        })
        disposers.set(name, disposer)
        console.log('[cls-cc-commands] registered:', name)
      } catch (e) {
        console.error('[cls-cc-commands] register failed:', name, e)
      }
    }
    // 注销已删除的命令
    for (const [name, disposer] of [...disposers]) {
      if (!known.has(name)) {
        try { disposer() } catch (e) { /* ignore */ }
        disposers.delete(name)
        console.log('[cls-cc-commands] unregistered:', name)
      }
    }
  }

  rescan()

  // 动态监听: CC 侧新增/删除命令 md → 自动重注册
  const watcher = watch(CC_CMD_DIR, { persistent: false }, () => {
    setTimeout(rescan, 200) // 防抖: 文件写入可能触发多次事件
  })
  ctx.on('dispose', () => watcher.close())
}
