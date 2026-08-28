// convert.js — CC jsonl 记录 ↔ dsh SessionEvent 转换核心
// 移植自assistant-node-1 session_bridge.py (20260814 交付), 与一号的差异:
//   1. 消息 id 确定性生成 (ccu-<seq>/cca-<seq>/cct-<seq>) — 同一文件多次 load 事件 id 稳定,
//      不随每次转换重新随机 (一号每次 uuid4, 会导致 dsh 侧 messageIds 每次 load 全变)
//   2. meta.createdAt 取 CC 首条记录时间戳 (一号用转换时刻的当前时间) — 跨 load 稳定, list 排序正确
//   3. 收尾 turn 关闭的时间戳取最后一条事件时间 (一号用当前时间) — 跨 load 转换结果完全确定
//   4. CC 记录 version 用配置 ccVersion (二号机 2.1.223), entrypoint 改 cc-session-persistence
//
// dsh 磁盘格式铁律 (一号 README, 本转换器遵循):
//   - surface 事件只有 3 种: user/message | assistant/message | tool/result, 必须带 surfaceOp:"append"
//   - 其他事件 (cfg/turn·step/session-title) 禁带 surfaceOp
//   - seq === index, 从 0 连续 (dsh foldSurface 硬校验)

import { randomUUID } from "node:crypto"

const SURFACE_TYPES = new Set(["user/message", "assistant/message", "tool/result"])

/**
 * 2026-08-16 修复: 码元边界安全截断。
 * slice 切在 emoji 代理对中间会留下孤立高代理 — JSON 序列化后 json.loads 还原成
 * 非法字符, 导致 mirror 双向崩溃 + 下游 API 400 打死会话 (暴毙: a399bba4 12:51)。
 * 截断点落在高代理之后时丢弃半个代理, 保证输出是合法 Unicode 标量序列。
 */
function safeSlice(str, len) {
	if (str.length <= len) return str
	const out = str.slice(0, len)
	const last = out.charCodeAt(out.length - 1)
	return last >= 0xd800 && last <= 0xdbff ? out.slice(0, -1) : out
}

// ---------- 时间 ----------
/** CC ISO 时间戳 → unix ms; 无法解析返回 undefined */
export function ccTsToMs(ts) {
	if (typeof ts !== "string") return undefined
	const ms = Date.parse(ts)
	return Number.isFinite(ms) ? ms : undefined
}

/** unix ms → CC ISO 时间戳 */
export function msToCcTs(ms) {
	return new Date(ms).toISOString()
}

// ---------- CC 行解析 ----------
/** 解析 CC jsonl 文本; 撕裂/不可解析行跳过 (CC 写中断/压缩中途的尾巴) */
export function parseCcLines(text) {
	const records = []
	let torn = 0
	for (const raw of text.split("\n")) {
		const line = raw.trim()
		if (line.length === 0) continue
		try {
			records.push(JSON.parse(line))
		} catch {
			torn += 1
		}
	}
	return { records, torn }
}

// ---------- meta 合成 ----------
/**
 * 合成 dsh SessionHeader (内存形状, 不含 "type" 字段 — 与 dsh fromHeaderLine 一致):
 * createdAt = 首条带可解析 timestamp 的记录 (兜底: 文件 mtime → Date.now)
 * cwd = 首条带 cwd 的记录 (兜底: config.ccCwd)
 */
export function synthesizeMeta({ records, id, cwdFallback, createdAtFallback }) {
	let createdAt
	let cwd
	for (const rec of records) {
		if (typeof rec !== "object" || rec === null) continue
		if (createdAt === undefined) {
			const ms = ccTsToMs(rec.timestamp)
			if (ms !== undefined) createdAt = ms
		}
		if (cwd === undefined && typeof rec.cwd === "string" && rec.cwd.length > 0) cwd = rec.cwd
		if (createdAt !== undefined && cwd !== undefined) break
	}
	if (createdAt === undefined) createdAt = createdAtFallback
	if (createdAt === undefined || !Number.isSafeInteger(createdAt) || createdAt < 0) createdAt = Date.now()
	const meta = { version: 0, id, createdAt, delegationDepth: 0 }
	const effectiveCwd = cwd ?? cwdFallback
	if (effectiveCwd !== undefined) meta.cwd = effectiveCwd
	return meta
}

