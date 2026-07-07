// .opencode/plugin/orca.ts —— in-session shell opencode plugin（SPEC v7 §2.2/§2.5/§2.6）
//
// **架构守门**（D-v7-1）：本 plugin 是**哑传输**。零 Orca 业务逻辑：
//   - 不调 advance/router/replay/tape 路径
//   - 除「剥 task_id 首行 + 解 <task_result> 包装」的扁平化提取（SPEC §2.5）外，
//     不做任何 Orca 业务判断（合规计数 / 失败 taxonomy 全在 CLI）
//   - 不持任何 Orca 状态：run_id / tape / yaml 全在激活 marker 文件里，由 CLI 维护；
//     plugin 仅读 marker 的 JSON 顶层字段（run_id / tape_path）作为透传 argv。
//
// 只做四件事（SPEC §2.6 多 command 架构）：
//   1. session.idle event hook（仅主 session，D-v7-5 子 session 过滤 + in-flight mutex F5）
//      → 经 client 拉最后 assistant 的 task ToolPart.state.output（D-v7-4）
//      → 读 marker（顶层字段 run_id / tape_path）→ spawn `orca in-session next` → promptAsync 注入
//   2. command.execute.before 拦截 /orca* → spawn 对应 CLI 子命令 → 改 output.parts
//   3. session.idle 里调 `next` CLI 后读 stdout JSON 顶层字段（done/node/prompt/reason）
//   4. command.execute.before 触发 bootstrap 时把 sessionID 作 owner 写进 marker filename

import type { Plugin } from "@opencode/core/plugin"
import { client } from "@opencode/core/client"

// in-flight mutex（F5 闭环）：防 await promptAsync 期间下一 idle 重入并发 spawn 两 CLI 撞 flock
const injecting: Set<string> = new Set()

interface Marker {
  run_id: string
  tape_path: string
  owner: string
  yaml?: string
  model?: string
  session_id?: string
  no_output_count?: number
}

async function spawnCli(args: string[]): Promise<any> {
  // 哑传输：spawn CLI 子进程 + 读 stdout JSON 顶层字段。零业务逻辑。
  const p = Bun.spawn(["orca", "in-session", ...args], {
    stdout: "string",
    stderr: "inherit",
  })
  const out = await new Response(p.stdout).text()
  try {
    return JSON.parse(out)
  } catch {
    return null
  }
}

async function readMarker(sessionID: string): Promise<Marker | null> {
  // marker 文件名 = runs/orca-<owner>.json；owner=sessionID for opencode
  const path = `runs/orca-${sessionID}.json`
  try {
    const text = await Bun.file(path).text()
    return JSON.parse(text) as Marker
  } catch {
    return null
  }
}

// 从最后 assistant message 的 ToolPart(tool=task, state.status=completed).state.output
// 提取（D-v7-4，spec-review r2 F10 闭环）。剥 task_id: 首行 + 解 <task_result> 包装。
// 守门：此处的「task output 提取」是宿主侧 payload 扁平化（不是 Orca 业务逻辑）——
// SPEC §2.5 明确把「从 tool_result 提取 output」划为宿主侧职责。
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

export const orcaPlugin: Plugin = () => {
  return {
    id: "orca",
    hooks: {
      event: async (event: any, _ctx: any) => {
        // 仅处理 session.idle
        if (event.type !== "session.idle") return

        const sessionID = event.properties?.sessionID
        if (!sessionID) return

        // 子 session 过滤（D-v7-5）+ 主 session 绑定：marker.run_id 存在 = 本 session 有活跃 run
        const marker = await readMarker(sessionID)
        if (!marker) return  // 非激活 session（子 session / 未 /orca 的 session）→ passthrough

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
          const reply = await spawnCli(args)
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

      // command.execute.before 拦截 /orca* → spawn 对应 CLI 子命令 → 改 output.parts
      // 守门：plugin 核心 idle 循环零改；加 command 只动此处 + .md + CLI 三处（SPEC §2.6）。
      "command.execute.before": async (event: any) => {
        const cmd = event?.properties?.command
        if (!cmd || !cmd.startsWith("/orca")) return

        const sessionID = event?.properties?.sessionID
        const parts = cmd.split(/\s+/)
        const sub = parts[1] ?? "run"   // /orca run | status | stop

        if (sub === "run") {
          const wf = parts[2] ?? ""
          if (!wf || !sessionID) return
          // spawn bootstrap（CLI 写 marker；plugin 透传 sessionID 作 owner + session_id）
          const reply = await spawnCli([
            "bootstrap", wf,
            "--owner", sessionID,
            "--session-id", sessionID,
          ])
          if (reply && reply.prompt) {
            const [providerID, modelID] = (reply.model ?? "deepseek/deepseek-v4-flash").split("/")
            await client.session.promptAsync({
              path: { id: sessionID },
              body: {
                parts: [{ type: "text", text: reply.prompt }],
                model: { providerID, modelID },
              },
            })
          }
        } else if (sub === "status" || sub === "stop") {
          // status/stop 仅回显 stdout（不注入主 session）
          const marker = await readMarker(sessionID)
          const rid = marker?.run_id ?? parts[2] ?? sessionID ?? ""
          const reply = await spawnCli([sub, rid])
          // 把 CLI 输出回显给用户（不进入 workflow 推进循环，仅显示）
          if (reply) {
            const text = typeof reply === "string"
              ? reply
              : JSON.stringify(reply, null, 2)
            await client.session.promptAsync({
              path: { id: sessionID },
              body: {
                parts: [{ type: "text", text: `[orca ${sub}] ${text}` }],
                model: { providerID: "deepseek", modelID: "deepseek-v4-flash" },
              },
            })
          }
        }
      },
    },
  }
}

export default orcaPlugin
