# Release: KD-NAS Trainer 引擎化 Phase 1（孤儿 + 叶子 skel + 单测）

**Date**: 2026-08-04
**Commit**: `6929685`
**Plan**: [`docs/plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md`](../plans/2026-08-04-kd-nas-trainer-engine-and-leaf-codegen.md) §5 Phase 1

## What was done

把单体 `train_pipeline.py`（~700 行 + 5 inline user slot）的训练循环固化为库引擎，LLM 未来只产 ~30 行叶子（Phase 2 切 emit）。**Phase 1 严格隔离**（N4）：仅新增引擎孤儿 + 单测，**不动 DAG / 不改 gen_train_script emit / 不删旧 references/templates/train_pipeline.py / 不改 kd-nas.yaml / 不改 kd 库 / 不改 CONTRACTS**。Phase 2 接口原子切换集中在同一 commit。

### 6 项产出

1. **`workflows/agents/_kd_scripts/kd/trainer.py`**（NEW）— `KDTrainer` + `TrainConfig` dataclass，三 mode：
   - **teacher**：纯 task_loss，存 ckpt（schema 同现模板）。
   - **distill**（Q2 命门顺序）：`KDStudentWrapper(student, hooks).to(device)` → teacher cache load → `build_kd_loss(leaves.compute_loss, kd_config)` → materialise one batch → `wrapper.eval()` + `no_grad` forward → `wrapper.train()` → **`kd_loss.prepare(s_feats0, t_feats0)`**（OFDAdapter 在此 lazy 创建）→ **`opt_params = wrapper.parameters() + kd_loss.kd_parameters()`**（prepare 后合并）→ `build_optimizer or Adam` → `build_scheduler`（可能 None）→ 循环。
   - **eval**：load ckpt（strict=False + unexpected fail loud F2）→ `leaves.eval_metric` → emit `STUDENT_ACCURACY`/`STUDENT_ACCURACY_KIND`/`MET_ACCURACY`/`ACCURACY_CONFIDENCE`（方向经 `kd_common.accuracy_direction`）。

   关键实现点：
   - **emit 双协议**：stdout 协议键（8 个）+ **日志前缀协议**字面命中 `metrics_tail._LOSS_LINE_RE`（`[train_pipeline:teacher] epoch=N loss_avg=F` / `[train_pipeline:distill] epoch=N kd_loss_avg=F`；**eval 不发此行** B2）。
   - **M1**：引擎只 `print` stdout，**不写 train.log**（调用方 redirect）。
   - **M3 scheduler None 守卫**：epoch 末 `if sch is not None: sch.step()`。
   - **M4 + B6 `_compute_proxy_mse`**：`max_batches=3`，每 batch `.to(device)` 后 forward，dataloader batch<3 graceful，空数据 fail loud（与 `_first_batch_x` F1 对齐——同样 fail loud 文案，禁裸 StopIteration stacktrace）。
   - **B5 + Q18 live push 降级**：`orca.chart` lazy import（防 iface/exec 循环）；push 失败 → stderr WARN + 训练继续 + stdout 协议行照发。
   - **D3 resume**：`latest.pt` 原子写（tmp + `os.replace`）+ sort_keys sha16 hash；mode/build_cfg/kd_config 不符 fail loud；scheduler_state 存在但当前 `build_scheduler` 返 None → stderr WARN + 丢弃（B4）。
   - **D4 早停**：`_metric_improved` 严格 `>` / `<`（等值不算改进，避免平坦指标每轮 ratchet 致 early-stop 永不触发）。
   - **R1**：`latest.pt` payload 只含 schema 8 键，杜绝 abs path 渗入 .pt（迁移友好）。

2. **`workflows/agents/_kd_scripts/kd/_leaves.py`**（NEW）— `Leaves` + 单文件加载器：
   - 不注入 sys.path（Q6），`spec_from_file_location` 单文件 exec。
   - **eager 校验**（D9-c）：启动校验 4 文件存在 + AST 签名（函数名 + 必填位置参数集相等，E9；optim.py 双 callable 共享一次 parse）+ AST 自包含 deny-list（拒相对 import；非白名单 top-level import 拒）。
   - **lazy exec**：body 在首次 property 访问时 exec，未用的叶子文件永不 exec。
   - **B7 错误契约**：缺文件 `FileNotFoundError` 带名；exec 失败 `LeafExecError` wrap 原异常（filename:lineno）。

3. **`workflows/agents/_kd_scripts/kd/_resume.py`**（NEW）— `ResumeState` + `ResumeMismatchError` + `save_latest` / `load_latest` / `maybe_load` / `warn_scheduler_drop`。R1 schema：`{state_dict, optimizer_state, scheduler_state, epoch, best_metric, mode, build_cfg_hash, kd_config_hash}`；`load_latest` 拒未知键。

