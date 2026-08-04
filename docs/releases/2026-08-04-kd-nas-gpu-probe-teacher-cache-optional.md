# 2026-08-04 — kd-nas gpu_probe teacher_cache 可选（device-only 模式）+ setup step3 grep bug 修复

## 背景

用户在真实 GPU 机器跑 kd-nas，setup step3（GPU 探测）崩：

```
[gpu_probe] FAIL (variant contract): ValueError: teacher_cache 损坏或格式不可加载：
UnpicklingError: invalid load key, '"'.（setup step 5 应保证可加载）
```

→ exit 2 → workflow_failed。

**根因（架构错位，非局部 bug）**：
- `gpu_probe.py` 为**并发判定**设计：测 per-variant 训练显存（model+grad+Adam+**teacher_cache**+activation），
  `torch.load(teacher_cache)` 算 concurrency。故 `--teacher_cache` 必须是 `.pt`。
- 串行版 setup 在 **teacher 训练之前**执行（DAG: `flatten→setup→…→train_teacher`），`teacher_cache.pt` 不存在。
- 但 setup step3 沿用旧批量版 gpu_probe 调用（SPEC §6.2 "复用 step7 GPU 探测"），把 `$BASELINE`
  （flatten 产的 `.py` 契约）当 `--teacher_cache` 传 → gpu_probe `torch.load(.py)` → UnpicklingError。
- 串行版 concurrency 恒 1（SPEC §3.1），**根本不需要 VRAM/并发探测**，只需 device（cuda/cpu）。
  setup 复用了为并发设计的 gpu_probe，被迫传 teacher_cache 满足 required 参数 → 必崩。

## 改动（方案 a：device 探测从 VRAM 探测解耦）

### 1. gpu_probe.py：teacher_cache 可选 + device-only 模式
- `--teacher_cache` 改 `required=False, default=""`（仍是注册 flag，`--help` 仍列，`test_cli_flags_exposed` 不破）。
- 新增 `_probe_device_only(device_arg)`：只解析 device + n_gpus + free_per_card（不 build model / 不 load cache / 不测 VRAM）。
- `_main` 分两路：
  - **VRAM 模式**（传 teacher_cache）：走现有 `_probe_per_variant_vram` 测 per-variant 占用算并发（并发蒸馏池场景）。
  - **device-only 模式**（不传 teacher_cache）：`_probe_device_only` + `concurrency=1` + `PER_VARIANT_VRAM_BYTES=0`，
    GPU_REPORT 标 `device-only (no teacher_cache yet; serial setup)`。串行版 setup 走此路径。
- 旧调用（传 teacher_cache）行为不变；teacher_cache 损坏（如 `.py`）在 VRAM 模式仍 fail loud（exit 2）。

### 2. kd-setup/agent.md step3：删 --teacher_cache + 修 grep bug
- 删 `--teacher_cache "$BASELINE"`（走 device-only）；注释说明"setup 在 teacher 训练前，禁止传 .py 当 teacher_cache"。
- **顺手修 pre-existing grep bug**：`grep '^DEVICE:'` 永远匹配不到 gpu_probe 实际 emit 的 `RESOLVED_DEVICE:`
  （`_emit_fail_soft` + 正常路径都 emit `RESOLVED_DEVICE:`）→ `DEVICE_RESOLVED` 总空 → 总 fallback `inputs.device`，
  gpu_probe 的 device 探测一直被 fallback 掩盖。改为 `grep '^RESOLVED_DEVICE:'`，device 探测真正生效。

### 3. 测试 + 文档
- `test_kd_redesign.py`：加 `test_gpu_probe_device_only_without_teacher_cache`（不传 teacher_cache 不崩，回归守护）+
  `test_kd_setup_step3_gpu_probe_device_only_no_teacher_cache`（setup step3 命令不含 `--teacher_cache`，regex 定位命令块，不误伤注释）。
- `CONTRACTS.md` §3.1：gpu_probe CLI 行 `--teacher_cache` 改可选 + step 8→3。
- SPEC §6.2 setup：注明 device-only 模式（teacher 未训不传 `--teacher_cache`）。

