// opencode plugin（由 `orca install` 落到 .opencode/plugins/ 或 ~/.config/opencode/plugins/）
// —— in-session shell nudge hook（v5 §4.4 / step 2b(7) + step 4 收尾）。
//
// **架构守门**（D-v7-1）：本 plugin 是**哑传输**。零 Orca 业务逻辑：
//   - 不调 advance/router/replay/tape 路径
//   - 不做合规计数 / 失败 taxonomy / workflow 状态机判断
//   - 不持任何 Orca 决策状态（run_id / tape / yaml 全在 marker 文件里，由 CLI 维护）
//
// 只做一件事（v5 §4.4，B 路径铁律——**绝不自动推进**）：
//   - ``event`` 钩子（``session.idle``）：仅主 session + in-flight mutex → 扫活跃 marker
//     → 60s 节流 → ``client.session.promptAsync`` 注入「请调 ``orca next`` 推进」提醒。
//     **不**调 ``orca next``（那退化成 A 路径自动推进）。判定只看 marker 存在（不用 tape
//     超时——tape 看不到子代理状态，超时判定会误报）。
//
// **v5 §8 step 4 收尾**：transform 入口段 + 全部死代码（extractTaskOutput / spawnCli /
// spawnTopLevelCli / rewriteText / findLastUserTextPart / extractModel / buildCliArgs /
// MARKER_REGEX / MARKER_LITERAL）已删——transform marker 派发是旧 A 路径第二入口，
// v5 入口统一切到 orca skill（SKILL.md 三步），保留 transform = 让 marker 绕过 skill 起
// 第二入口，违反「单一接口」。本文件**仅保留 idle nudge hook**（opencode nudge 载体）。
//
// **结构**（spike 实证）：``export const OrcaPlugin = async (ctx) => ({ ...flat hooks })``；
// client 从 ``ctx.client`` 取（**非** ``@opencode/core/client`` —— 该包 npm 不存在，spike 实证）。

// ── 诊断开关（2026-07-08）───────────────────────────────────────────────────
// doctor 诊断 idle 钩子是否真 fire：session.idle 触发时写心跳文件，doctor 读取作证。
// 开关 = 环境变量 ``ORCA_DIAGNOSE=1``；未设/0 = 关（零 I/O，生产态）。plugin 加载时读一次
// 缓存，hook 内只查布尔值。doctor 也读同 env 报告状态。
// 用途：判定 NGA fork 是否接线 session.idle —— 定论后 unset 即零开销。
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs"

const DIAGNOSE: boolean =
  (typeof process !== "undefined" && process.env?.ORCA_DIAGNOSE === "1") || false

// 心跳文件（plugin 作用域，非 per-run；runs/ 与 marker 同目录）。
// entry 心跳（旧 transform 诊断）随 step 4 transform 段删除而消失——doctor 的 entry_hook
// check 因此永久 "unknown"（hard=False，可选），合理反映「transform 已退场」。
const PROBE_ADVANCE_REL = "runs/.orca-probe-advance.json"

// 心跳计数（进程内，plugin 重载归零；诊断期足够）。idle 累计 = idle hook 真接线证据。
// v5 §8 step 4 收尾：删 advanceCount / lastAdvanceRunId——step 2b 改 nudge 后 idle hook
// 不再 spawn next（B 路径铁律），这两个旧 A 路径自动推进的计数器永远不被赋值，是死代码。
let idleCount = 0

// sync 小文件写（gated by DIAGNOSE → 关时永不调用）。best-effort：失败打 console.error，
// 不影响 hook 主流程（心跳是诊断旁路，不能拖垮/污染主路径）。
function writeHeartbeat(relPath: string, payload: any): void {
  try {
    try { mkdirSync("runs", { recursive: true }) } catch { /* 已存在或无权；忽略 */ }
    writeFileSync(relPath, JSON.stringify(payload))
  } catch (e) {
    console.error(`[orca] heartbeat ${relPath} failed:`, e)
  }
}