4. **`workflows/agents/_kd_scripts/train_pipeline.py`**（NEW 孤儿）— argparse + 可选 `--config run_config.yaml` 合并（CLI > yaml > 默认）+ `--artifacts_dir`（叶子定位）+ `--experiment`（=variant_id）+ `--resume` + `--early_stop_patience`。仅单测驱动；Phase 2 接入 DAG 后才被节点调。

5. **`workflows/agents/kd-train-script/references/templates/leaves/{loss,data,eval,optim}.py.skel`**（NEW）— 4 个 NotImplementedError 骨架 + 函数签名 + 方向提示注释（eval.py 含 `snr/acc=max`；`mse/nmse/ber/db=min`）。AST 签名与引擎 loader 期望一致。

6. **`tests/workflows/test_kd_engine_trainer.py`**（NEW，33 单测）— fixture = minimal leaf（4 最小实现：mse loss + random re-iterable data + nmse kind eval + SGD optim）与 skel **共用签名契约**。覆盖：
   - 三 mode 跑通（teacher/distill/eval）；
   - **Q2 顺序 spy 断言**（patch `KDComposite.prepare` / `kd_parameters`，断言 prepare 先于 kd_parameters；用 hooks model + ofd kd_config）；
   - resume：mock latest.pt 后断言 `start_epoch == ckpt.epoch + 1` + **端到端续训真跑剩余 epoch**；
   - mode / build_cfg / kd_config hash 不符 fail loud（3 测试）；
   - 早停：patience 触发 break + best.pt 正确（constant metric 测试，断言 loss 行数 < epochs）；
   - kind 6 个方向参数化（nmse/mse/ber/db=min；snr/acc=max）；
   - live push 降级（mock orca.chart 缺失 → 训练继续 + stdout 协议照发）；
   - proxy_mse batch<3 graceful + 空数据 fail loud + `.to(device)`；
   - scheduler 返 None 不崩 / 返 StepLR 时 scheduler_state 入 latest.pt；
   - **R1**：latest.pt 无 abs path 字段（递归扫字符串值，跳过 state_dict 子字典）；
   - AST 自包含 deny-list（拒 sibling import）+ 签名相等 + lazy exec 真的不在 load 时跑；
   - entry yaml 合并 + 端到端跑通。

## Verification

- 33 单测全绿（`pytest tests/workflows/test_kd_engine_trainer.py`，3.41s CPU）。
- code-reviewer 四轮对抗闭环：1 blocker (Q2 测试顺序 spy) + 3 high + 3 决策；F1-F10 全闭环。
- 既有 `references/templates/train_pipeline.py` + `kd-nas.yaml` + `kd/{compose,wrapper,ema,losses}.py` + `CONTRACTS.md` 未改（`git diff` 空）。
- `grep -rln "KDTrainer\|_leaves\|_resume" workflows/agents/_kd_scripts/` 命中 4 个新文件。
- 现有 `tests/workflows/test_kd_train_script.py`（131s）零回归。
- `test_struct_kd_p7.py` 3 个失败（`test_kd_setup_node_exposes_path_fields` + `TestTeacherSetupLatencySource::test_latency_*`）经 `git stash` 验证为**预先存在**，与本次改动无关。

## Deviations from plan

- **`_metric_improved` 严格比较**（`>` / `<`）：原计划没明说，但 code-reviewer 审查发现非严格比较会让 best.epoch 在平坦指标上每轮 ratchet 致 early-stop 永不触发。改为严格后早期 epoch 不再算「改进」，与「patience=N epochs 无改进 break」的语义一致。
- **eval mode strict load**（F2）：原模板用 strict=False 仅 WARN；按 Rule 12 改为 unexpected 键 fail loud（missing 仍 WARN，主分支权重错配可接受）。
- **`_first_batch_x` 空数据 fail loud**（F1）：与 `_compute_proxy_mse` 的空数据 fail loud 对齐，避免 distill 启动期裸 StopIteration stacktrace。

## Next (Phase 2)

接口原子切换（同一 commit）：`kd-train-script` SKILL/agent/workflow 重写产 4 叶子 + run_config.yaml + run.sh + D8 AST 检测 → `gen_train_script` output_schema 切 `train_pipeline_path` 指向固定引擎 + additive 新字段 → 5 调用点（train-teacher train+eval / distill train+eval / finalize eval）按 §3.3 矩阵改 inline flag + `--artifacts_dir` → `fidelity_check.py` 逐叶子数值等价 + AST 自包含 + kind 硬校验 → `train-script-verify` 4 叶子并行 review → 删 `references/templates/train_pipeline.py` + CONTRACTS §3.1 flag diff 表。
