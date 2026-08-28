# Workflows per-workflow 目录隔离改造（平铺 → per-wf 自包含目录）

> 2026-08-28 收尾。SPEC：`C:/Users/mozzie/.claude/plans/crystalline-chasing-dewdrop.md`｜计划：`docs/plans/2026-08-27-workflow-per-dir-layout-plan.md`｜E2E 验证报告：仓库根 `LAYOUT_MIGRATION_REPORT.md`（E2E_RESULT: PASS）。

## 1. 为什么改

旧 `workflows/` 是**平铺 + 全局共享池**形态：15 个 yaml 平铺在根、所有 agent 挤在全局 `agents/` 池、subagents 按 wf 二级挂在全局 `subagents/`、knowledge_base 在仓库根。结构性代价：

- **跨 wf 耦合**：删一个 wf（如 kd-nas）要在共享池里甄别哪些 agent 是它独占的，牵连 81 个文件；
- **多真相源**：共享 agent 被 2-4 个 wf 引用，install 时要 merge 共享池到安装态，升级语义复杂；
- **资产不可见**：web/CLI 无法展示一个 wf 到底"自带哪些 agent/subagents/脚本/KB"。

新形态：**每个 workflow 一个自包含目录**，删除/复制/展示都是整目录级操作。

## 2. 布局前后对比

**旧（平铺 + 全局池）**：

```
workflows/
├── nas-supernet.yaml            # 15 个平铺 yaml
├── nas-supernet-v2.yaml
├── puzzle.yaml
├── ...
├── agents/                      # 全局共享池（~69 个 agent 目录混住）
│   ├── ns_run_train/
│   ├── _quant_scripts/          # 共享脚本池（跨 wf 引用）
│   ├── _puzzle_scripts/
│   └── ...
├── subagents/                   # 二级：subagents/<wf>/*.md
│   ├── nas-supernet/*.md
│   └── puzzle/*.md
└── (knowledge_base/ 在仓库根)
```

**新（per-wf 自包含）**：

```
workflows/
├── nas-supernet/
│   ├── workflow.yaml            # 目录名 == yaml name 字段
│   ├── agents/                  # 本 wf 全部 agent（原池分流）
│   │   ├── ns_run_train/
│   │   └── ...
│   └── subagents/               # 拍平为 wf 内一级目录
├── puzzle/
│   ├── workflow.yaml
│   ├── agents/                  # 含 _puzzle_scripts（独占池整迁）
│   └── subagents/
├── agent-struct-exploration/
│   ├── workflow.yaml
│   ├── agents/                  # 含 _struct_scripts
│   ├── knowledge_base/          # 仓库根 KB 整树收编（唯一使用方）
│   └── scripts/kb_graph.py      # 原仓库根 scripts/kb_graph.py 随迁
└── ... （共 14 个 wf 目录）
```

规则：**wf 目录名 == yaml `name` 字段 == 原 yaml 文件名 stem**（迁移前置探针 14/14 验证）。共享池按引用关系分流：独占池（`_struct_scripts`/`_puzzle_scripts`/`_po_scripts`）整目录 git mv 到唯一使用方；被多 wf 引用的资产复制多份（见 §4）。

## 3. 双形态兼容（加载层，批 C：commit `2445674`）

新 `orca/compile/layout.py` 作为布局**单一真相源**，catalog / orchestrator / validator 三处 import 复用：

- **catalog 扫描** `scan_workflow_yamls`：**平铺优先**（同目录两种形态并存时平铺 yaml 先被发现），再扫 `*/workflow.yaml`——旧项目级平铺用法**继续可用**；
- **subagents 解析** `resolve_subagents_dir`：双形态（`workflows/subagents/<wf>/` 旧 vs `workflows/<wf>/subagents/` 新），带误命中守卫（目录下须有 `*.md` 才认）；
- **KB 解析** per-wf 来源链：`ORCA_KB_DIR`（env）> config > `<wf>/knowledge_base`（判据含 `index.json`）> `~/.orca/knowledge_base` > cwd，`_INJECTED_KB_ENV` 防进程级 env 伪显式；
- render / validator 的错误文案双形态化（报错提示同时给新旧两种可能路径）。

