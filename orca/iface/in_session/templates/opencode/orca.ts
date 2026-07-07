// .opencode/plugin/orca.ts —— in-session shell opencode plugin
// SPEC v8（`docs/specs/in-session-shell-design-draft.md` §2.6 / §2.6.1 / §2.6.2 / §2.5）
//
// **架构守门**（D-v7-1，§2.6）：本 plugin 是**哑传输**。零 Orca 业务逻辑：
//   - 不调 advance/router/replay/tape 路径
//   - 不做合规计数 / 失败 taxonomy / workflow 状态机判断
//   - 不持任何 Orca 决策状态（run_id / tape / yaml 全在激活 marker 文件里，由 CLI 维护）
//
// 只做四件事（哑传输边界）：
//   1. `experimental.chat.messages.transform` 入口钩子：marker 检测 → spawn 对应 CLI 子命令
//      → 按 §2.6.2 提取 stdout JSON 顶层字段替换该 text part（非整 JSON 替换）。
//   2. `event` 钩子（session.idle）：仅主 session（D-v7-5 子 session 过滤）+ in-flight mutex
//      → 从最后 assistant 的 task ToolPart.state.output 提取（§2.5 D-v7-4 扁平化）→ spawn `next`
//      → promptAsync 注入下一 prompt。
//   3. spawn CLI + parse JSON 顶层字段。
//   4. 从 marker JSON 顶层字段（run_id / tape_path / model / session_id）透传 argv。
//
// **v8 改动**（推翻 v7 的 `command.execute.before` 入口）：spike `/tmp/orca-cmd` +
// `/tmp/orca-xform` 实证 `command.execute.before` 在 opencode 1.14.22 runtime **不触发**；
// `experimental.chat.messages.transform` 触发且能改写送 LLM 的消息数组（模型未见原文）。
//
// **结构**（spike 实证）：`export const OrcaPlugin = async (ctx) => ({ ...flat hooks })`；
// client 从 `ctx.client` 取（**非** `@opencode/core/client` —— 该包 npm 不存在，spike 实证）；
// `Bun.spawnSync({stdout:"pipe",stderr:"pipe"})`（**非** `spawn`+`stdout:"string"`）。

// SPEC §2.6.1 marker regex —— 与 Python `orca/iface/in_session/templates/_constants.py`
// 的 MARKER_REGEX 字面同步。行首/行尾锚定 + 子命令名 \w+ + args 非贪婪 [^>\n]*?。
const MARKER_REGEX = /^<!--\s*orca:cmd\s+(\w+)(?:\s+([^>\n]*?))?\s*-->$/

// SPEC §2.6.1 一次性消费：替换文本不得含本字面。
const MARKER_LITERAL = "<!--orca:cmd"

// in-flight mutex（F5 闭环）：防 await promptAsync 期间下一 idle 重入并发 spawn 两 CLI 撞 flock。
const injecting: Set<string> = new Set()

interface Marker {
  run_id: string
  tape_path: string
  owner?: string
  yaml?: string
  model?: string
  session_id?: string
  no_output_count?: number
}

interface CliReply {
  [k: string]: any
}

// 哑传输：spawn CLI 子进程 + 读 stdout JSON 顶层字段。零业务逻辑。
// spike `/tmp/orca-cmd` 实证 `Bun.spawnSync({stdout:"pipe",stderr:"pipe"})` 是合法形态
// （v7 的 `Bun.spawn({stdout:"string"})` 在 opencode 内嵌 Bun runtime 非法）。
//
// Fail loud（SPEC 鲁棒性底线）：检查 exitCode，非 0 时把 stderr 首 400 字符回显，
// 不静默吞错（与 CLAUDE.md「报错处理：重试/失败原因必须用户可见」一致）。
function spawnCli(args: string[]): CliReply | null {
  let r: any
  try {
    r = Bun.spawnSync(["orca", "in-session", ...args], {
      stdout: "pipe",
      stderr: "pipe",
    })
  } catch (e) {
    console.error("[orca] spawn orca in-session failed:", e)
    return null
  }
  const stderr = (r.stderr && r.stderr.toString()) ?? ""
  if (r.exitCode !== 0) {
    // CLI 失败 → fail loud：把 stderr 首 400 字符作为「错误回显」文本返（非 null），
    // 让 transform 把错误信息替换进 user text（一次性消费：错误串无 marker 字面）。
    const tail = stderr.trim().slice(0, 400)
    return { __orca_error: true, exitCode: r.exitCode, stderr: tail }
  }
  const out = (r.stdout && r.stdout.toString()) ?? ""
  try {
    return JSON.parse(out)
  } catch {
    return { __orca_error: true, exitCode: 0, stderr: `non-JSON stdout: ${out.slice(0, 200)}` }
  }
}

async function readMarker(sessionID: string): Promise<Marker | null> {
  // marker 文件名 = runs/orca-<owner>.json；owner=sessionID for opencode（§5）。
  const path = `runs/orca-${sessionID}.json`
  try {
    const text = await Bun.file(path).text()
    return JSON.parse(text) as Marker
  } catch {
    return null
  }
}