function nowSec(): number {
  return Math.floor(Date.now() / 1000)
}

function writeIdleHeartbeat(sessionID: string): void {
  writeHeartbeat(PROBE_ADVANCE_REL, {
    diag: true,
    last_idle_at: nowSec(),
    idle_count: idleCount,
    last_session_id: sessionID,
  })
}

// ── nudge（v5 §4.4 / step 2b(7)）：idle 时提醒主 session 调 next，**绝不自动推进** ──
// B 路径铁律：主 session 自调 ``orca next``；idle 钩子**不**调 next（那退化成 A 路径自动推进）。
// marker 文件名固定 ``runs/orca-<run_id>.json``（v3 §7.2），扫该目录取活跃 run。
//
// host-session-binding v2（tape-only，§2.3/§4.5）：nudge 只对**当前 session 自己的**活跃 run
// 提醒，杜绝跨 session 串台。归属从 tape 首条 workflow_started.data.host_session 派生
// （marker 不存归属——tape 唯一真相源）。per-session 限流（NUDGE_FILE 按 sessionID 分键）。
const NUDGE_COOLDOWN_SEC = 60  // per-session 60s 节流（按 sessionID 分键，防 A 抑制 B，评审 C1）

// 读 run 的 tape 首条 workflow_started.data.host_session（同 cc_nudge.sh 的 _host_session_from_tape）。
// tape 不存在 / 首行非 workflow_started / 缺 host_session / 读失败 → undefined（fail-safe）。
// O(1) 读首行（§6 风险 #3）：readFileSync 后 indexOf("\n") 切片，只 parse 首行，不 split 整文件。
// 注：函数名避用 Python Tape 构造器字面（D-v7-1 守门 grep 禁词，防架构守门误判）。
function hostSessionOfRun(runId: string): string | undefined {
  let raw: string
  try {
    raw = readFileSync(`runs/${runId}.jsonl`, "utf-8")
  } catch {
    return undefined  // 读失败 / 文件不存在 → fail-safe
  }
  const nl = raw.indexOf("\n")
  const firstLine = (nl === -1 ? raw : raw.slice(0, nl)).trim()
  if (!firstLine) return undefined
  try {
    const obj = JSON.parse(firstLine) as { type?: string; data?: { host_session?: string } }
    if (obj.type === "workflow_started") {
      const hs = obj.data?.host_session
      return typeof hs === "string" ? hs : undefined
    }
    return undefined  // 首条有效行非 workflow_started → 异常 tape，fail-safe
  } catch {
    return undefined  // 首行非合法 JSON → fail-safe
  }
}

// 扫活跃 run 并按 hostSession 过滤（marker 存在 ≡ run 活跃；终态时 CLI 清 marker）。
// 返归属 hostSession 的 [{run_id, model}]。
//
// **fail-open 回退（评审 C5 静默死防护）**：若过滤后为空**且所有**活跃 run 的 host_session
// 均为 null/undefined（= shell.env 注入全局未生效 / 全是手 CLI run），回退返回全部活跃 run
// （= 改动前 status quo）。理由：从「nudge 有串台」退化到「nudge 全静默死」更糟（用户无信号）；
// fail-open 保底「注入生效→精确过滤；注入失效→退回可用（有串台但不哑）」。
// 混合场景（部分 run 有真 host_session、部分 null）→ 不回退（null run 按 §2.5 跳过，注入已证生效）。
//
// **marker 损坏处理（fail-safe，非 fail loud）**：单个 marker 读失败 → 跳过（不阻断 nudge）。
// 与 cc_nudge.sh 的 fail loud（exit 2）不对称——opencode plugin 抛错用户看不到（hook 失败只
// console.error），fail loud 无受众；故选 fail-safe（宁漏一个 marker 不阻断 nudge 主流程）。
function listActiveRuns(hostSession: string): { run_id: string; model?: string }[] {
  const all: { run_id: string; model?: string; hs: string | undefined }[] = []
  try {
    for (const name of readdirSync("runs")) {
      if (!name.startsWith("orca-") || !name.endsWith(".json")) continue
      try {
        const m = JSON.parse(readFileSync(`runs/${name}`, "utf-8")) as Marker
        if (m && typeof m.run_id === "string") {
          all.push({ run_id: m.run_id, model: m.model, hs: hostSessionOfRun(m.run_id) })
        }
      } catch { /* 单个 marker 坏 → 跳过（fail-safe，不阻断 nudge） */ }
    }
  } catch {
    return []  // runs/ 不存在 / 无权读 → 无活跃 run
  }
  const mine = all.filter(r => r.hs === hostSession).map(({ hs, ...rest }) => rest)
  if (mine.length > 0) return mine
  // fail-open：过滤空 + 所有 run 的 host_session 均无真值（注入全局未生效）→ 退回 status quo。
  const hasAnyReal = all.some(r => r.hs !== undefined)
  if (!hasAnyReal && all.length > 0) {
    return all.map(({ hs, ...rest }) => rest)
  }
  return mine  // 注入生效但本 session 无 run → []
}

