// cc-backend.js — CC jsonl 后端 (dsh PersistenceCoordinator 的 backend 契约实现)
// 铁律: 只读/只追加已存在的 CC 文件, 绝不创建 — 文件存在性 = 会话归属 (isCcId 唯一判据)。
// CC 文件是 append-only jsonl, 无需 materialize/repair 语义:
//   - appendBatch 用 O_APPEND 单次写入 (不读改写), 与 CC 自身写入天然行级交错;
//   - 撕裂行由 parseCcLines 跳过, loadStored 不报错;
//   - commitRepair 永不触发 (cc2dsh 转换总是闭合 turn → interruptedTurnClosers 恒为 [])。
import { existsSync } from "node:fs"
import { stat, readFile, open } from "node:fs/promises"
import path from "node:path"
import { parseCcLines, cc2dsh, dsh2ccRecords } from "./convert.js"

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

// 读取上限 (2026-08-14 maintainer批准#3): 大文件(>24MB)只载头512KB+尾24MB 视图, 防 52MB CC 会话 OOM
const MAX_FULL_READ_BYTES = 24 * 1024 * 1024
const HEAD_SLICE_BYTES = 512 * 1024

export function isCcUuid(id) {
	return typeof id === "string" && UUID_RE.test(id)
}

export class CcBackend {
	constructor({ ccProjectsDir, cwd, version, logger }) {
		this.name = "cc-jsonl"
		this.ccProjectsDir = ccProjectsDir
		this.cwd = cwd
		this.version = version
		this.logger = logger
		this.disposed = false
		this._lastSize = new Map()
	}

	ccFile(id) {
		return path.join(this.ccProjectsDir, `${id}.jsonl`)
	}

	locate(meta) {
		if (!isCcUuid(meta?.id)) return void 0
		const p = this.ccFile(meta.id)
		return existsSync(p) ? { kind: "cc-jsonl", path: p } : void 0
	}

	async loadStored(id, signal) {
		if (this.disposed || !isCcUuid(id)) return void 0
		const file = this.ccFile(id)
		// 稳定读: stat → 读 → stat 一致才采用; 不一致重试 (CC 正在写)
		for (let attempt = 0; attempt < 3; attempt++) {
			signal?.throwIfAborted()
			let before
			try {
				before = await stat(file)
			} catch (error) {
				if (error?.code === "ENOENT") return void 0
				throw error
			}
			const big = before.size > MAX_FULL_READ_BYTES
			const { text, expectedTorn } = await this._readSlices(file, before.size, big, signal)
			signal?.throwIfAborted()
			const after = await stat(file)
			if (before.size === after.size && before.mtimeMs === after.mtimeMs) {
				const { records, torn } = parseCcLines(text)
				// 大文件头/尾切片边界各带 ≤1 条撕裂行属预期, 超出才告警
				if (torn > expectedTorn) this.logger?.warn(`[cc-session-persistence] ${id}: ${torn} 撕裂行跳过 (预期 ≤${expectedTorn})`)
				const converted = cc2dsh({
					records,
					id,
					cwdFallback: this.cwd,
					createdAtFallback: Math.floor(after.mtimeMs),
				})
				if (converted === void 0) return void 0 // 无 surface 事件 (如仅 mode 记录的新会话)
				this._lastSize.set(id, after.size)
				if (big) this.logger?.warn(`[cc-session-persistence] ${id}: 大文件 (${Math.round(before.size / 1048576)}MB) 载入头部 ${HEAD_SLICE_BYTES / 1024}KB + 尾部 ${MAX_FULL_READ_BYTES / 1048576}MB 视图`)
				return {
					meta: converted.meta,
					events: converted.events,
					revision: this._revisionOf(after),
				}
			}
			// 读期间文件被修改 → 重试
		}
		this.logger?.warn(`[cc-session-persistence] ${id}: 稳定读 3 次失败 (写入太频繁), 放弃`)
		return void 0
	}

