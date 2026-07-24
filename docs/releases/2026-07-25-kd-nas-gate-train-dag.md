# Release Note —— KD-NAS v2: setup → gate → train DAG

> 日期：2026-07-25。分支：`in-session-unified-backend`。
> 前置：[2026-07-24 KD-NAS 重构](./2026-07-24-kd-nas-distill-redesign.md)（串行 sweep，199 测试过）。
> 权威设计 = 重构 prompt 本身；本 note 记录实际落地与偏差。

## 改动点

### DAG：`setup → selector → distill → recorder → …` → `setup → gate → train → $end`

原 4 节点 workflow 循环（每变体一轮 LLM 编排）改为 3 节点线性 DAG：
- **setup**（扩展）：原 7 步全保留 + **step 8 GPU 预检**（调新 `gpu_probe.py`）→ emit
  `concurrency / device_plan / per_variant_vram_bytes / gpu_report`（setup 是并发数唯一权威）。
- **gate**（新节点 + 新 `kd-gate/agent.md` + 新 `gate_all.py`）：**一个节点内串行遍历全部变体**。
  每变体 `_validate_variant` + `tune_latency.py` + `distill_dispatch.py`；FAIL_latency / FAIL_train
  **当场增量落账**（主线程持 `orca.lock`，逐行 write+flush）；ACCEPTED 收集进
  `<kd_artifacts_dir>gate_manifest.json`。路由 `n_accepted | int == 0` → `$end`，否则 → train。
- **train**（新节点 + 新 `kd-train/agent.md` + 重构后 `train_pool.py`）：吃 gate manifest + setup
  并发参数。Phase B 启动 VRAM 再校验（不够降级 WARN / 连 1 都放不下 fail loud 退 2）→
  `ThreadPoolExecutor(max_workers=concurrency)` device_plan round-robin 绑卡 → 每 worker
  `train_adapter_template.py` + `measure_student.py --skip_latency`（复用 gate 干净 latency，HI-1）→
  `as_completed` 主线程逐行增量 append ledger（单 worker 失败 try/except 记 FAIL_train 不杀整批）→
  末尾 `viz_kd.py` 推 sweep 散点。

### 删除
- `workflows/agents/kd-selector/` / `kd-distill/` / `kd-recorder/` agent 目录（脚本 tune_latency /
  distill_dispatch / measure_student / train_adapter / pick_variant 全保留，被 gate/train 复用）。
- `train_variants_parallel.py` 重命名 + 重构为 `train_pool.py`（只做训练阶段，去掉 tune/dispatch）。

### 为什么（设计动机）
- **时延测量必串行**：latency 对 contention 敏感（并发测→读数失真→false FAIL_latency）。gate 串行测；
  train 用 `--skip_latency` 复用。
- **LLM 编排开销**：原 workflow 循环每变体起一轮 agent，N 变体 = N 轮 LLM；gate 收进一个节点一个脚本，
  LLM 只调一次解析 stdout（rule 5：确定性逻辑全在脚本）。
- **账本增量持久**：原 train_variants_parallel 末尾批量 append（主进程 kill 则全丢）；改 `as_completed`
  逐行 write+flush，crash 不丢已完成行。
- **OOM 防护四层**：(1) setup gpu_probe 探 free VRAM 算并发上限 (2) 多卡 round-robin (3) train 启动再校验
  free VRAM，不够降级 WARN (4) 安全系数 0.8。
- **fail-soft**：无 CUDA/NPU → concurrency=1 + WARN，仍可跑（不阻塞 workflow）。

## 偏差与决策
- **`outputs:` 不引 `train.output.X`**：train 可能被跳过（gate.n_accepted==0 → $end），skipped node
  output=None，render 会 ExecError。故 workflow outputs 只暴露 setup + gate 字段（恒跑）；train 的
  variants_done / sweep_status 由 train agent stdout + ledger.jsonl 真相源暴露。
- **`gate_all.py` 不接 `--variants` 过滤**：契约是「一个节点处理全部变体」（确定性序），单测用临时
  小 KB 目录而非过滤。
- **gate 异常 FAIL_train 行**：gate 内 tune/dispatch 异常 → 记 FAIL_train + `all_processed=false`；
  done 谓词把 FAIL_train 视为终态 → 下次跳过（与原 train_variants_parallel 行为一致；`force_rerun`
  可覆盖）。

## 验证
- ✅ `tars validate workflows/kd-nas.yaml` → 0 error
- ✅ `pytest tests/workflows/` → 255 passed（含新 gate_all / train_pool / gpu_probe 单测：串行 gate
  FAIL_latency 增量落账 + accepted manifest + 二次 done-skip；gpu_probe 并发公式 + round-robin +
  fail-soft + 契约校验；train_pool VRAM 再校验纯函数 + 空 manifest + 增量账本 helper）
- ✅ `test_kd_agent_md_output_refs_in_schema` 过（新 agent.md 的 `{{ node.output.X }}` 引用都在 schema 内）
- ⏳ 真机 E2E（opencode + deepseek-v4-flash，GPU 机）—— 由后续 agent 执行

## Commit
- `<待 code-reviewer 反馈处理后填>`