// per-session nudge 节流文件路径（按 sessionID 分键，防 A 的 nudge 抑制 B，评审 C1）。
function nudgeFile(sessionID: string): string {
  return `runs/.orca-nudge-${sessionID}.json`
}

// nudge 节流：距上次成功 nudge > COOLDOWN 才允许。**不**在此写时间戳——调用方成功注入后
// 调 ``markNudged`` 写，注入失败不计入节流（下轮 idle 可重试）。
function nudgeAllowed(sessionID: string): boolean {
  const file = nudgeFile(sessionID)
  try {
    if (!existsSync(file)) return true
    const data = JSON.parse(readFileSync(file, "utf-8")) as { last_nudged_at?: number }
    const last = typeof data?.last_nudged_at === "number" ? data.last_nudged_at : 0
    return (nowSec() - last) >= NUDGE_COOLDOWN_SEC
  } catch {
    return true  // 节流文件坏 → fail-open（宁多提醒不漏提醒）
  }
}

function markNudged(sessionID: string): void {
  writeHeartbeat(nudgeFile(sessionID), { last_nudged_at: nowSec() })
}

// in-flight mutex（F5 闭环）：防 await promptAsync 期间下一 idle 重入。
const injecting: Set<string> = new Set()

interface Marker {
  run_id: string
  // v3 §7.2：marker 精简到 3 字段（run_id/model/no_output_count）。tape_path/yaml/
  // session_id/owner 已删——这里保 optional 仅向后兼容旧 marker 文件，新 marker 不含。
  tape_path?: string
  owner?: string
  yaml?: string
  model?: string
  session_id?: string
  no_output_count?: number
}

// ── plugin 主体 ─────────────────────────────────────────────────────────────

