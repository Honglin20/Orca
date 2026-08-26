# Prof-Opt 设计草稿 (SDD)

> 跨阶段设计议题，prof-opt 各 phase SPEC 撰写前必读。
> **定位**：全新 workflow `workflows/prof-opt.yaml`（非 ns3/psu fork——问题域不同：ns3/psu 是「搜结构」，prof-opt 是「profiling 证据驱动的结构修改闭环」）。**继承 ns3/psu 的编排骨架资产**：全 agent 节点 + 确定性脚本资产、生成/执行节点分离、有界轮询执行节点、reuse_check 幂等、project_manifest 跨会话记忆、reporter 收敛（磁盘读态、零跨节点 output 引用）、外部脚本唯一权威哲学。
> **问题重述**：profiling（OR-Tools 调度）已是给定算子集下的最优排布 ⇒ 算子融合/调度重排无收益空间，**唯一杠杆 = 改变算子组合的构成（模型结构）**。闭环 = 实测硬件成本模型驱动的提案生成 → 时延证伪 → 精度证伪 → 组合迭代。
> **审查状态**：R1 fail（33 项：4B/12M/13m）→ v2 全闭环；R2 fail（**窄域收敛型**，26 项：2B/8M/16m，全部为落点钉死/措辞改写，**无架构返工项**；R1 四 blocker 核销属实、骨架成立）→ v3 全闭环；R3 targeted re-check（R2 验收线五处）：**conditional-pass → 三残留 + 建议级全部闭环进 v3.1 → SPEC-ready**；v3.2 用户拍板（D13 三档）/v3.3 SPEC-R1 回卷/v3.4 E2E-R1 回卷；**v3.5 训练范式切换（用户 2026-08-21 拍板：彻底从头训练、proxy 公平对比、pre-trained 仅参考）**——D5/D11/D13/D15/§3/§5/§6/§7/§9/§10/§11 全面改写（详见附 A v3.5），实现与 E2E 按此重跑。

---

## 0. 用户诉求与已确认决策

**用户诉求**（2026-08-20 两轮讨论拍板）：
1. 有一个 profile 函数（onnx 进 → profiling 三件套出），先用 placeholder，真函数后续替换；
2. 分析产出瓶颈报告，**定位到具体操作**；
3. 依据瓶颈 + 硬件特性 + SOTA 提出**模型结构级**优化方案（明确排除算子融合）；
4. 方案先测时延（重 profile）是否达预期，再测精度；
5. 有 pre-trained 基线：除被替换组件外其余权重直接加载微调；
6. zero-cost 不做（候选少 + 有继承权重 + 目标直接可测，proxy 无增益）；
7. 落地要求：**用户项目只改模型文件**、预处理参考 ns3 flatten、有历史记录管理、变体连接用户原训练脚本（不改训练代码，至多调 epochs / 传 ckpt）、评估 v1 串行、**不达目标持续循环**、评估分层（时延过了才微调，微调到什么程度才配完整训练）。