	/** 小文件全读; 大文件读头切片(meta/标题)+尾切片(近期), 行级去重后拼接
	 *  2026-08-16 修复(二号): fh.read({length: 512KB}) 在 dsh 运行时触发
	 *  "ERR_OUT_OF_RANGE: length must be <= 16384" (internal 层单次读上限),
	 *  改 readFile 全量读 + 内存切片 — 57MB 字符串 Node 可承受, 消除 16KB 冲突。 */
	async _readSlices(file, size, big, signal) {
		if (!big) return { text: await readFile(file, "utf8"), expectedTorn: 0 }
		signal?.throwIfAborted()
		const full = await readFile(file, "utf8")
		const headText = full.slice(0, HEAD_SLICE_BYTES)
		const tailText = full.slice(Math.max(0, full.length - MAX_FULL_READ_BYTES))
		// 头切片最后一行可能截断 → 丢尾行; 尾切片首行必截断 → 丢首行
		const headLines = headText.split("\n")
		const headComplete = headLines.length > 0 && headLines[headLines.length - 1] === "" ? headText : headLines.slice(0, -1).join("\n")
		const tailStart = tailText.indexOf("\n")
		const tailClean = tailStart === -1 ? "" : tailText.slice(tailStart + 1)
		// 头尾重叠去重 (CC 记录含 uuid, 行文本唯一)
		const seen = new Set()
		for (const line of headComplete.split("\n")) {
			const t = line.trim()
			if (t.length > 0) seen.add(t)
		}
		const kept = []
		for (const line of tailClean.split("\n")) {
			const t = line.trim()
			if (t.length > 0 && !seen.has(t)) kept.push(line)
		}
		return { text: (headComplete ? headComplete + "\n" : "") + kept.join("\n"), expectedTorn: 2 }
	}

	async loadStoredFrom(id, fromSeq, signal) {
		const stored = await this.loadStored(id, signal)
		if (stored === void 0) return void 0
		return { meta: stored.meta, events: stored.events.filter((e) => e.seq >= fromSeq) }
	}

	async readStoredRevision(id, signal) {
		if (this.disposed || !isCcUuid(id)) return void 0
		signal?.throwIfAborted()
		try {
			const st = await stat(this.ccFile(id))
			return this._revisionOf(st)
		} catch (error) {
			signal?.throwIfAborted()
			if (error?.code === "ENOENT") return void 0
			throw error
		}
	}

	async appendBatch(meta, events, _isMaterialized) {
		const id = meta?.id
		if (this.disposed || !isCcUuid(id)) return
		const file = this.ccFile(id)
		const records = dsh2ccRecords({
			events,
			sessionId: id,
			cwd: typeof meta?.cwd === "string" ? meta.cwd : this.cwd,
			version: this.version,
		})
		if (records.length === 0) return
		// 写入前 stat: 文件必须已存在 (绝不创建); size 收缩说明 CC 重写过 → warn 但继续追加
		let before
		try {
			before = await stat(file)
		} catch (error) {
			if (error?.code === "ENOENT") return // 文件被删除 → 放弃, 绝不创建
			throw error
		}
		const lastSize = this._lastSize.get(id)
		if (lastSize !== void 0 && before.size < lastSize) {
			this.logger?.warn(`[cc-session-persistence] ${id}: 文件收缩 (CC 重写), 继续追加`)
		}
		const lines = records.map((r) => JSON.stringify(r)).join("\n") + "\n"
		const fh = await open(file, "a") // O_APPEND: 单次写入, 与 CC 自身追加行级交错, 不覆盖
		try {
			await fh.appendFile(lines, "utf8")
		} finally {
			await fh.close()
		}
		const after = await stat(file)
		this._lastSize.set(id, after.size)
	}

	/** 永不触发: cc2dsh 转换总是闭合 turn, interruptedTurnClosers 恒为 []; 无 zstd/物化, 无撕裂截断可修 */
	async commitRepair(meta, _tornMarker, _closers) {
		this.logger?.warn(`[cc-session-persistence] commitRepair(${meta?.id}) 意外触发 — 无操作`)
	}

	close() {
		// 无句柄持有, 无操作
	}

	_revisionOf(st) {
		return `${st.size}:${st.mtimeMs}`
	}
}
