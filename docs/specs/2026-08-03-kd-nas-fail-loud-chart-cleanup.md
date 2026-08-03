# SPEC：kd-nas fail-loud 加固 + CONTRACTS 对齐 + 终态图补全

**日期**：2026-08-03
**状态**：v2（spec-review v1 FAIL 后修订；待复审）
**背景**：2026-08-03 完成 kd-nas 四项改动（单位 us / KD 强制 / teacher 评估 / 全模型总表）后的 broad review 发现 3 类遗留。本 SPEC 收尾，目标：**fail-loud 铁律无漏洞 + 活跃文档与代码一致 + 终态图覆盖帕累托/FAIL 分布**。

**v2 修订摘要**（应 spec-review v1）：
- §1.2(2) grep 改 **AST 判定**（v1 的 `^def` 永远匹配不到缩进的 class method，会让 ofd 永远被剥离——与目的相悖）。
- §2 **收窄**为「CONTRACTS 重写 + 4 脚本加 DEPRECATED 头」。**完整删除脚本 + 80+ 测试外科迁/删** defer 到独立 followup SPEC（理由见 §2.0）。
- §3 字段名 `latency_us`（双 fallback）、`chart_type=pareto` + 方向参数 + unknown-kind WARN-skip（D1b：把 viz_kd 的可复用不变量迁进 §3 测试）。
- §3.3 文本报 SUCCESS 计数，不算非支配集（D3b）。
- §4 bool 渲染统一。
- §6.1 列出 5 个预存失败；§7 补 F2 风险。

**不在范围**：(a) 活跃串行 DAG 不改；(b) 单位不改（已 us）；(c) 死**脚本**+其测试的删除（→ followup SPEC）；(d) `setup_helpers.py` / `teacher_model.py` / `kd-gate|kd-train|kd-select` folder-agents 清理（D2：out of scope，后续 SPEC）。

---

## §1 P0 — 特征蒸馏项 fail-loud（OFD / FitNets / RKD）

### 1.1 问题
`kd/compose.py` `_compute_term` 对 ofd/fitnets/rkd 在 `s_feats`/`t_feats` 为空时 `return None`，主调 `__call__` 静默 `continue`。`distill/agent.md:175-182` 默认 `["mse","ofd"]` 且写"无 hook → ofd 静默退化为零"。student 缺 `feature_hook_names()` 时配置声称 ofd 在跑、实际只跑 mse——违反 Rule 12。

### 1.2 契约
1. **`kd/compose.py` `KDComposite.__call__`**（在 term 循环前加守卫）：
   ```python
   _FEATURE_TERMS = ("ofd", "fitnets", "rkd")
   s_feats_list = list(s_feats) if s_feats else []
   t_feats_list = list(t_feats) if t_feats else []
   feature_requested = [n for n in self.kd_losses if n in _FEATURE_TERMS]
   if feature_requested and (not s_feats_list or not t_feats_list):
       raise ValueError(
           f"kd_losses 含特征项 {feature_requested} 但无 feature feats"
           f"（s_feats={len(s_feats_list)}, t_feats={len(t_feats_list)}）。"
           f"特征蒸馏需模型暴露 feature_hook_names() 且 forward 经过该层。"
           f"给 student 加 feature_hook_names()，或从 kd_losses 去掉特征项（只留 mse）。"
       )
   ```
   - 仅当**含特征项**且 feats 空才抛；`mse`/`ema`-only 不受影响（mse 不依赖 feats）。
   - 首次 forward 抛 → distill rc≠0 → `FAIL_train`。
   - 作用域天然限 distill（teacher/eval 不构造 KDComposite）。

2. **`distill/agent.md`**：默认 KD_CONFIG 按 student 是否有 hook 条件化（**用 AST 判定，不用 `^def` grep**）：
   ```bash
   HAS_HOOK=$(python3 -c '
import ast,sys
t=ast.parse(open(sys.argv[1]).read())
print(any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=="feature_hook_names" for n in ast.walk(t)))
' "$STUDENT")
   if [ "$HAS_HOOK" = "True" ]; then
     KD_CONFIG='{"kd_losses":["mse","ofd"],"weights":{"mse":1.0,"ofd":0.3},"ema":true}'
   else
     KD_CONFIG='{"kd_losses":["mse"],"weights":{"mse":1.0},"ema":true}'
   fi
   ```
   - 删除 `distill/agent.md:175-176` 的"静默退化为零"那句。
   - 注释：ofd/fitnets/rkd 仅在 student 暴露 feature_hook_names 时启用；否则 §1.2(1) 守卫会 fail-loud。
   - **同步修 `gen-student/agent.md:241`** 的同款 `grep '^def feature_hook_names'`（dormant bug，同样匹配不到 class method）→ 改 AST 判定，与 distill 一致。