// ---------- CC → dsh ----------
/** CC content block → dsh content block; 无法映射返回 undefined */
function ccBlockToDsh(block) {
	switch (block?.type) {
		case "text":
			return { type: "text", text: typeof block.text === "string" ? block.text : "" }
		case "thinking":
			return { type: "reasoning", text: typeof block.thinking === "string" ? block.thinking : "" }
		case "tool_use":
			return {
				type: "tool-call",
				id: block.id,
				name: block.name,
				arguments: JSON.stringify(block.input ?? {}),
			}
		default:
			return undefined
	}
}

/** CC tool_result 内容 → 单段文本 (string 直用; block 数组: 文本段拼接, 其余 JSON 化) */
function ccToolContentText(blk) {
	const content = blk?.content
	if (typeof content === "string") return content
	if (Array.isArray(content)) {
		const parts = []
		for (const b of content) {
			if (b !== null && typeof b === "object") {
				if (b.type === "text" && typeof b.text === "string") parts.push(b.text)
				else parts.push(JSON.stringify(b))
			}
		}
		return parts.join("\n")
	}
	return content === undefined ? "" : String(content)
}

/**
 * CC jsonl 记录 → dsh 事件流。
 * @param {object} opts
 * @param {object[]} opts.records 已解析的 CC 记录 (mode/permission 等元记录自然跳过)
 * @param {string} opts.id 会话 id (= CC 文件名去 .jsonl)
 * @param {string} [opts.cwdFallback] 记录里没有 cwd 时的兜底
 * @param {number} [opts.createdAtFallback] 记录里没有可解析时间戳时的兜底 (ms)
 * @returns {{meta: object, events: object[]} | undefined} 无 surface 事件返回 undefined
 */
