# create-workflow skill benchmark

> 评测 create-workflow skill 的用例集：每个 case **钉死输入 + 预期输出**，且每个预期产物
> 都经 `tars validate` 校验通过（格式 + 语义正确）。schema 演化让某 case 失效时，守门测试先红。

## scope（这个 skill 管什么）

**只做生成 / 转换**：从用户描述或既有素材产出 Orca workflow（+ agent md）。

**不做**（已显式排除，留给未来或新功能）：
- 变异 / 修复已有 Orca workflow（改路由、加节点、修 validate 错误）
- 从真实 run / 会话历史逆向
- 跨 CLI 格式导入单个 agent（opencode/Gemini/Codex agent → Orca agent）
- 多个 workflow 组合成大 workflow

## case 结构

```
cases/<NN>-<slug>/
  case.md               # 场景 + 输入（NL / 内联素材）+ 预期不变量
  expected/
    workflow.yaml       # 钉死的预期产物（经 tars validate）— agent-pool-only case 无此文件
    agents/             # 预期 agent md（workflow 用 agent: 引用时必有，与 workflow.yaml 同级）
      <name>.md
      <name>/agent.md   # 文件夹 agent（含 scripts/ 资源）
      <name>/scripts/...
```

**校验契约**：`expected/workflow.yaml` 的目录即 workflow_dir，resolver 查 `expected/agents/`。
故 workflow 引用 `agent: x` 时，`expected/agents/x.md`（或 `x/agent.md`）必须存在——否则 validate 红。

## 场景 → case 映射

| 场景 | 说明 | cases |
|---|---|---|
| 1 从零描述（NL） | 纯自然语言，零素材 | 01 线性 / 02 并行 / 03 foreach / 04 条件 / 05 validator |
| 2 转换异构 workflow 文件夹 | 整套别家配置或 prose | 06 异构 YAML / 07 prose md + 散 prompt |
| 3 散 agent md 组装 | 只有 agent 池，skill 补编排 | 08 线性 / 09 并行 |
| 4+5 skill 当 agent + 资产迁移 | CC/opencode skill + 编排 md | 10 无脚本 / **11 含脚本（路径重写）** |
| B 混合 | NL + 部分既有 agent | 12 |
| C 设计文档 | PRD → workflow | 13 |
| E 只造 agent 池 | 无 workflow | 14（无 workflow.yaml） |
| F script → 节点/agent | 散脚本 | 15 script 节点链 / 16 文件夹 agent |

## 关键语义规则（case 里强制体现，skill 必须遵守）

1. **foreach 的数组源必须字面 JSON 字符串**（双引号），不能用 `{{ inputs.repos }}`：
   Jinja2 渲染真 list 产单引号 repr，`orca/run/foreach.py:123` 的 JSON 解析会失败。见 case 03。
2. **skill 含脚本 → 文件夹 agent，脚本迁移 + 路径重写**：CC skill 的相对引用 `scripts/x.py`
   必须重写成 `$ORCA_AGENT_RESOURCES/scripts/x.py`（spawn 时 executor 注入该 env）。见 case 11、16。
3. **script 节点不打包资源**：`script` 节点在 cwd 跑命令，脚本不迁移；要"带脚本走"必须封文件夹
   agent。case 15（script 节点，仅引用）vs case 16（文件夹 agent，迁移）刻意对照。
4. **agent 三态自动判**：短且不复用 → 内联 `prompt:`；复用角色 → `agent:` 单 MD；带资源 → 文件夹 agent。
   多 case 混用三态（如 case 09 inline merge + agent ref；case 12 inline + agent ref）。
5. **catch-all route 必须最后**；`terminate` 不作 entry / 不进 parallel.branches / routes 必空。见 case 04。