3. **`gen-student/agent.md` step 5**：把"silent 提醒，不阻断"措辞升级——distill 侧已条件化 + fail-loud；缺 hook 时特征项被自动剥离（不崩），有 hook 时必移植。

### 1.3 验收
- `KDComposite(lambda a,b: torch.tensor(0.0, requires_grad=True), {"kd_losses":["mse","ofd"],"weights":{"mse":1.0}})`，用 dummy `s_out/t_out/y`（`torch.randn(2,3)`）+ `s_feats=None/t_feats=None` 调 `__call__(...,epoch=0)` → raise `ValueError`（含"特征项"）。
- 同上但 `kd_losses=["mse"]` → 不 raise，返回 finite loss。
- `kd_losses=[]`+`ema=True` → 不 raise（无特征项）。
- `prepare(sample=[feat,...])` 后再 `__call__(...,s_feats=None,...)` → **仍 raise**（守卫看运行时 feats，不看 prepare 历史；锁 intent）。
- 新单测 `test_compose_feature_term_without_hooks_fails_loud` 覆盖以上 4 分支。

---

## §2 P1-收窄 — CONTRACTS 对齐 + 死脚本加 DEPRECATED 头

### 2.0 范围与 deferral 理由
spec-review v1 证实：`gate_all.py`/`train_pool.py`/`select_and_report.py`/`viz_kd.py` 在活跃 runtime 无调用，但**活跃测试覆盖它们的逻辑**——`test_kd_redesign.py` 85 测中 74 活跃（仅 11 skip），大量 `test_gate_all_*`/`test_train_pool_*`/`test_select_*`/`test_train_pool_r2_viz_kd_*` 直接 import 这些模块；`test_struct_kd_p7.py::TestVizKD*`（~20 测）覆盖 viz_kd 的 pareto 方向/unknown-kind/sentinel 等**可复用不变量**。完整删除 = 80+ 测试的删/迁外科，单 SPEC 失控。

**本 SPEC 只做**：(a) 重写 `CONTRACTS.md` 为串行版（消除"活跃流程是旧 gate/train/select"的误导——这是真实危害）；(b) 给 4 个死脚本加 DEPRECATED 模块头。
**Defer 到独立 followup SPEC**：脚本物理删除 + 80+ 测试删/迁（含把 `TestVizKD*` 可复用不变量迁到 viz_kd_stage 测试，再删 viz_kd.py）。本 SPEC §3 会先补上 viz_kd_stage 的 pareto/方向/unknown-kind 测试覆盖，为 followup 铺路。

### 2.1 契约
1. **`workflows/agents/_kd_scripts/CONTRACTS.md`** 重写：
   - §0 DAG 改活跃串行版，删 gate/train/select 描述。
   - §3 删 `gate_all`/`train_pool`/`select_and_report`/`viz_kd` 的 CLI/stdout 契约段；它们改在文件头部统一标注「DEPRECATED——旧并行 sweep 残留，活跃串行 workflow 不调用，保留供历史测试；计划后续 SPEC 删除」。
   - `measure_student.py` 保留段（标注"纯函数供测试/struct 复用，KD 精度路径已由 train_pipeline --mode eval 取代"）。
   - 新增 §3 条目：`viz_kd_stage --stage final` 的 `all_models_table`/`pareto_front`/`fail_status_bar`（§3 后）。
2. **4 脚本加 DEPRECATED 头**（`gate_all.py`/`train_pool.py`/`viz_kd.py`/`kd-select/scripts/select_and_report.py`）：模块 docstring 顶部加 `# ⚠️ DEPRECATED —— 旧并行 sweep 路径，活跃串行 kd-nas.yaml 不调用；保留供历史测试，删除见 followup SPEC。` —— 不动逻辑、不动测试。

### 2.2 验证
- `CONTRACTS.md` §0 DAG 与 `kd-nas.yaml` 节点一致；不再出现 gate/train/select 为活跃路径的描述。
- 4 脚本 docstring 含 DEPRECATED 标记。
- 全套 kd-nas 测试仍 green（脚本+测试未动，零行为变化）。

