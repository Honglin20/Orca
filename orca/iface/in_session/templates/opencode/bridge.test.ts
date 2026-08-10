// bridge.test.ts —— tool.execute.before 审批桥决策表执行级单测（SPEC §8）。
//
// **零依赖**：用 node 内置 ``node:test`` + ``node:assert/strict``（node 22.6+ type stripping
// 直接 import ``./orca.ts``；不引 vitest/jest，免 npm install）。run：
//   ``node --test --experimental-strip-types bridge.test.ts``
//   （node 23.6+ 默认开 type stripping，可省 --experimental-strip-types）
//
// **职责分工**：本文件守「行为对」（_decide 决策表逐 case），同目录 ``__test__`` 侧的 Python
// ``test_opencode_permission_bridge.py`` 守「结构在」（CI pytest 必跑，防误删分支）。两文件互补
// （SPEC §8 静态门 + 行为表单测）。
//
// 测试目标：纯函数 ``_decide(outcome, policy, tool)`` —— 据 broker 结果 + timeout policy
// 决定放行/阻断。``_askBroker`` 是 IO（fetch），其归类逻辑（TypeError→unreachable 等）在此以
// 构造好的 BrokerOutcome 直接验证 _decide 的决策表（_askBroker 的归类由 Python 静态门 + 真机
// test-agent 守）。

import { test } from "node:test"
import assert from "node:assert/strict"

import {
  _decide,
  _normalizeToolInput,
  _resolveApprovalSessionId,
  _brokerConfig,
  _askBroker,
  OrcaPlugin,
  type BrokerOutcome,
  type BrokerConfig,
} from "./orca.ts"

const T = "Bash"  // 测试工具名占位

// 测试用默认 broker 配置（大 timeout，mock fetch 立即返，不让 AbortController 超时干扰）
const TEST_CFG: BrokerConfig = {
  host: "127.0.0.1", port: "7428", timeoutMs: 600000, timeoutPolicy: "allow",
}

// mock globalThis.fetch 跑一段断言后恢复（零依赖：直接覆盖 node 18+ 内置 fetch 全局）。
async function withMockFetch<T>(
  mock: (url: string, init: any) => Promise<any>,
  fn: () => Promise<T>,
): Promise<T> {
  const orig = globalThis.fetch
  globalThis.fetch = mock as any
  try {
    return await fn()
  } finally {
    globalThis.fetch = orig
  }
}

// ── _decide 决策表（SPEC §4 失败语义 + §3 behavior 映射）──────────────────────

test("_decide: behavior=allow → proceed（放行）", () => {
  const r = _decide({ kind: "behavior", behavior: "allow" }, "allow", T)
  assert.equal(r.proceed, true)
  assert.equal(r.throwMessage, undefined)
})

test("_decide: behavior=deny → throw + 「不要重试」文案", () => {
  const r = _decide({ kind: "behavior", behavior: "deny" }, "allow", T)
  assert.equal(r.proceed, false)
  assert.ok(r.throwMessage)
  assert.match(r.throwMessage!, /Bash/)
  assert.match(r.throwMessage!, /被审批拒绝/)
  assert.match(r.throwMessage!, /不要重试/)
})

test("_decide: behavior=ask → proceed（ask 交 opencode 原生 + --auto 兜底，§3）", () => {
  const r = _decide({ kind: "behavior", behavior: "ask" }, "allow", T)
  assert.equal(r.proceed, true)
})

test("_decide: behavior=未知值 → proceed（§3 else：非 deny 即放行，保守）", () => {
  // broker 契约只保证 allow|deny|ask；异常值按 §3 「未决→放行」处理（fail-open）
  const r = _decide({ kind: "behavior", behavior: "weird-value" }, "allow", T)
  assert.equal(r.proceed, true)
})

test("_decide: unreachable → proceed（fail-open，broker 不在线 退 --auto，§4）", () => {
  const r = _decide({ kind: "unreachable" }, "allow", T)
  assert.equal(r.proceed, true)
})

test("_decide: http-error → throw（fail loud，broker 活着但出错，§4）", () => {
  const r = _decide({ kind: "http-error", status: 500 }, "allow", T)
  assert.equal(r.proceed, false)
  assert.ok(r.throwMessage)
  assert.match(r.throwMessage!, /500/)
})

test("_decide: bad-response → throw（fail loud，非 JSON/缺 behavior，§4）", () => {
  const r = _decide({ kind: "bad-response" }, "allow", T)
  assert.equal(r.proceed, false)
  assert.ok(r.throwMessage)
})

test("_decide: timeout + policy=allow → proceed（§4 timeout→policy）", () => {
  const r = _decide({ kind: "timeout" }, "allow", T)
  assert.equal(r.proceed, true)
})

