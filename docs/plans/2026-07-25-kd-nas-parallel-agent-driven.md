# KD-NAS 并行：Agent 驱动 + setup 探 GPU 定并发 + 两阶段流水线

> 计划日期 2026-07-25。前置：`2026-07-24-kd-nas-distill-redesign.md`（串行 sweep 已落地，199 测试过）。
> 状态：**已被 2026-07-25 权威重构取代**（见下方「权威更新」）。下文原 `setup → sweep → $end` 方案仅作
> `gpu_probe.py` 规格与背景参考。

## 权威更新（2026-07-25，supersede）

**权威设计 = 重构 prompt 本身**。原计划的 `setup → sweep → $end` + 单 sweep agent 已弃。真实目标 DAG：

```
setup → gate → train → $end      （方案 A：确定性 gate，不加 LLM adjust 循环）
```

变更要点：
- **setup**（扩展 `kd-setup/agent.md`）：原 7 步保留 + **step 8 GPU 预检**（调新 `gpu_probe.py`）→ emit
  `concurrency` / `device_plan` / `gpu_report` / `per_variant_vram_bytes`（fail-soft：无 CUDA/NPU →
  concurrency=1 + WARN）。
- **gate**（新节点 + 新 `kd-gate/agent.md` + 新 `gate_all.py`）：**一个节点内串行处理全部变体**（不是
  workflow 循环）。每变体 `_validate_variant` + `tune_latency.py` + `distill_dispatch.py`；
  FAIL_latency → 立即落账（持 orca.lock）；ACCEPTED → 收集进 `<kd_artifacts_dir>gate_manifest.json`。
  emit `accepted_manifest_path / n_accepted / n_fail_latency / all_variants_count / all_processed`。
  路由 `n_accepted | int == 0` → `$end`；否则 → train。**确定性逻辑全在 `gate_all.py`**，agent 只调一次。
- **train**（新节点 + 新 `kd-train/agent.md` + 重构后 `train_pool.py`）：吃 gate manifest + setup
  `concurrency/device_plan/per_variant_vram_bytes`。Phase 启动 VRAM 再校验（不够降级，连 1 都放不下 fail
  loud）→ `ThreadPoolExecutor(max_workers=concurrency)` round-robin 绑卡 → 每 worker
  `train_adapter_template.py` + `measure_student.py --skip_latency`（复用 gate 干净 latency）→
  `as_completed` 主线程逐行增量 append ledger（单 worker 失败 try/except 记 FAIL_train 不杀整批）→ 末尾
  `viz_kd.py`。emit `variants_done / variants_total / sweep_status / fail_reason`。
- **删**：`kd-selector` / `kd-distill` / `kd-recorder` agent 目录（脚本 tune_latency / distill_dispatch /
  measure_student / train_adapter / pick_variant 全保留，被 gate/train 复用）。
- **`train_variants_parallel.py` → `train_pool.py`**：重命名 + 重构为「只做训练阶段」，去掉 tune/dispatch。

下方原 `setup → sweep → $end` 内容保留作 `gpu_probe.py` 算法规格与背景参考（算法本身仍权威）。

---

## Context（原计划，背景参考）

当前并行只有一条**游离的手动 CLI** `train_variants_parallel.py`：

- 没有任何 agent / workflow 节点调它（`grep` kd-distill/agent.md 零匹配）；用户得自己 SSH 上 GPU 机手跑
- `--concurrency` **写死默认 2**，没有任何判定逻辑——全仓 grep 无 `mem_get_info`/`nvidia-smi`/free VRAM 探测
- 每个 worker **独立跑全流水线**（tune→dispatch→train→measure），concurrency>1 时多个 worker 在同一张卡上**同时测时延** → contention 抬高读数 → **false FAIL_latency**（污染 sweep 核心判定，是正确性 bug 不是性能问题）
- N 个 train_kd 子进程叠一张卡 → **OOM 风险**，launch 前不查显存
- 账本**整批跑完才一次性 append**（`train_variants_parallel.py:260-266`）→ 主进程被 kill 则已完成的变体全丢