---

## §3 补图 — 终态帕累托前沿 + FAIL 分布（含可复用不变量迁移）

### 3.1 动机
活跃串行 workflow 终态只有 latency 维度图 + 总表，缺：帕累托前沿（latency×accuracy 挑 champion 核心）+ FAIL 分布（看搜索卡哪）。当前只在即将标 DEPRECATED 的 `viz_kd.py` 里。本节把 viz_kd 的 **pareto 语义 + 方向门 + sentinel 过滤** 重新实现在 `viz_kd_stage`（port 语义，非 import），并补测试（为 §2 followup 删 viz_kd 铺路）。

### 3.2 契约（`viz_kd_stage.py` 新增 2 pusher，接 `--stage final`）
复用 `kd_common.accuracy_direction`（DRY，单一真相源）。**字段读**：`lat = _to_float(e.get("latency_us_median"))`，None 则 `_to_float(e.get("latency_us"))`（与现有 `_push_all_models_table` 一致）。

1. **`_push_pareto_front(ledger, baseline_latency_us, baseline_accuracy, accuracy_baseline_kind)`**：
   - 有效点过滤：`is_measured_row`（status ∈ {SUCCESS, FAIL_accuracy}，即真测过的行）∧ `latency` 非None ∧ `accuracy` 非 None。**不按值 `!= -1` 过滤**（db-kind 下 -1.0 dB 是合法真测；sentinel 已由 `is_measured_row` 经 `accuracy_kind` 非空门覆盖——FAIL_latency/FAIL_train 的 accuracy_kind 为空 → 自动剔除）。FAIL_accuracy + accuracy_kind 非空 = 真测值，**计入**前沿（与 viz_kd `_push_pareto` 一致）。
   - **方向门**：`direction = accuracy_direction(accuracy_baseline_kind)`；若 `direction==""`（unknown kind）→ WARN-skip（**不 auto 猜方向**，防 -20dB/-22dB 反转）。
   - display 变换：`direction=="min"` → y 取负（误差型越低越好 → 轴上越大越好统一）。
   - `chart_type="pareto"`、`pareto_x_direction="min"`（latency）、`pareto_y_direction="max"`（display 后）、x=`latency_us`、y=`accuracy`、hue=`met_accuracy`。
   - hue 取值：student 行 `str(bool(e.get("met_accuracy")))`（"True"/"False"）；baseline 参考点行 `met_accuracy="ref"`（与 viz_kd `_push_accuracy_compare` 的 "ref" 约定一致，避免空串成第三类目）。
   - 有效点 <2 → 跳过 WARN（沿用 `_run` 容错）。
2. **`_push_fail_status_bar(ledger)`**：
   - 全量按 `status` 分组计数（SUCCESS/FAIL_latency/FAIL_train/FAIL_build/FAIL_accuracy/FAIL_export/其它）。
   - `chart_type="bar"`、title `"Distill Outcome (status counts)"`、x=`status`、y=`count`。空 ledger → 跳过 WARN。
3. **接入 `--stage final`**：在 `all_models_table` 之后 `_run("pareto_front", ...)` + `_run("fail_status_bar", ...)`。ledger 已在 final 分支读取，复用。

### 3.3 final_report.md 同步（D3b：不算非支配集）
在 "All Architectures" 段后追加（纯文本计数，图表是唯一真相源，不重复实现 pareto）：
```
## Search Outcome
- SUCCESS: N / FAIL_latency: N / FAIL_train: N / FAIL_build: N / FAIL_accuracy: N / FAIL_export: N / 其它: N
```
（未出现的 status 计 0 或省略；"其它" 兜底未知 status。）

### 3.4 验收
- 测试 fixture：ledger 至少 **2 行 SUCCESS**（含一 min-kind 方向、一 max-kind）+ 1 行 FAIL_accuracy（accuracy_kind 非空、真测值）+ FAIL_latency/FAIL_train 各 1 行（哨兵）。
- `viz_kd_stage --stage final` → `charts.pareto_front.pushed==true`、`charts.fail_status_bar.pushed==true`，且 **pareto 点数 == 有效测量行（SUCCESS + FAIL_accuracy 真测）+ baseline**。
- **可复用不变量迁移测试**（D1b，原 viz_kd TestVizKD* 守的）：① min-kind 时 y 取负（display）；② unknown kind → pareto WARN-skip（pushed==false）；③ FAIL_latency/FAIL_train 哨兵行（accuracy_kind 空）不计入；④ **FAIL_accuracy + accuracy_kind 非空 计入** pareto（与 SUCCESS 同列前沿）；⑤ db-kind 下 accuracy=-1.0 的真测点**不**被误剔（NEW-2 回归守护）。
- `final_report.md` 出现 `## Search Outcome`。