// 从最后 assistant message 的 ToolPart(tool=task, state.status=completed).state.output
// 提取（D-v7-4，§2.5 spec-review r2 F10 闭环）。剥 task_id: 首行 + 解 <task_result> 包装。
// 守门：此处的「task output 提取」是宿主侧 payload 扁平化（SPEC §2.5 划为宿主侧职责），
// 不是 Orca 业务逻辑（合规计数 / 状态机 / 路由判断）。
function extractTaskOutput(parts: any[]): string | null {
  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i]
    if (
      p &&
      p.type === "tool" &&
      p.tool === "task" &&
      p.state &&
      p.state.status === "completed" &&
      typeof p.state.output === "string"
    ) {
      // 剥 task_id: 首行（spike-2 F10 实证 payload 形态）
      let s = p.state.output
      const idx = s.indexOf("\n")
      if (idx >= 0 && s.slice(0, idx).trim().startsWith("task_id:")) {
        s = s.slice(idx + 1)
      }
      // 解 <task_result>…</task_result> 内文
      const m = s.match(/<task_result>([\s\S]*?)<\/task_result>/)
      if (m) s = m[1].trim()
      return s.trim() || null
    }
  }
  return null
}

// SPEC §2.6.2 改写语义：按子命令从 stdout JSON 提取顶层字段作替换文本。
// 禁止整 JSON 字面替换（B1 闭环：模型见 `{"run_id":...}` 困惑）。
function rewriteText(sub: string, reply: CliReply): string | null {
  if (!reply) return null
  // spawnCli 失败信封（exitCode 非 0 / 非 JSON）：fail loud 回显 stderr。
  if (reply.__orca_error) {
    return `[orca] ${sub} failed (exit ${reply.exitCode}): ${reply.stderr}`
  }
  if (sub === "run") {
    // bootstrap：取 .prompt（entry 节点 prompt）
    return typeof reply.prompt === "string" ? reply.prompt : null
  }
  if (sub === "doctor") {
    // doctor：取 .report
    return typeof reply.report === "string" ? reply.report : null
  }
  if (sub === "status") {
    // status：友好串（不直接 dump JSON）
    if (reply.ok === false && reply.reason) {
      return `[orca status] failed: ${reply.reason}`
    }
    const status = reply.status ?? "unknown"
    const node = reply.node_status ?? {}
    const done = reply.progress ?? ""
    return `[orca status] status=${status} progress=${done} node_status=${JSON.stringify(node)}`
  }
  if (sub === "stop") {
    // stop：ok + run_id 友好串
    if (reply.ok === false && reply.reason) {
      return `[orca stop] failed: ${reply.reason}`
    }
    const ok = reply.ok ?? false
    const rid = reply.run_id ?? ""
    return `[orca stop] ok=${ok} run_id=${rid}`
  }
  return null
}

// 从 transform 的 out.messages 取最后一条 role=user 的最后一个 type=text part 的 (text, sessionID)。
// SPEC §2.6.1 扫描范围；§2.6.2 sessionID 从 out.messages[i].info.sessionID 取。
function findLastUserTextPart(messages: any[]): {
  text: string
  part: any
  sessionID: string | null
  msgIdx: number
} | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (!m || m.info?.role !== "user") continue
    const parts: any[] = Array.isArray(m.parts) ? m.parts : []
    for (let j = parts.length - 1; j >= 0; j--) {
      const p = parts[j]
      if (p && p.type === "text" && typeof p.text === "string") {
        // sessionID 路径待 phase 实证，spike 未打印；先按 out.messages[i].info.sessionID 取（§2.6.2）。
        // 多种可能路径兜底（M3）。
        const sid =
          m.info?.sessionID ?? m.info?.sessionId ?? m.sessionID ?? m.sessionId ?? null
        return { text: p.text, part: p, sessionID: sid, msgIdx: i }
      }
    }
    // 已找到最后一条 user 消息，但其无 text part → 停（不再往前找，§2.6.1）
    return null
  }
  return null
}

// ── plugin 主体 ─────────────────────────────────────────────────────────────