目标：把并行从「手动 CLI」升级为**agent 驱动**——setup 阶段探 GPU 状态、定并发数，sweep agent 拿着这个数跑脚本；时延串行测、训练并发跑；账本逐行 append。

## 已确认决策

| 维度 | 决策 | 为什么 |
|---|---|---|
| 入口形态 | **新 workflow `kd-nas-parallel.yaml`**：`setup → sweep → $end` | 不动已测的串行 `kd-nas.yaml`（确定性参考，199 测试）；DAG 固定不分支 → 路由确定性、易测；两 workflow 共享 ledger/teacher/KD 脚本 → 跨工具进度复用不变 |
| 被否决方案 | ❌ 在 `kd-nas.yaml` 里按 GPU 有无分支 | 非确定 DAG 形状，污染路由/测试；两种执行模型（单变体循环 vs 全批量）混一个 workflow |
| 谁定并发 | **setup（agent 驱动）**，数学在**新确定性脚本 `gpu_probe.py`** 里 | 用户要求「setup 检查 GPU 定并发」；rule 5：deterministic 数学用代码不用 model；agent 只调脚本读 `CONCURRENCY:` |
| 时延 vs 训练 | **两阶段**：Phase A 串行 latency gate → Phase B 并发 train | 时延对 contention 敏感（并发测→读数失真→false FAIL_latency）；训练对 contention 不敏感（只慢不错）。串行 gate 代价小（时延探测快） |
| OOM 防护 | (1) setup 探 free VRAM 算并发上限 (2) 多卡 round-robin (3) Phase B 启动再校验 free VRAM，不够则降级/WARN (4) 安全系数 0.8 | 四层防护；`_device.py::resolve_device` 已支持 `cuda:<local_rank>`，多卡基础设施现成 |
| 账本写 | **逐行增量 append**（`as_completed` 循环内，主线程已持 orca.lock） | 单线程写无竞争；JSONL append-only 逐行原子；kill 不丢已完成行 |
| 并发默认 | gpu_probe 探不到 CUDA/NPU → `concurrency=1` + WARN（仍可跑） | fail-soft；CPU 训练本就少见 |

## 目标架构（DAG）

```
kd-nas-parallel.yaml:
  setup → sweep → $end      （无循环；sweep 内部两阶段处理全部变体）
```

- **setup**（复用 `kd-setup` agent，**扩展**）：原 7 步全保留 + 新增 **GPU 预检步**（调 `gpu_probe.py`）→  emit `concurrency` / `device_plan` / `gpu_report`。
- **sweep**（**新 agent `kd-sweep`**）：调重构后的 `train_variants_parallel.py --concurrency <setup> --device_plan <setup>` → 解析 SUMMARY → 读 ledger 算 `variants_done/total` → 调 `viz_kd.py` 推 sweep 散点图 → emit done。
- **无 selector/distill/recorder 循环**：并行工具内部一次性遍历全部变体（两阶段），ledger 是唯一真相源。

> 串行 `kd-nas.yaml` **逻辑不动**，仅 setup 的 `output_schema` additive 加 3 个可选字段（concurrency/device_plan/gpu_report，serial 的 distill/recorder 忽略）。

## GPU 探测与并发判定（新脚本 `gpu_probe.py`）

确定性 CLI（stdout `KEY: value`，fail-soft，非零退出仅在输入契约不符）：

```
python3 gpu_probe.py \
  --teacher_cache <teacher_cache.pt> \
  --representative_variant <baseline_model.py 或首个 KB 变体> \
  --variants_count <N> --device auto --safety 0.8 --max_concurrency 8
```

**算法**（deterministic，全在代码里）：

1. **设备解析**：`resolve_device(device)`（复用 `_device.py`）。
2. **per-variant footprint 探测**（最准的廉价估法）：
   - 建 representative variant（`build_model(**KNOBS.default)`）→ move to device
   - **加载 teacher_cache**（每个 train 子进程各自加载一份，必须计入）
   - 建 Adam optimizer → 一个 dummy batch 跑 forward + backward + optimizer.step
   - `per_variant_bytes = torch.cuda.max_memory_allocated()` 增量（含 model+grad+Adam(m,v)+activations+teacher_cache）