---

## §4 P4 — finalize report 小瑕疵（含一致性）

`finalize_kd._write_report`：
- baseline 行 `b_met` 由 `"true" if ... else ""` 改 `"true" if ... else "false"`。
- **student 行 bool 渲染统一**为 `str(bool(r.get('met_latency'))).lower()`（当前是 Python 原生 `True/False`，与 baseline 行 `true/false` 字面不一致）→ met_lat/met_acc 两列全部 `str(bool(...)).lower()`。
- 两段 latency 读取统一用 `r.get("latency_us_median", r.get("latency_us", ""))`（"各轮 student 汇总"段对齐"All Architectures"段）。

---

## §5 执行顺序
1. §1 P0（compose 守卫 + distill AST 条件化 + gen-student step5 + 同款 grep 修 + 测试）。
2. §3 补图（viz_kd_stage 2 pusher + final 接入 + 不变量测试）+ §4 finalize 一致性。
3. §2 CONTRACTS 重写 + 4 脚本 DEPRECATED 头。
每步后跑对应单测；全 green 后按 § 提交（1-3 commit）。

## §6 验证
1. **单测**：`pytest tests/workflows/test_{kd_train_script,viz_kd_stage_metrics_tail,finalize_kd,kd_reducer,struct_kd_p7,model_flatten,teacher_gen,kd_redesign}.py` 全 green。
   - **5 个预存失败**（与本次无关，HEAD 已存在，不计）：`test_finalize_kd.py::test_main_baseline_fallback_writes_report_no_eval`、`test_finalize_kd.py::test_main_real_champion_runs_eval_onnx_latency`、`test_struct_kd_p7.py::TestTeacherSetupLatencySource::test_latency_from_param_skips_provider`、`test_struct_kd_p7.py::TestTeacherSetupLatencySource::test_latency_fallback_to_provider_when_param_absent`、`test_struct_kd_p7.py::test_kd_setup_node_exposes_path_fields`。
2. **P0**：student 无 hook + KD_CONFIG 含 ofd → distill FAIL_train（stderr 含守卫消息）；有 hook → ofd 生效。
3. **补图**：demo ledger 跑 `viz_kd_stage --stage final` → all_models_table/pareto_front/fail_status_bar 全 pushed。
4. **AST grep**：构造一个含**缩进** `def feature_hook_names(self)` 的 student 文件 → AST 判定返回 True（旧 `^def` 会漏判）。

## §7 风险
- **P0 改变失败行为**：无 hook + 配 ofd 的 distill 以前静默退化纯 mse，现在 FAIL_train。distill agent 默认 KD_CONFIG 必须 AST 条件化（§1.2(2)），否则所有无 hook student 失败。
- **F2 风险（v1 已修）**：若 grep 仍用 `^def`，demo student（hook 是缩进 class method）会被漏判 → ofd 永远剥离 → 回归"静默降级"。v2 改 AST 判定消除该风险；§6.4 锁定。
- **demo E2E**：需确认 demo student 是否有 feature_hook_names（决定 demo 默认走 mse-only 还是 mse+ofd；两条路都应跑通）。

## 关键文件
- `workflows/agents/_kd_scripts/kd/compose.py`（§1 守卫）
- `workflows/agents/distill/agent.md`（§1 AST 条件化）
- `workflows/agents/gen-student/agent.md`（§1 step5 + grep 修）
- `workflows/agents/_kd_scripts/viz_kd_stage.py`（§3 2 pusher + final 接入）
- `workflows/agents/_kd_scripts/finalize_kd.py`（§3 outcome + §4 一致性）
- `workflows/agents/_kd_scripts/CONTRACTS.md`（§2 重写）
- `workflows/agents/_kd_scripts/{gate_all,train_pool,viz_kd}.py` + `kd-select/scripts/select_and_report.py`（§2 DEPRECATED 头）
- `tests/workflows/test_{kd_train_script,viz_kd_stage_metrics_tail,finalize_kd}.py`（§1/§3 测试）