export function cc2dsh({ records, id, cwdFallback, createdAtFallback }) {
	// 2026-08-16 maintainer裁决: 只显示主窗口 — 内嵌子 agent (sidechain) 记录整体剔除,
	// 不进入 dsh 会话视图 (sidechain 是交错子序列, 剔除后主窗口序列保持闭合)。
	records = records.filter((rec) => rec?.isSidechain !== true)
	const events = []
	let seq = 0

	// 每记录时间戳 (不可解析则沿用上一条有效值)
	let firstTs
	let lastValidTs
	const tsOf = (rec) => {
		const ms = ccTsToMs(rec?.timestamp) ?? lastValidTs
		if (ms !== undefined) {
			lastValidTs = ms
			if (firstTs === undefined) firstTs = ms
		}
		return ms
	}

	// 配置前导事件 (仿 dsh 真实运行写入的头部配置; 非 surface)
	const cfgEvents = [
		{ type: "permission/preset", data: { preset: "workspace-write" } },
		{ type: "sandbox/mode", data: { mode: "workspace-write" } },
		{ type: "approval/policy", data: { policy: "ask" } },
	]
	// firstTs 此时未定 (还没遍历记录) — 两遍式: 先预扫描得 firstTs
	for (const rec of records) {
		if (typeof rec !== "object" || rec === null) continue
		if (ccTsToMs(rec?.timestamp) !== undefined) {
			firstTs = ccTsToMs(rec?.timestamp)
			break
		}
	}
	if (firstTs === undefined) firstTs = createdAtFallback ?? Date.now()
	for (const [i, ce] of cfgEvents.entries()) {
		events.push({ type: ce.type, seq: seq++, time: firstTs + i, data: ce.data })
	}

	const push = (etype, data, ts, isSurface) => {
		const ev = { type: etype, seq: seq++, time: ts, data }
		if (isSurface) ev.surfaceOp = "append"
		events.push(ev)
	}

	// ---- turn/step 生命周期 (dsh 判 blank = 会话无 turn/start; 必须补齐) ----
	// step 边界贴近真实 dsh: user 文本开新 turn+step; tool-result 归属当前 step 并关闭;
	// 下一条 assistant 开新 step。
	let turn = 0
	let step = 0
	let turnOpen = false
	let stepOpen = false
	let surfaceCount = 0
	let firstUserText
	let firstUserSeq

	const openTurn = (ts) => {
		turn += 1
		step = 0
		push("turn/start", { turn }, ts, false)
		turnOpen = true
	}
	const openStep = (ts) => {
		step += 1
		push("step/start", { turn, step }, ts, false)
		stepOpen = true
	}
	const closeStep = (ts) => {
		if (stepOpen) {
			push("step/end", { turn, step }, ts, false)
			stepOpen = false
		}
	}
	const closeTurn = (ts) => {
		closeStep(ts)
		if (turnOpen) {
			push("turn/end", { turn, reason: { kind: "completed" } }, ts, false)
			turnOpen = false
		}
	}
	const openTurnWithStep = (ts) => {
		closeTurn(ts)
		openTurn(ts)
		openStep(ts)
	}

	// 遍历 CC 记录 → dsh 事件
	for (const rec of records) {
		if (typeof rec !== "object" || rec === null) continue
		const rtype = rec.type
		const ts = tsOf(rec)
		if (rtype === "user") {
			const content = rec.message?.content
			const textParts = []
			const toolResultBlocks = []
			if (typeof content === "string") {
				if (content.trim()) textParts.push(content)
			} else if (Array.isArray(content)) {
				for (const blk of content) {
					if (typeof blk !== "object" || blk === null) continue
					if (blk.type === "text" && typeof blk.text === "string" && blk.text.trim()) textParts.push(blk.text)
					else if (blk.type === "tool_result") toolResultBlocks.push(blk)
				}
			}
			if (textParts.length > 0) {
				// 用户真实输入 → 新 turn
				openTurnWithStep(ts)
				push(
					"user/message",
					{
						content: [{ type: "text", text: textParts.join("\n") }],
						source: { kind: "user" },
						role: "user",
						id: `ccu-${seq}`,
					},
					ts,
					true,
				)
				surfaceCount += 1
				if (firstUserText === undefined) {
					firstUserText = textParts.join("\n").trim()
					firstUserSeq = seq - 1
					// 侧边栏标题 (取第一条用户文本), 紧跟首条 user/message
					push(
						"session/title",
						{
							title: safeSlice(firstUserText, 60),
							messageSeqs: [firstUserSeq],
							source: { kind: "fallback" },
						},
						ts,
						false,
					)
				}
			}
			for (const blk of toolResultBlocks) {
				// 工具结果 → 归属当前 step, 写入后关闭 step (下一条 assistant 开新 step)
				if (!turnOpen) openTurn(ts)
				if (!stepOpen) openStep(ts)
				const toolUseId = blk.tool_use_id ?? ""
				push(
					"tool/result",
					{
						turn,
						step,
						message: {
							role: "user",
							content: [
								{
									type: "tool-result",
									toolCallId: toolUseId,
									content: [{ type: "text", text: ccToolContentText(blk) }],
									isError: Boolean(blk.is_error),
								},
							],
							source: { kind: "tool", callId: toolUseId },
							id: `cct-${seq}`,
						},
					},
					ts,
					true,
				)
				surfaceCount += 1
				closeStep(ts)
			}
		} else if (rtype === "assistant") {
			const msg = rec.message ?? {}
			const dshBlocks = (Array.isArray(msg.content) ? msg.content : []).map(ccBlockToDsh).filter(Boolean)
			if (dshBlocks.length > 0) {
				if (!turnOpen) openTurn(ts)
				if (!stepOpen) openStep(ts)
				push(
					"assistant/message",
					{
						turn,
						step,
						message: {
							role: "assistant",
							content: dshBlocks,
							source: {
								kind: "model",
								provider: "claude-code-import",
								model: typeof msg.model === "string" && msg.model ? msg.model : "claude-import",
							},
							id: `cca-${seq}`,
						},
					},
					ts,
					true,
				)
				surfaceCount += 1
			}
		}
	}

	if (surfaceCount === 0) return undefined

	// 收尾: 关闭最后一个 step/turn — 转换器保证 turn 完整闭合,
	// 因此 coordinator 的 interruptedTurnClosers 不会补合成事件 (commitRepair 不触发)
	closeTurn(lastValidTs ?? firstTs)

	return { meta: synthesizeMeta({ records, id, cwdFallback, createdAtFallback }), events }
}

// ---------- dsh → CC ----------
/** dsh content block → CC block; 无法映射返回 undefined (image 等跨 harness 不迁移) */
function dshBlockToCc(block) {
	switch (block?.type) {
		case "text":
			return { type: "text", text: typeof block.text === "string" ? block.text : "" }
		case "reasoning":
			return { type: "thinking", thinking: typeof block.text === "string" ? block.text : "" }
		case "tool-call": {
			let input = {}
			try {
				input = JSON.parse(block.arguments ?? "{}")
			} catch {
				input = {}
			}
			return { type: "tool_use", id: block.id, name: block.name, input }
		}
		default:
			return undefined
	}
}

