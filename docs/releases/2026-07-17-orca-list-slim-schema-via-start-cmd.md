# Release: `orca list` 瘦身 + inputs_schema 移到启动命令

**日期**：2026-07-17
**范围**：in-session CLI 接口契约（`orca list` / `orca <wf>`）+ TARS skill + SPEC v5 + 测试
**commit**：`<SHA>`

---

## 背景

`orca list` 单命令返全部 workflow 的 `name + description + 完整 inputs_schema`。TARS skill
第一步「选 workflow」只需 `name + description` 做语义匹配，全量 `inputs_schema` 是纯噪音——
实测输出 4010 字节里 **84% 是 schema**（`agent-struct-exploration` 一个 wf 就 21 个 input 字段、
2688 字节，占该 wf 输出的 90%）。

`inputs_schema` 是 LLM **决定启动某个 wf 之后**才需要的信息（用来抽 inputs）。它本就不该
出现在「发现/选择」阶段的 `list` 里——这跟用户最初的判断一致：「schema 是到启动 workflow
时才需要的」。

## 方案（做法 A，用户拍板）

**零新命令**：`orca list` 砍掉 schema，只返 `{workflows:[{name, description}]}`；schema 改由
**已有的启动命令** `orca <wf>` 按需带出——不带 `--inputs` → 只返
`{name, description, inputs_schema}`（不真启动）；带 `--inputs` → 真启动。

相比 CURRENT.md 原登记的「选项 1：加 `describe` 命令」，做法 A 的好处：命令数仍 7、保留字
黑名单不动、CI `grep describe=0` 不变、SPEC 改动小。顺带修掉一个隐患——旧 `orca <wf>` 不带
inputs 会**静默用 `{}` 真启动**，等必填字段缺失才 fail loud。

TARS 新三步：**`orca list` 选 wf（name+desc）→ `orca <wf>` 看 inputs_schema 抽 inputs →
`orca <wf> --inputs` 启动**。

## 改动

### 源码
- `orca/iface/in_session/cli.py`
  - `list_workflows`：列表推导砍 `inputs_schema`，只返 `{workflows:[{name, description}]}`。
  - `bootstrap`（`orca <wf>` 语法糖真身）：`inputs` option 默认 `"{}"`→`None`；`load_workflow`
    之后、dupe-check lock / `advance_step` 之前插分流——`inputs is None` → `typer.echo({name,
    description, inputs_schema})` + `Exit(0)`（**纯只读**：不 gen run_id / 不建 tape / 不写
    marker / 不 spawn chart daemon）；非 None → 原启动逻辑不变。`json.loads(inputs)` 去掉
    `if inputs else {}`（None 已分流，空串 → JSONDecodeError → BadParameter，fail loud）。
- `orca/compile/catalog.py`：`_inputs_to_schema_list`（私有）公开化为 `inputs_schema_list`
  （给 bootstrap 复用，避免跨模块调私有 + DRY；MCP/tars list 仍经 `list_workflows` 全字段，
  单一真相源契约不破）。

### 文档
- `orca/skills/tars/SKILL.md`：三步流程重组（命令清单 + 流程 + success_criteria）。
- `docs/specs/in-session-entry-and-simplification.md`：§2.1 命令族表、§2.3 返回契约、§4.2 skill
  流程、决策 5、§8 实施计划、§11 验收标准同步（line 4 加 `〔2026-07-17 演进〕` 注）。命令数 7 /
  保留字 / CI 禁 describe 保持不变。

### 测试
- `test_v3_step1.py`：`test_orca_list_returns_inputs_schema_json` 重写为
  `test_orca_list_returns_name_and_description_only`（断言 list 每项恰 `{name, description}`，
  按名定位、**不再 `len==1`** → 顺手解 `CURRENT.md` 登记的 `~/.orca/workflows` 隔离缺陷）；
  新增 `test_wf_without_inputs_flag_returns_schema_no_run`（验返 schema + **不产生 runs/tape/marker**）。
- 所有不带 `--inputs` 的 bootstrap 测试调用补 `--inputs "{}"`：`test_in_session_cli.py` /
  `test_in_session_chart.py` / `test_host_session_binding.py` 的 `_bootstrap` helper（一处覆盖
  全部调用）+ `test_v3_step1.py` / `test_in_session_v8.py` 的直接 invoke（click last-wins 让
  显式传 `--inputs` 的调用覆盖 helper 默认）。

## 验证
- `orca list` 输出字节 **4010 → 636（降 84%）**。
- `orca <wf>`（不带 inputs）→ 返 `{name, description, inputs_schema}`，`runs/` 不产生任何文件。
- `pytest tests/iface/in_session/ tests/compile/test_catalog.py tests/iface/mcp/test_unit_tools.py
  tests/iface/cli/test_commands.py` → **268 passed**；review 修复后再跑 in_session → **185 passed**。
- `tars validate workflows/*.yaml`（3 个 wf）全过（保留字 / compile 未变）。
- code-reviewer 自检：**0 🔴**（2 🟡 SPEC stale + 3 🟢 优化，全修）。

## 不在范围
- MCP `describe_workflow` / `list_workflows` 工具不变（MCP 壳独立，仍走 catalog 全字段）。
- `tars list`（后端人类可读）不变。
- daemon 路径不经 bootstrap CLI（直接 `advance_step`），不受影响。
