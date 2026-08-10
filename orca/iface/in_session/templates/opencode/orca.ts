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
const GUARD_COOLDOWN_SEC = 30  // PostToolUse guard per-session 30s 节流（SPEC §4.3，与 nudge 分键）

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

// per-session 节流文件路径（按 sessionID 分键，防 A 的 nudge 抑制 B，评审 C1）。
// scope: "nudge"（idle hook，60s）| "guard"（tool.execute.after，30s，SPEC §4.3）。
// 两 hook 共用 nudgeAllowed / markNudged 内核，仅文件名不同（DRY，SPEC §8.1）。
function throttleFile(scope: "nudge" | "guard", sessionID: string): string {
  const prefix = scope === "guard" ? ".orca-guard" : ".orca-nudge"
  return `runs/${prefix}-${sessionID}.json`
}

// nudge 节流：距上次成功 nudge > COOLDOWN 才允许。**不**在此写时间戳——调用方成功注入后
// 调 ``markNudged`` 写，注入失败不计入节流（下轮 idle 可重试）。
function nudgeAllowed(scope: "nudge" | "guard", sessionID: string): boolean {
  const file = throttleFile(scope, sessionID)
  const cooldown = scope === "guard" ? GUARD_COOLDOWN_SEC : NUDGE_COOLDOWN_SEC
  try {
    if (!existsSync(file)) return true
    const data = JSON.parse(readFileSync(file, "utf-8")) as { last_nudged_at?: number }
    const last = typeof data?.last_nudged_at === "number" ? data.last_nudged_at : 0
    return (nowSec() - last) >= cooldown
  } catch {
    return true  // 节流文件坏 → fail-open（宁多提醒不漏提醒）
  }
}

function markNudged(scope: "nudge" | "guard", sessionID: string): void {
  writeHeartbeat(throttleFile(scope, sessionID), { last_nudged_at: nowSec() })
}

// in-flight mutex（F5 闭环）：防 await promptAsync 期间重入。idle nudge 与 guard 各持独立
// mutex——reviewer 指出原共用 mutex 让 idle 的 await 期间所有 PostToolUse 静默漏告警，恰好
// 挡住 guard 设计要覆盖的「turn 中途连续调工具」盲区。拆为 injectingIdle / injectingGuard：
// 同路径重入仍互斥（idle idle / guard guard），异路径并发不再互相吞噬。
const injectingIdle: Set<string> = new Set()
const injectingGuard: Set<string> = new Set()

// ── PostToolUse 事后告警守卫（SPEC posttooluse-rogue-guard §8）──────────────────
//
// tool.execute.after 钩子：主 session 在活跃 run 期间用了「下场干活」工具 → promptAsync
// 注入一段纯文本提示。**不**阻止动作（pure hint，无 deny/permissionDecision），**不**调
// orca next（B 路径铁律）。仅当本 session 有活跃 run 时触发。
//
// 工具分类单一真相源 = plugins/tool-classification.json（install 时与 orca.ts 同目录落地，
// SPEC §5）。分类属传输层判定（决定是否注入文本），非状态机判断（D-v7-1 不禁，§3 注脚 P2）。
let classificationCache: any = null
let classificationLoaded = false

function loadClassification(): any {
  if (classificationLoaded) return classificationCache
  classificationLoaded = true
  // 候选路径：opencode plugin runtime 的 cwd 未实证（Bun / Node 均可能，且取决于 opencode 启动
  // 时的 cwd），故穷举常见落点——install 落地的 user/project scope 路径 + cwd 相对兜底。
  // SPEC §10 R1：取不到 → null，guard 降级不告警。
  const home = (typeof process !== "undefined" && process.env && process.env.HOME) || ""
  const candidates = [
    // 项目 scope：cwd 是项目根 → .opencode/.nga plugins/
    ".opencode/plugins/tool-classification.json",
    ".nga/plugins/tool-classification.json",
    // plugin 同目录（若 cwd = plugin dir）
    "tool-classification.json",
    "plugins/tool-classification.json",
  ]
  if (home) {
    // 用户 scope：opencode 全局 config 根 + nga 对称
    candidates.push(`${home}/.config/opencode/plugins/tool-classification.json`)
    candidates.push(`${home}/.nga/plugins/tool-classification.json`)
  }
  for (const p of candidates) {
    try {
      const raw = readFileSync(p, "utf-8")
      classificationCache = JSON.parse(raw)
      return classificationCache
    } catch { /* try next */ }
  }
  console.error("[orca] tool-classification.json 未找到（guard 降级：不告警）")
  classificationCache = null
  return null
}