### 决策表

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 用户项目改动面 | **零既有文件修改**；新增写入仅两处：① `$ORCA_ARTIFACTS_DIR`（= `<project_root>/artifacts/prof-opt/` 子树，工作区）② 终态 success 且 `write_back=true` 时 po_report 一次性写回。**写回源 = 最终全局 shadow（= winner 完整树）相对用户项目对应原文件的 diff 全集**（用户既有文件全程只读，天然参照，机械正确——不依赖 declaration.edited_files 的单轮增量语义）；写回文件名 = 原相对路径 + `<stem>_prof_optimized<suffix>`（保持目录结构；同名冲突 → 不覆盖 + 报告冲突清单）；diff 中 shadow 侧**删除**的文件不写回、报告列示（v1 四杠杆均为原地修改不删文件，实际不触发——R3 声明）；**写回前复验 BASELINE.lock**，失配 → 列冲突不写；**启用语义显式声明**：新文件名 ⇒ 用户须手动把 import 切到新文件（与「零训练代码改动」的张力如实披露——改的是模型文件，训练入口 import 语句的切换是用户的一次性动作） | R1-B2 + R2-B1：轮间叠加下按单轮 edited_files 写回会静默丢上游改动文件；终局 shadow diff 是唯一机械正确的写回源 |
| D2 | 变体 → 用户训练脚本的连接 | **shadow 包 + sitecustomize meta-path-finder 注入**（§2.2/§2.3，实测唯一可行形态）。注入物全部在 `$ORCA_ARTIFACTS_DIR`（orca_inject/ + scripts/），**零用户既有文件写入**（新增仅 artifacts/ 子树与终态新文件，见 D1） | R1-B3 实证；U4：Q4 否决的是往用户项目注入，边界不同 |
| D3 | baseline 锚点 | baseline onnx **恒从 shadow 副本导出**；`reference_onnx` 交叉校验落 po_baseline 链**首步**（形状/op 集不一致 → 警告不阻断）；导出确定性钉死：同一 `export_onnx.py`、同 seed、同 opset 17、静态 shape，禁 `python -c` 内联 | 变体与 baseline 同源导出才可比 |
| D4 | 循环载体 | **DAG 回边**：`po_gate → po_propose`（编译层显式容环——`compile/validator.py` `_check_entry_reachable_to_end` 不动点容环）。**in-session 无 max_iter 兜底**（`resolve_max_iter` 仅 Orchestrator 路径）⇒ 防死循环双保险 = po_gate 脚本内轮数硬帽。**开工前置验证 P0**（§8.1）：纯 agent 双节点回边 in-session E2E + **跨节点引用回边节点 output 的语义证伪实验** | 轮次控制归 orchestrator 路由；执行节点折叠为降级预案（§8.2） |
| D5 | 评估分层 | L0 时延 gate（不烧 LLM 判断与训练预算）→ L2 **proxy 从头训练 + eval**（同预算公平对比；v3.5 起无 L1 零样本——从头范式下随机初始化的零样本精度无意义）→ L3 完整训练（仅 winner）。公式 §7 | 贵的验证只花在便宜验证已通过的变体上 |
| D6 | 组合语义 | **轮内互斥**；**轮间叠加**（下一轮 base = 本轮双 gate 通过的 best，lineage 链）；无 promoted → base 不动 | beam-width-1 顺序组合 |
| D7 | 历史管理 | `BASELINE.lock`（key_inputs = 结构锚 `{model_path, pretrained_ckpt}`——不含 target_makespan 等目标类 input，改目标不该炸锁；py 校验和**限 shadow 闭包内 *.py**）+ `history.jsonl`（append-only 多版本行 + 分节点 append 职责）+ `best.json` + `.run_lock` + 变体 DONE marker。详见 §5 | R1-M5/M8 + R2-m5 |
| D8 | 时延主单位 | **cycles**；`freq_ghz` 仅展示换算不进 gate | 消除换算错误面 |
| D9 | 提案来源 | **playbook 四杠杆**（激活替换 / 归一化结构 / 深宽重构[v1 排除] / 计算搬移[v1 仅零参数差异条目]）；**提案准入**：`predicted_delta_cycles < 0`（严格负，顺带消解除零）；`edited_files ⊆ shadow 闭包`（变体只许改 shadow 内文件） | R1-M6 + R2-m10/M8 |
| D10 | placeholder profiler | 自带，保真度目标 = **delta 方向正确**。SPEC 标注：placeholder 下 `min_pred_actual_ratio` 近乎恒真（预测与实测同源），该 gate 仅真 profiler 下有区分力 | R1 evaluator 洞见 |
| D11 | 精度预算与 promote 锚（v3.5 重写：从头训练范式） | **训练范式 = 一律从头训练，无权重继承**（用户 2026-08-21 拍板：基线与优化模型同预算从头训才有可比性；pre-trained 仅作参考）。promote 锚 = **baseline_proxy_acc**（基线以 proxy 预算**从头**训练 + eval，po_baseline 期产出；幂等判据 = `baseline/baseline_proxy_acc.json` 存在性）；`promoted ⇔ variant_proxy_acc ≥ baseline_proxy_acc − promote_relax × accuracy_budget`（同预算同数据公平对比——这正是从头范式下 promote 语义比微调恢复力更干净的地方）。**公平不变量**：同一 run 内基线与全部变体的 proxy 训练参数（epochs/数据子集/步数/seed）逐字段一致，唯一来源 contracts.json。final gate 锚 = `baseline_ref_acc` input（用户已知收敛精度）；缺省 → workflow 以完整预算从头训基线一次并缓存（`baseline/baseline_full_acc.json`，跨轮跨 run 复用） | 微调恢复力会偏向「继承友好」而非「结构更优」的结构；从头同预算直接测「哪个结构值得训」，且解锁 weight_delta 装不下的结构改动（深宽/低秩，OCP 加条目） |
| D12 | 交付物 | 终局 shadow diff 写回（D1）+ `prof_opt_report.md` + winner onnx + final ckpt。`final.makespan` = winner 结构 makespan（结构决定时延，引用 best.json，非重新测量） | R2-m16 附注 |
| D13 | 训练/评估入口契约（**适配三档**，v3.2 用户拍板修订，v3.5 参数需求随从头范式更新） | 契约参数需求：**train** ① epochs ③ out-dir ④ 步数/批次截断 ⑥ **数据子集/限量旋钮**（**有则必用**——proxy 预算优先砍数据量；无则退到纯 epochs 缩短，v3.5 删② init-ckpt——无继承语义）；**eval** ⑤ ckpt 路径（contract 期**双 ckpt 实测** metric 联动）。满足路径三档：**Tier A** = 用户入口已参数化 → 直接模板化，零适配；**Tier B** = 缺个别参数 → po_contract 按 ns3 **User-Paradigm Authority Iron Rule** porter 生成适配入口（`train_proxy_entry.py` / `eval_entry.py`，落 `$ORCA_ARTIFACTS_DIR`）：用户训练范式（loss/optimizer/scheduler/数据流/metric/eval 入口）**逐字移植**，唯一允许改动 = 契约开关参数化（epochs/out-dir/截断/数据子集）+ proxy 预算压缩，过 User-Paradigm 自检 + 双 ckpt 实测，**用户文件零改动**；**Tier C** = 训练逻辑强耦合无法安全参数化（罕见）→ `viable=false` fail loud。**防漂移**：contracts.json 记 train/eval 入口文件 sha256，失配 → 契约重做 fail loud。**proxy 预算落盘**：选定的数据子集值/epochs/步数/seed 记 `contracts.json.proxy_budget`（基线与变体共用单一来源——D11 公平不变量） | R1-B2/U3 + R2-B2 + 用户 2026-08-20/21 两轮拍板 |
| D14 | 节点形态 | **全 `kind: agent`**（in-session v1 仅支持 agent——`step.py:876-888`）。确定性逻辑 = agent 执行脚本资产 + emit 单行 JSON（psu emit 模式）；「stdout 纯 JSON、日志走 stderr」硬契约。**跨节点共享静态脚本**（placeholder_profiler.py / analyze.py / assert_shadow.py / predict_delta.py / advance_round.py 等）flatten 期复制到 **`$ORCA_ARTIFACTS_DIR/scripts/`**，全节点经此路径执行（`$ORCA_AGENT_RESOURCES` 是 per-node 的，共享脚本放单节点 resources 下其余节点不可达） | R1-N1/U1(a) + R2-M7 |
| D15 | 训练范式（v3.5 新增） | **一律从头训练**：变体与基线的 proxy/L3 训练都从固定 seed 随机初始化开始，不加载任何权重；`make_variant_ckpt` / weight_delta 机制 / γβ 折叠 / 零样本灾难线（L1）**整体退役**（OCP 留档：将来若做「继承加速模式」再捞回）；结构校验收敛为**两层**（文件层 ⊕ edited_files、图层 op_type 计数差 ⊕ op_delta——权重层随继承退役）。**proxy 旋钮发现**（D13 train 契约扩展）：contract 期枚举用户训练入口的现成可调参数——数据集子集/限量类旋钮（**优先**，「加载少量数据集」）、epochs（基本都有）、步数截断、batch——按用户预设参数调整，不发明新参数；proxy 预算值由 contract 选定并记 contracts.json，**基线与变体逐字段同值**（D11 公平不变量）。`pretrained_ckpt` 从 [ask] 降为 [advanced] 可选**仅参考**（不进任何 gate；报告可引用其已知精度作对照） | 用户拍板 + 设计论据见 D11 |

### 继承 ns3/psu 的骨架资产（不重新设计）

- 全 agent 节点 + 确定性脚本资产（reuse_check.sh / check_*.sh / emit 单行 JSON）
- `$ORCA_ARTIFACTS_DIR`（project-scoped）+ `$ORCA_AGENT_RESOURCES` 资源锚点
- `project_manifest.md`（metric 方向必标 higher/lower-better）
- 执行节点「有界轮询 + 常驻到真正完成 + self-heal 仅补丁层 + **禁二次 detach**（重入先查存活进程，防双训练进程互写 ckpt——ns3 铁律 6）」
- 长任务协议：单次 bash 调用 cap ~10min，长步骤（训练/满 budget eval/真 profiler）**detach + 有界轮询**，跨 turn source of truth = 节点状态文件（ns3 train_status.md 模式）
- reporter 收敛 + 每节点 catch-all → report + workflow outputs 全读 `po_report.output`
- pathlib / 生成代码英文化 / `.user_pkg`

---

## 1. 总体形态：DAG + 回边循环（全 agent 节点）

```
                          ┌────────────────────────────────────────────┐
                          │  轮次循环（DAG 回边；po_gate 脚本内硬帽双保险）│
                          │                                            │
 po_flatten (agent)       │   po_propose (agent) ──────────────┐       │
   ↓                      │     ↑ 开头跑 analyze.py → base/bottleneck    │
 po_contract (agent)      │     │   _report.json（含首轮后的每轮刷新）    │
   ↓                      │     │                                │       │
 po_baseline (agent·脚本链)│     │        未达成目标且可继续         │       │
   ↓ ─────────────────────┼──── po_gate (agent·脚本) ←── po_probe (agent) │
 po_propose (agent) ──────┘          │ 达成/耗尽                  ↑       │
   ↓                                 ↓                           po_verify (agent·脚本)
 po_implement (agent)          po_full_train (agent)              ↑       │
   ↓                                 ↓        ┌────────────────────┘       │
 po_verify (agent·脚本) ─────────→ po_report (agent) → $end               │
```

「agent·脚本」= agent 节点执行 `$ORCA_ARTIFACTS_DIR/scripts/` 下确定性脚本并 emit 其单行 JSON（D14），判断量恒为零。