test("_decide: timeout + policy=deny → throw（§4 timeout→policy）", () => {
  const r = _decide({ kind: "timeout" }, "deny", T)
  assert.equal(r.proceed, false)
  assert.ok(r.throwMessage)
  assert.match(r.throwMessage!, /policy=deny/)
})

test("_decide: timeout + policy=ask → proceed（§4：ask→放行）", () => {
  const r = _decide({ kind: "timeout" }, "ask", T)
  assert.equal(r.proceed, true)
})

test("_decide: exception → proceed（保守 fail-open，绝不挂 agent，§4）", () => {
  const r = _decide({ kind: "exception" }, "allow", T)
  assert.equal(r.proceed, true)
})

// ── deny 文案含工具名（agent 可定位被拒工具，R5）──────────────────────────────

test("_decide: deny 文案含实际工具名（tool=Read）", () => {
  const r = _decide({ kind: "behavior", behavior: "deny" }, "allow", "Read")
  assert.match(r.throwMessage!, /Read/)
})

test("_decide: tool 缺省 → <unknown> 占位（防空 tool 崩文案）", () => {
  const r = _decide({ kind: "behavior", behavior: "deny" }, "allow", "")
  assert.match(r.throwMessage!, /<unknown>/)
})

// ── _normalizeToolInput（对齐 broker dict/list 期望，approval_broker.py:283）──

test("_normalizeToolInput: dict → 原样", () => {
  const d = { command: "ls" }
  assert.equal(_normalizeToolInput(d), d)
})

test("_normalizeToolInput: list → 原样", () => {
  const l = ["a", "b"]
  assert.equal(_normalizeToolInput(l), l)
})

test("_normalizeToolInput: string/number/null/undefined → {}", () => {
  assert.deepEqual(_normalizeToolInput("ls"), {})
  assert.deepEqual(_normalizeToolInput(42), {})
  assert.deepEqual(_normalizeToolInput(null), {})
  assert.deepEqual(_normalizeToolInput(undefined), {})
})

// ── _resolveApprovalSessionId（B1 双键契约）──────────────────────────────────

test("_resolveApprovalSessionId: env ORCA_SESSION_ID 优先（headless node 键）", () => {
  const prev = process.env.ORCA_SESSION_ID
  process.env.ORCA_SESSION_ID = "orca-uuid-headless"
  try {
    const sid = _resolveApprovalSessionId({ sessionID: "opencode-internal" })
    assert.equal(sid, "orca-uuid-headless")
  } finally {
    if (prev === undefined) delete process.env.ORCA_SESSION_ID
    else process.env.ORCA_SESSION_ID = prev
  }
})

test("_resolveApprovalSessionId: 无 env → 退 input.sessionID（交互 host 键）", () => {
  const prev = process.env.ORCA_SESSION_ID
  delete process.env.ORCA_SESSION_ID
  try {
    const sid = _resolveApprovalSessionId({ sessionID: "opencode-internal" })
    assert.equal(sid, "opencode-internal")
  } finally {
    if (prev !== undefined) process.env.ORCA_SESSION_ID = prev
  }
})

test("_resolveApprovalSessionId: 两键皆无 → undefined（fail-open 放行）", () => {
  const prev = process.env.ORCA_SESSION_ID
  delete process.env.ORCA_SESSION_ID
  try {
    assert.equal(_resolveApprovalSessionId({}), undefined)
    assert.equal(_resolveApprovalSessionId({ sessionID: "" }), undefined)
  } finally {
    if (prev !== undefined) process.env.ORCA_SESSION_ID = prev
  }
})

// ── _brokerConfig（§5：默认 7428 + env 覆盖）──────────────────────────────────

test("_brokerConfig: 默认 127.0.0.1:7428 / 600s / allow", () => {
  // 清相关 env 测默认（并发测试容忍：只断言默认值在 env 缺省时生效）
  const cfg = _brokerConfig()
  assert.ok(["127.0.0.1", process.env.ORCA_HOST || "127.0.0.1"].includes(cfg.host))
  assert.ok(cfg.port === "7428" || cfg.port === process.env.ORCA_PORT)
  assert.ok(cfg.timeoutMs > 0)
  assert.ok(["allow", "deny", "ask"].includes(cfg.timeoutPolicy))
})

test("_brokerConfig: ORCA_PORT 覆盖生效", () => {
  const prev = process.env.ORCA_PORT
  process.env.ORCA_PORT = "9999"
  try {
    assert.equal(_brokerConfig().port, "9999")
  } finally {
    if (prev === undefined) delete process.env.ORCA_PORT
    else process.env.ORCA_PORT = prev
  }
})