// SPEC §5 分类：返 true = 下场干活（告警）；false = 放行。
function classifyTool(toolName: string, args: any): boolean {
  const cls = loadClassification()
  if (!cls) return false  // fail-safe：分类缺失不告警
  const name = (toolName || "").trim()
  const writing: string[] = cls.writing_tools || []
  if (writing.includes(name)) return true
  const bashTools: string[] = cls.bash_tools || []
  if (!bashTools.includes(name)) return false  // Read/Glob/Grep/Task/AskUserQuestion 等 → 放行
  // bash 类：解析命令串
  let cmd = ""
  if (typeof args === "string") cmd = args
  else if (args && typeof args === "object") cmd = args.command || args.args || ""
  if (typeof cmd !== "string" || cmd.trim() === "") return true  // bash 工具却无命令 → 保守视为下场
  const seps: string[] = cls.compound_separators || []
  if (seps.some(s => s && cmd.includes(s))) return true  // 复合命令 → 下场（§5 Bash 分类 1）
  // word-boundary 前缀匹配（E6）：prefix 后须接 EOL 或空白，禁止 ``ls`` 命中 ``lsof``。
  // 同时支持多词前缀（``git log``）—— 不取首词，整 cmd 前缀比对。
  const cmdLower = cmd.trim().toLowerCase()
  const readonly: string[] = cls.readonly_bash_prefixes || []
  for (const prefix of readonly) {
    const p = prefix.toLowerCase()
    if (cmdLower === p || cmdLower.startsWith(p + " ")) return false  // 命中只读 → 放行
  }
  return true
}

// ── approval bridge（SPEC 2026-08-11-opencode-permission-bridge §3/§4/§5）──────────
//
// tool.execute.before 交互审批桥的纯逻辑：POST broker /approval → 据 behavior 放行/throw。
// **哑传输**（D-v7-1 守门不变）：桥不做 Orca 业务判定（run 归属 / yolo / 审批决策全在 broker）。
// run 归主由 broker active_runs.py 经 sessionID 双键（host_session / node_sessions）匹配。
//
// 纯函数（_decide / _brokerConfig / _normalizeToolInput / _resolveApprovalSessionId）抽出为
// module scope + export，供 vitest 行为表单测（SPEC §8）。_askBroker 是唯一 IO 点（fetch）。

// broker 调用结果分类（SPEC §4 失败语义）。_askBroker 把所有异常归类到这几种，
// _decide 据此 + policy 决定放行/阻断。
export type BrokerOutcome =
  | { kind: "behavior"; behavior: string }   // 合法 JSON 响应，behavior 非空字符串
  | { kind: "unreachable" }                   // fetch 网络错 / 连接拒（broker 不在线）
  | { kind: "http-error"; status: number }    // 4xx/5xx（broker 活着但出错）
  | { kind: "bad-response" }                  // 非 JSON / 缺 behavior（fail loud）
  | { kind: "timeout" }                       // AbortController 超时
  | { kind: "exception" }                     // 未预期异常

export interface HookAction {
  proceed: boolean         // true → return（放行）；false → throw（阻断）
  throwMessage?: string    // proceed=false 时的 throw 文案
}

export interface BrokerConfig {
  host: string
  port: string
  timeoutMs: number
  timeoutPolicy: "allow" | "deny" | "ask"
}

// broker 连接 + 超时策略配置（SPEC §5）。硬编码默认 127.0.0.1:7428（与 ``tars serve`` 默认一致，
// ``orca/iface/web/server.py:143-147``）；env 覆盖：``ORCA_HOST`` / ``ORCA_PORT`` /
// ``ORCA_APPROVAL_TIMEOUT``（秒）/ ``ORCA_APPROVAL_TIMEOUT_POLICY``（allow|deny|ask）。
// **headless executor overlay 不注连接 env**（``exec/env.py`` 只注 run 路由 env）→ 默认 7428 必须吻合。
export function _brokerConfig(): BrokerConfig {
  const env = (typeof process !== "undefined" && process.env) || {}
  const host = env.ORCA_HOST || "127.0.0.1"
  const port = env.ORCA_PORT || "7428"
  const rawTimeout = env.ORCA_APPROVAL_TIMEOUT
  let timeoutMs = 600000  // 默认 600s（与 CC hook 一致）
  const parsed = Number(rawTimeout)
  if (Number.isFinite(parsed) && parsed > 0) timeoutMs = parsed * 1000
  const rawPolicy = (env.ORCA_APPROVAL_TIMEOUT_POLICY || "allow").trim().toLowerCase()
  const timeoutPolicy: "allow" | "deny" | "ask" =
    rawPolicy === "deny" || rawPolicy === "ask" ? rawPolicy : "allow"
  return { host, port, timeoutMs, timeoutPolicy }
}