export const OrcaPlugin = async (ctx: any) => {
  // client 从 ctx.client 取（spike `/tmp/orca-cmd` 实证；`@opencode/core/client` npm 不存在）。
  const client = ctx.client

  return {
    id: "orca",

    // 入口钩子（v8 换入，§2.6）：每次 LLM 调用前触发。marker 检测 → spawn CLI → 改写
    // 该 user text part（非整消息、非整 JSON）。无 marker → 透传（不动）。
    "experimental.chat.messages.transform": async (input: any) => {
      const out = input?.out ?? input
      const messages: any[] = out?.messages ?? []
      if (messages.length === 0) return input

      const found = findLastUserTextPart(messages)
      if (!found) return input

      // 行首/行尾锚定（§2.6.1）：marker 是 text part 的**整条**文本（非子串）。
      const m = found.text.match(MARKER_REGEX)
      if (!m) return input   // 无 marker → 透传

      const sub = m[1]
      const args = (m[2] ?? "").trim()
      const sid = found.sessionID

      // 派发到对应 CLI 子命令（§2.6.2）。
      // status/stop 若无 args 且有 sid → 查 marker 拿 run_id（plugin 透传，零业务逻辑）。
      let markerRunId: string | null = null
      if ((sub === "status" || sub === "stop") && !args && sid) {
        const mk = await readMarker(sid)
        markerRunId = mk?.run_id ?? null
      }
      const cliArgs = buildCliArgs(sub, args, sid, markerRunId)
      if (cliArgs === null) {
        // 未知子命令 / 缺关键 argv → 标记错误回显（一次性消费：替换文本无 marker 字面）
        found.part.text = `[orca] cannot dispatch: ${sub} (args=${args || "<empty>"})`
        return input
      }

      const reply = spawnCli(cliArgs)
      if (!reply) {
        found.part.text = `[orca] CLI spawn failed for: ${sub}`
        return input
      }

      // §2.6.2 改写语义：按子命令提取字段。
      const rewritten = rewriteText(sub, reply)
      if (rewritten === null) {
        found.part.text = `[orca] ${sub}: no expected field in CLI reply`
        return input
      }

      // 一次性消费保证（§2.6.1）：替换文本不得含 marker 字面。
      if (rewritten.includes(MARKER_LITERAL)) {
        found.part.text = rewritten.split(MARKER_LITERAL).join("`orca:cmd`")
      } else {
        found.part.text = rewritten
      }
      return input
    },

    // 驱动钩子（§2.2 / §2.5 / §5）：session.idle 时推进 workflow。
    event: async (event: any) => {
      if (event.type !== "session.idle") return

      const sessionID = event.properties?.sessionID
      if (!sessionID) return

      // 子 session 过滤（D-v7-5）+ 主 session 绑定：marker 存在 = 本 session 有活跃 run。
      const marker = await readMarker(sessionID)
      if (!marker) return   // 非激活 session → passthrough

      // in-flight mutex（F5）
      if (injecting.has(sessionID)) return
      injecting.add(sessionID)
      try {
        // 经 client 拉本 session 最后 assistant message（D-v7-4）
        const msgs = await client.session.message({ id: sessionID })
        const arr: any[] = Array.isArray(msgs) ? msgs : (msgs?.data ?? [])
        let output: string | null = null
        for (let i = arr.length - 1; i >= 0; i--) {
          const m = arr[i]
          if (m?.info?.role === "assistant" && Array.isArray(m.parts)) {
            output = extractTaskOutput(m.parts)
            if (output) break
          }
        }

        // spawn next（哑传输；--output 省略 → CLI branch 4 + 合规计数，B2）
        const args = ["next", "--tape", marker.tape_path, "--run-id", marker.run_id]
        if (output) args.push("--output", output)
        const reply = spawnCli(args)
        if (!reply) return

        if (reply.done) {
          // 终态：不再注入（CLI 已清 marker）
          return
        }
        if (reply.prompt) {
          const [providerID, modelID] = (marker.model ?? "deepseek/deepseek-v4-flash").split("/")
          await client.session.promptAsync({
            path: { id: sessionID },
            body: {
              parts: [{ type: "text", text: reply.prompt }],
              model: { providerID, modelID },
            },
          })
        }
      } finally {
        injecting.delete(sessionID)
      }
    },
  }
}

// 按 §2.6.2 构造各子命令的 argv（marker 派发分支）。
// 加新 command = 这里加 case + CLI 加子命令（§2.6 「两处」）。
function buildCliArgs(
  sub: string, args: string, sid: string | null, markerRunId: string | null,
): string[] | null {
  if (sub === "run") {
    // bootstrap：wf 路径在 args（必填）；sid 作 --owner + --session-id（§2.6.2 B4 闭环）
    const wf = args.trim()
    if (!wf) return null
    const out = ["bootstrap", wf]
    if (sid) {
      out.push("--owner", sid, "--session-id", sid)
    }
    return out
  }
  if (sub === "status") {
    // status：有 marker run_id 则查特定 run，否则列全部 run
    // --json：plugin 改写契约要求 stdout 是 JSON（SPEC §2.6.2，MAJOR-1 闭环）
    if (markerRunId) return ["status", markerRunId, "--json"]
    return ["status", "--json"]
  }
  if (sub === "stop") {
    // stop：marker 派发场景 plugin 无 args（用户只敲 /orca stop）→ 用 --owner <sid>
    // 查 marker 拿 run_id（CLI 端解析，§2.6.2，MAJOR-2 闭环）
    if (sid) return ["stop", "--owner", sid]
    // 用户在 args 里直接给 run_id（slash 路径）
    if (args) return ["stop", args.trim()]
    return null
  }
  if (sub === "doctor") {
    // doctor：无 argv
    return ["doctor"]
  }
  return null
}

export default OrcaPlugin
