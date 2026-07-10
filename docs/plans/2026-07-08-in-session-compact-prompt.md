# 实施计划：in-session compact prompt（文件交付 + 缺字段干净 fail loud）

> **日期**：2026-07-08
> **分支**：`phase13-in-session-v8`
> **SPEC**：`docs/specs/in-session-shell-design-draft.md`（本计划同步回填 SPEC §2.1/§2.6.2 compact 契约）

## Context（为什么做）

in-session shell 当前把**完整渲染后的节点 prompt** 经 `.prompt` 字段整段注入宿主主 session（opencode `messages.transform` 改写 / CC Stop `reason`）。长 prompt（尤其引用 `agents/<name>/agent.md` 的节点，几 KB）全量进主 session 上下文且永久驻留对话历史，再复制一份进 task 子代理 —— 主 session 上下文随节点数线性膨胀。

**目标**：compact 直接替换默认交付逻辑——Orca 把渲染后的 prompt **写文件**，主 session 只收一句**指针**（"派子代理读 `<path>` 执行"），子代理从文件读完整指令。两种 agent 形态（`agent: <name>` 引用 md / inline `prompt:`）渲染时无差别（compile 已把 md 扁平化进 `node.prompt`），统一处理。

**顺手修的既有 bug**：output_schema 声明但子代理返回缺字段时，`_parse_output` 只 `json.loads` 不校 schema（注释明说"留 phase SPEC"）→ 下游 `render_prompt` Jinja `StrictUndefined` 抛 `ExecError(render)` → **非 InSessionError，逃逸 cli.py 的 except → 脏崩溃**：无 `workflow_failed`、不清 marker、tape 悬挂、下次 `/orca run` 卡死。compact 让此坑显形，必须一并修。

**不做的**：不接 `validator`（LLM 语义判官 + 自动重跑）。in-session 主 session 自己当判官，子代理在 turn 内据 rendered prompt 里的 output_schema 要求自我纠正；产不对则 Orca 层 fail loud（不做重试循环）。

## 两套契约（设计的根，勿混）

| 契约 | 方向 | 形态 | 决定者 |
|---|---|---|---|
| 输出契约 | agent → Orca（`next --output`） | 无 schema=自由文本；有 `output_schema`=JSON | `output_schema` |
| 输入契约 | Orca → 下一 agent（prompt 文件） | **永远是渲染后 markdown**，上游值 Jinja 插值 | prompt 模板 |

节点间不传 JSON 信封；上游 output 存 `{"output": <raw>}`，下游 prompt 用 `{{ a.output.field }}` 引用、渲染成 markdown 自然语言。子代理读人话指令 + 已代入的值，不是 JSON payload。

## 端到端流程（compact）

```
bootstrap / next 调 advance_step
  advance_step:
    rendered = _render_or_fail(node, ctx)        # Jinja 渲染（ExecError→InSessionError）
    若 prompts_dir 给定：
        原子写 rendered → <prompts_dir>/<node>.md
        StepResult(prompt_file=<path>, resources_root=node.resources_root, prompt=None)
    否则（单测/inline 回退）：
        StepResult(prompt=rendered, prompt_file=None)
cli.py 据 result 拼 pointer（host-facing 文本）:
    "【Orca 节点执行】用 task 派子代理。完整指令已写入 <path>。
     附资源目录 <resources_root>。子代理先 Read 再执行，输出即本节点输出。"
  reply["prompt"] = pointer   # plugin 仍读 reply.prompt，零改动
```

事件序列不变（ws/ns/nc/rt/wc），G2 编排骨架对齐不受影响。

## 改动位置

### 1. `orca/run/step.py`（核心）

- `StepResult` 加字段：`prompt_file: str | None = None`、`resources_root: str | None = None`。
- `advance_step(..., prompts_dir: Path | None = None)` 新参。
  - `prompts_dir` 给定 → 写文件、返 `prompt_file`+`resources_root`、`prompt=None`。
  - `None` → inline 回退（`prompt=rendered`），保现有单测不破。