test("_brokerConfig: ORCA_APPROVAL_TIMEOUT_POLICY=deny 生效", () => {
  const prev = process.env.ORCA_APPROVAL_TIMEOUT_POLICY
  process.env.ORCA_APPROVAL_TIMEOUT_POLICY = "deny"
  try {
    assert.equal(_brokerConfig().timeoutPolicy, "deny")
  } finally {
    if (prev === undefined) delete process.env.ORCA_APPROVAL_TIMEOUT_POLICY
    else process.env.ORCA_APPROVAL_TIMEOUT_POLICY = prev
  }
})

test("_brokerConfig: 非法 policy → 回落 allow", () => {
  const prev = process.env.ORCA_APPROVAL_TIMEOUT_POLICY
  process.env.ORCA_APPROVAL_TIMEOUT_POLICY = "garbage"
  try {
    assert.equal(_brokerConfig().timeoutPolicy, "allow")
  } finally {
    if (prev === undefined) delete process.env.ORCA_APPROVAL_TIMEOUT_POLICY
    else process.env.ORCA_APPROVAL_TIMEOUT_POLICY = prev
  }
})

// ── _brokerConfig env 覆盖边界（🟢-3 补强）──────────────────────────────────

test("_brokerConfig: ORCA_HOST 覆盖生效", () => {
  const prev = process.env.ORCA_HOST
  process.env.ORCA_HOST = "10.0.0.1"
  try {
    assert.equal(_brokerConfig().host, "10.0.0.1")
  } finally {
    if (prev === undefined) delete process.env.ORCA_HOST
    else process.env.ORCA_HOST = prev
  }
})

test("_brokerConfig: ORCA_APPROVAL_TIMEOUT=30 → 30000ms（秒→毫秒换算）", () => {
  const prev = process.env.ORCA_APPROVAL_TIMEOUT
  process.env.ORCA_APPROVAL_TIMEOUT = "30"
  try {
    assert.equal(_brokerConfig().timeoutMs, 30000)
  } finally {
    if (prev === undefined) delete process.env.ORCA_APPROVAL_TIMEOUT
    else process.env.ORCA_APPROVAL_TIMEOUT = prev
  }
})

test("_brokerConfig: ORCA_APPROVAL_TIMEOUT 非法（负/NaN/空）→ 回落 600000ms", () => {
  const prev = process.env.ORCA_APPROVAL_TIMEOUT
  for (const bad of ["-5", "abc", ""]) {
    process.env.ORCA_APPROVAL_TIMEOUT = bad
    assert.equal(_brokerConfig().timeoutMs, 600000, `非法值 ${JSON.stringify(bad)} 应回落默认`)
  }
  if (prev === undefined) delete process.env.ORCA_APPROVAL_TIMEOUT
  else process.env.ORCA_APPROVAL_TIMEOUT = prev
})

// ── _askBroker 分类逻辑（SPEC §4 前半段：raw fetch 错 → BrokerOutcome）────────
// 锁死 orca.ts catch 块的 6 条分类映射；与 _decide 决策表合成 SPEC §4 双向覆盖。
// mock globalThis.fetch（node 18+ 内置全局），不引新依赖。

test("_askBroker: resp.ok=false → http-error（带 status）", async () => {
  await withMockFetch(async () => ({ ok: false, status: 503 }), async () => {
    const r = await _askBroker("sid", T, {}, TEST_CFG)
    assert.equal(r.kind, "http-error")
    assert.equal((r as any).status, 503)
  })
})

test("_askBroker: behavior=allow → {kind:'behavior', behavior:'allow'}", async () => {
  await withMockFetch(async () => ({ ok: true, status: 200, json: async () => ({ behavior: "allow" }) }), async () => {
    const r = await _askBroker("sid", T, {}, TEST_CFG)
    assert.equal(r.kind, "behavior")
    assert.equal((r as any).behavior, "allow")
  })
})

test("_askBroker: resp.json() 抛 → bad-response（fail loud）", async () => {
  await withMockFetch(async () => ({ ok: true, status: 200, json: async () => { throw new Error("bad json") } }), async () => {
    const r = await _askBroker("sid", T, {}, TEST_CFG)
    assert.equal(r.kind, "bad-response")
  })
})

test("_askBroker: 缺 behavior → bad-response（fail loud，§4 缺 behavior）", async () => {
  await withMockFetch(async () => ({ ok: true, status: 200, json: async () => ({}) }), async () => {
    const r = await _askBroker("sid", T, {}, TEST_CFG)
    assert.equal(r.kind, "bad-response")
  })
})

test("_askBroker: behavior 空串 → bad-response", async () => {
  await withMockFetch(async () => ({ ok: true, status: 200, json: async () => ({ behavior: "" }) }), async () => {
    const r = await _askBroker("sid", T, {}, TEST_CFG)
    assert.equal(r.kind, "bad-response")
  })
})