| 节点 | 职责 | 判断/确定 |
|---|---|---|
| po_flatten | shadow 建立 + BASELINE.lock + manifest + 就绪检查（§3.1） | 勘察判断 + 脚本校验 |
| po_contract | 三契约发现 + 实测验证 + D13 硬前置 + run 模板生成（§3.2） | 判断 + 脚本验证 |
| po_baseline | reference_onnx 交叉校验 → export → profile → analyze → 基线参考精度 → 基线 proxy 从头训练（§3.3） | 纯脚本链 |
| po_propose | analyze.py（当前 base）→ playbook + history 去重 → proposals.json（§9） | 判断（提案）+ 脚本 |
| po_implement | 逐提案变体实现 + make_variant_ckpt + DONE marker；失败记 skipped 不阻断 | 判断 + 脚本校验 |
| po_verify | 三层声明校验 + 重 profile + 时延 gate → verdicts.jsonl（§7.1） | 纯脚本 |
| po_probe | 幸存者串行 L1→L2 + promote；轮末 advance_round.py 推进（§7.2-7.3） | 执行 + 脚本 gate |
| po_gate | **纯读现算零写盘**：读盘面 → decision；脚本内轮数硬帽（§7.5） | 纯脚本 |
| po_full_train | winner 完整微调 + final eval（psu 常驻模式，§7.4） | 执行 |
| po_report | 终端 reporter + 终局 shadow diff 写回（D1） | 磁盘读态 |

节点数 10，全 agent。

---

## 2. 用户项目改动面与 shadow 注入机制

### 2.1 改动面总表

| 路径 | 何时被写 | 内容 |
|---|---|---|
| （用户既有文件） | **永不** | 全程只读 |
| `<project_root>/artifacts/prof-opt/**` | workflow 全程 | 工作区（shadow / 变体 / 历史 / 脚本 / 注入物） |
| `<orig>_prof_optimized.*`（按原相对路径） | 仅终态 success 且 `write_back=true`（po_report） | 终局 shadow diff 全集（D1）；冲突不覆盖；写回前复验 lock |

原地副作用豁免：contract 期干跑**前后对 project_root 做内容快照 diff（排除 artifacts/）**得出 `exemptions[]`，报告披露。

### 2.2 shadow 包

- flatten 期解析 `model_path` import 语境：模块限定名 + **`ORCA_SHADOW_PKGS` = shadow 根下全部顶层模块/包名**（flatten 机械枚举——包形态 = 顶层包名；裸 `model.py` 形态 = `model.py` 及其本地依赖闭包复制到 shadow 根下，**闭包内每个兄弟顶层模块名全部入列**，防「改了 shadow/layers.py 但 import 仍解析回原文件」的静默不生效——R2-M8）。
- 复制排除：`__pycache__/`、`*.pyc`、`.git`；单文件 >10MB 非代码 → fail loud 列清单。BASELINE.lock 校验和限 shadow 闭包内 `*.py`（D7）。
- 每变体完整复制 base shadow 到 `variants/<vid>/shadow/`（排除同上）。
- **stdlib 撞名预检**（flatten 期静态）：shadow 顶层名 ∩ `sys.stdlib_module_names` 非空 → fail loud（撞名模块经 §2.3 stdlib guard 会解析回原文件，运行时断言必败——预检提前到入口暴露，R3 建议）。
- shadow 过期守卫：lock 校验和不匹配 → fail loud 要求 `fresh_start`。

### 2.3 注入机制（实测验证，2026-08-20）

**已证死**（勿再尝试）：① PYTHONPATH 直注（script 形态 sys.path[0]=脚本目录优先）；② sitecustomize + `sys.path.insert(0)`（脚本目录在 site 初始化后才插，仍输）。

**采用**：`$ORCA_ARTIFACTS_DIR/orca_inject/sitecustomize.py`（静态资产，flatten 期复制）：

```python
import os, sys
from importlib.machinery import PathFinder
_s = os.environ.get("ORCA_SHADOW_DIR")
_pkgs = frozenset(filter(None, os.environ.get("ORCA_SHADOW_PKGS", "").split(",")))
if _s and _pkgs:
    class _ShadowFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname in sys.stdlib_module_names:   # 防遮蔽同名 stdlib 模块（R2-m1）
                return None
            if "." not in fullname and fullname in _pkgs:
                return PathFinder.find_spec(fullname, path=[_s])
            return None
    sys.meta_path.insert(0, _ShadowFinder())
```

- meta finder 先于 sys.path 扫描拦截顶层名 → 解析到 shadow；子模块经父包 `__path__` 跟随；裸模块 spec 解理相同（R2 已核）。
- 运行模板统一：`cd <project_root> && ORCA_SHADOW_DIR=<shadow_dir> ORCA_SHADOW_PKGS=<pkgs> PYTHONPATH=<artifacts>/orca_inject<sep><project_root> <sys.executable> <入口> …`——**`<sep>` 按 `os.pathsep` 渲染**（Windows 原生为 `;`，R2-m11）；`sys.executable` 绝对路径记于 contracts.json（防 env 漂移）。
- **运行时断言**：`assert_shadow.py`（scripts/ 静态资产，**遍历全部 `ORCA_SHADOW_PKGS`** 断言各 `__file__` 在 shadow 内），以与训练完全相同的调用形态执行，嵌入每个 run 模板，失败即中止。禁 `python -c` 断言（形态不同 = 假绿）。
- **一切 import 用户模型的运行**（export / eval / probe / full / 断言）恒带注入头（R2-m12）。
- 副作用披露（R2-m11）：注入物优先于用户自有 sitecustomize（若用户项目依赖自己的 sitecustomize 行为需在 contract 期发现并合并）；`-S`/`-E` 解释器旗标会杀注入——contract 期实测发现即 fail loud。

---

## 3. 预处理

### 3.1 po_flatten（entry）

1. **Reuse-check**（脚本）：`.run_lock` 无他 run 存活（run_id+pid+心跳 mtime；检测到存活 → fail loud）→ BASELINE.lock 匹配 + shadow/manifest 存在 → REUSE；不匹配 → fail loud 提示 `fresh_start`。`profile_script_path` 非空时存在性校验（fail loud——R2-m14）。
2. **项目勘察** → `project_manifest.md`（ns3 骨架；metric 方向必标）+ `.user_pkg`。
3. **shadow 建立**（§2.2）+ `orca_inject/` + `scripts/`（共享静态脚本，D14）落位。
4. **就绪检查**（mandatory，失败 fail loud 列清单）：模型可构造（参数静态可推断）/ 可导出（静态 shape + opset 17）/ 模型定义可静态定位。`pretrained_ckpt` 若提供（v3.5 起可选仅参考）：可 torch.load 载入即过（信息性，不做 strict 键比对——无继承语义）。
5. **flatten 分析视图**（可选）：定义散布 >2 文件时生成 `<base>_flat.py`（仅分析视图，执行恒走 shadow）。
6. memory-verifier 子代理复核（ns3 协议）。

产出 schema：`flatten_passed, shadow_root, shadow_pkgs[], model_module, manifest_path, baseline_lock_path, error, generated_artifacts`。

### 3.2 po_contract（三契约）

| 契约 | 内容 | 实测验证 | 硬前置（D13） |
|---|---|---|---|
| train | 入口命令；argparse 映射：epochs / out-dir / seed（若支持）/ 步数截断机制形态 / **数据子集/限量旋钮（v3.5 ⑥——有则必用，无则退纯 epochs）**；ckpt 输出路径规则（时间戳随机目录 = fail loud）；`train_epochs_full` 钉 argparse 机械事实；`sys.executable`；**`proxy_budget`**（epochs/数据子集值/步数/seed——用户未显式给 probe_epochs 时 epochs = min(1, train_epochs_full)、显式给时 = min(用户值, train_epochs_full)；在此选定写入 contracts.json，input default 置空，R2-m2 + v3.5 扩展） | 最小预算干跑一次（**从头，无 ckpt**）：区分「epochs=0 报错但脚本可跑」（合格）vs「脚本跑不通」（不合格）；确认 out-dir 生效、输出 ckpt 路径可预测 | ①③④ 任一缺 → viable=false（⑥ 有则必用） |
| eval | 入口命令 + metric 提取（stdout 正则 / json path）+ 方向 + **ckpt 参数** + 容器形态（bare / wrapper:key） | **双 ckpt 实测**：两个不同 ckpt 各跑一次，metric 必须随之联动（不联动 = 测的不是传入 ckpt，fail loud）；**两 ckpt = 两个不同 seed 的随机初始化**（seed 0/1，直接实例化模型存 state_dict——v3.5 替扰动副本，快速无需训练，随机初始化间 eval 差异足够判别） | ⑤ ckpt 路径可参数化 |
| export | 已有脚本钉 argv；无则生成 `export_onnx.py`（D3 确定性） | 实测导出 + `onnx.load` + shape_inference | — |