## 4. 共享资产复制表

被多 wf 引用、无法唯一归家的资产，**复制多份、逐文件 sha256 一致**（git blob hash + worktree 双证明，见报告 §9 标准 4b）：

| 资产 | 份数 | 去向 |
|---|---|---|
| `supernet-train-script` / `nas-search-pipeline` / `nas-train-runner` / `nas-select`（4 个 agent） | ×2 | `nas-agent-pipeline/agents/` + `nas-hp-search/agents/`（首份 git mv 到 nas-agent-pipeline，其余 cp） |
| `_quant_scripts/_common.py` + `_device.py` | ×4 | `quant-ptq-sweep` / `quant-qat` / `quant-sensitivity` / `quant-bit-curve` 各一份（只取两文件子集；prune-channel-sweep 自包含不用） |

代价是副本间未来可能漂移（改一处忘另一处），换取目录级自包含与删除安全性——SPEC 明示的取舍。

## 5. kd-nas 净删除（批 B：commit `a7cb0a5`）

迁移前置：kd-nas 早已被用户判死（memory：2026-08-11 实测跑不通），随本改造**净删除**。81 文件、-21690 行：

- `workflows/kd-nas.yaml`（605 行）；
- 全局池 10 个 kd 系 agent 目录：`_kd_scripts` / `decide` / `distill` / `gen-student` / `kd-setup` / `kd-train-script` / `model-flatten` / `teacher-gen` / `train-script-verify` / `train-teacher`；
- `subagents/kd-nas/project-fidelity-verifier-kd.md`；
- `scripts/e2e_kd_nas_launch.sh`、`scripts/e2e_kd_nas_script_level.sh`；
- 测试：10 个整删（`tests/workflows/` 下 9 个 kd 专属测试 py + 1 个 kd replay fixture jsonl；计划预期 11 个 py 中的 `test_struct_kd_p7` / `test_receiver_variants` 改判保留 kd 外用例）+ `tests/e2e_redesign/contract.py` kd 条目清零；
- 全仓 `kd-nas` / `_kd_scripts` 注释死例换存活例（orca/exec、run、iface 等 10+ 文件）。

## 6. install 重构（批 E：commit `aeb22b0`）

旧 install 三函数（workflows / knowledge_base / subagents 分别 merge 到安装态）合一为 **per-wf 整树 sync**：源 `workflows/` 下每个含 `workflow.yaml` 的子目录 `copytree` 整树到 `~/.orca/workflows/<dir>/`（忽略 `__pycache__`/`*.pyc`）。CLI 输出按 per-wf 目录打印（`<wf>/`（agents N · subagents M · knowledge_base））。

**旧布局 backup 清理（UD-1 四分支）**——install 时对旧安装态 `~/.orca/workflows/agents|subagents/`、`~/.orca/knowledge_base/` 逐一判定：

1. 条目名**不在随包集合**（用户自建）→ 整目录移入 `~/.orca/_legacy_layout_backup_<date>/` + warn 清单；
2. 名在随包集合但**内容不一致**（逐文件 sha256 比对；共享 agent 多副本 any-match）→ backup；
3. **平铺 yaml 一律 backup**（与随包 wf 同名者为升级安装残骸，防其按「平铺优先」shadow 同名 per-wf 新目录；未知尸体如 po-probe.yaml 同样入 backup）；未知非 yaml 只 warn；
4. 与随包**完全一致**（纯我们装的）→ 直接删。

即：**只删确定无信息损失的，凡可能含用户改动一律 backup 留档**。

## 7. web 全资产展示（批 G/H：commit `31ed2cd` + `37b4295`）

