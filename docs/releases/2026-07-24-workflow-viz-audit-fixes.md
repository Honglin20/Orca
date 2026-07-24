# 2026-07-24 — workflow 可视化审计修复（P1×5 + P2×7）

## 背景

两轮审计对 workflow sidecar / viz 脚本列出 12 项问题（5 P1 必修 + 7 P2 清理）。
本提交全部闭环：实现 → 自检（43 新测）→ code-reviewer 两轮（0 🔴 / 2 🟡 / 5 🟢）→ 全修。

## 改动清单（file:line + 意图）

### P1（必修）

#### P1-1 超时候选污染图轴 —— `nas-agent/nas_agent/search/problem.py:21-49, 211-220`
infeasible（latency > latency_constraint）候选之前把**所有**目标写 WORST_FITNESS，
导致帕累托图 latency 轴被伪造。修法：death-penalty 只惩罚质量目标，latency-like 目标
保留实测值。抽 `_is_preserved_objective` + `_infeasible_result` 两个纯函数（含
`_PRESERVED_OBJECTIVE_TOKENS = ("lat",)` 常量），按命名约定泛化（未来 `npu_latency`
等含 `lat` 子串的目标自动纳入）。caller 仍只测 latency 一个；docstring 显式声明
「未来加多 latency-like 目标需 caller 各自传实测」caveat（Rule 12 fail loud）。

#### P1-2 C5-live 退化分支方向硬编码 —— `workflows/agents/nas-train-runner/scripts/tail_metrics.py:300-322, 264-298`
`_pick_pareto_axes` 退化分支（无 quality 目标）时仍硬编码 `pareto_y_direction="max"`，
与 `push_pareto_final.py:88-92`「无 quality 轴直接跳过 C5」口径不一致。新增
`_axis_direction(kind)` 共享 helper：cost → "min"，quality → "max"。C5-live 推图时
x/y_direction 按 `obj_kind[ax]` 动态决定，退化分支（两 cost）两向均 min。caption
同步反映方向。纯函数单测钉死。

#### P1-3 bit-curve 逐层位宽热力图 —— `workflows/agents/bit-curve-searcher/scripts/run_bit_curve.py:235-355, 482-520`
原 docstring 承诺可视化 `bit_trend.json` 但零读取。SDK 自落盘到 `search_artifacts/bit_trend.json`，
schema 未公开稳定。新增 `_bit_from_value` + `_load_bit_trend_layer_bits` +
`_extract_records` 三个 fail-soft 解析 helper（覆盖 5 种合理 schema 形态：
dict[layer, bit|dict] / list[{layer, bit}] / 嵌套 layers|records 容器 / 兜底
records[0].layer_configs）。任一形态不匹配 → stderr warn + None（caller 跳过本图）。
新增 bar 图：x=layer, y=bit, color 区分 ≤4 / >4 档（珊瑚=低位宽，钢蓝=高位宽），
title=`Per-layer Bit-width Assignment (selected)`。

**兜底决策（schema 探不到）**：ts_quant 源不在本仓可探针处，repo 内无历史 bit_trend.json
产物。按 audit 允许的「合理兜底结构多形态 fail-soft 解析」实现，未做离线 bit_trend 推图测试
（render_chart 推图只在 Orca run 上下文可跑），多形态解析本身有 8 单测覆盖。

#### P1-4 寻优过程状态图 —— `workflows/agents/bit-curve-searcher/scripts/run_bit_curve.py:357-405, 522-548`
m0_pareto 是一次性 report 无代际推进，但 `report["archive"]["records"]` 含全部 evaluated
候选。新增 `_cumulative_best(records, y_direction)` 纯函数：按评估序派生 running best
（max/min 双向），空集 / 单点 / 全 NaN 三种边界显式处理。新增 line 图：
title=`Search Progress — cumulative best`，caption 显式标注「非代际推进曲线，
m0_pareto 一次性 report，本图按 archive records 评估序累加最优」。

**源头契约**：archive.records 顺序 = 评估序是 ts_quant 行为假设（探针 2026-07-20 实证）。
若 SDK 未来改为 candidate_id 排序，曲线会变锯齿状——单测只覆盖 cumulative best 计算本身
（按入参 list 序），不钉 SDK 行为。