生成物：`run_probe_finetune.sh` / `run_full_finetune.sh` / `run_eval.sh` / `export_onnx.sh`（均含 §2.3 注入头 + 运行时断言 + `sys.executable`）；**Tier B 时额外生成适配入口** `train_finetune_entry.py` / `eval_entry.py`（D13 porter：用户范式逐字移植 + 契约开关参数化，User-Paradigm 自检通过），run 模板指向适配入口而非用户原入口。

产出 schema：`viable, reason, contracts_path, run_probe_script, run_full_script, run_eval_script, export_script, metric_direction(higher_better|lower_better), train_epochs_full, proxy_budget{epochs,dataset_knob,data_value,max_steps,seed}, probe_cap_mechanism, exemptions[], error, generated_artifacts`（v3.5 与 §11 一致：`baseline_acc_raw`/`probe_epochs_effective` 删）。`viable=false` → po_report（归因 `training_prerequisites_missing`，psu D17 双保险）。

### 3.3 po_baseline（agent·脚本链）

链首到链尾（**每步产物存在性 = 逐步幂等判据**，重入跳过已完成步——R2-M3；wrapper 状态文件 `baseline_status.md` 跨 turn 记录进度）：

```
① reference_onnx 交叉校验（D3，警告不阻断）
② export_onnx.sh                → base/model.onnx
③ profile(base/model.onnx)      → base/profile/{四件套}
④ analyze.py base/profile       → base/bottleneck_report.json
⑤ 基线参考精度                  → baseline/baseline_ref.json        # baseline_ref_acc input 若给出则落盘；缺省留空（final 期自动补满训，见 §7.4）
⑥ 基线 proxy 从头训练（D11 锚） → baseline/baseline_proxy_acc.json  # 固定 seed 从头训练 @proxy 预算（contracts 单一来源）+ eval；幂等判据 = 文件存在性；detach+有界轮询
⑦ target_makespan 校验          # base.makespan < target 否则 fail loud 指引
```

rc 聚合：每步独立脚本 rc，wrapper 汇总；训练/满 budget eval/真 profiler 等长步骤（⑥及真 profiler 下③）走 **detach + 有界轮询子模式**（单次 bash cap ~10min，ns3 铁律 7——R2-M3）。任一步最终失败 → business-failed → po_report（stage=baseline）。

`analyze.py` 产出：makespan_cycles / pipeline 分解（以 taskgraph 实际 pipeline 值为准）/ 关键路径 / **热点模式**（重复 op_type 签名聚类：次数、总 cycles、占比、onnx 节点名清单、task_id 清单——定位到操作）/ cost table。**落点恒 `base/bottleneck_report.json`**——首轮由本节点生成，后续轮由 po_propose 开头重跑刷新（advance 后的 winner profile 为输入，R2-M2 加重 3）。

---

## 4. Profiler 契约（用户可替换核心接口）

```
<profile_script> --onnx <path> --out-dir <dir> [--seed 0]
→ taskgraph.json / ops.csv / schedule.json / profile_summary.json{makespan_cycles, op_count}
rc=0；unsupported op → rc≠0 + stderr 列 op_type（禁静默跳过）
```

`profile_script_path` [advanced]：空 = placeholder；非空 = 全链唯一权威（禁 fallback）。placeholder 限制标注见 D10。

---

## 5. 变体与历史管理

### 5.1 目录布局

```
$ORCA_ARTIFACTS_DIR = <project_root>/artifacts/prof-opt/
├── .run_lock / BASELINE.lock（D7 语义）
├── project_manifest.md / .user_pkg
├── orca_inject/            # sitecustomize.py（§2.3）
├── scripts/                # 跨节点共享静态脚本（D14）：placeholder_profiler.py / analyze.py /
│                           # assert_shadow.py / predict_delta.py / perturb_ckpt.py /
│                           # advance_round.py / emit_*.py（export_onnx.py 不在此——contract 期
│                           # 生成物落 artifacts 根 + sha256 入 contracts.json，SPEC-R1-M11 回卷）
├── shadow/<pkg 或裸模块>/   # 当前 base 的 shadow（advance_round.py 轮末替换）
├── base/                   # 当前 base 的 model.onnx + profile/ + bottleneck_report.json（B1 统一参照系）
├── baseline/               # baseline_proxy_acc.json（promote 锚）+ baseline_ref.json（final 锚，可后补）+ baseline_full_acc.json（自动满训基线，仅缺省 ref 时产生）（不动历史锚）
├── contracts.json / run_*.sh
├── variants/<vid>/         # vid = r{round}-{seq}（轮号唯一权威 = rounds/ 最大编号，vid 前缀一致）
│   ├── shadow/ declaration.json / DONE
│   ├── onnx/model.onnx + export.log / profile/ / diff_report.json
│   └── proxy_train/ eval/proxy.json verdict.json
├── rounds/<NNN>/           # proposals.json（顶层 {round, exhausted, proposals[]}——R2-M1）
│                           # verdicts.jsonl（writer=po_verify）/ probe_results.jsonl（writer=po_probe）
├── final/                  # full_train 产物
├── history.jsonl / best.json
```

**写盘职责**（R2-M5 补 history append 分布）：
- po_implement：append 首版行（sig/predicted/implemented）+ `variant_broken` 类 **outcome** 行（终态字段全链唯一名 `outcome`——R3 统一）
- po_verify：append 时延字段行（structural_check / makespan / latency_gate / ratio / **outcome**：L0 通过写过程态 `latency_pass`，淘汰写终态 `structural_mismatch` / `unsupported_op` / `latency_fail`）
- po_probe：append 精度字段行（proxy_acc/promote/outcome）+ `probe_results.jsonl`
- po_probe 末尾调 **advance_round.py**（§7.3）
- po_gate：纯读零写

### 5.2 跨 run 语义

BASELINE.lock 匹配 → 续跑（history/best/rounds 保留、round 编号续接、propose 依 history 去重）；不匹配 → fail loud（`fresh_start=true` 清单：`variants/ rounds/ history.jsonl best.json base/ shadow/ baseline/ contracts.json`）。

### 5.3 history.jsonl（append-only 多版本行；读法 = 每 vid 取最后版本，**去重判定除外**）

```json
{"vid":"r1-03","round":1,"seq":3,"parent_vid":null,
 "change_sig":"activation:gelu->relu2:blocks.0.mlp.act,blocks.1.mlp.act,...",
 "probe_epochs":1,"probe_max_steps":500,"probe_data_value":2000,
 "target_modules":[...],"predicted_delta_cycles":-3792,"implemented":true,
 "structural_check":"pass","makespan_cycles":11401,"latency_gate":"pass","pred_actual_ratio":0.91,
 "proxy_acc":0.83,"promote_gate":"pass","outcome":"promoted",
 "base_at_proposal":{"vid":null,"makespan_cycles":15288},"ts":"..."}
```