## 设计决策

- **为何 device-only 而非造一个 dummy teacher_cache.pt**：串行版 concurrency 恒 1，不需要 VRAM 测量。造假 `.pt`
  仅为满足旧 gpu_probe 的 required 参数是 hack；解耦 device 探测（方案 a）让 gpu_probe 的 fail-soft 架构自然扩展
  "无 teacher_cache = 只测 device"，改动收敛在 gpu_probe + setup，不破坏 VRAM 模式契约。
- **grep bug 为何顺手修**：不修则即使 gpu_probe 正确 emit `RESOLVED_DEVICE:`，setup 也不用它（fallback），
  device 探测白做。修后 device-only 的 device 解析真正进 `setup.output.device`。

## 验证

- `test_kd_redesign.py` + `test_struct_kd_p7.py` 的 gpu_probe/cli/setup-step 子集：**16 passed**。
- 全套 `tests/workflows/`：**421 passed, 3 skipped, 5 failed**——5 个全为 HEAD 预存失败
  （finalize_kd ×2 / teacher_setup ×2 / kd_setup path fields ×1），**0 新红**。

## Review 第二轮闭环（code-reviewer，7 项全闭环）

code-reviewer 第二轮审全部改动（A gpu_probe + B flatten/baseline/R1），🔴 必须修复无，🟡/🟢 7 项全闭环：

- **🟡1 gpu_probe DRY**：抽 `_resolve_backend(device_arg)` + `_probe_gpu_inventory(backend)`，`_probe_per_variant_vram`
  + `_probe_device_only` 各自调用，消除 ~25 行 device 解析 + GPU inventory 逐字重复。helper 用 `return`/`free`
  局部变量与现有 `backend=`/`free_per_card` 不同名，replace_all 不误伤。
- **🟡2 llm_artifacts fallback 文档矛盾**：4 处 stale（yaml `flat_artifacts_dir` / `agent.md` input 段 / flatten
  release §1 / CHANGELOG）还说有 fallback，但 step3 代码（R1 闭环）已去 fallback → 统一删 4 处，与代码对齐
  （PROJECT_ROOT=dirname 总非空 → OUTPUT_DIR 总非空，无 fallback）。
- **🟡3 CONTRACTS §4 节点输出表整表 stale**：setup 含 `teacher_*/receiver_dir/device_plan/per_variant_vram_bytes/gpu_report`、
  `ckpts_dir`（实际 `checkpoints_dir`）；train_teacher "透传 setup"（实为自产）；train_script_verify `verify_status`（实 `verified`）；
  decide `terminate`（实 `continue_loop`）；finalize `champion_is_baseline/champion_student`（是 finalize_kd stdout 非节点 output）
  → 整表对齐 `kd-nas.yaml` 各节点 `output_schema` required。
- **🟡4 PROJECT_ROOT_IN 约定歧义**：step3 bash `"$PROJECT_ROOT_IN"`（shell 变量从未赋值）→ 改 inline argv[1]
  `"<LLM 填：step2 推断值>"`（与其他 agent.md `<placeholder>` 约定一致，无未定义变量风险）。
- **🟢1 device-only 测试 docstring 诚实标注**：CI 无 CUDA → `_main` 在硬件检查就 `_emit_fail_soft`，未进
  `_probe_device_only` 函数体；happy path 需 GPU 机器，等价性经 DRY helper 与 VRAM 模式共用间接覆盖。
- **🟢2 setup step3 CONCURRENCY grep 死代码**（被 `CONCURRENCY=1` 无条件覆盖）→ 删。
- **🟢3 setup device enum 含 "auto"**（output 经 gpu_probe resolved 不该 auto）→ 注明 `resolved: cuda:0/npu:0/cpu | fallback=inputs.device`。

验证：全套 `tests/workflows/` **421 passed / 5 预存失败 / 0 新红**。

## 未 commit

按惯例未 commit / 未 push。Commit SHA 待补。
