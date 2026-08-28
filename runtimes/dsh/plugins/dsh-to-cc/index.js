// dsh-to-cc — dsh 原生会话 → CC transcript 反向导出 (2026-08-16)
// 背景: cc-session-persistence 已让 dsh 读 CC (路径 B); 反向缺失 —
//       dsh 原生 session-<uuid> 在 CC 侧不可见。本插件把 dsh 原生会话
//       转录为 CC 项目目录下的 <uuid>.jsonl, 使 CC 可 resume/list。
// 边界:
//   - 只导出 id 形如 session-<uuid> 的 dsh 原生会话; 纯 uuid 的 CC 会话不碰
//   - 目标文件已存在则绝不覆盖, 只追加 (CC 文件 append-only)
//   - 启动全量回填缺失文件 + session/event 实时追加 + 定时扫缺
// 注意: 本插件位于 node_modules 解析树之外, 禁止 import 外部包(含 zod)。
import { existsSync } from 'node:fs'
import { appendFile, mkdir, rename, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { dsh2ccRecords } from '../cc-session-persistence/lib/convert.js'

const inject = ['sessionPersistence']
const name = 'dshToCcExporter'

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const SURFACE_TYPES = new Set(['user/message', 'assistant/message', 'tool/result'])

function cfg(config = {}) {
  return {
    ccProjectsDir: config.ccProjectsDir || '<HOME>/.claude/projects/E-------claude-api-claude',
    ccCwd: config.ccCwd || '<REPO_ROOT>',
    ccVersion: config.ccVersion || '2.1.223',
    intervalMs: config.intervalMs ?? 30 * 1000,
    syncOnStart: config.syncOnStart ?? true,
  }
}

/** session-<uuid> → uuid; 其余(含纯 uuid CC 会话)一律不导出 */
function ccUuidFor(id) {
  if (typeof id !== 'string' || !id.startsWith('session-')) return undefined
  const uuid = id.slice('session-'.length)
  return UUID_RE.test(uuid) ? uuid : undefined
}

function ccMetaRecords(uuid) {
  return [
    { type: 'mode', mode: 'normal', sessionId: uuid },
    { type: 'permission-mode', permissionMode: 'bypassPermissions', sessionId: uuid },
  ]
}

function serialize(records) {
  return records.map((r) => JSON.stringify(r)).join('\n') + '\n'
}

export function apply(ctx, config) {
  const c = cfg(config)
  const pending = new Map() // uuid -> Promise<void>

  const ccFile = (uuid) => path.join(c.ccProjectsDir, `${uuid}.jsonl`)

  const writeNewFile = async (uuid, events) => {
    const records = dsh2ccRecords({ events, sessionId: uuid, cwd: c.ccCwd, version: c.ccVersion })
    if (records.length === 0) return false
    const file = ccFile(uuid)
    await mkdir(path.dirname(file), { recursive: true })
    const tmp = `${file}.tmp-${Date.now()}-${Math.random().toString(16).slice(2)}`
    await writeFile(tmp, serialize([...ccMetaRecords(uuid), ...records]), 'utf8')
    try {
      await rename(tmp, file)
    } catch (error) {
      // 并发 bootstrap 撞车: 目标已被建, 用已有文件即可
      if (error?.code !== 'EEXIST' && error?.code !== 'EPERM') throw error
    }
    ctx.logger?.info(`[dsh-to-cc] +${uuid} (${records.length} records)`)
    return true
  }

  const appendEvents = async (uuid, events, meta) => {
    const records = dsh2ccRecords({
      events,
      sessionId: uuid,
      cwd: meta?.cwd || c.ccCwd,
      version: c.ccVersion,
    })
    if (records.length === 0) return
    const file = ccFile(uuid)
    await mkdir(path.dirname(file), { recursive: true })
    await appendFile(file, serialize(records), 'utf8')
  }

  const ensureSession = async (id) => {
    const uuid = ccUuidFor(id)
    if (!uuid) return
    if (existsSync(ccFile(uuid))) return
    let prev = pending.get(uuid)
    if (prev) return prev
    const task = (async () => {
      try {
        const view = await ctx.sessionPersistence.inspect(id)
        if (view?.events?.length) await writeNewFile(uuid, view.events)
      } catch (error) {
        ctx.logger?.warn(`[dsh-to-cc] load ${id} 失败(可能在 live 写入, 等实时事件): ${error.message}`)
      }
    })()
    pending.set(uuid, task)
    try {
      await task
    } finally {
      pending.delete(uuid)
    }
  }

  const scan = async () => {
    let created = 0
    let skipped = 0
    try {
      const headers = await ctx.sessionPersistence.list()
      for (const h of headers ?? []) {
        const id = h?.id ?? h?.header?.id ?? h?.meta?.id
        const uuid = ccUuidFor(id)
        if (!uuid) continue
        if (existsSync(ccFile(uuid))) {
          skipped += 1
          continue
        }
        try {
          await ensureSession(id)
          if (existsSync(ccFile(uuid))) created += 1
        } catch {
          // 单会话失败不阻断扫描
        }
      }
    } catch (error) {
      ctx.logger?.warn(`[dsh-to-cc] scan failed: ${error.message}`)
    }
    return { created, skipped }
  }

  // 实时: dsh 原生会话的新 surface 事件 → 追加到对应 CC 文件
  ctx.root.on('session/event', (session, event) => {
    const uuid = ccUuidFor(session?.id)
    if (!uuid || !event || !SURFACE_TYPES.has(event.type)) return
    const file = ccFile(uuid)
    if (!existsSync(file)) {
      // 首次见该会话: 全量回填(含当前事件), 不再单独 append, 防重复
      ensureSession(session.id).catch((error) =>
        ctx.logger?.warn(`[dsh-to-cc] bootstrap ${session.id} failed: ${error.message}`),
      )
      return
    }
    appendEvents(uuid, [event], session.header).catch((error) =>
      ctx.logger?.warn(`[dsh-to-cc] append ${session.id} failed: ${error.message}`),
    )
  })

  if (c.syncOnStart) {
    scan()
      .then((r) => ctx.logger?.info(`[dsh-to-cc] start scan: +${r.created} (${r.skipped} 已有文件)`))
      .catch((error) => ctx.logger?.warn(`[dsh-to-cc] start scan failed: ${error.message}`))
  }

  const timer = setInterval(() => {
    scan().catch(() => {})
  }, c.intervalMs)

  ctx.on('dispose', () => clearInterval(timer))
  ctx.logger?.info(`[dsh-to-cc] ready: ccProjectsDir=${c.ccProjectsDir} interval=${c.intervalMs}ms`)
}

export { inject, name }
export default { name, inject, apply }