- `change_sig` canonical = `lever:params:modules_canonical`；**params 由 predict_delta.py 同源脚本归一化生成**（禁 LLM 手写——R2-M5b）；modules_canonical = 排序后逗号连接。
- **去重规则（脚本机械判定，R3 钉死）**：终态字段全链唯一名 `outcome`。**永久去重集** = `promoted`（真验证有效）∪ `unsupported_op`（结构性不可行，不烧 stall 配额）——v3.5 删 `zeroshot_catastrophic`（L1 退役）；按任意版本行判定；**`latency_pass` 为过程态、不参与去重**（L0 通过但 probe 未完成的中间行，probe 完成后由结果行覆盖为 promoted / probe_insufficient，同 vid 上 latency_pass 与 probe_insufficient 互斥——保住「proxy 配置变化可重试」通道）；`structural_mismatch` / `variant_broken` → 同 sig **两类合计重试 ≤1 次**（联合预算，防无限重试）；`probe_insufficient` → proxy 配置（epochs/步数/数据子集值）变化时可重试（行内字段与当前 contracts.proxy_budget 值机械比对——v3.5 首要旋钮数据子集值入指纹）。
- `base_at_proposal`：提案产生时 base 指针。

### 5.4 幂等 / resume（全节点）

- flatten/contract/baseline：reuse-check + contracts.json 存在 + baseline 链逐步产物存在性（§3.3）→ 跳过。
- propose：`rounds/<NNN>/proposals.json` 存在且可解析 → **复用本轮编号不跳轮**（防跳轮把 max_rounds 提前触发——R2-M6）。
- implement：`variants/<vid>/DONE` + declaration 校验和一致 → 跳过该提案。
- verify/probe：verdict.json / probe.json 已存在 → 跳过该变体。
- gate：读盘现算，无自身状态。
- **full_train：显式 psu 常驻模式**——detach 后重入先查训练进程存活（状态文件 + pid），存活则续轮询，**禁二次 detach**（防双训练进程互写 ckpt，ns3 铁律 6）；中断的 probe 变体重跑（预算小可接受）。

---

## 6. 训练与评估连接（零训练代码改动）

### 6.1 执行形态

run 模板渲染（vid/epochs/预算旋钮/out-dir 占位符）后执行；全含 §2.3 注入头 + 运行时断言：
- proxy（L2）：**从头训练**（固定 seed 随机初始化，不加载任何权重——D15）；预算 = contracts.json 的 proxy 预算（epochs + 数据子集旋钮值 + 步数截断，**与基线 proxy 逐字段同值**——D11 公平不变量）；out = `variants/<vid>/proxy_train/`；完成后 eval（ckpt = 训练输出，路径按 train 契约规则解析）→ `eval/proxy.json`。
- full（L3）：epochs = `min(full_train_epoch_cap 或 ∞, train_epochs_full)`、数据 = 完整预算（无子集旋钮值）、out = `final/`；同样从头训练。
- 长任务协议：detach + 有界轮询（§3.3/D14 继承清单）。

### 6.2 （已退役）权重继承机制

v3.5 起从头训练范式下无权重继承：`make_variant_ckpt.py` / weight_delta（none/dropped/folded/folded_vector）/ γβ 折叠 / 容器同构校验**整体退役**（OCP 留档——将来若做「继承加速模式」（激活替换类改动微调评估省 10 倍）再捞回，接口位置：proposal 增可选 weight_delta 字段 + implement 期重建 init ckpt）。变体训练起点 = 固定 seed 随机初始化，别无其他。

---

## 7. 分层评估与 gate

### 7.1 L0：时延 gate（po_verify，纯脚本）

对本轮每个 DONE 变体依序：
1. **两层声明校验**（v3.5：权重层随继承退役；参照系恒为当前 **base**（base/model.onnx + 全局 shadow）；不一致 → `structural_mismatch` 淘汰 + 差异清单）：
   - 文件层：变体 shadow 相对 base shadow diff 文件集（排除 `__pycache__`/`*.pyc`）== declaration.edited_files；
   - 图层：base onnx vs 变体 onnx 的 op_type 计数差 multiset == op_delta（opset 17 真实 op 名，如 `{Erf:-4,Tanh:-4,Mul:-8,Add:-4,Relu:+4}`）。
2. **重 profile**（unsupported → `unsupported_op` 淘汰）。
3. **时延 gate**：`base.makespan − variant.makespan ≥ max(min_improvement_cycles, min_improvement_pct% × base.makespan)` 且 `实测改善/预测改善 ≥ min_pred_actual_ratio`（placeholder 下近乎恒真，D10）。

### 7.2 L2：proxy 从头训练 + promote（po_probe 内）+ 轮末推进

（v3.5：L1 零样本已删——从头范式下无意义，`catastrophic_floor` input 与 `zeroshot_catastrophic` outcome 一并退役；历史去重集相应只剩 {promoted, unsupported_op}）

每个 L0 幸存者：proxy 从头训练（§6.1，与基线同预算同 seed 策略）→ eval → promote 判定（脚本，锚 = baseline_proxy_acc，D11）：

```
promoted ⇔ variant_proxy_acc ≥ baseline_proxy_acc − promote_relax × accuracy_budget
```

未 promote → `probe_insufficient`。

