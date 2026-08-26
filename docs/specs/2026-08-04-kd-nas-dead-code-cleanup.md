# SPEC：kd-nas 死代码清理 + review 修复

**日期**：2026-08-04
**状态**：草稿（审计已验证 reachability，作为对抗预检；待 coder-agent 实现）
**背景**：2026-08-03 的 cleanup SPEC（fail-loud + 补图 + CONTRACTS）只给死脚本加了 DEPRECATED 头、把完整删除 defer 到本 SPEC。今日三 agent（code-review + logic verify + obsolete audit）确认了删除清单与 review 修复项。

**审计来源**：obsolete audit 已逐文件核实 reachability（active yaml `agent:` 字段 + agent.md 递归 + 测试消费者），结论可靠。

---

## §1 review 修复（小而确认）

1. **Y1 — `_LEDGER_STATUS` 对齐合约**（`kd_reducer.py:95`）：扩为 `{SUCCESS, FAIL_latency, FAIL_train, FAIL_build, FAIL_accuracy, FAIL_export}`，与 `kd_common.ALL_TERMINAL_STATUSES` / `viz_kd_stage.fail_status_bar order` / `finalize_kd.known_statuses` 一致。加 reducer 单测「FAIL_accuracy/FAIL_export 候选合法 append」锁合约（消除延时炸弹）。
2. **Y3 — `_parse_accuracy` 优先级**（`teacher_setup.py`）：`TEACHER_ACCURACY` 优先于 `STUDENT_ACCURACY`（用户自写 eval_command 可能同时含两者；TEACHER_ 是显式真值）。改 pattern 顺序：`TEACHER_ACCURACY → STUDENT_ACCURACY(+KIND) → JSON → NMSE/MSE/BER/SNR/accuracy`。加测试：stdout 同时含 TEACHER_ + STUDENT_ → 取 TEACHER_。
3. **Y5 — hue/met_* 字面统一**（`viz_kd_stage._push_all_models_table` + `finalize_kd`）：student 行 met_* 全用 `str(bool(...)).lower()`（"true"/"false"）；baseline/teacher 行填 `"ref"`（不再空串）。viz_kd_stage 与 finalize_kd 字面对齐小写。
4. **release note 补全**：为 `16bd8b5` 写 `docs/releases/2026-08-03-kd-nas-us-kd-mandatory-teacher-eval-master-table.md` + `docs/status/CHANGELOG.md` 加索引（commit SHA `16bd8b5`），覆盖 4 项改动。

**Y2/Y4/G1-G5**：不在本 SPEC（Y2/Y4 随 §2 删 viz_kd/setump_helpers 自然消除或显式白名单；G 类纯风格，defer）。

---

## §2 死代码清理 — Phase 1（纯删，零迁移）

reachability = 0 active yaml/agent；测试仅守护已删并行 sweep 行为。**删脚本 + 删其专属测试 + 删 parametrize 行**。

| 删 | 删测试（test_kd_redesign.py 行号，除非另注） | 备注 |
|---|---|---|
| `_kd_scripts/gate_all.py` + `agents/kd-gate/`（整目录）+ `_kd_scripts/distill_dispatch.py` | `:84(test_distill_dispatch_gate)`, `:324,370,909,937,975,1034(test_gate_all_*)`, `:1393(test_kd_gate_agent_md_*)`；`test_struct_kd_p7.py:858,860` parametrize 行 | distill_dispatch 仅 gate_all 用 |
| `_kd_scripts/train_pool.py` + `agents/kd-train/`（整目录） | `:508,516,524,540,555,581,597,832,1098,1170,1193,1507,1584,1644,1698,1723,1895(test_train_pool/train_one/*_r2_viz_kd_*)`, `:1416,1423(test_kd_train_agent_md_*)`；`test_struct_kd_p7.py:862` | distill/agent.md:38 的 train_pool 注释提及（provenance）一并删 |
| `_kd_scripts/viz_kd.py` | `test_struct_kd_p7.py::TestVizKd` 整类（`:259-651`，~20 测）；`test_struct_kd_p7.py:859` parametrize | pareto/sentinel/direction 不变量已在 d9fcd9c 迁进 viz_kd_stage（验证：`test_viz_kd_stage_metrics_tail.py:318,367,381,415`） |
| `agents/kd-select/`（整目录，含 `scripts/select_and_report.py`）+ `scripts/e2e_kd_nas_select.sh` | `:1918,1936,1964,1982,2008,2041,2068,2091,2115(test_select_*)` + const `:1795` | `test_select_pareto_front_*`(:1964,1982) 与已迁移的 pareto 覆盖重叠 → 删 |
| `_kd_scripts/_deprecated/train_adapter_template.py` + `_deprecated/README.md` | `:262,271(test_train_adapter_*)` | 整 `_deprecated/` 目录可删 |

