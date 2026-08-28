// index.js — cc-session-persistence: CC 会话零拷贝挂载到 dsh sessionPersistence
// 路径 B (与一号镜像方案对照后的定案):
//   CC 会话 = uuid 文件名存在于 ccProjectsDir 的 jsonl (文件存在性 = 归属判据);
//   读: 稳定读 → CC 记录转 dsh 事件 (确定性转换, 同一文件每次 load 事件完全一致);
//   写: dsh 侧 surface 事件 → CC 记录 → O_APPEND 追加到 CC 原生文件 (不复制、无游标、无计划任务);
//   dsh 原生会话: 全部走原 jsonl 后端, 本插件零影响。
// 部署: cordis.patch.yml insert 行 (name = file:///<DSH_HOME>/plugins/cc-session-persistence/lib/index.js)
import { createHash } from "node:crypto"
import { existsSync } from "node:fs"
import { open, readdir, stat, readFile } from "node:fs/promises"
import { CcBackend, isCcUuid } from "./cc-backend.js"
import { parseCcLines, synthesizeMeta } from "./convert.js"

const MAX_HEAD_BYTES = 256 * 1024
// 2026-08-17 maintainer裁决: 互通只放行 ≥5MB 的长会话, 短对话/subagent 不互通 (JSONL 明文大小)
const MIN_MB = 5.0

