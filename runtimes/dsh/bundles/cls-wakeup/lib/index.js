// cls-wakeup — 会话恢复自动唤醒插件 v0.3 (心跳机制 web 侧)
// v0.3: 大会话限流 — ①磁盘冷却锁(10min 内跨重启不重复唤醒, 防重启循环反复唤醒大会话)
//        ②大会话(>50k 事件)用简短指令, 减少大上下文处理成本
// 挂点: agent/created (兼容 resume/startup 两种恢复路径)
// 判定: 会话事件历史非空 = 恢复的会话(新会话为空) → 自动发送续跑指令
import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const name = 'cls-wakeup'

const COOLDOWN_MS = 10 * 60 * 1000 // 磁盘冷却: 跨重启 10 分钟内不重复唤醒
const LARGE_EVENTS = 50000          // 大会话阈值: 事件数超过则用简短指令
const WAKE_LOCK = join(process.env.DSH_HOME || '<DSH_HOME>', 'data', 'wakeup.lock')

const AUTOWAKE =
  '[autowake] 会话已自动恢复。请读取 E:\\dsh_home\\data\\replication_autopilot.md 并按其指令执行; ' +
  '执行完成后, 如有需要推进的复刻阶段(状态文件 phases 中 status=pending 的), 继续推进并在完成后更新状态文件。' +
  '若所有阶段均已完成, 简短汇报当前状态即可。'

// 大会话简短指令: 不要求读指令/推进, 只要求确认状态, 减少大上下文处理
const AUTOWAKE_BRIEF =
  '[autowake] 会话已自动恢复。当前会话历史较大, 请勿全量回顾; 检查 E:\\dsh_home\\data\\replication_state.json 的 ' +
  'pending 队列, 若有关键未完成项就推进, 否则一句话说明当前状态即可。'

function makeMsg(text) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'plugin', plugin: 'cls-wakeup' },
  }
}

/** 磁盘冷却: 10 分钟内已唤醒过则跳过(防重启循环反复唤醒)。 */
function inCooldown() {
  try {
    if (!existsSync(WAKE_LOCK)) return false
    return Date.now() - statSync(WAKE_LOCK).mtimeMs < COOLDOWN_MS
  } catch (e) {
    return false
  }
}

function markWake() {
  try {
    mkdirSync(dirname(WAKE_LOCK), { recursive: true })
    writeFileSync(WAKE_LOCK, String(Date.now()), 'utf8')
  } catch (e) {
    // ignore
  }
}

export function apply(ctx) {
  let woke = false

  ctx.root.on('agent/created', ({ agent }) => {
    if (woke) return
    let hasHistory = false
    let eventCount = 0
    try {
      eventCount = agent.session.events.length
      hasHistory = eventCount > 0
    } catch (e) {
      hasHistory = false
    }
    if (!hasHistory) {
      console.log('[cls-wakeup] fresh session, no wake:', agent.id)
      return
    }
    woke = true
    if (inCooldown()) {
      console.log('[cls-wakeup] cooldown active, skip wake:', agent.id)
      return
    }
    markWake()
    const large = eventCount > LARGE_EVENTS
    console.log('[cls-wakeup] waking:', agent.id, 'events:', eventCount, 'large:', large)
    setTimeout(() => {
      try {
        agent.followup(makeMsg(large ? AUTOWAKE_BRIEF : AUTOWAKE))
        console.log('[cls-wakeup] autowake sent:', agent.id, 'brief:', large)
      } catch (e) {
        console.error('[cls-wakeup] wake failed:', e)
      }
    }, 2000)
  }, 'cls-wakeup.created')
}