**已 skip 的 obsolete 测试**（`@pytest.mark.skip` "obsolete after serial rework"，行 234,634,667,689,708,744,762,789,818,1407,1435）→ 同批删除（无分析成本）。

---

## §3 死代码清理 — Phase 2（先迁移不变量再删）

| 删 | 迁移什么 → 去哪 | 改测试 |
|---|---|---|
| `_kd_scripts/pick_variant.py` | `_validate_variant`（build_model/DUMMY_INPUT/step<0/leverage 校验）→ `kd_common.py`（或新 `_contract_checks.py`） | `test_receiver_variants.py:49-79` 改 import；删 `test_kd_redesign.py:95,115,127,142,157(test_pick_variant_*)` + `test_struct_kd_p7.py:856` |
| `_kd_scripts/measure_student.py` | `_compute_met_accuracy_absolute` + `_parse_accuracy` → `kd_common.py` | `test_struct_kd_p7.py:683-708` 改 import；删 `test_kd_redesign.py:1879` + `test_struct_kd_p7.py:854` |
| `_kd_scripts/setup_helpers.py` | `_walk_with_prune`/`_PRUNE_DIRS` → **不迁移（YAGNI，活跃路径无 tree walk）** | 删 `test_kd_redesign.py:1224,1238,1259,1288,1303,1327,1754,1784(test_setup_helpers_*)` |
| `_kd_scripts/teacher_model.py` | 无（CONTRACTS 标 legacy；活跃 teacher 来自 teacher-gen wrapper）。测试用作 fixture（contract .py 形状） | 用 `examples/kd-nas-demo/knowledge_base/.../spt_alt.py` 或最小合成 contract .py 替换 fixture（`test_kd_train_script.py:51,244-264`；`test_struct_kd_p7.py::TestTeacherSetup*` 766-835）；删 topology 测 `test_struct_kd_p7.py:40,62` |

迁移纪律：先迁移 → 改 import → 跑测试确认迁移后 invariant 仍被覆盖 → 再删源文件。

---

## §4 文档清扫

- `kd_common.py:26,36` 注释：`measure_student / viz_kd / kd-select` 三处消费者 → 改为 `viz_kd_stage` 单一消费者（迁移后）。
- `examples/kd-nas-demo/README.md:75`："measure_student / viz_kd / kd-select 三处同源判定" → "viz_kd_stage / kd_common"。
- `CONTRACTS.md`：移除已删脚本的 DEPRECATED 段（脚本不在了）。
- 历史 docs（`docs/plans/2026-07-25-*`, `docs/releases/2026-07-25-*` 等）：**不改写**（历史记录），仅在顶部加 "superseded by serial kd-nas (2026-08-03)" 一行 banner（可选）。

---

## §5 执行顺序 + 验证
1. §1 review 修复（Y1/Y3/Y5/release note）+ 测试 → commit `fix(kd-nas): review 修复（status 集对齐 + parse 优先级 + hue 一致 + release note 补全）`。
2. §2 Phase 1 纯删（5 组）+ 删测试 + 删 parametrize 行 + 删 obsolete skip 测试 → 跑全套 green → commit `chore(kd-nas): 删并行 sweep 死代码（gate_all/train_pool/viz_kd/kd-select/_deprecated）`。
3. §3 Phase 2 迁移（pick_variant/measure_student 不变量 → kd_common；setup_helpers YAGNI 删；teacher_model fixture 替换）→ 跑全套 green → commit `refactor(kd-nas): 迁移可复用不变量到 kd_common + 删 legacy helpers`。
4. §4 文档清扫 → 随 §3 commit 或独立。

**验证门**：每步后 `wsl.exe -e bash -lc 'cd /mnt/d/Projects/Orca && .venv/bin/python3 -m pytest tests/workflows/ -q'`（用 `.venv/bin/python3`，非 Windows stub）。预期：删 ~80 死测试后总数下降，5 个预存失败仍是仅有的红，0 新红。codereview agent 复核 diff。每 commit 仅 `git add` kd-nas 文件（不碰 frontend/e2e）。

## §6 风险
- **迁移漏 invariant**：Phase 2 必须先改 import 跑测试确认覆盖再删源。`_validate_variant` 守所有 receiver KB variant，漏迁会断 `test_receiver_variants.py`。
- **teacher_model fixture 替换**：替换 contract .py 须有相同 build_model/DUMMY_INPUT 形状，否则 teacher_setup fixture 测崩。优先复用 demo 的 spt_alt.py。
- **删整目录**（kd-gate/kd-train/kd-select）：确认无其它 yaml `agent:` 引用（审计已确认 0）。
- **commit 顺序**：§1 先（独立小修），§2/§3 分开 commit 便于回滚。