3. **free VRAM**：`torch.cuda.mem_get_info()[0]`（每卡）。
4. **并发公式**：
   ```
   concurrency = max(1, floor(free_bytes * safety / per_variant_bytes))
   concurrency = min(concurrency, variants_count, max_concurrency)
   ```
5. **多卡 round-robin**：`n_gpus = device_count()`；`device_plan = [f"cuda:{i % n_gpus}" for i in range(concurrency)]`。
6. **stdout**：
   ```
   RESOLVED_DEVICE: cuda:0
   N_GPUS: 2
   FREE_VRAM_BYTES: 21873864704
   PER_VARIANT_VRAM_BYTES: 3221225472
   CONCURRENCY: 3
   DEVICE_PLAN: ["cuda:0","cuda:1","cuda:0"]
   GPU_REPORT: 2x GPU, 21.9GB free, ~3.2GB/variant → concurrency=3 (safety 0.8)
   ```

**fail-soft 分支**：
- 无 CUDA：`torch.npu.mem_get_info()` 若可用则走 NPU 同算法；否则 `CONCURRENCY: 1` + `DEVICE_PLAN: [""]` + `GPU_REPORT: WARN no CUDA/NPU, serial fallback`，**退出 0**。
- 探测异常（建模型/加载 teacher_cache 失败）：`CONCURRENCY: 1` + WARN + 退出 0（不阻塞 sweep）。
- 仅输入契约不符（如 representative_variant 缺 build_model）：非零退出 + stderr（fail loud）。

## 两阶段流水线（`train_variants_parallel.py` 重构）

把现在「每 worker 独立全流水线」拆成两阶段。distill agent.md 的脚本调用顺序**不变**（tune→dispatch→train→measure），只是跨阶段分布。

### Phase A — 串行 latency gate（一次一个变体）
```
for variant in sorted(variants):           # 确定序
    tune_latency.py → TUNE_STATUS + accepted_cfg + latency
    distill_dispatch.py → noop|train
    if FAIL_latency / noop:
        组 FAIL_latency ledger 行 → 立即 append（增量）   # 不占显存，不进 Phase B
    else:
        gated.append((variant, accepted_cfg, latency))    # 收集待训
```
- **串行**：时延读数干净，无 contention 失真。
- FAIL_latency 行**当场落账**（增量写，Phase A 里就持久化）。

### Phase B 启动前 — VRAM 再校验（安全网）
- 重新 `mem_get_info()`；若 `free < per_variant_bytes`（被别进程抢了）：
  - 还能放 ≥1 → 降级 concurrency（WARN：实际 free 多少 → 降到几）
  - 连 1 都放不下 → **fail loud**（stderr 报 free/need，退出非零；sweep agent 记 FAIL 并提示用户）
- 用 setup 传来的 `per_variant_vram_bytes`（gpu_probe 测的）作 need 基准。

### Phase B — 并发 train（VRAM-sized）
```
with ThreadPoolExecutor(max_workers=concurrency) as pool:
    futs = {pool.submit(_train_one, ctx, g, device_plan[i]): g
            for i, g in enumerate(gated)}        # round-robin 绑卡
    for fut in as_completed(futs):               # 主线程消费（已持 orca.lock）
        row = fut.result()                        # _train_one: train_adapter + measure(--skip_latency)
        append_ledger_row(row)                    # ← 逐行增量写 + flush
        log done
```
- `_train_one`：跑 `train_adapter_template.py`（`--device <device_plan[i]>` 绑定）+ `measure_student.py --skip_latency`（**复用 Phase A 的干净 latency**，HI-1）→ 组 SUCCESS/FAIL_accuracy/FAIL_train 行。
- **增量 append**：`as_completed` 在主线程，主线程持 `orca.lock` → 逐行 `f.write(json+"\n"); f.flush()`，无竞争；kill 不丢已完成行。
- 删掉原 `_main` 末尾的批量 append（`:260-266`）。