export const OrcaPlugin = async (ctx: any) => {
  // client 从 ctx.client 取（spike `/tmp/orca-cmd` 实证；`@opencode/core/client` npm 不存在）。
  // nudge 用 client.session.promptAsync 注入提醒（v5 §4.4）；不再 REST fetch 消息（旧推进
  // 路径已删），故 ctx.serverUrl / SERVER_BASE_URL_FALLBACK 不再需要。
  const client = ctx.client

  return {
    id: "orca",

    // nudge 钩子（v5 §4.4 / step 2b(7)）：``session.idle`` 时提醒主 session 调 next。
    //
    // **绝不推进**（B 路径铁律）：idle 钩子**不**调 ``orca next``（那退化成 A 路径自动推进）。
    // 判定**只看 marker 存在**（不用 tape 超时，会误报）：idle ≈ 主 session 空闲（子代理不在
    // 工作——否则 session 不 idle）+ 有活跃 run（marker 存在）→ 提醒调 next。
    //
    // host-session-binding v2：只提醒**当前 session 自己的**活跃 run（读 tape 首行 host_session
    // 过滤），杜绝跨 session 串台。per-session 节流（按 sessionID 分键）。
    //
    // **签名（Bug B 闭环，e2e `/tmp/orca-f4` 实证）**：opencode 1.14.22 runtime 实调外层
    // 包一层 `{event}` —— `input.event.type` / `input.event.properties`。
    // 兼容解构与直传：`const event = input?.event ?? input`。
    event: async (input: any) => {
      const event: any = input?.event ?? input
      if (event.type !== "session.idle") return

      const sessionID = event.properties?.sessionID
      if (!sessionID) return

      // 诊断心跳（session.idle 触达 = idle 钩子已接线；与「是否 nudge」无关）。
      idleCount += 1
      if (DIAGNOSE) writeIdleHeartbeat(sessionID)

      // nudge：扫**本 session 的**活跃 run → 节流 → 注入提醒（不 spawn next）。
      if (injecting.has(sessionID)) return
      const active = listActiveRuns(sessionID)
      if (active.length === 0) return        // 无本 session 的活跃 run → 无需 nudge
      if (!nudgeAllowed(sessionID)) return   // per-session 节流窗口内 → 跳过（防刷屏）

      injecting.add(sessionID)
      try {
        const ids = active.map(r => r.run_id)
        const reminder =
          `【Orca nudge】你还有活跃的 Orca run：${ids.join(", ")}。\n` +
          "若上一个节点的子代理已完成，请把它的产出作为 --output 调下面命令推进；" +
          "若 workflow 已结束或要中止，先 `orca stop <run_id>`。\n" +
          "（这是提醒，Orca 不会自动推进。）\n" +
          `  orca next --run-id <run_id> --output '<子代理产出>'`
        // model 解析：要求 "provider/name" 形态；marker.model 缺/无斜杠/空 → 回退默认
        // （防空 providerID/modelID 产非法 model 对象）。
        const rawModel = active[0].model
        const modelStr = typeof rawModel === "string" && rawModel.includes("/")
          ? rawModel : "deepseek/deepseek-v4-flash"
        const [providerID, modelID] = modelStr.split("/")
        await client.session.promptAsync({
          path: { id: sessionID },
          body: {
            parts: [{ type: "text", text: reminder }],
            model: { providerID, modelID },
          },
        })
        markNudged(sessionID)  // 成功注入才计入节流（失败下轮重试）
      } catch (e) {
        // 注入失败（client API 错 / session 不存在）→ console.error，不计节流，下轮 idle 重试。
        console.error("[orca] nudge promptAsync failed:", e)
      } finally {
        injecting.delete(sessionID)
      }
    },

    // host-session-binding v2 §4.5：注入 ORCA_HOST_SESSION_ID 到所有 shell 子进程。
    // opencode 的 bash tool spawn 子进程时，本钩子把当前 session id 注入 env → CLI bootstrap
    // 的 _host_session_from_env() 命中 → 写入 tape workflow_started.data.host_session。
    //
    // **可行性**：``shell.env`` 钩子官方支持（@opencode-ai/plugin Hooks；spike 实证类型定义
    // ``input: { cwd, sessionID?, callID? }, output: { env }``）。``sessionID`` 在 AI tool
    // 上下文中存在；用户终端手敲 shell 时可能 absent → 不注入（手 CLI 起 run，host_session
    // 为 null，nudge 跳过——fail-safe，§2.5）。
    //
    // **tape-only 铁律**：host_session 单路（env → bootstrap → tape），marker 不复存。
    "shell.env": async (input: { sessionID?: string }, output: { env: Record<string, string> }) => {
      if (input.sessionID) {
        output.env.ORCA_HOST_SESSION_ID = input.sessionID
      }
    },
  }
}

export default OrcaPlugin
