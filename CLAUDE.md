# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

> 框架设计与决策见 [docs/TASK.md](docs/TASK.md)。本文件只包含**协作规则**。

## 项目背景

Orca 是对 AgentHarness（前一个 claude code 编排框架）的重写，目标是根治后者的结构性缺陷——前端多 store + 非幂等 reducer 导致渲染卡顿、6 种 sidecar 造成多真相源漂移。本仓库所有架构决策围绕「不重蹈覆辙」展开：单 tape 唯一真相源 + 幂等 reducer + 一条读路径是底线（诊断见 `docs/specs/phase-3-events.md` §1）。

**参考项目位置**：AgentHarness 在 `/Users/mozzie/Desktop/Projects/AgentHarness`（前作，本项目的重写对象——**只读不改**，作为对照反例与客观协议事实来源，**不迁移其代码**）；Conductor 在 `/tmp/conductor`（设计参考，借鉴其 Executor 抽象 / capabilities / event_log 模式，不抄其实现）。

**重写原则**：AgentHarness 框架问题很多，本项目一律重写而非迁移其代码；仅采纳其客观事实（如 claude -p 的真实调用协议、stream-json 行格式），这些是协议契约不是框架资产。

**测试后端约定**：E2E / 集成测试固定使用 **opencode + deepseek-v4-flash**（API 已配置），不再使用 claude 作为后端测试。

---

## 协作原则（12-Rule）

1. **Think Before Coding** — 明确假设，不确定则问
2. **Simplicity First** — 最少代码解决
3. **Architectural Conformance** — 代码改动必须符合架构，不要打补丁（架构问题先设计方案对齐再改，见下「问题分类」）
4. **Goal-Driven Execution** — 定义成功标准
5. **Use the model only for judgment calls** — deterministic 逻辑用代码
6. **Token budgets are not advisory** — 接近 budget 及时总结
7. **Surface conflicts, don't average them** — 选一个，说明 why
8. **Read before you write**
9. **Tests verify intent, not not just behavior**
10. **Checkpoint after every significant step**
11. **Match the codebase's conventions**
12. **Fail loud**

---

## SDD 开发流程

```
读 SPEC → 写计划（不写代码）→ 确认 → 实现 → 自我 review → 更新状态
```

**禁止**：未读 SPEC 就实现、未写计划就写代码。

**SPEC 驱动**：每个阶段的接口/数据契约在 `docs/specs/phase-N-<name>.md`，是契约不是建议。逐字实现，不自作主张加字段。有疑问先问。

**前置设计草稿**：跨阶段的设计议题先落草稿（`docs/specs/<topic>-design-draft.md`），各 phase SPEC 撰写前必读对应草稿。当前已有：
- [`shells-design-draft.md`](docs/specs/shells-design-draft.md) —— **phase 7（CLI）/ 9（Web）/ 10（MCP）开工前必读**。三壳共同契约 + MCP 协议约束（CC 60s 超时 / elicitation 未支持）+ HandleId pattern + 三通道竞速机制。

---

## 代码质量底线（符合软件设计原则）

1. **Clean Code**：命名达意，函数单一职责，只在 *why* 非显然时注释
2. **高内聚低耦合**：模块边界清晰，单向依赖，禁止反向调用
3. **SOLID / OCP**：新能力靠新增策略/插件/子类，不改核心路径
4. **鲁棒性**：边界、空值、失败路径显式处理，**fail loud**，不静默吞错
5. **可扩展性**：扩展点显式（如 discriminated union、Translator 注册），加新 backend/kind 零核心改动
6. **DRY**：禁止三处以上重复逻辑；发现重复先抽象
7. **易定位**：关键路径有结构化日志/事件

**报错处理**：重试必须用户可见（"重试中 / 第 N 次 / 失败原因"）；三层重试（transport/协议/业务）不能互相吞错；限流走退避重试而非直接中断。

---

## 问题分类（必须遵守）

遇到问题先判定 **bug** 还是 **架构问题**：

- **Bug**：局部错误、状态丢失、边界未处理 → 最小化 surgical fix
- **架构问题**：跨模块耦合、抽象错位、职责越界、数据/事件流断裂、需要 hack 才能跑通 → **先写设计方案对齐再改，不允许直接打补丁**

判定信号：现象在多模块复现 / 修复涉及 ≥3 个不相关文件 / 需要 hack 兼容代码 → 架构问题。

---

## 自我 Review（每次写完代码后）

每个功能实现完，**必须自己分发 review agent** 做一次自检，重点检查：
- 是否违反依赖铁律（schema/run/exec/events/iface 单向依赖）
- 是否有职责越界（exec 碰编排、iface 反向调用）
- 是否引入重复逻辑（DRY）
- 是否 fail loud（边界/失败路径显式）
- 测试是否覆盖意图而非仅行为

发现问题立即修，不要把问题留给下一阶段。

---

## 状态文档规则

- `docs/status/CURRENT.md` —— 当前任务快照（任务、状态、必读文件、待办）
- `docs/status/CHANGELOG.md` —— 索引，每条 1-2 句话 + commit
- `docs/releases/<date>-<name>.md` —— 详细 release note
- `docs/plans/<date>-<name>.md` —— 事前实施计划

**开始前必读**：`CLAUDE.md` + `docs/status/CURRENT.md`。未读不要开工。

**任务完成强制流程**：写 release note → CHANGELOG 加索引（1-2 句话 + commit SHA）→ 更新 CURRENT.md。**不积累，不延后。**