### 并发数权威
- **setup 是并发数的唯一权威**（gpu_probe 算，agent 读）。工具**信任** `--concurrency`，不再自定默认 2。
- 工具仅做 Phase B 的 VRAM 再校验（防护 setup→train 之间显存被抢），不做并发重算。
- 工具保留独立 CLI 可跑（向后兼容手动），但并发数现在来自 setup 传入。

## 新 agent / workflow 文件

### `workflows/agents/kd-sweep/agent.md`（新）
- 输入：setup 的全部路径字段 + `concurrency` / `device_plan` + inputs（test_command / accuracy_baseline / target_latency_ms / latency_provider / full_epochs / device / seed）
- 职责：
  1. 调 `train_variants_parallel.py`（两阶段）传 `--concurrency {{setup.output.concurrency}} --device_plan '{{setup.output.device_plan}}'` + 全路径
  2. 解析末尾 SUMMARY（每变体 status）；非零退出（Phase B VRAM fail loud 等）→ emit `status=FAIL`，粘 stderr 末段
  3. 读 `ledger_path` 算 `variants_done / variants_total`
  4. 调 `viz_kd.py` 推 sweep 散点图
- 输出：`done: true` / `variants_done` / `variants_total` / `sweep_status` / `fail_reason`

### `workflows/kd-nas-parallel.yaml`（新）
```yaml
name: kd-nas-parallel
description: "KD-NAS 并行蒸馏 sweep（agent 驱动）：setup 探 GPU 定并发 → sweep 两阶段（串行 latency gate + 并发 VRAM-sized train）..."
entry: setup
requires: [knowledge_base]
inputs: <同 kd-nas.yaml 的全部 inputs；并行无需新 input，并发由 setup 自定>
nodes:
  - name: setup
    kind: agent
    executor: opencode
    model: "deepseek/deepseek-v4-flash"
    agent: kd-setup           # 复用，扩展后多 emit concurrency/device_plan/gpu_report
    output_schema: <同 kd-nas.yaml setup + concurrency/device_plan/gpu_report（optional）>
    routes: [{to: sweep}]
  - name: sweep
    kind: agent
    executor: opencode
    model: "deepseek/deepseek-v4-flash"
    agent: kd-sweep
    output_schema:
      type: object
      required: [done, variants_done, variants_total]
      properties: {done, variants_done, variants_total, sweep_status, fail_reason}
    routes: [{to: $end}]
outputs:
  ledger_path: "{{ setup.output.ledger_path }}"
  kd_artifacts_dir: "{{ setup.output.kd_artifacts_dir }}"
  baseline_latency_ms: "{{ setup.output.baseline_latency_ms }}"
  concurrency: "{{ setup.output.concurrency }}"
```

## setup 扩展（additive，DRY）

`kd-setup/agent.md` 在原 step 7（预检变体）后加 **step 8 GPU 预检**：

```bash
# teacher_cache 已就绪（step 5）；representative_variant 用 baseline_model_path
GPU_OUT="$(python3 "$KD_SCRIPTS_DIR/gpu_probe.py" \
  --teacher_cache "$TEACHER_CACHE" \
  --representative_variant "$BASELINE" \
  --variants_count "$VARIANTS_COUNT" --device "{{ inputs.device }}" \
  --safety 0.8 --max_concurrency 8 2>&1)" || { echo "$GPU_OUT" >&2; CONCURRENCY=1; DEVICE_PLAN='[""]'; }
CONCURRENCY="$(echo "$GPU_OUT" | grep '^CONCURRENCY:' | awk '{print $2}')"
DEVICE_PLAN="$(echo "$GPU_OUT" | grep '^DEVICE_PLAN:' | cut -d' ' -f2-)"
GPU_REPORT="$(echo "$GPU_OUT" | grep '^GPU_REPORT:' | cut -d' ' -f2-)"
```