#### P1-5 QAT 收敛曲线改 live 推 —— `workflows/agents/qat-trainer/scripts/run_qat.py:130-185, 221-289`
原全部 scheme 跑完才一次性 `_render_charts`。新增 `_make_live_push_fn(metric_kind)`
工厂：在 `_run_scheme` 训练 loop 内每 period 步（含 eval 采样点）增量推 line，
同 label=`quant/qat` + 同 title=`f"QAT Convergence — {scheme} ({metric_kind}, live)"`
= 刷新语义。**多 scheme 串行选「每 scheme 一张图（title 带 scheme 名）」分支**（audit
允许的两选一），避免同 title 刷新覆盖先跑完的 scheme；cross-scheme 对比仍由
`_push_charts` 在 main 末尾推一次终态图（hue=scheme 合一对比）。caption 说明清楚。
orca.chart 不可用时 stderr WARN（含「终态图仍会推送」提示）+ no-op，不阻断训练。

### P2（清理 + 覆盖）

#### P2-1 struct viz accuracy 维度 champion 演进 —— `viz_struct.py:275-372`
新增 `_push_champion_accuracy_trace`：x=round, y=accuracy, hue=series（candidate/champion），
对齐「降时延保精度」语义（latency 轨迹单独看不到精度牺牲）。FAIL_latency/FAIL_export
行 accuracy 缺失/负数 → 剔除（与 `_push_pareto` 同款过滤）。render_all pushers 列 +
import_failed 兜底 + `_main` fallback 三处同步加 `champion_accuracy_trace` 键。

#### P2-2 kd viz latency 维度 candidate 演进 —— `viz_kd.py:174-220`
新增 `_push_candidate_latency_trace`：x=round, y=latency_ms, hue=met_latency，对齐 KD
「latency-first」哲学。FAIL_export 行 latency_ms=-1 → 剔除。render_all pushers 加键。

#### P2-3 struct champion_trace x 轴 index → round —— `viz_struct.py:216-273, 264`
原 `x="index"` 改为 `x="round"`（ledger 行已含 round 字段）。caption + x_label 同步改。
round 缺失时用 ledger 行序兜底（保数，前端 x 轴仍单调）。

#### P2-4 删除孤儿 nas-viz —— `workflows/agents/nas-viz/` 全删
grep 全仓确认零引用（workflow yaml / agent.md / orca/ 源码 / 其它 scripts 均无引用），
目录被 `nas-select/scripts/` 取代。整个 `nas-viz/`（agent.md + 3 scripts）删除。
`nas-select/scripts/select_and_report.py:74-75` PARITY 注释检查后保留原样
（注释只列 select_and_report / tail_metrics / nas-select push_pareto_final 三处，
本就不含 nas-viz，删除不改变 parity 计数）。

#### P2-5 quant 共享 helper 下沉 —— `workflows/agents/_quant_scripts/_common.py`（新建）
四脚本重复的 7 份副本下沉到 `_common.py`：
- `BITWIDTH_PRESETS`（bit-curve 无对应字段；ptq-sweep/qat 三脚本的 _BITWIDTH_PRESETS 合一）
- `load_env_file(path, log_prefix)` — 参数化 stderr 前缀
- `load_adapter(path, module_name, log_prefix)` — 参数化每脚本独立 module 名
- `dump_json(obj, path)` — 原子 tmp + os.replace，default=str 兜底
- `free_model(q_model)` — 容错 del + gc + cuda cache
- `is_better(new, cur, higher_is_better)` — 纯函数
- `resolve_eval(adapter, fp_model, eval_loader, forward_fn, *, log_prefix)` — 三脚本
  （bit-curve/ptq-sweep/qat）契约相同，sensitivity 契约不同不强制并入。局部 import
  `build_teacher_student_eval_fn` 仅在 teacher-student 分支触发，业务路径 early return
  不被迫加载 ts_quant.eval。

四脚本改 wrapper 委托（保留各自 log_prefix / module_name / _free_q_model 命名以最小化
caller diff）。git diff 验证字节等价（除 log_prefix 参数化外不改业务逻辑）。移除各脚本
不再需要的 `gc`/`importlib.util`/`os`/`re`/`build_teacher_student_eval_fn` 顶层 import。

#### P2-6 sensitivity 业务异常 fail loud —— `run_sensitivity.py:246-253`
原直接调 `analyze_low_precision_sensitive_layers(**kwargs)` → SDK raise 以 exit 1 +
traceback 退出，根因埋栈里、退出码与其它三脚本「业务错 exit 3」漂移。包 try/except →
stderr + `sys.exit(3)`。

