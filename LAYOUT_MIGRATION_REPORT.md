# Workflows per-workflow 目录隔离改造 —— E2E 真实执行验证报告

```
E2E_RESULT: PASS
DEFECT_SUMMARY: MINOR=1 | PLAN=0 | SPEC=0 | ENV=0
SELF_REVIEW: CLOSED (rounds=1)
UPDATED: 2026-08-28 —— MINOR_FIX 闭环（monitor_real_test.sh:6 死链已修 + grep 复验零命中，见 §7「修复后复验」），由 FAIL 改判 PASS
```

> **PASS 的精确含义**：六项成功标准全部以真实执行证据判定通过（§1-§8 + §9 逐条表）。验证当时（2026-08-27，SHA `37b4295`）曾报 FAIL：唯一降级项是成功标准 1 的「R2' 域内旧布局引用零残留」子句有 **1 处** MINOR 残留（手工驱动脚本死链，无 CI 引用）。该缺陷已于 2026-08-28 由 coder 修复并复验闭环（证据见 §7 末小节），六标准零降级，改判 PASS。除该项外验证全程零缺陷。

---

## 0. 被测状态

- **SHA**：`37b42958e680c4e001266d1ac2a3668d12aa614b`（branch `puzzle-supernet`）
- **验证范围**：批 A..H 全链（`a379375`→`37b4295`，含端点 18 commits；`git rev-list --count a379375..37b4295` = 17 为不含批 A 起点口径）。其中 prof-opt v5 并行 loop 提交（`fdd7a52..4c8c6f1` + `b03d8fb`/`d259668`/`2257c2e`）为其自有范围，验证时知悉并单列归因（见 §4）。
- **树状态**：`M docs/status/CURRENT.md`（+1 行，批 H 进行中标注）；未跟踪 `.e2e_po/`、`.e2e_spe2e/`（scratch）、`.layout_baseline_list.txt`（批 A 基线）。**无源码未提交改动**。
- **运行环境**：全部 python/CLI 走 WSL `.venv`（`/mnt/d/Projects/Orca/.venv/bin/`）。复杂命令经纯 LF 临时 `.sh`/`.py`（`C:\Users\mozzie\AppData\Local\Temp\orca_verify\`）执行。
- **mock 声明**：**全程零 mock**。唯一非socket项：web 冒烟用 fastapi TestClient（进程内真实 ASGI app + 真实文件系统安装态；契约明示可接受，且本次从 `/tmp` 起、catalog 命中的是**安装态**而非源态，强于要求）。playwright 类与 `tests/e2e_*` 按契约**排除**（未跑、非 mock；libnspr4 缺失为已知 pre-existing）。
- **破坏性动作**：仅步骤 1 的旧安装态处置，打在用户级 `~/.orca` 上但按任务书采用 **mv 改名留档**（`~/.orca/_pre_perwf_snapshot_20260827/`，SPEC「存档后整删」的更稳实现——等效删除、天然可回滚），未删任何字节。`~/.orca` 其余内容（projects.json、config.json、tars-serve.log 等）零触碰。

---

## 1. 旧安装态存档 + 清除 —— ✓

**命令**：`wsl bash .../step1_archive.sh`（find 全量列档 → mv 改名；脚本：`C:\Users\mozzie\AppData\Local\Temp\orca_verify\step1_archive.sh`）

**真实观测**（节选，exit 0）：

- 旧 `~/.orca/workflows` 共 **767 文件**，完整清单落 `/tmp/pre_perwf_workflows_files.txt`（WSL 侧）。形态与计划描述吻合：
  - **16 个平铺 yaml**（含 `kd-nas.yaml` 尸体 + `po-probe.yaml` 尸体）
  - 共享 `agents/` 池（`_puzzle_scripts`×14、`_quant_scripts`×2、`_struct_scripts`×8、kd 系 agent `decide`/`distill`/`gen-student`/`kd-gate`/`kd-select`/`kd-setup`/`kd-train-script` 等）
  - `subagents/` 二级 7 目录（kd-nas/nas-supernet×3/prof-opt/puzzle/puzzle-supernet）
- 旧 `~/.orca/knowledge_base` 存在（**75 文件**，清单落 `/tmp/pre_perwf_kb_files.txt`）
- mv 后：`~/.orca/workflows` **不存在**（脚本断言 `OK: ~/.orca/workflows does NOT exist`）；快照目录含 `workflows` + `knowledge_base` 两子树，完整。

**判定**：✓ works（exit 0）。

---

## 2. 安装（tars install）+ 安装态结构断言 —— ✓

**命令**：`wsl bash .../step2_install.sh` → `cd /mnt/d/Projects/Orca && .venv/bin/tars install 2>&1 | tee /tmp/install_out.txt`

**真实观测**（INSTALL_RC=0）：

- 真实 CLI 全量安装（cc/opencode/cac/nga 四 target + workflows）。
- workflows 段按 per-wf 目录打印（节选）：
  ```
  [workflows] → ~/.orca/workflows/（per-wf 自包含目录，全局内置，orca list 可扫到）
    ✓ agent-struct-exploration/（agents 5 · knowledge_base）
    ✓ nas-supernet/（agents 7 · subagents 5）
    ...（共 14 行）
    ✓ quant-sensitivity/（agents 2）
  ```
- 结构断言（脚本自动核验，全过）：
  - `~/.orca/workflows/` **dir_count=14 total_entries=14**（14 目录、零多余条目）
  - 14/14 目录均含 `workflow.yaml`
  - **无** `workflows/agents`、**无** `workflows/subagents`、**无** `workflows/knowledge_base`、**flat_yaml_count=0**、**无** `~/.orca/knowledge_base`
  - `agent-struct-exploration/` 四资产齐：`workflow.yaml + agents + knowledge_base + scripts`（`scripts/kb_graph.py` 实测在安装态）

**判定**：✓ works（INSTALL_RC=0，全部断言 OK）。

---

## 3. 安装态功能探针（防 catalog first-wins 假绿） —— ✓

**命令**：`wsl bash .../step3_probe.sh`（cwd=`/tmp`，非仓库目录；`/tmp/workflows` 不存在）

**真实观测**：

1. **真实 CLI**：`/mnt/d/Projects/Orca/.venv/bin/orca list`（自 `/tmp`）→ JSON 输出 **14 个 workflow**，`ORCA_LIST_RC=0`。
2. **路径归因**：`find_workflow_yaml_path` 对 14 wf 全部返回 `/home/mozzie/.orca/workflows/<wf>/workflow.yaml` ——**命中安装态，排除「源态 first-wins 遮蔽」假绿**。
3. **逐 yaml 加载**（`orca.compile.load_workflow`，内含 agent 解析 + subagents md 校验三重覆盖）：
   ```
   OK   agent-struct-exploration: nodes=6 inputs=9 subagents_md=0
   OK   nas-agent-pipeline:  nodes=5 inputs=5      OK nas-supernet:    nodes=10 inputs=6 subagents_md=5
   OK   nas-hp-search:       nodes=5 inputs=5      OK nas-supernet-v2: nodes=8  inputs=6 subagents_md=8
   OK   nas-supernet-v3:     nodes=9 inputs=6 subagents_md=8
   OK   prof-opt:            nodes=8 inputs=8 subagents_md=8
   OK   prune-channel-sweep: nodes=1 inputs=4      OK puzzle:          nodes=9  inputs=9 subagents_md=6
   OK   puzzle-supernet:     nodes=9 inputs=7 subagents_md=8
   OK   quant-bit-curve / quant-ptq-sweep / quant-qat / quant-sensitivity: nodes=1 各自 inputs=6/3/3/3
   load_fails=0  PROBE2_RC=0
   ```
4. **R4' 补充真功能探针**（`step3b_kb_probe.py`，安装态）：
   - `resolve_kb_dir(ase)` → `/home/mozzie/.orca/workflows/agent-struct-exploration/knowledge_base`（per-wf 命中）
   - `resolve_kb_dir(nas-supernet)`（无自有 KB、旧根 KB 已存档）→ `''`（无脏回退）
   - 显式坏 env `ORCA_KB_DIR=/nonexistent` → `''`（fail-loud，不静默回退）

**判定**：✓ works（三探针 rc=0，14/14 加载零失败）。

---

## 4. 基线 diff（vs `.layout_baseline_list.txt`，批 A 15 wf） —— ✓（差异恰为预期两类）

**命令**：`wsl .venv/bin/python .../step4_baseline.py`（源态直扫 `workflows/*/workflow.yaml` 逐个 load，不经 catalog 混扫）

**真实观测**：

- 源态结构：`flat_yaml=[] flat_yml=[]`，14 目录，顶层无 agents/subagents/knowledge_base，仓库根无 knowledge_base/。
- diff 结果：`added=[]`，`removed=['kd-nas']`，公共 wf 字段差异**恰 2 处、均在 prof-opt**：
  - `prof-opt.inputs_count: 14 → 8`
  - `prof-opt.description` 重写（时延链式推进/恢复轮/规则池语义）
- **归因证据（v5 自有范围，非迁移回归）**：
  - `git show 56d0db1 -M -- 'workflows/prof-opt.yaml' 'workflows/prof-opt/workflow.yaml'` → `similarity index 100%, rename from workflows/prof-opt.yaml, rename to workflows/prof-opt/workflow.yaml`（迁移提交对 prof-opt yaml **纯改名零内容改动**）
  - `git log --oneline -- workflows/prof-opt/workflow.yaml` 首条 = `ff86ef0 feat(prof-opt): C5 v5 workflow 契约重写——inputs 8 化/顺序门控 prompt/规则消费点`
- 其余 13 wf 的 name/description/entry/inputs_count **逐字段一致**。

**判定**：✓ works（diff 契约精确满足）。

---

## 5. tars validate（真实 CLI，14 wf） —— ✓

**命令**：`wsl bash .../step5_validate.sh`（对 `workflows/*/workflow.yaml` 逐个 `.venv/bin/tars validate <yaml>`）

**真实观测**：

```
== workflows/agent-struct-exploration/workflow.yaml -> rc=0 PASS (warn_lines=0 err_lines=0)
...（14 行全部同形态）...
VALIDATE_PASS=14 VALIDATE_FAIL=0
✓ workflows/nas-supernet/workflow.yaml 校验通过
```

**判定**：✓ works（14/14 rc=0；prompt 洁净 warning = **0**，符合「0 或既有合理集」的 0 端）。

---

## 6. 全量单测（WSL venv，排除 e2e_* 与 playwright 类） —— ✓（零新增失败）

**命令**：`wsl bash .../step6_pytest.sh` → `.venv/bin/python -m pytest tests/ -q --ignore=<9 个 e2e_* 目录> --ignore=<7 个 playwright/back_navigation 文件>`（完整清单见脚本；全量日志 `/tmp/step6_pytest_full.log`）

**真实观测（首轮全量）**：`34 failed, 4279 passed, 18 skipped, 53 warnings in 2300.24s (0:38:20)`

**归因方法（比桶算术更强的逐条重放证明）**：对 34 个失败做了两轮交叉验证——

- **A. 隔离重跑**（`step6_rerun.sh`，仅 34 失败 + 同文件邻域）：`31 failed, 182 passed`。3 项转绿：`test_server.py::test_concurrent_ask_user…`、`test_script_env_inject.py` ×2（chart_sock）——全量长跑负载下才挂。
- **B. 参照提交重放**（`step6_baseline_replay.sh`）：`git archive 56d0db1`（批 D 收口 = pre-existing 集合的报告点）导出到 `/tmp/orca_at_56d0db1`，PYTHONPATH 隔离重放同批 node IDs：**30 failed, 27 passed (29.23s)**。

**归因结论**：

| 集合 | 数量 | 证据 |
|---|---|---|
| **pre-existing（30）** | 30 | 在 56d0db1 重放**同样失败**：[v3] audit 13（`test_v2_audit_attribution/resume_attempt` 全 [v3] 参数，断言 v3 agent.md 内容契约——md 被 R100 证明未被迁移改动，属内容漂移）；push_probe 5（WS 探针收到 `approval_snapshot` 而非目标事件，环境时序性）；skill_md 1（tars SKILL.md 未提 `orca doctor`，文档漂移）；v3_step1 1；mcp 6（缺 opencode 后端 3 + 缺 `uv` 二进制 `FileNotFoundError: 'uv'` 3，环境性）；bg_integration 1（`--background 启动失败：Usage: orca bootstrap`）；web_does_not_import_cli 1（`run_manager.py: from orca.iface.cli.config import apply_kb_requirement`，KB 特性期引入的 web→cli 引用，批 E 报告「批前后均恰 2」已认定）；exit_codes 1（SPEC §3.3 裸 sys.exit 扫描命中，56d0db1 同挂）；puzzle_evaluator_recall 1（manifest 退役字段判定，内容性） | 
| **负载/时序 flake（4）** | 4 | 仅全量长跑挂、隔离重跑绿（HEAD 与 56d0db1 双绿）：server 1 + env_inject 2 + measure_baseline 1（其错误文本自证：`LATENCY-INFEASIBLE: 测量噪声疑似…CPU 争用…建议重跑`；重跑时同文件另一子用例转红，同因）——与批 D 报告「flaky 2」桶性质一致 |
| **新增失败（迁移/E/F/G/H 造成）** | **0** | 无任何一条满足「HEAD 挂 + 56d0db1 绿」 |

> 注：34 = 30 + 4 与任务书 pre-existing 算术（13+17+1+2=33）的 ±1 差异来自桶边界（flaky 实测 4 而非 2，其中 measure_baseline 两个子用例跨轮次交替挂）。逐条重放证明取代桶算术。

**判定**：✓ works（零新增失败；pre-existing 集合逐条坐实）。

---

## 7. 残留 grep（R2' 域） —— ✓（原 1 处 MINOR 残留，2026-08-28 修复后零残留）

**命令**：`wsl bash .../step7_grep2.sh`（结果全文：`C:\Users\mozzie\AppData\Local\Temp\orca_verify\step7_grep_results.txt`）

**域与豁免判定声明**：pattern `kd-nas|_kd_scripts|workflows/agents|workflows/subagents`；域 = `orca/` + `tests/`（遍历层排除 `tests/e2e_*`）+ `workflows/`（排除 `knowledge_base/` 子树）+ `scripts/`。**人工加判**：`orca/` 遍历层排除 `node_modules`（`orca/iface/web/frontend/node_modules` 为 241MB 第三方依赖、gitignored，非 R2' 意义上的一方源码——判定记录在案）。

**命中逐条人工判定**（共 25 行）：

| 命中 | 判定 |
|---|---|
| `orca/compile/validator.py:1253`、`orca/exec/render.py:88` | 双形态（新旧布局并存支持）的**诊断文案**，代码本就双形态——合法 |
| `orca/exec/env.py:18,77` | docstring 讲 $ORCA_WORKFLOWS_ROOT 机制的**动机举例**，路径示例为旧形态、机制本身现行——描述性陈旧，非功能缺陷（记待决策区） |
| `orca/iface/cli/install_cmds.py:782` | 新 install 的**旧布局 backup 清理**文档（UD-1）——合法 |
| `orca/iface/web/routes/workflows.py:130,222` | 日志前缀标签 / 双形态 docstring——合法 |
| `tests/exec/test_render.py:278` | 双形态测试注释——合法 |
| `tests/iface/cli/test_install_cmds.py:919` | 测试内历史性提及（「如已下架的 kd-nas」举例 ghost-wf 清理分支）——白名单类 |
| **`tests/iface/web/_e2e_artifacts/monitor_real_test.sh:6`** | **原 ✗ 真残留，已于 2026-08-28 修复**（见本节末「修复后复验」）：`MONITOR="/mnt/d/Projects/Orca/workflows/agents/ns_run_train/scripts/monitor_until_done.sh"` —— 旧路径已不存在（该脚本现位于 `workflows/nas-supernet/agents/ns_run_train/scripts/monitor_until_done.sh`，实测存在），此手工驱动脚本一旦执行立即死链。全仓 grep 无任何引用方（不进 CI），但它是可执行路径赋值而非「历史性提及」，不落任何豁免桶 |
| `workflows/agent-struct-exploration/workflow.yaml:20/87/108/239` | 已知 deferred 4 处**描述性**旧路径文本（`:59` 的功能性 default 已是新路径 `workflows/agent-struct-exploration/agents/_struct_scripts/latency_onnxrt.py::measure`，实测确认） |
| `workflows/puzzle/subagents/workflow-verifier.md:34` | 白名单（UD-2 零改动铁律），实测在案 |
| `scripts/run_skill_benchmark.py:79` | 三布局探测注释（含旧布局向后兼容探测）——合法 |
| `scripts/_migrate_per_dir.py`（9 行） | 白名单（已于 2026-08-28 收尾 commit git rm，历史留档） |
| 根级 `knowledge_base` 引用（extra 域） | `config.py` 解析链（env > config > per-wf > `~/.orca/knowledge_base` > cwd）与 `install_cmds.py` backup 文档均为**现行双形态语义**；仓库根 `knowledge_base/` 目录已不存在（§4 实测）——零残留 |

**kd-nas / _kd_scripts 专项**：四域内除上表白名单类历史提及（`test_install_cmds.py:919` 一处）外**零命中**。

**判定（验证当时，2026-08-27）**：✗ broken（1 处 MINOR 残留）。
**DEFECT_CLASS：MINOR_FIX** —— 理由：单文件单行的路径接线缺陷，修复不动契约与计划（把 `monitor_real_test.sh:6` 的路径改为 `workflows/nas-supernet/agents/ns_run_train/scripts/…`，或将该手工驱动移入 e2e 豁免区并注明冻结）；不影响任何 CI/用户表面。**复现**：`bash -c 'test -f /mnt/d/Projects/Orca/workflows/agents/ns_run_train/scripts/monitor_until_done.sh'` → 失败（No such file）；新路径 `test -f /mnt/d/Projects/Orca/workflows/nas-supernet/agents/ns_run_train/scripts/monitor_until_done.sh` → 存在。

### 修复后复验（2026-08-28，收尾 commit）

- **修复**：`tests/iface/web/_e2e_artifacts/monitor_real_test.sh:6` 的 `MONITOR` 赋值改为
  `"/mnt/d/Projects/Orca/workflows/nas-supernet/agents/ns_run_train/scripts/monitor_until_done.sh"`（单行外科手术，其余零触碰）。
- **复现闭环**（修复前实测，Windows Git Bash）：
  - `ls D:/Projects/Orca/workflows/agents/ns_run_train/scripts/monitor_until_done.sh` → `No such file or directory`（rc=2）
  - `ls -la D:/Projects/Orca/workflows/nas-supernet/agents/ns_run_train/scripts/monitor_until_done.sh` → 存在（3522 字节，rc=0）
- **grep 复验**（域 `tests/iface/web/`，与原 R2' 同 pattern 族）：
  - `pattern: workflows/agents` → **No matches found**（0 命中，死链消除、无新引入）
  - `pattern: kd-nas|_kd_scripts|workflows/subagents` → **No matches found**（附带复核，同样零命中）
- **判定**：✓ works（该域零残留；本表其余命中项均为白名单/双形态合法项，原判定不变）。

---

## 8. web 冒烟（TestClient，自 /tmp 起 = 命中安装态） —— ✓（19/19）

**命令**：`wsl bash -c 'cd /tmp && .venv/bin/python .../step8_web_smoke.py'`（真实 ASGI app：`create_app(RunManager(...))` + `fastapi.testclient.TestClient`；catalog 命中 `~/.orca/workflows` 安装态——tree root 实测为 `/home/mozzie/.orca/workflows/...`，强于「仓库 cwd 可接受」的要求）

**真实观测（全 PASS）**：

1. `GET /api/workflows` → 200，**14 项**，kd-nas 不在列。
2. `GET /api/workflows/nas-supernet` → 200，`subagents` 数组 **5 项**，name=md stem：
   `memory-verifier / project-fidelity-verifier / project-porter / supernet-evaluator / workflow-verifier`
   （description 全空 —— **by design**：真实 subagent md frontmatter 只有 subagent/version/sentinel 三键，fail-soft 文件名兜底；如需富描述可后续给 frontmatter 加 description 键，非缺陷）
3. `GET /api/workflows/nas-supernet/tree` → 200，顶层 `{agents: dir, subagents: dir, workflow.yaml: file}`，root=安装态。
4. `GET /api/workflows/agent-struct-exploration/tree` → 200，含 `knowledge_base/` + `scripts/` + `agents/`。
5. `GET /api/workflows/quant-sensitivity/tree` → 200，`agents/` 子树 = `['_quant_scripts', 'sensitivity-analyzer']`。
6. `GET /api/workflows/nas-supernet/file?path=subagents/memory-verifier.md` → 200，`ext=md size=5306`，正文首行 frontmatter `subagent: memory-verifier / sentinel: MM4ZR6`（真实文本）。
7. 附加守卫：越界路径 `../../workflows.yaml` → **404**。

**判定**：✓ works（SMOKE_FAILS=0）。

---

## 9. SPEC 成功标准 6 条逐条判定

| # | 标准 | 判定 | 证据指针 |
|---|---|---|---|
| 1 | `workflows/` 只有 14 个 per-wf 目录；R2' 域零残留 | **✓（2026-08-28 修复后）** | 结构半句 ✓（§4：14 目录、无根级 yaml/agents/subagents/knowledge_base）；残留半句：验证当时 ✗（`monitor_real_test.sh:6` 一处死链，MINOR_FIX），已于收尾 commit 修复 + grep 复验零命中（§7「修复后复验」）→ ✓ |
| 2 | install 同构；list/逐 wf load/validate | **✓** | §2（结构断言）+ §3（安装态探针 + R4' KB 三态）+ §5（validate 14/14 rc=0） |
| 3 | 全量单测绿 | **✓（按零新增判）** | §6：34 失败 = 30 条在参照提交 56d0db1 重放同挂（pre-existing 逐条坐实）+ 4 条负载 flake（双点隔离重跑绿）；**新增 = 0**。字面「全绿」在迁移前即不可达（pre-existing 集合存在） |
| 4 | web 硬验收：detail 含 subagents；tree 含全资产（scripts + agents 内脚本） | **✓** | §8：六断言 + 附加守卫全过，且命中安装态 |
| 4b | agent prompts 零内容改动 + 共享副本 sha256 一致 | **✓** | 迁移提交 56d0db1 md 统计：**348 R100（纯改名）+ 16 A + 0 M**；32 个 A 文件中 30 个在迁移前树有**逐字节同内容原件**（git blob hash 证明；含 evaluation_paradigm.md 的 rename 检测歧义个案——now=pre=31ae71c3）；唯二无同内容原件：`scripts/_migrate_per_dir.py`（计划内一次性脚本）与 `kb_graph.py`（计划 4b 明文改 KB 根锚定，且其迁移前 `scripts/kb_graph.py` 为**未跟踪**文件——A 无 D 由此而来，非内容漂移）；worktree 侧 `_quant_scripts` ×4 wf 与 nas-agent-pipeline↔nas-hp-search 共享 agent 全 sha256 MATCH |
| 5 | skill 文档 + 17 benchmark expected per-wf | **✓** | 17 case 实测：16 个 `expected/<wf-name>/`（目录名与 yaml `name` 一致，抽查 `01-nl-linear/expected/pipeline_linear/workflow.yaml` → `name: pipeline_linear`）+ case 14 平铺例外钉死不动；SKILL.md `:15/:34/:43-53` per-wf 落盘 + 目录树示意在案；功能性由绿测 `tests/test_skill_benchmark.py` 覆盖（在 §6 全量内） |
| 6 | 本报告完整记录 | **✓** | 本文件 |

---

## 待用户决策区

### 新发现缺陷（原 FAIL 项）
1. **`tests/iface/web/_e2e_artifacts/monitor_real_test.sh:6` 旧路径死链** — ~~MINOR_FIX~~ **已修复闭环（2026-08-28 收尾 commit）**：路径改为 `workflows/nas-supernet/agents/ns_run_train/scripts/…`，§7「修复后复验」grep 零命中。不再待决策。

### deferred 清单（前批已知，本次逐一核实仍在案）
2. **`workflows/puzzle/subagents/workflow-verifier.md:34` 断链后果**（UD-2 零改动铁律）：checklist 扫描指引指向旧 `workflows/agents/*/references/workflow-checklists/…`，迁移后查不到 → verifier 走 no-checklist fallback（静默降级）。白名单在案，待用户裁决是否修。
3. **struct yaml 描述性旧路径文本 4 处**：`workflows/agent-struct-exploration/workflow.yaml:20/87/108/239`（描述串/注释里的 `workflows/agents/_struct_scripts`）。功能性 default（`:59`）已是新路径，纯文本误导，非功能缺陷。
4. **`.gitignore:112` 旧条目 `scripts/kb_graph.py`**：文件已迁至 wf 目录，旧忽略条目残留（无害死条目；行号为本收尾 commit .gitignore 加行后的现行行号，验证时点原为 :109）。`.gitignore:97`（验证时点 :94）的 `kb_graph.html` 为无斜杠模式、匹配任意层级，迁移后依然忽略 wf 目录内生成的 HTML——不受影响。
5. **`tests/e2e_redesign/contract.py` 冻结**：`:60` 注释「kd 系已净删除」，目录整体冻结不改。
6. **KB kd 专属卡**：`workflows/agent-struct-exploration/knowledge_base/families/receiver/`（README + spt_*.py 多文件含 KD/蒸馏语义）——kd-nas 已删但 KB 知识卡保留（R2' 豁免域），是否修剪归用户。
7. **examples README**：仓库根 `examples/README.md` 自身无死链（其引用的 `examples/agents/` 项目级平铺池为双形态合法用法）；kd-nas-demo 等用户数据目录未动。前批所记「死链」未在本仓 R2' 域复现，如指其它文件请点名。
8. **reviewer 备忘 openFile 切 wf**：批 H（`37b4295` store 竞态守卫）已修，本次不重复计。
9. **`scripts/_migrate_per_dir.py` 已删**（2026-08-28 收尾 commit，git rm 历史留档）；原条目「计划步骤 8（commit I）删除；R2' 白名单在案」已闭环，不再待决策。

### pre-existing 失败集合明细（非本改造造成，30 + 4）
- [v3] audit 13：`tests/workflows/test_v2_audit_attribution.py`(8) + `test_v2_audit_resume_attempt.py`(5)，全 [v3] 参数；断言 v3 agent.md 内容契约（PYEOF heredoc / RESUME_SEARCH 分支 / summary substring），md 内容经 R100 证明未被迁移触碰 → v3 演进与审计测试漂移。
- in_session/cli/mcp 16：push_probe 5（WS 事件类型时序）、v3_step1 1（inputs_schema 键集）、skill_md 1（tars SKILL.md 缺 `orca doctor`）、mcp 6（3 需 opencode 后端 + 3 需 `uv` 二进制，本机环境缺）、bg_integration 1、web_does_not_import_cli 1（`run_manager` import `iface.cli.config`，KB 特性期已知张力）、exit_codes 1、puzzle_evaluator_recall 1。
- flake 4（双点隔离重跑绿）：mcp_tools server 1、script_env_inject 2（chart_sock，全量负载下）、puzzle_measure_baseline 1（错误文本自证 CPU 争用测量噪声）。

### v5 归因项（自有范围，非迁移回归）
- prof-opt `inputs_count 14→8` + description 重写：归因 `ff86ef0`（C5 v5 契约重写）；迁移提交 `56d0db1` 对 prof-opt yaml 为 similarity 100% 纯改名（git 证据在 §4）。

---

## 收尾摘要

**已证明真实可用**（真实命令 + 真实输出 + 真实退出码）：清场后真实 `tars install` 装出 14 个 per-wf 自包含目录（含 KB/scripts 收编）；真实 `orca list`（非仓库 cwd）14 wf 全部来自安装态；14 个安装态 yaml 逐一 `load_workflow` 零失败；R4' KB per-wf 解析三态（命中/不回退/显式坏路径 fail-loud）真跑通过；`tars validate` 14/14 零 warning；全量非 e2e 单测 4279 passed 且**零新增失败**（34 失败逐条经参照提交重放归因）；web 六硬断言 + 越界守卫全过且命中安装态；prompt 零改动铁律经 git（348 R100/0 M）+ blob hash（30/32 副本同内容 + 2 计划内例外）双证明。

**仍未证明**（及原因）：
- playwright 类与 `tests/e2e_*`：按契约排除（libnspr4 系统库缺失为已知 pre-existing 环境项）。
- `tars serve` 真实 socket HTTP（本次用进程内 TestClient，契约明示可接受；1MB/二进制守卫逻辑经单测覆盖）。
- `tars install` 二次运行的幂等性（同一安装态上重复 install）：未在本清单内，未验证。
- 前端浏览器层可见性（Subagents 区/资产树的渲染）：属「设计依赖部分」，本次只验后端硬断言；前端有批 H vitest 47 passed 记录（CURRENT.md），未由本 agent 重放。

**证据文件**（绝对路径）：
- 驱动脚本与结果：`C:\Users\mozzie\AppData\Local\Temp\orca_verify\`（step1_archive.sh / step2_install.sh / step3_probe.sh / step4_baseline.py / step5_validate.sh / step6_pytest.sh / step6_rerun.sh / step6_baseline_replay.sh / step7_grep2.sh + step7_grep_results.txt / step8_web_smoke.py / step9_4b_copies.sh / step3b_kb_probe.py）
- WSL 侧日志：`/tmp/install_out.txt`、`/tmp/step6_pytest_full.log`、`/tmp/step6_rerun.log`、`/tmp/pre_perwf_workflows_files.txt`（767 行旧态全清单）、`/tmp/pre_perwf_kb_files.txt`（75 行）
- 旧安装态快照（可回滚）：`~/.orca/_pre_perwf_snapshot_20260827/`（WSL 侧）
