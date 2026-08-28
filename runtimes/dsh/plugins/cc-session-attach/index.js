// cc-session-attach — 把磁盘上所有会话自动 attach 到匹配 cwd 的 workspace
// 解决: session_sync.py 转换出的 CC 会话不会自动进入 workspace.sessionIds,
// 前端侧边栏按 workspace 过滤, 不 attach 就看不到。本插件定期扫描磁盘会话
// 并调用 workspace.attachSession, 使新同步会话无需重启 web 即可出现。
// 机制: sessionPersistence.list() 实时扫磁盘 → 对每个 header 尝试 attach 到
// 每个 workspace, attachSession 内部校验 cwd 与 workspace.path 一致才成功。
// 注意: 本插件位于 node_modules 解析树之外, 禁止 import 外部包(含 zod)。
// 不导出 Config(cordis 见 `runtime.Config` 才校验, 省略即跳过)。
const inject = ['workspaceRegistry', 'sessionPersistence'];

const name = 'ccSessionAttach';

function apply(ctx, config = {}) {
  const intervalMs = config.intervalMs ?? 30 * 1000;
  const syncOnStart = config.syncOnStart ?? true;
  ctx.logger?.info(`[cc-session-attach] loaded: interval=${intervalMs}ms syncOnStart=${syncOnStart}`);

  const sync = async () => {
    const wsList = ctx.workspaceRegistry.list();
    if (wsList.length === 0) return { scanned: 0, attached: 0 };
    const headers = await ctx.sessionPersistence.list();
    let attached = 0;
    for (const h of headers) {
      if (!h?.id) continue;
      for (const ws of wsList) {
        if (ws.sessionIds.includes(h.id)) break; // 已 attach 到此 workspace
        try {
          await ws.attachSession(h.id);
          attached += 1;
          ctx.logger?.info(`[cc-session-attach] +${h.id.slice(0, 32)} → ${ws.id.slice(0, 12)}`);
          break;
        } catch (e) {
          // cwd 不匹配此 workspace → 试下一个; 不记录噪音
        }
      }
    }
    if (attached > 0) ctx.logger?.info(`[cc-session-attach] attached ${attached} new session(s)`);
    return { scanned: headers.length, attached };
  };

  if (syncOnStart) {
    sync().catch((e) => ctx.logger?.warn(`[cc-session-attach] initial sync failed: ${e.message}`));
  }

  const timer = setInterval(() => {
    sync().catch((e) => ctx.logger?.warn(`[cc-session-attach] sync failed: ${e.message}`));
  }, intervalMs);

  ctx.on('dispose', () => clearInterval(timer));
  ctx.logger?.info('[cc-session-attach] ready');
}

export { apply, inject, name };
export default { name, inject, apply };