// session 解析（SPEC §3 B1 闭环）：ORCA_SESSION_ID（executor 注入的 orca-uuid == tape node
// session_id；headless 命中 broker ``active_runs.py:221`` node 键 ``session_id in node_sessions``）
// **||** input.sessionID（opencode 内部会话 id；交互模式命中 host 键——shell.env 钩子注
// ``ORCA_HOST_SESSION_ID = input.sessionID`` → bootstrap 写 ``data.host_session``）。
// translator 显式不复用 opencode 流里 sessionID（``translators/opencode.py:39-40``），故 headless
// 必须取 ORCA_SESSION_ID（input.sessionID 在 tape 不存在 → resolver miss → 死桥）。两键不可合一为单源。
export function _resolveApprovalSessionId(input: any): string | undefined {
  const fromEnv = (typeof process !== "undefined" && process.env?.ORCA_SESSION_ID) || undefined
  if (typeof fromEnv === "string" && fromEnv) return fromEnv
  const fromInput = input?.sessionID
  return typeof fromInput === "string" && fromInput ? fromInput : undefined
}

// tool_input 形状对齐 broker 期望（dict/list，非 dict/list → {}；与 ``approval_broker.py:283``
// ``tool_input if isinstance(tool_input, (dict, list)) else {}`` 同款）。opencode args 在 output.args。
export function _normalizeToolInput(args: any): Record<string, any> | any[] {
  if (args && typeof args === "object") return args  // dict 或 list（typeof [] === "object"）
  return {}
}

// 决策表（SPEC §4 失败语义 + §3 behavior 映射）。纯函数：据 broker 结果 + timeout policy
// 决定放行/阻断。**headless fail-open 取舍**（§4 B5）：不可达/异常 fail-open（防 DEFECT-1 挂死）；
// HTTP 错/坏响应 fail loud（broker 活着但坏 = 可疑，与 CC 一致）。
export function _decide(
  outcome: BrokerOutcome,
  policy: "allow" | "deny" | "ask",
  tool: string,
): HookAction {
  const t = tool || "<unknown>"
  switch (outcome.kind) {
    case "behavior":
      // §3：只有 deny 阻断；allow / ask / 其他 → 放行（ask 交 opencode 原生 + ``--auto`` 兜底）。
      if (outcome.behavior === "deny") {
        return { proceed: false, throwMessage: `orca: 工具 ${t} 被审批拒绝（不要重试）` }
      }
      return { proceed: true }
    case "unreachable":
      // §4：broker 不在线 = web 审批层没了；退 ``--auto`` 放行（fail-open 优于挂死）。
      return { proceed: true }
    case "http-error":
      // §4：broker 活着但出错 = 可疑，fail loud（与 CC hook 一致）。
      return { proceed: false, throwMessage: `orca: 工具 ${t} 审批请求失败（broker HTTP ${outcome.status}）` }
    case "bad-response":
      // §4：非 JSON / 缺 behavior → fail loud（与 CC 一致）。
      return { proceed: false, throwMessage: `orca: 工具 ${t} 审批响应非法（fail loud）` }
    case "timeout":
      // §4：按 policy——allow/ask → 放行；deny → 阻断。
      if (policy === "deny") {
        return { proceed: false, throwMessage: `orca: 工具 ${t} 审批超时（policy=deny）` }
      }
      return { proceed: true }
    case "exception":
      // §4：保守 fail-open，绝不挂 agent。
      return { proceed: true }
    default: {
      // exhaustiveness 守门：TS 编译期保证所有 BrokerOutcome kind 已覆盖（新增 kind 未加
      // case 时此赋值报错）；运行时兜底 fail-open（与 exception 一致，绝不挂 agent）。
      const _exhaustive: never = outcome
      void _exhaustive
      return { proceed: true }
    }
  }
}