// uuid5 (RFC 4122 v5, NAMESPACE_URL) — mirror 的 dsh→CC 导出用 uuid5('dsh:'+id) 命名 CC 文件;
// 2026-08-16 maintainer裁决「只显示主窗口」: adapter 列表据此跳过与 dsh 原生会话同源的回声文件。
function uuid5(name) {
	const ns = Buffer.from("6ba7b8119dad11d180b400c04fd430c8", "hex")
	// sha1(20 字节) 按 RFC 4122 v5 取前 16 字节, 再置版本/变体位
	const h = createHash("sha1").update(Buffer.concat([ns, Buffer.from(name, "utf8")])).digest().subarray(0, 16)
	h[6] = (h[6] & 0x0f) | 0x50
	h[8] = (h[8] & 0x3f) | 0x80
	const x = h.toString("hex")
	return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`
}

export const name = "cc-session-persistence"
// sessions: PersistenceCoordinator 构造函数内 installWritePath 访问 ctx.sessions,
// 该访问在 apply 执行时按本 fiber 的 inject 校验, 必须显式声明
export const inject = ["sessionPersistence", "sessions"]

export function apply(ctx, config = {}) {
	const persistence = ctx.sessionPersistence
	if (!persistence) {
		ctx.logger.warn("[cc-session-persistence] sessionPersistence 服务不可用, 跳过")
		return
	}
	if (persistence.__ccWrapped === true) {
		ctx.logger.warn("[cc-session-persistence] 已安装, 跳过重复 apply")
		return
	}
	const ccProjectsDir = config.ccProjectsDir
	if (typeof ccProjectsDir !== "string" || ccProjectsDir.length === 0) {
		ctx.logger.warn("[cc-session-persistence] 未配置 ccProjectsDir, 跳过 (patch 行 config 里提供)")
		return
	}
	const ccCwd = typeof config.ccCwd === "string" ? config.ccCwd : undefined
	const ccVersion = typeof config.ccVersion === "string" ? config.ccVersion : "2.1.223"
	persistence.__ccWrapped = true

	const backend = new CcBackend({ ccProjectsDir, cwd: ccCwd, version: ccVersion, logger: ctx.logger })
	// 复用官方 PersistenceCoordinator: 缓存/顺序化/写入路径语义全部继承
	const Coordinator = persistence.coordinator.constructor
	const cc = new Coordinator(persistence.ctx, backend, {
		preparedSessionCacheSize: config.preparedSessionCacheSize ?? 5,
		writeBatchMaxDelayMs: config.writeBatchMaxDelayMs ?? 200,
	})

	const isCcId = (id) => isCcUuid(id) && existsSync(backend.ccFile(id))

	const restores = []

	// 服务方法路由: CC id → cc coordinator; 其余 → 原实现
	const wrapRoute = (method, ccMethod = method) => {
		if (typeof persistence[method] !== "function") return
		const orig = persistence[method].bind(persistence)
		persistence[method] = function (...args) {
			const id = typeof args[0] === "string" ? args[0] : args[0]?.id
			if (id !== undefined && isCcId(id)) return cc[ccMethod](...args)
			return orig(...args)
		}
		restores.push(() => {
			delete persistence[method]
		})
	}
	for (const m of ["append", "prepare", "load", "inspect", "readFrom"]) wrapRoute(m)

	// readRaw: CC id → 原文 + 合成 meta (快照导出 UI 用)
	if (typeof persistence.readRaw === "function") {
		const origReadRaw = persistence.readRaw.bind(persistence)
		persistence.readRaw = async function (id, signal) {
			if (isCcId(id)) {
				signal?.throwIfAborted()
				const file = backend.ccFile(id)
				const st = await stat(file)
				const content = await readFile(file, "utf8")
				const { records } = parseCcLines(content)
				return {
					meta: synthesizeMeta({
						records,
						id,
						cwdFallback: ccCwd,
						createdAtFallback: Math.floor(st.mtimeMs),
					}),
					filename: "session.jsonl",
					content,
				}
			}
			return origReadRaw(id, signal)
		}
		restores.push(() => {
			delete persistence.readRaw
		})
	}

	// locate: CC meta → CC 文件位置
	if (typeof persistence.locate === "function") {
		const origLocate = persistence.locate.bind(persistence)
		persistence.locate = function (meta) {
			const ccLoc = backend.locate(meta)
			if (ccLoc !== void 0) return ccLoc
			return origLocate(meta)
		}
		restores.push(() => {
			delete persistence.locate
		})
	}

	// 扫描 CC 目录 (每文件仅读头部 256KB, 够合成 meta; 不做整文件解析)
	async function ccScan(signal, skip) {
		const out = []
		let entries
		try {
			entries = await readdir(ccProjectsDir)
		} catch {
			return out
		}
		for (const fname of entries) {
			signal?.throwIfAborted()
			if (!fname.endsWith(".jsonl")) continue
			const id = fname.slice(0, -6)
			if (!isCcUuid(id)) continue
			if (skip?.has(id)) continue
			try {
				const file = backend.ccFile(id)
				const st = await stat(file)
				// 2026-08-17 maintainer裁决: 只互通长会话 — CC jsonl < MIN_MB 的短会话/subagent 不在 dsh 展示
				if (st.size < MIN_MB * 1024 * 1024) continue
				const fh = await open(file, "r")
				try {
					const len = Math.min(st.size, MAX_HEAD_BYTES)
					const buf = Buffer.alloc(len)
					const { bytesRead } = await fh.read(buf, 0, len, 0)
					const { records } = parseCcLines(buf.subarray(0, bytesRead).toString("utf8"))
					// 2026-08-16 maintainer裁决: 与 CC /resume 对齐 — 仅"头部含 mode 记录"的才是 CC 会话;
					// SDK(queue-operation)/session_bridge 导出等文件 CC 侧也不显示, dsh 同样跳过。
					if (!records.some((r) => r?.type === "mode")) continue
					out.push({
						meta: synthesizeMeta({
							records,
							id,
							cwdFallback: ccCwd,
							createdAtFallback: Math.floor(st.mtimeMs),
						}),
						revision: `${st.size}:${st.mtimeMs}`,
					})
				} finally {
					await fh.close()
				}
			} catch (error) {
				if (error?.code !== "ENOENT") {
					ctx.logger.warn(`[cc-session-persistence] 扫描 ${fname} 失败: ${error.message}`)
				}
			}
		}
		return out
	}

	// list / listSnapshots: 原生 + CC 合并 (按 id 去重, 原生优先)
	const wrapMergeList = (method) => {
		if (typeof persistence[method] !== "function") return
		const orig = persistence[method].bind(persistence)
		persistence[method] = async function (signal) {
			const native = await orig(signal)
			const nativeIds = new Set(native.map((item) => item?.id ?? item?.header?.id))
			// 2026-08-16 maintainer裁决: 只显示主窗口 — 跳过与 dsh 原生会话同源的 CC 回声文件
			// (同名 uuid = dsh-to-cc 导出; uuid5 = mirror dsh→CC 导出), 防同一会话双列
			const ccSkip = new Set()
			for (const nid of nativeIds) {
				if (typeof nid !== "string") continue
				ccSkip.add(nid.startsWith("session-") ? nid.slice("session-".length) : nid)
				ccSkip.add(uuid5(`dsh:${nid}`))
			}
			const ccEntries = await ccScan(signal, ccSkip)
			const merged = [...native]
			for (const entry of ccEntries) {
				if (nativeIds.has(entry.meta.id)) continue
				merged.push(method === "listSnapshots" ? { header: entry.meta, revision: entry.revision } : entry.meta)
			}
			return merged
		}
		restores.push(() => {
			delete persistence[method]
		})
	}
	wrapMergeList("list")
	wrapMergeList("listSnapshots")

	// 静默原 jsonl 后端对 CC id 的读/写: 防原生 coordinator 的 ctx 监听器为 CC 会话
	// 创建影子 dsh 文件 (文件存在性 = 归属, 原生侧必须把 CC 会话视为"不存在")。
	// 本机 jsonl 服务自身即 backend (coordinator.backend === 服务实例), 直接静默服务方法。
	const silenceTarget = persistence.coordinator.backend ?? persistence
	const saved = {}
	for (const m of ["loadStored", "readStoredRevision", "appendBatch", "loadStoredFrom"]) {
		if (typeof silenceTarget[m] !== "function") continue
		saved[m] = silenceTarget[m].bind(silenceTarget)
		silenceTarget[m] = function (...args) {
			const id = typeof args[0] === "string" ? args[0] : args[0]?.id
			if (id !== undefined && isCcId(id)) return void 0
			return saved[m](...args)
		}
	}
	restores.push(() => {
		for (const [m, fn] of Object.entries(saved)) {
			if (fn !== undefined) silenceTarget[m] = fn
		}
	})

	// 恢复: 全部包装还原, 后端停用 (残留监听器对 CC 会话变为无操作)
	ctx.on("dispose", () => {
		for (const restore of restores) {
			try {
				restore()
			} catch {
				/* 忽略 */
			}
		}
		backend.disposed = true
		delete persistence.__ccWrapped
	})

	return persistence
}