- 新 `_render_or_fail(node, ctx) -> str`：包 `render_prompt`，`ExecError` → `InSessionError("渲染节点 {name!r} prompt 失败（可能上游 output 缺字段）：{e}")`。3 处 `render_prompt` 调用（197/224/233）全换。
- 新 `_write_prompt_file(prompts_dir, node_name, rendered) -> str`：`tmp + os.replace` 原子写 `<node>.md`（loop 时覆盖，最新即所用；历史在 tape）。OSError → `InSessionError`（fail loud）。
- `_parse_output` 加 **jsonschema 字段校验**（`jsonschema>=4.0` 已是依赖）：声明 schema 时，`jsonschema.validate(parsed, schema)` 失败 → `InSessionError("节点 {name!r} 输出不满足 output_schema：{e.message}（路径 {path}）")`。缺字段在 parse 期被抓（早于 render）。

### 2. `orca/iface/in_session/cli.py`（交付层）

- 删 `_TASK_TOOL_INSTRUCTION` / `_with_task_instruction`（被 pointer 取代）。
- 新 `_build_pointer(result) -> str`：据 `result.prompt_file` + `result.resources_root` 拼 host-facing 指针文本。
- `bootstrap` / `next` 的 reply 构建：`result.prompt_file` 给定 → `reply["prompt"] = _build_pointer(result)`；否则 inline 回退 `reply["prompt"] = result.prompt`。
- `bootstrap` / `next` 传 `prompts_dir = tape_path.parent / run_id / "prompts"`。
- `_classify_in_session_error`：扩 `"渲染节点"` → `render_error`；`"output_schema"` 关键字（含"不满足 output_schema"）→ `output_schema_mismatch`（覆盖原"声明了 output_schema"/"非 JSON"两条）。

### 3. `orca/iface/in_session/templates/opencode/orca.ts`（**零改动**）

plugin 仍取 `reply.prompt`（现是指针文本）。验证一遍即可。

### 4. 文档

- SPEC `in-session-shell-design-draft.md` §2.1/§2.6.2 回填 compact 契约（`.prompt`=指针、文件在 `<rundir>/<run_id>/prompts/`）+ §2.5 taxonomy 加 `render_error`。
- SPEC §2.5 失败表：`output_schema_mismatch` 范围扩到"非 JSON 或 schema 违反"；新增 `render_error`（渲染期上游缺字段等）。

## 验证

1. **单测**（`tests/iface/in_session/test_in_session_v8.py` / 新文件）：
   - compact：`prompts_dir` 给定 → 文件含渲染全文、`prompt_file` set、`prompt=None`。
   - inline 回退：无 `prompts_dir` → `prompt=rendered`、`prompt_file=None`（既有行为）。
   - 缺字段+schema：`_parse_output` 对违反 schema 的 output raise InSessionError → 归类 `output_schema_mismatch`。
   - render 错（无 schema 但下游引用缺字段）：advance_step raise InSessionError("渲染节点…") → 归类 `render_error`。
   - 集成：`next` 收 render 错 → emit `workflow_failed` + 清 marker（干净终态，不卡死）。
2. **e2e 快速实验**（复用 `/tmp/orca-e2e-v81/repro.sh` 形态）：3 节点（含 inline prompt + agent-md 引用 + 引用上游 output），断言：① 每步主 session 收短指针（非整段 prompt）；② `runs/<run_id>/prompts/*.md` 落盘且含渲染值；③ 跑到 `workflow_completed`。

## 工作量 / 风险

- **工作量**：核心 + 单测 ~1 天；e2e 实验 ~半天。
- **风险**：
  - 🟡 文件跨进程可见性：CLI（Python）写、opencode task 子代理读。用绝对路径 `runs/<run_id>/prompts/<node>.md`，子代理 cwd 下可读。
  - 🟡 classifier 关键字扩 `output_schema` 要确认无误伤（仅 `_parse_output`/render 两 raise 用此词，安全）。
  - ✅ render/router/compile/drive_loop/事件 schema 零改；plugin 零改；G2 对齐不变。

## 不变项

router / compile / render 核心 / drive_loop / Tape / EventBus / 事件 schema / opencode plugin —— 全零改。compact 纯粹是 step.py 返回层 + cli.py 交付层 + 缺字段确定性校验。