**轮末推进 = `advance_round.py`（确定性幂等脚本，po_probe 末尾调用）**——从 history 确定性重算，固定顺序原子步进（R2-M2）：
1. 本轮 promoted 集非空 → 更新 best.json（makespan 最小，并列取 probe_acc 高者）；
2. best 更新 → 复制 winner 变体 `onnx/`+`profile/` → `base/`；替换全局 `shadow/` = winner 变体 shadow；
3. 写 `.round_advanced` marker（记录轮号 + winner vid）；**幂等键 = marker.round == rounds/ 最大编号**（相等 → 已推进直接返回；不等 → 重放推进——防 existence-only 实现在 round 2 起 no-op；SPEC-R1-M6 回卷）。
`base/bottleneck_report.json` 不随复制——由下一轮 po_propose 开头 analyze.py 重跑刷新（输入 = 新 base profile）。best.json 的 profile_dir 字段降级为诊断信息，**运行时参照恒 base/**。

### 7.3 L3：完整训练（po_full_train，仅 winner，psu 常驻模式 §5.4）

winner 从头完整训练 + final eval → `final_acc ≥ baseline_full_acc − accuracy_budget` → 达标；否则 `out_of_budget` 如实报告（不自动重训——Q2）。

**baseline_full_acc 的来源（依序）**：① `baseline_ref_acc` input（用户已知收敛精度）→ ⑤ 步落盘 `baseline/baseline_ref.json`；② 缺省 → po_full_train 期先以完整预算从头训**基线**一次（同一 run/工作区缓存 `baseline/baseline_full_acc.json`，幂等判据 = 文件存在性——只花一次训练预算，跨轮跨 run 复用）。报告同时引用 pre-trained 参考值（若用户提供）作对照，不进 gate。

### 7.4 gate 汇总与循环退出（po_gate，纯读现算零写盘）

读 history（每 vid 末版）+ best.json + `rounds/<NNN>/proposals.json` 的 `exhausted`（**读盘不读 po_propose.output**——回边下跨节点 output 引用语义未证，P0 专测，R2-M1）→ decision：

| decision | 条件（依序判定） | 去向 |
|---|---|---|
| `full-train` | best 存在且 promoted 且 best.makespan ≤ target_makespan | po_full_train |
| `loop` | 未达标 且 轮数 < max_rounds 且 !exhausted 且 stall < stall_rounds | 回边 po_propose |
| `full-train-best-effort` | 轮数/stall/枯竭耗尽 但存在 promoted best | po_full_train（如实标注） |
| `finish-failed` | 无任何 promoted | po_report（failed 归因） |

- 轮数 = rounds/ 最大编号；**stall**：初值 0，本轮无 promoted 则 +1，有 promoted 清零（R2-m8）；脚本内硬帽：轮数 ≥ max_rounds 时 decision 永不取 loop（D4 双保险）。
- best 无时（首轮前）`best` 字段 nullable（R2-m15）。
- 默认值：max_rounds=5 / stall_rounds=2（「不达目标持续循环」由 stall/枯竭/max_rounds 有界退出——纯无界 = 训练预算失控，张力显式声明，可调大）。

---

## 8. 循环载体

### 8.1 回边（主方案）+ P0 前置验证清单

routes：po_gate 按 decision 分流，`loop` → `to: po_propose`（回边；route 顺序钉死 + catch-all 兜底，first-match-wins 语义）。**P0 清单**（开工第一步，纯 agent 双节点最小 cyclic workflow + in-session E2E）：
1. 回边重入（同节点二次执行的 tape/reducer 呈现 + `orca next` 续跑）；
2. **跨节点引用回边节点 output 的语义证伪**（`po_x.output.field` 在目标节点二次执行后取哪版——本设计规避之（gate 读盘），但需实证记录以免后续误用）；
3. 节点内长任务（detach + 有界轮询）在 in-session 的行为；
4. chart daemon 对回边 run 的推送；
5. advance_round.py / po_gate 硬帽在构造数据下的判定单测。
任一不符 → 降级方案（§8.2）。

不设 `iterations` input（in-session 不消费，声明误导；防死循环 = po_gate 硬帽）。

### 8.2 降级方案（回边不可用）

循环折叠进 `po_cycle` 单执行节点（psu 常驻模式）：内部轮次循环 {派子代理 propose → implement → verify 脚本 → probe 驱动 → gate 脚本判 decision}。DAG 退化为 6 节点。**节点职责/契约/盘面布局全部不变**（状态在盘不在节点）。

---

## 9. 提案生成

### 9.1 playbook（静态资产，产品说明书式）

| 杠杆 | v1 条目（v3.5：weight_delta 维度整体删除——从头训练不关心参数形状变化） |
|---|---|
| 激活替换 | GELU→ReLU / ReLU² / hard-swish / sigmoid |
| 归一化结构 | L2-norm 链→去 norm / RMSNorm / 融合 LayerNormalization 直接删除（v3.5 起无需 γβ 折叠——权重反正重训，删了就删了） |
| 深宽重构 | **机制解锁**（v3.5：从头训练无参数继承约束，参数全变也可行）——条目录用 OCP 渐进加 |
| 计算搬移 | softmax→relu 类 attention 改型等；low-rank/共享投影（同样解锁，OCP 渐进加） |

**条目强制含「融合单算子导出形态」触发条款（v3.4，E2E-R1 P1b）**：现代导出器（opset 17）常把分解链融合为单算子（LayerNormalization/Softmax 即是）——每个条目除分解链形态外必须给出对应融合形态的触发判定与改法模板（如融合 LN 的去 norm = 删 N 个 LayerNormalization 节点 + γ/β 向量折入相邻线性层），预测一律 **count from the actual graph**（按实际导出图的算子计，不按理论分解链计）。

### 9.2 proposal schema

```json
{"vid":"r1-03","lever":"activation","change_sig":"<脚本归一化生成>",
 "target_modules":[...],"target_pattern_id":"P2","pattern_evidence":"...",
 "change_spec":"nn.GELU → square-relu，blocks 全部 4 处",
 "op_delta":{"Erf":-4,"Tanh":-4,"Mul":-8,"Add":-4,"Relu":+4},
 "predicted_delta_cycles":-3792,"prediction_basis":"cost_table 引用行",
 "accuracy_risk":"low","sota_ref":"...","edited_files":["pkg/model.py"]}
```

**准入**（D9）：`predicted_delta_cycles < 0`（严格负）；`edited_files ⊆ shadow 闭包`；op_delta ⊕ change_spec 一致性自检。`predicted_delta_cycles` 与 `change_sig.params` 均由 `predict_delta.py` 同源生成。proposals.json 顶层 `{"round": N, "exhausted": bool, "proposals": [...]}`。

### 9.3 po_propose 工作流

analyze.py（当前 base，产物落 base/bottleneck_report.json）→ playbook + history 去重（§5.3 规则）→ ≤ max_proposals_per_round 条（预测收益 × 低风险优先）→ 自检 → 落盘（幂等：§5.4）。

---

## 10. inputs 清单

| input | 档 | default | 说明 |
|---|---|---|---|
| project_root / model_path | [ask] | — | 同 ns3 语义 |
| target_makespan | [ask] | — | cycles；po_baseline 运行期校验 |
| accuracy_budget | [ask] | — | 相对基线精度的最大可接受损失 |
| write_back | [ask] | true | 终态成功后写回（D1） |
| pretrained_ckpt | [advanced] | "" | **v3.5 起可选、仅参考**：不进任何 gate；报告引用作对照 |
| baseline_ref_acc | [advanced] | "" | 用户已知的基线满训练收敛精度（final gate 锚）；缺省 → workflow 满训基线一次并缓存（§7.3） |
| profile_script_path / reference_onnx / freq_ghz | [advanced] | ""/""/1.0 | §4/D3/D8 |
| max_rounds / stall_rounds / max_proposals_per_round | [advanced] | 5 / 2 / 4 | §7.4 |
| probe_epochs / probe_max_steps | [advanced] | **""（effective 由 contract 期计算入 contracts.json）** / 500 | proxy 预算的 epochs/步数分量（v3.5：另有数据子集旋钮由 contract 发现，值入 contracts.json） |
| promote_relax / min_improvement_cycles / min_pred_actual_ratio | [advanced] | 1.0 / 100 / 0.5 | §7（catastrophic_floor 已随 L1 退役——v3.5） |
| min_improvement_pct | [advanced] | 1 | 时延 gate 百分比项（v3.4：gate = max(min_improvement_cycles, min_improvement_pct% × base)——placeholder 下 makespan=总 cycles 且 MatMul 常占绝对大头，结构性小赢被 1% 硬门槛全灭，开放为可调；真 profiler 下维持默认） |
| full_train_epoch_cap | [advanced] | "" | 空 = 不截断；非空 = min(cap, train_epochs_full)——完整微调预算阀（通用 input 非项目特判；SPEC-R1-U2a 回卷） |
| fresh_start | [advanced] | false | §5.2 |
| seed | [default] | 0 | 复现性 |

---

## 11. 节点 output_schema 与路由（含失败路径）

- **po_flatten**：`flatten_passed, shadow_root, shadow_pkgs[], model_module, manifest_path, baseline_lock_path, error, generated_artifacts`
- **po_contract**：`viable, reason, contracts_path, run_probe_script, run_full_script, run_eval_script, export_script, metric_direction, train_epochs_full, proxy_budget{epochs,dataset_knob,data_value,max_steps,seed}, probe_cap_mechanism, exemptions[], error, generated_artifacts`（v3.5：`baseline_acc_raw` 删——契约期不再预评精度；proxy_budget 为基线与变体共用单一来源）
- **po_baseline**：`status(executed|failed), base_onnx, makespan_cycles, baseline_proxy_acc, baseline_ref_acc, profile_dir, bottleneck_report, error, generated_artifacts`
- **po_propose**：`proposals_count, exhausted, proposals_path, error, generated_artifacts`（**gate 消费盘面 proposals.json，不消费本 output**——R2-M1）
- **po_implement**：`implemented[], skipped[{vid,reason,outcome?}], variants_root, error, generated_artifacts`（全 skipped 不算失败）
- **po_verify**：`status(executed|failed), verdicts_count, latency_pass_count, verdicts_path, summary, error`
- **po_probe**：`status(executed|failed), survivors_probed, promoted[], best_updated, base_advanced, artifacts, assessment, max_retries_hit, healed_files`
- **po_gate**：`decision(loop|full-train|full-train-best-effort|finish-failed), round, stall, best{vid,makespan_cycles,proxy_acc}|null, reason, error`
- **po_full_train**：`status(executed|failed), final_acc, baseline_full_acc, baseline_full_acc_source(ref-input|auto-trained), within_budget, final_ckpt, final_onnx, assessment, max_retries_hit, healed_files`
- **po_report**：`status(success|failed), stage, reason, winner{vid,change_sig,lineage}|null, baseline{makespan,proxy_acc,ref_acc}, final{makespan,acc,gap,within_budget}, pretrained_ref_acc, rounds_completed, proposals_total, history_path, write_back{done,files[],conflicts[]}, charts_summary, artifacts, error`
  - `stage` enum：`[flatten, contract, baseline, propose, implement, verify, probe, gate, full-train, report]`（R2-m6）

**路由**（每节点 catch-all → po_report；业务失败路由 vs 引擎崩溃两分开；route 顺序钉死 first-match-wins）：
- flatten_passed!=false → contract；viable!=false → baseline；baseline.status==executed → propose；catch-all → report
- propose.error 为空 → implement；implement → verify；verify.status==executed → probe；probe.status==executed → gate；catch-all → report
- gate.decision：full-train* → full_train；loop → propose（回边）；finish-failed → report
- full_train 任意终态 → report
- 进程崩溃/schema 20 次失败 = 引擎级 workflow_failed（reporter 兜底读盘归因）；workflow outputs 全读 `po_report.output`

chart：v1 = po_report 聚合 history 出静态图（每轮 makespan 趋势 + verdict 分布），live 推送 best-effort，生产者 = po_report。

---

## 12. 风险与已拍板决策点

| # | 议题 | 拍板 |
|---|---|---|
| Q1 | 无任何 promoted 终态 | finish-failed（如实失败） |
| Q2 | full_train 后 out_of_budget | 如实报告不自动重训 |
| Q3 | 深宽重构 | 机制解锁（v3.5 从头训练无参数继承约束），条目录用 OCP 渐进加 |
| Q4 | 注入边界 | 不往用户项目写注入物；注入物全在 artifacts（U4） |
| Q5 | probe lr | 沿用用户原值（已知限制，D11 锚校准吸收部分风险） |
| Q6 | 写回 | 终局 shadow diff 全集 + 新文件名 + 冲突不覆盖 + 写回前复验 lock + 启用语义声明（D1） |
| Q7 | script 节点去向 | (a) 全 agent + 脚本资产（D14） |
| Q8 | 入口适配 | D13 适配三档：A 直接用 / B porter 生成适配入口（artifacts 内，用户文件零改动）/ C fail loud（2026-08-20 用户拍板：少量适配可接受，参考 ns3 porter，改动量极小） |

---

## 13. E2E 计划

1. **P0**：§8.1 五项（回边/跨节点 output 证伪/长任务/chart/advance+硬帽单测）。
2. **B3 PoC 已完成**（§2.3 证据链）；P0 探针即真实 in-session 形态（v3.3：demo 项目取消，两 E2E 真项目覆盖）。
3. ~~playground 样例~~（v3.3 取消——demo 项目不再单独建）。
4. **placeholder profiler 单测**：GELU→ReLU 变体 delta 方向断言（不断言 ratio，D10）。
5. **全链 E2E**（in-session）断言：≥1 提案 latency gate 过；promote 判定执行；回边 ≥2 轮或 stall 退出；失败路径（contract viable=false / proxy 训练中断重入）；**非退化**：winner.makespan < baseline.makespan；**公平不变量断言**（v3.5）：基线与全部变体的 proxy 训练渲染命令中预算参数（数据子集/epochs/步数/seed）逐字段一致（读 contracts.json 与各 .rendered.sh 机械比对）；**变体真实训练断言**：变体 proxy 训练输出 ckpt 的 eval 与基线 eval 来自不同模型实例（shadow 断言链保证 + ckpt 哈希互异）；**eval 双 ckpt 联动断言**（R2-B2 的 E2E 级探测）；**写回断言**（R3 补）：success 路径 write_back.files 落盘存在且内容与终局 shadow diff 一致 + 构造一个同名冲突验证不覆盖；history/verdict 盘面齐全；逐 agent 产出质量检查。
6. **真 profiler 替换演练**。

---

## 附 A. 审查闭环记录

**R1**（spec-reviewer，2026-08-20）：verdict fail → 33 项真问题（4B/12M/13m）+ 4 权衡修订，v2 全闭环：
- N1（in-session 仅 agent，`step.py:876-888`）→ D14；B3（PYTHONPATH 注入失效）→ 复核时发现审查推荐的 insert(0) 修法同样失效，改 meta path finder（§2.3 实测证据链）；B1（op_delta 参照系）→ base/ 统一；B2（零写入）→ D1 收窄 + D13。
- M1-M14/m1-m13/N2/N6/N7/N8：落 D5-D13、§2-§13。
- R1 中未在 v2 正文括注的 8 项（N3/N4/N5/M12/M13/m1/m2/m9）实际去向（R2-m16 补账）：N4→D11（promote 锚）、N5→D13④、N3→§2.3 断言形态强化、M12→§3.2 干跑归因区分、M13→§7.1 符号修正、m1→§1 节点计数重列、m2→§5.1 base/ 目录（v3 进一步由 advance_round.py 承载）、m9→§13 失败路径/非退化/变体加载断言。
- U1=Q7(a)、U2=D11(a)、U3=D13④、U4=Q4。

**R2**（spec-reviewer，2026-08-20）：verdict **fail（窄域收敛型）**——R1 四 blocker 核销属实（B2 train 半边 partial）、骨架（DAG 回边 + 全 agent + 分层 gate + shadow/meta-finder）两轮攻击后成立；26 项新问题全部为落点钉死/措辞改写，**无架构返工**。v3 闭环：
- R2-B1（写回丢上游文件）→ D1 终局 shadow diff 写回源；R2-B2（eval ckpt 参数化缺失）→ D13⑤ 双 ckpt 实测。
- R2-M1（exhausted 落盘）→ proposals.json 顶层 schema + gate 读盘 + P0②；R2-M2（轮末推进原子性/参照系分裂/analyze 落点）→ advance_round.py 幂等脚本 + base/ 统一 + best.profile_dir 降级；R2-M3（baseline 长任务）→ detach+轮询子模式 + 逐步幂等 + baseline_status.md；R2-M4（容器同构）→ §6.2；R2-M5（outcome 值域/去重）→ §5.3 任意版本行 + 脚本归一化 params + verdict 去重语义表；R2-M6（propose/full_train resume）→ §5.4；R2-M7（共享脚本落位）→ D14 scripts/；R2-M8（裸模块闭包不可达）→ §2.2 全顶层名入列 + 断言遍历 + D9 约束。
- R2-m1~m16：stdlib guard（§2.3）/ probe_epochs effective（§3.2/§10）/ baseline_acc 复用与幂等判据（§3.3⑤⑥）/ D2 措辞（§2.1）/ lock 语义（D7）/ 三 enum（§11）/ 启用语义+写回复验（D1）/ stall 钉死（§7.5）/ reference_onnx 落点（§3.3①）/ 提案准入除零（D9/§9.2）/ os.pathsep+sitecustomize 披露（§2.3）/ 注入统一（§2.3）/ exemptions 快照 diff（§2.1）/ profiler 路径校验（§3.1）/ best nullable（§7.5/§11）/ m16 补账（本附 A）。

**R3**（targeted re-check，2026-08-20，general-purpose 定向复核五处）：verdict **conditional-pass** —— R2-B1（写回）/ R2-B2（eval ckpt）/ R2-M2（轮末推进）/ R2-M8（裸模块闭包）四处 **closed**（D1 五要素齐备、双 ckpt 三处一致、advance_round 原子幂等无双真相源残留、四点齐备）；R2-M5 **partial**（三处字段级残留）+ 回归扫描一处缺口。**全部闭环进 v3.1**：
- 残留①（verdict/outcome 字段名分裂）→ 终态字段全链唯一名 `outcome`（§5.1/§5.3/§11 统一）；
- 残留②（latency_pass 写入时机与 probe_insufficient 重试通道矛盾）→ latency_pass 定为过程态不参与去重，永久去重集 = promoted ∪ {unsupported_op, zeroshot_catastrophic}；
- 残留③（重试预算联合 vs 每类）→ structural_mismatch/variant_broken 同 sig 两类合计 ≤1 次（联合，保守）；
- 回归缺口（E2E 无写回断言）→ §13.5 补写回断言（落盘一致 + 冲突不覆盖）；
- 建议级四项全采纳：第二 ckpt 来源（确定性扰动副本）/ probe_epochs 用户覆盖公式 / shadow 删除文件不写回声明 / flatten 期 stdlib 撞名预检。
**终态：SPEC-ready**（R2 验收线「修订 + targeted 复核」达成；实现期发现新问题走 SPEC 变更流程，不回卷草稿）。

**v3.2 用户拍板修订**（2026-08-20，会后）：D13「入口硬前置 fail loud」改为**适配三档**（A 直接用 / B ns3 porter 模式生成适配入口——User-Paradigm Authority Iron Rule 逐字移植，artifacts 内，用户文件零改动 / C fail loud）+ contracts 记入口 sha256 防漂移。改动范围：D13 / §3.2 生成物 / §12 Q8。Q1（全军覆没→如实失败）与 P0（回边等引擎机制先实证再铺开）当场确认无异议。

**v3.5 训练范式切换**（2026-08-21，用户拍板：**彻底从头训练**——「基线也是训练少量数据加 epochs，优化后的模型也是，这样才有对比效果」；pre-trained 仅参考；走 SDD 全流程重做实现与 E2E）：
- **D11 重写**：promote 锚 = baseline_proxy_acc（基线从头 @proxy 预算）；公平不变量 = 同 run 基线与变体 proxy 参数逐字段一致（contracts.json 单一来源）；final 锚 = baseline_ref_acc input，缺省自动满训基线一次并缓存。
- **D15 新增**：一律从头训练；make_variant_ckpt/weight_delta/γβ 折叠/容器同构/**L1 零样本（§7.2 删，catastrophic_floor 与 zeroshot_catastrophic 退役，永久去重集 = {promoted, unsupported_op}）**整体退役 OCP；结构校验收敛两层（文件/图）；proxy 旋钮发现（数据子集优先、epochs 兜底，D13⑥）。
- **D13 更新**：参数需求删② init-ckpt、增⑥ 数据子集旋钮；Tier B porter 产物更名 train_proxy_entry.py；contracts.json 增 proxy_budget 块。
- **inputs**：pretrained_ckpt [ask]→[advanced] 仅参考；+baseline_ref_acc；−catastrophic_floor。
- **playbook（§9.1）**：weight_delta 维度删除；融合 LN 条目简化（无需折叠）；深宽/低秩**机制解锁**（条目 OCP 渐进加，Q3 更新）。
- **§7 重排**：L0→L2（7.2）→L3（7.3）→gate（7.4）；§6.1 训练形态改从头；§11 schemas 同步（po_baseline/po_contract/po_gate/po_full_train/po_report 字段）；§13 E2E 断言换公平不变量 + 变体真实训练。
- **范围外波及**：E2E-R2 已证 L0/循环/报告链机械正确（target 修复后 round1 已出提案、LN 折叠变体零样本 0.9779 非灾难——该证据随 L1 退役仅作历史参考）；本轮改动集中在 po_contract/po_baseline/po_probe/po_full_train 四节点 + playbook + retired 脚本清理。
- **实现轮补全**（coder 回卷）：§5.3 去重指纹扩为三字段（epochs/步数/**数据子集值**——v3.5 首要旋钮必须进指纹）；§3.2/§5.1 样例行与 latency_fail 残留清扫；基线链②新增 `baseline/original_shadow/` 原始快照（轮次推进后自动满训基线的 round-0 结构来源——全局 shadow 届时已是 winner）；eval 双 ckpt 来源 = 两个不同 seed 随机初始化（文档化唯一来源，perturb_ckpt 降级为无消费方通用工具留档）；`probe_epochs_effective` 并入 `proxy_budget.epochs`（contracts.json 单一来源）。

**v3.4 E2E-R1 回卷**（2026-08-21，两真项目首轮 E2E 触发；machinery 全绿、内容线半 DAG 未行使，问题 P1-P5）：① §6.2 folded 增**向量 affine** 折叠（γ/β 折入相邻线性 W/b——融合 LN 去 norm 的非灾难化前提，P1b）；② §10/§7.1 增 `min_improvement_pct`（P1：placeholder 下 makespan=总 cycles、MatMul 绝对大头，1% 硬门槛全灭结构性小赢；真 profiler 维持默认 1%）；③ §9.1 playbook 条目强制含**融合单算子导出形态**触发条款（P1b：opset17 实际导出 LayerNormalization/Softmax 融合单算子，分解链假设失配）；④ verifier（memory/paradigm）**报告落盘化**：报告必须写工作区文件（首行哨兵），入节点 generated_artifacts——「复核真的发生过」成为盘面可查事实（P4）；⑤ 用户项目根**唯一来源 = readiness.json**，全链禁读 `ORCA_PROJECT_ROOT` env（引擎已占用该名=Orca 仓库根，同名冲突曾随机炸基线链，P2）；⑥ charts 空盘面行为钉死（确定性规则，P5）。predict_delta 改按 **shape-class 行计价**（cost_table 既有 per-shape-class 数据用满，P1a）。MatMul 杠杆（宽度/低秩）维持 v2 范围（P1c，OCP 不变）。

**v3.3 SPEC-R1 回卷**（2026-08-20，实现 SPEC 对抗审查触发，1 blocker + 13 major + 8 minor 全闭环；SPEC 侧修订见 prof-opt-spec.md）：① §7.3 advance_round 幂等键 existence-only → 轮号比对（M6，草稿措辞缺陷——existence-only 实现可过全验收线但 round 2 起静默 no-op）；② §5.1 export_onnx.py 移出静态共享清单 → contract 期生成物 + sha 防漂移（M11，归属矛盾裁决）；③ §10 增 `full_train_epoch_cap`（U2a，通用预算阀）；④ §13.3 playground demo 项目取消——P0 探针 + 两 E2E 真项目覆盖其职责，Windows 原生 os.pathsep 场景随 WSL-only 决策放弃覆盖。决策采纳：U1a（P0 in-session 回边探针 = 开工第一步）/ U2a / U3a（E2E 后端口径 = claude，CLAUDE.md 已同步——用户 /goal 拍板，prof-opt E2E 用 claude + tars skill，其余 workflow E2E 约定不变）。