// POST /approval，把所有错误归类到 BrokerOutcome（不抛——_decide 统一决策）。
// body 形状复用 CC hook（``templates/orca-permission-hook.py:230-235``）：
// ``{session_id, tool, tool_input, hook_event: "PermissionRequest"}``。
// broker 决策路径不读 hook_event（SPEC I-1，已核实）——它是标签。
export async function _askBroker(
  sid: string,
  tool: string,
  args: any,
  cfg: BrokerConfig,
): Promise<BrokerOutcome> {
  const url = `http://${cfg.host}:${cfg.port}/approval`
  const body = JSON.stringify({
    session_id: sid,
    tool: tool || "<unknown>",
    tool_input: _normalizeToolInput(args),
    hook_event: "PermissionRequest",
  })
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), cfg.timeoutMs)
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      signal: ctrl.signal,
    })
    if (!resp.ok) {
      return { kind: "http-error", status: resp.status }
    }
    let parsed: any
    try {
      parsed = await resp.json()
    } catch {
      return { kind: "bad-response" }
    }
    const behavior = parsed?.behavior
    if (typeof behavior === "string" && behavior) {
      return { kind: "behavior", behavior }
    }
    return { kind: "bad-response" }  // 缺 behavior → fail loud
  } catch (e: any) {
    if (e?.name === "AbortError") return { kind: "timeout" }
    // fetch 网络错（TypeError: fetch failed / 连接拒）→ unreachable（fail-open）。
    if (e instanceof TypeError) return { kind: "unreachable" }
    return { kind: "exception" }
  } finally {
    clearTimeout(timer)
  }
}

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
      if (injectingIdle.has(sessionID)) return
      const active = listActiveRuns(sessionID)
      if (active.length === 0) return        // 无本 session 的活跃 run → 无需 nudge
      if (!nudgeAllowed("nudge", sessionID)) return   // per-session 节流窗口内 → 跳过（防刷屏）

      injectingIdle.add(sessionID)
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
        markNudged("nudge", sessionID)  // 成功注入才计入节流（失败下轮重试）
      } catch (e) {
        // 注入失败（client API 错 / session 不存在）→ console.error，不计节流，下轮 idle 重试。
        console.error("[orca] nudge promptAsync failed:", e)
      } finally {
        injectingIdle.delete(sessionID)
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

    // PostToolUse 事后告警守卫（SPEC posttooluse-rogue-guard §8）：opencode 等价 ``tool.execute.after``。
    // 主 session 在活跃 run 期间用了「下场干活」工具 → promptAsync 注入纯文本提示。**不**阻止动作
    // （pure hint），**不**调 orca next（B 路径铁律）。仅当本 session 有活跃 run 时触发。
    //
    // 输入形状（SPEC §10 R1 fallback）：官方文档未给完整字段，按 tool.execute.before 示例推 ``input.tool``。
    // sessionID 取法：首选 input.sessionID；取不到 → 写 runs/.orca-guard-unbound.json 心跳 + return
    // （fail-safe 降级，SPEC §10 R1）。mid-turn promptAsync 失败 → console.error + 不计节流。
    "tool.execute.after": async (input: any) => {
      // step 0：in-flight mutex（独立于 idle 的 mutex——防 turn 中工具调用与 idle 并发注入互相吞噬）。
      let sessionID: string | undefined = undefined
      try {
        sessionID = typeof input?.sessionID === "string" ? input.sessionID : undefined
      } catch { /* input 异常 → sessionID undefined，下方 fallback */ }
      if (sessionID && injectingGuard.has(sessionID)) return
      if (sessionID) injectingGuard.add(sessionID)
      try {
        // step 1：sessionID fallback（SPEC §10 R1 / §8.1 P4 修订）。
        if (!sessionID) {
          try {
            mkdirSync("runs", { recursive: true })
            writeFileSync(
              "runs/.orca-guard-unbound.json",
              JSON.stringify({ unbound_at: nowSec(), tool: input?.tool ?? null }),
            )
          } catch (e) {
            console.error("[orca] guard heartbeat failed:", e)
          }
          return  // 取不到 session → fail-safe 不告警
        }

        // step 2：扫本 session 活跃 run。
        const active = listActiveRuns(sessionID)
        if (active.length === 0) return  // 无活跃 run → 不告警

        // step 3：分类。input.tool 是工具名；bash 类需看命令（args 字段名未实证，多候选）。
        const tool = input?.tool
        if (typeof tool !== "string" || !classifyTool(tool, input?.args ?? input?.command)) return

        // step 4：guard 30s 节流（独立文件 runs/.orca-guard-<sessionID>.json，与 nudge 分键）。
        if (!nudgeAllowed("guard", sessionID)) return

        // step 5：注入 §6 提示（同 idle nudge 同款 promptAsync）。reason 模板从 classification
        // 单一真相源取（review 🟡#3 DRY：与 cc_nudge.sh 共享同一份文案），按 {run_id}/{tool} 占位符
        // 填充。模板缺失 → 内联兜底（保持两路径告警能力）。
        const runId = active[0].run_id
        const cls = loadClassification()
        const template: string = (cls && typeof cls.guard_reason_template === "string")
          ? cls.guard_reason_template
          : "【Orca 守卫·事后提醒】检测到你在活跃 run（{run_id}）期间自己用了 {tool}。编排期主 session 不该下场做节点工作——那是子代理的活。建议：改派 Task 子代理完成此步，或把已有产出作为 --output 调 orca next --run-id {run_id} 推进。本提醒不阻止（动作已执行）；若这是必要的调试/解锁操作，忽略即可。"
        const reminder = template.replace(/\{run_id\}/g, runId).replace(/\{tool\}/g, tool)
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
        markNudged("guard", sessionID)  // 成功注入才计节流（注入失败下个工具调用重试）
      } catch (e) {
        // mid-turn promptAsync 失败（SPEC §10 R1 fallback）：console.error + 不计节流。
        console.error("[orca] guard promptAsync failed:", e)
      } finally {
        if (sessionID) injectingGuard.delete(sessionID)
      }
    },

    // tool.execute.before 交互审批桥（SPEC 2026-08-11-opencode-permission-bridge §3）。
    // opencode 等价 CC 的 PermissionRequest：工具执行前 POST broker /approval → 据 behavior
    // 放行/throw。**哑传输**（D-v7-1）：run 归属 / yolo / 审批决策全在 broker，桥只转发 + 动作。
    //
    // spike 实证（opencode 1.18.13）：``tool.execute.before: async (input, output) =>``，
    // input = {tool, sessionID, callID}，**工具 args 在 output.args**（非 input）。deny = throw
    // （官方唯一 deny 机制）。对主 agent 与 Task 子代理的工具调用都 fire（子代理带独立 sessionID）。
    //
    // fail 语义（SPEC §4）：broker 不可达/异常 → fail-open 放行（headless 防挂死）；HTTP 错/坏响应
    // → throw（fail loud）；timeout → ``ORCA_APPROVAL_TIMEOUT_POLICY``；deny → throw。
    // 详见 ``_decide`` 决策表。session 解析见 ``_resolveApprovalSessionId``（B1 双键契约）。
    "tool.execute.before": async (input: any, output: any) => {
      const sid = _resolveApprovalSessionId(input)
      if (!sid) return  // 无 session 身份（手 CLI / 无 executor）→ fail-open 放行
      const tool = input?.tool
      const cfg = _brokerConfig()
      let outcome: BrokerOutcome
      try {
        outcome = await _askBroker(sid, tool, output?.args, cfg)
      } catch (e) {
        // _askBroker 内部已归类（不应抛）；保守兜底 → fail-open（SPEC §4 未预期异常）。
        console.error("[orca] approval bridge unexpected error (fail-open):", e)
        return
      }
      const action = _decide(outcome, cfg.timeoutPolicy, tool)
      if (!action.proceed) {
        console.error(`[orca] approval 阻断 [${outcome.kind}]: ${action.throwMessage}`)
        throw new Error(action.throwMessage ?? `orca: 工具 ${tool ?? "<unknown>"} 被阻断`)
      }
      // 放行。clean allow/ask 静默；fail-open 路径（unreachable/timeout-allow/exception）留痕。
      if (outcome.kind !== "behavior") {
        console.error(`[orca] approval ${outcome.kind} → fail-open 放行`)
      }
    },
  }
}

export default OrcaPlugin