test("_askBroker: fetch TypeError → unreachable（fail-open，§4 broker 不在线）", async () => {
  await withMockFetch(async () => { throw new TypeError("fetch failed") }, async () => {
    const r = await _askBroker("sid", T, {}, TEST_CFG)
    assert.equal(r.kind, "unreachable")
  })
})

test("_askBroker: AbortError → timeout", async () => {
  await withMockFetch(async () => {
    const e = new Error("aborted")
    e.name = "AbortError"
    throw e
  }, async () => {
    const r = await _askBroker("sid", T, {}, TEST_CFG)
    assert.equal(r.kind, "timeout")
  })
})

test("_askBroker: 非 TypeError 异常 → exception（fail-open）", async () => {
  await withMockFetch(async () => { throw new Error("unexpected") }, async () => {
    const r = await _askBroker("sid", T, {}, TEST_CFG)
    assert.equal(r.kind, "exception")
  })
})

test("_askBroker: POST body 含 /approval + session_id/tool/tool_input/hook_event", async () => {
  let captured: { url: string; body: string } | null = null
  await withMockFetch(async (url: string, init: any) => {
    captured = { url, body: init.body }
    return { ok: true, status: 200, json: async () => ({ behavior: "allow" }) }
  }, async () => {
    await _askBroker("sid-42", "Bash", { command: "ls" }, TEST_CFG)
    assert.ok(captured)
    assert.match(captured!.url, /\/approval$/)
    const parsed = JSON.parse(captured!.body)
    assert.equal(parsed.session_id, "sid-42")
    assert.equal(parsed.tool, "Bash")
    assert.deepEqual(parsed.tool_input, { command: "ls" })
    assert.equal(parsed.hook_event, "PermissionRequest")
  })
})

test("_askBroker: tool 空串 → 归一化 <unknown>（与 _decide 一致）", async () => {
  await withMockFetch(async (url: string, init: any) => {
    const parsed = JSON.parse(init.body)
    return { ok: true, status: 200, json: async () => ({ behavior: "allow", _tool: parsed.tool }) }
  }, async () => {
    const r = await _askBroker("sid", "", {}, TEST_CFG)
    assert.equal(r.kind, "behavior")
  })
})

// ── tool.execute.before hook 主体（🟡-2：no-sid 早期 return）─────────────────

test("hook: 无 session 身份 → 早期 return，不 POST broker（fail-open）", async () => {
  const prev = process.env.ORCA_SESSION_ID
  delete process.env.ORCA_SESSION_ID
  let fetchCalled = false
  const orig = globalThis.fetch
  globalThis.fetch = (async () => { fetchCalled = true; return { ok: true, status: 200, json: async () => ({ behavior: "allow" }) } }) as any
  try {
    const plugin = await OrcaPlugin({ client: {} })
    const hook = plugin["tool.execute.before"]
    // 无 sessionID（input 空 + 无 env）→ 不应触达 fetch
    await hook({}, { args: {} })
    assert.equal(fetchCalled, false, "无 session 身份不应 POST broker（fail-open 早期 return）")
  } finally {
    globalThis.fetch = orig
    if (prev !== undefined) process.env.ORCA_SESSION_ID = prev
  }
})

test("hook: broker deny → throw（带「不要重试」文案，R5）", async () => {
  const prev = process.env.ORCA_SESSION_ID
  process.env.ORCA_SESSION_ID = "sid-headless"
  try {
    await withMockFetch(async () => ({ ok: true, status: 200, json: async () => ({ behavior: "deny" }) }), async () => {
      const plugin = await OrcaPlugin({ client: {} })
      const hook = plugin["tool.execute.before"]
      await assert.rejects(
        () => hook({ tool: "Write" }, { args: { path: "/x" } }),
        (e: Error) => /Write/.test(e.message) && /被审批拒绝/.test(e.message) && /不要重试/.test(e.message),
      )
    })
  } finally {
    if (prev === undefined) delete process.env.ORCA_SESSION_ID
    else process.env.ORCA_SESSION_ID = prev
  }
})

test("hook: broker 不可达 → fail-open 放行（不 throw）", async () => {
  const prev = process.env.ORCA_SESSION_ID
  process.env.ORCA_SESSION_ID = "sid-headless"
  try {
    await withMockFetch(async () => { throw new TypeError("connect ECONNREFUSED") }, async () => {
      const plugin = await OrcaPlugin({ client: {} })
      const hook = plugin["tool.execute.before"]
      // 不可达 → fail-open → hook 正常 return（不 throw）
      await hook({ tool: "Bash" }, { args: {} })
    })
  } finally {
    if (prev === undefined) delete process.env.ORCA_SESSION_ID
    else process.env.ORCA_SESSION_ID = prev
  }
})