/** dsh tool-result content → CC tool_result 文本块 (扁平化 + 截断 30000) */
function dshResultText(content, maxLen = 30000) {
	const out = []
	for (const b of content ?? []) {
		if (b?.type === "text") {
			let txt = typeof b.text === "string" ? b.text : ""
			if (txt.length > maxLen) txt = `${safeSlice(txt, maxLen)}\n...[截断 ${txt.length - maxLen} 字符]`
			out.push({ type: "text", text: txt })
		} else if (b?.type === "image") {
			out.push({ type: "text", text: "[图片附件: 跨 harness 未迁移]" })
		}
	}
	return out.length > 0 ? out : [{ type: "text", text: "(空结果)" }]
}

// ---- CC 记录构建器 (字段对齐一号 _cc_user_text/_cc_tool_result/_cc_assistant) ----
function ccBase(type, cwd, sessionId, ts, version) {
	return {
		type,
		uuid: randomUUID(),
		timestamp: msToCcTs(ts),
		isSidechain: false,
		userType: "external",
		entrypoint: "cc-session-persistence",
		cwd,
		sessionId,
		version,
	}
}

function ccUserRecord(text, cwd, sessionId, ts, version) {
	return { ...ccBase("user", cwd, sessionId, ts, version), message: { role: "user", content: text } }
}

function ccToolResultRecord(toolUseId, contentBlocks, isError, cwd, sessionId, ts, version) {
	return {
		...ccBase("user", cwd, sessionId, ts, version),
		message: {
			role: "user",
			content: [{ type: "tool_result", tool_use_id: toolUseId, content: contentBlocks, is_error: isError }],
		},
	}
}

function ccAssistantRecord(contentBlocks, model, cwd, sessionId, ts, version) {
	return {
		...ccBase("assistant", cwd, sessionId, ts, version),
		message: {
			id: randomUUID(),
			type: "message",
			role: "assistant",
			model,
			content: contentBlocks,
			stop_reason: "end_turn",
			stop_sequence: null,
		},
	}
}

/**
 * dsh 事件 (live 写路径批次) → CC 记录数组。
 * 只转录 3 种 surface 事件; end-seed/cfg/turn/step/title 等丢弃。
 * surfaceOp 为 replace 的事件也按 append 视角转录 (同一号 dsh2cc 行为: 编辑以新记录呈现)。
 * @param {object} opts
 * @param {object[]} opts.events dsh 事件 (seq 连续)
 * @param {string} opts.sessionId
 * @param {string} [opts.cwd] CC 记录 cwd 字段
 * @param {string} [opts.version] CC 记录 version 字段 (默认 2.1.223)
 */
export function dsh2ccRecords({ events, sessionId, cwd, version = "2.1.223" }) {
	const records = []
	for (const ev of events ?? []) {
		const etype = ev?.type
		if (!SURFACE_TYPES.has(etype)) continue
		const ts = Number.isFinite(ev.time) ? ev.time : Date.now()
		const data = ev.data ?? {}
		if (etype === "user/message") {
			const textParts = (Array.isArray(data.content) ? data.content : [])
				.filter((b) => b?.type === "text")
				.map((b) => (typeof b.text === "string" ? b.text : ""))
			if (textParts.length > 0) records.push(ccUserRecord(textParts.join("\n"), cwd, sessionId, ts, version))
		} else if (etype === "assistant/message") {
			const msg = data.message
			if (msg === undefined || msg === null) continue
			const blocks = (Array.isArray(msg.content) ? msg.content : []).map(dshBlockToCc).filter(Boolean)
			if (blocks.length > 0) records.push(ccAssistantRecord(blocks, msg.source?.model ?? "unknown", cwd, sessionId, ts, version))
		} else if (etype === "tool/result") {
			const msg = data.message
			if (msg === undefined || msg === null) continue
			let toolUseId = ""
			let inner = []
			let isError = false
			for (const b of Array.isArray(msg.content) ? msg.content : []) {
				if (b?.type === "tool-result") {
					toolUseId = b.toolCallId ?? ""
					inner = b.content ?? []
					isError = Boolean(b.isError)
				}
			}
			records.push(ccToolResultRecord(toolUseId, dshResultText(inner), isError, cwd, sessionId, ts, version))
		}
	}
	return records
}