- 新端点：`GET /api/workflows/{name}/tree`（wf 目录资产树，root 指向实际解析到的安装态/源态）、`GET /api/workflows/{name}/file?path=<rel>`（文件内容，路径越界守卫 404）；detail 端点（`GET /api/workflows/{name}`）新增 `subagents` 数组（name=md stem，fail-soft 文件名兜底）；
- 前端：WorkflowBrowsePage 新增 **Subagents 区**与**资产树**浏览（workflow-browse-store）；批 H 补 store 跨 wf 切换竞态守卫（wf 身份快照 + fileSeq gate + 入口 bump 作废在途请求）。

## 8. create-workflow skill per-wf 产出（批 F：commit `e6acda2`）

skill 产出布局同步：新 workflow 默认落盘 `./workflows/<name>/workflow.yaml`，agents 落 `<name>/agents/`、subagents 落 `<name>/subagents/`、design.md 入 wf 目录；SKILL.md 新增「产出布局」目录树示意；reference 五文件锚定/范例路径同步；benchmark 16 个 case 的 expected 重排为 `expected/<wf-name>/`（目录名 == yaml name 字段，30 个纯 rename 零内容改动；case 14 平铺例外钉死不动）。

## 9. 迁移 commit 链

| commit | 批 | 内容 |
|---|---|---|
| `a7cb0a5` | B | kd-nas 净删除（81 文件 -21690 行） |
| `2445674` | C | 加载层双形态（layout.py 单一真相源 + KB per-wf 来源链 + 单测） |
| `56d0db1` | D | workflows/ 大迁移（14 wf 目录 + ~69 agent 分流 + 共享副本 + KB/scripts 收编；md 铁律 348 R100 + 0 M） |
| `aeb22b0` | E | install per-wf 整树 sync + 旧布局 backup 四分支 |
| `e6acda2` | F | create-workflow skill 产出布局 per-wf 化 + benchmark expected 重排 |
| `31ed2cd` | G | web detail subagents + tree/file 端点 + 前端 Subagents 区/资产树 |
| `37b4295` | H | review 修复：store 跨 wf 竞态守卫 + 4 处布局注释同步 |
| 本 commit | I | 收尾：monitor_real_test.sh 死链修复 + release note/CHANGELOG/CURRENT + 删一次性迁移脚本 `scripts/_migrate_per_dir.py`（git 历史留档） |

（批 A `a379375` 为前置：固化 create-workflow skill v2 基线 + 本改造计划/基线清单入库。）

## 10. 存量项目影响

- **外部项目的项目级 `./workflows` 平铺布局**（双形态兼容）：**继续可用，无需任何动作**。加载层平铺优先、双形态解析（§3）。
- **`~/.orca` 用户级旧布局安装态**：**下次 `tars install` 自动处理**——旧 `agents/`、`subagents/`、`knowledge_base/` 按 §6 四分支判定，凡可能含用户自建/自改内容一律移入 `~/.orca/_legacy_layout_backup_<date>/` 留档（不直接删）。装完即得 14 个 per-wf 自包含目录。
- **共享资产副本**（§4）：今后改动这 4 agent / 2 脚本需同步多份（或日后引入引用机制，当前 SPEC 裁决为复制）。

## 11. 验证与遗留

E2E 真实执行全链验证（零 mock：清场 → 真实 `tars install` → 安装态 `orca list`/逐 wf load/`tars validate` 14/14 → 全量单测 4279 passed 零新增失败 → web 六硬断言命中安装态 → prompt 零改动 git+blob hash 双证明），详见仓库根 **`LAYOUT_MIGRATION_REPORT.md`**（E2E_RESULT: PASS；验证当时唯一 MINOR 缺陷 monitor_real_test.sh 死链已于收尾 commit 修复闭环）。

**待用户决策项**（详见该报告「待用户决策区」）：puzzle verifier checklist 旧路径断链（UD-2 零改动铁律下走 no-checklist fallback）、struct yaml 4 处描述性旧路径文本、KB kd 专属知识卡是否修剪、`.gitignore` 旧 `kb_graph.py` 死条目等。