- `kd-setup` 输出 JSON 加：`concurrency`（int）/ `device_plan`（JSON 串）/ `gpu_report`（string）。
- `kd-nas.yaml` 的 setup `output_schema`：加这 3 个为 **optional properties**（不入 required）→ serial 测试不受影响，serial 的 distill/recorder 不读它们。

## Ledger 增量写契约

- **格式不变**（CONTRACTS.md §5 行 schema）。
- **写时机变**：Phase A 的 FAIL_latency 行 + Phase B 的每行训练结果，**各自完成后立即 append**（主线程持 `orca.lock`，逐行 `write+flush`）。
- 删 `train_variants_parallel.py` 末尾批量 append。
- done 谓词（`kd_common.is_variant_done`）不变 → 串行 workflow 下次启动仍把这些变体当 done 跳过（跨工具进度共享不变）。

## CONTRACTS 更新

- §0 目录布局：加 `gpu_probe.py` + `kd-sweep/agent.md` + `kd-nas-parallel.yaml`。
- §3 脚本 CLI 表：加 `gpu_probe.py` 条目；改 `train_variants_parallel.py` 条目（两阶段 + `--device_plan` + 增量写 + 并发权威在 setup）。
- §4 节点 I/O 表：setup 加 concurrency/device_plan/gpu_report；加 sweep 行。
- §6 铁律：加「时延测量必串行（contention 失真）」「并发数权威 = setup.gpu_probe」。

## 测试（验证意图，非仅行为）

- **`gpu_probe.py` 单测**（mock `mem_get_info`/`max_memory_allocated`）：
  - 并发公式正确性（free=20GB, per=4GB, safety=0.8 → 4；cap 到 variants_count/max_concurrency）
  - 多卡 round-robin（2 卡 → plan 交替 cuda:0/cuda:1）
  - fail-soft：无 CUDA → CONCURRENCY=1 + WARN，退出 0
  - 契约不符（representative 缺 build_model）→ 非零退出
- **`train_variants_parallel.py` 两阶段**：
  - Phase A 串行：3 变体其中 1 个 FAIL_latency → 该行先落账，另 2 进 Phase B
  - Phase B 并发：ACCEPTED 变体并发训练，device_plan round-robin 绑卡
  - 增量 append：模拟 kill（断点）→ 已完成行已在 ledger；末尾无批量写
  - Phase B VRAM 再校验：mock free 不足 → 降级 WARN；连 1 都不放 → fail loud 非零
- **契约**：`tars validate kd-nas-parallel.yaml`（路由/Jinja/required inputs 等价 0 error）
- **回归**：`kd-nas.yaml`（串行）全测试仍 199 passed（setup additive 字段不破坏）
- **E2E**：contract 测试过；真机 E2E（opencode+deepseek-v4-flash，GPU 机）待用户执行（同 serial 流程）

## 实施顺序（checkpoint）

1. `gpu_probe.py` + 单测 → 自测公式/fail-soft
2. `train_variants_parallel.py` 两阶段重构 + 增量写 + VRAM 再校验 + 单测
3. `kd-setup/agent.md` 加 step 8 + 输出字段；`kd-nas.yaml` setup schema additive
4. `kd-sweep/agent.md` + `kd-nas-parallel.yaml`
5. CONTRACTS 更新
6. spec-reviewer 过计划实现一致性 → code-reviewer 自检 → tars validate → 回归
7. commit + release note + CHANGELOG/CURRENT

## 风险与回退

- **per-variant footprint 估偏**：teacher_cache + activations 的实际峰值可能超探测值 → safety 0.8 + Phase B 再校验双保险；若仍 OOM，单变体崩被 try/except 兜住记 FAIL_train（不杀整批），ledger 增量写保证不丢。
- **多卡绑卡失败**：device_plan 给 `cuda:i` 但 i 越界 → train_adapter `resolve_device` 已处理（fallback local_rank）。round-robin 用 `i % n_gpus` 保证不越界。
- **回退**：`kd-nas.yaml`（串行）完全不动 → 任何时候用户可退回串行 sweep。