#### P2-7 bit-curve candidates_evaluated 语义 —— `run_bit_curve.py:566-577`
原取 `report["eval_calls"]`（评估调用数，含诊断锚点）但字段名暗示候选数。改为优先
`len(archive_records)`（真实候选数），回退 `eval_calls`。stdout JSON 字段名
`candidates_evaluated` 保持不变（output_schema 未破坏）。

## 测试覆盖

**新增** `tests/workflows/test_workflow_viz_audit_fixes.py`（43 单测）：
- P1-1：`_infeasible_result` / `_is_preserved_objective` 纯函数（latency 保留 / 多
  latency-like 泛化 / 无 latency 全 WORST / 大小写）
- P1-2：`_axis_direction`（cost→min / quality→max / 未知→min）
- P1-3：`_load_bit_trend_layer_bits` 多形态（flat dict 标量 / flat dict n_bits /
  records list / 嵌套 layers / 文件缺失 / 非法 JSON / bit=0 过滤 / 未知 schema）
- P1-4：`_cumulative_best`（空集 / 单点 / running max / running min / score 兜底 / 默认 0）
- P2-5：`_common.is_better` / `load_env_file`（exports 解析 / 不覆盖已有 / 缺文件 / 空路径）/
  `dump_json`（原子写 / default=str / 无 tmp 残留）/ `load_adapter`（成功 / 缺文件 exit 2）/
  `free_model`（None / 普通对象）/ `resolve_eval`（业务 / 业务缺 metric_spec exit 2 /
  业务空 primary_metric exit 2 / teacher-student 缺 forward_fn exit 2 / teacher-student
  带 mock ts_quant）/ `BITWIDTH_PRESETS`（全集 + w4a16 a_quant_enabled=False + mx block_size=16）

**测试纪律**：P1-1 / P1-2 用 AST 切片取**真源码** exec（不手抄），未来 problem.py /
tail_metrics.py 改 helper 逻辑测试自动跟随（Rule 9）。

**更新**：
- `tests/workflows/test_struct_kd_p7.py:228-252`：3 图断言 → 4 图（加 Champion Trace — Accuracy）
- `tests/workflows/test_viz_struct_robustness.py`：3 处 chart 集合断言加 `champion_accuracy_trace`、
  `len(calls) == 3` → `== 4`

## code-reviewer 结论

两轮自检（impl + 覆盖并行）：
- 🔴 **0 blocker**
- 🟡 **2 major**（已全修）：
  - 🟡-1/🟡-2：P1-1/P1-2 单测用手抄 exec → 改 AST 切片取真源码（Rule 9 真意图验证）
- 🟢 **5 minor**（3 采纳 1 接受现状 1 doc）：
  - 🟢-1（采纳）：ptq_sweep `_free_q_model` 局部 import → 顶部 import
  - 🟢-2（采纳）：`bit_trend_path` Optional 语义生效（caller 仅在文件存在时传 Path）
  - 🟢-3（接受现状）：cumulative-best archive.records 评估序契约加 release note 锚点（本文）
  - 🟢-4（采纳）：qat live push 禁用 WARN 补「终态图仍会推送」提示
  - 🟢-5（接受现状）：`_infeasible_result` 多 latency-like caveat 已 docstring 标注

## 验证

- `tests/workflows/`：159 passed（含 43 新测）
- `tests/compile/ tests/chart/ tests/workflows/ tests/schema/ tests/run/`：654 passed / 1
  pre-existing flaky（`test_demo_mixed_reaches_reporter` 隔离跑绿，非本改动引入）
- 4 脚本 AST parse 全 OK；import 清理干净（pyflakes-level 静态检查）

## 遗留 / 兜底决策

- **bit_trend.json schema 探不到**：ts_quant 源不在本仓可探针处，repo 无历史产物。
  按 audit 允许的多形态 fail-soft 解析实现；schema 升级时需重新探测补形态。
- **render_chart 推图本身无离线测试**：按 SPEC §13「render_chart 推图只在 Orca run
  上下文可跑」原则，新加的 4 张图（per-layer bit / cumulative best / accuracy trace /
  latency trace / per-scheme live line）留待真机 in-session E2E 验证。

## Commit SHA

`<填入 commit 后>`
