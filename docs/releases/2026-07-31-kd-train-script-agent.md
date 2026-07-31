# 2026-07-31 — KD-NAS train-script agent（生成统一 train_pipeline.py）

## 任务

新建 KD-NAS 训练脚本生成 agent（`kd-train-script`），把用户的 `train.py` +
teacher/student 模型契约（`build_model` + `DUMMY_INPUT` + `KNOBS`）变成
**自包含** 的 `train_pipeline.py`（一个脚本两模式：teacher / distill）。

复用 nas-agent-pipeline 的 `supernet-train-script` 的生成式哲学（folder-agent
结构 + 读用户 train.py → 自包含拷贝逻辑进生成脚本 + 校验闭环），做 KD-NAS
5 处针对性适配。

**范围隔离**：只新建 `workflows/agents/kd-train-script/` + 独立测试
`tests/workflows/test_kd_train_script.py`，不嵌入 workflow（留到统一阶段）、
不退役 `train_adapter_template.py`、不改范围隔离清单里的任何文件。

## 实际做了什么

### 1. folder-agent 结构（镜像 nas-agent-pipeline）

```
workflows/agents/kd-train-script/
├── agent.md                                          # 入口（执行指令 + 红线 + 输出 schema）
├── SKILL.md                                          # 3 步工作流 + verifier prompt 模板
└── references/
    ├── workflows/
    │   └── train_pipeline_script_generation.md       # 生成规则（9 sections + Validation + Forbidden）
    ├── workflow-checklists/train_pipeline_script_generation/
    │   ├── 01_training.md                            # 训练逻辑 checklist（18 项，8 [CRITICAL]）
    │   └── 02_cli.md                                 # CLI 一致性 checklist（14 项，5 [CRITICAL]）
    └── templates/
        └── train_pipeline.py                         # 参考实现（self-contained，~480 行）
```

`templates/train_pipeline.py` 是**参考实现 / gold example**——agent 复制它到
`<output_dir>/train_pipeline.py` 并特化（替换 placeholder / 搬用户逻辑）。
**自包含**：placeholder 未展开时也能 smoke 跑通（dummy MSE loss + re-iterable
随机 loader）。

### 2. KD-NAS 5 处针对性适配

1. **模型构建按路径 import**：`_load_model_by_path` 用
   `importlib.util.spec_from_file_location` 加载 teacher/student 的
   `build_model`，sys.path 注入 model_dir 让共享积木（`from _model8_blocks
   import ...`）解析；re-register `sys.modules` 让下游 import 命中缓存。
   **替代** nas-agent-pipeline 的超网 sandwich 采样。

2. **两模式一脚本**（nas-agent-pipeline 没有）：
   - `--mode teacher --model_path <teacher.py>`：纯 task_loss 训 teacher，
     存 ckpt（state_dict + build_cfg），供 teacher_setup 生成 cache。
   - `--mode distill --student_model_path <student.py> --teacher_cache
     <cache.pt>`：task_loss + KD loss 蒸馏，存 student_state_dict ckpt。
   - 共享训练基础设施（optimizer / scheduler / dataloader / task_loss /
     loop 骨架 / live chart push）—— `main()` 先解析 `(user_loss,
     build_dataloader)` 再 dispatch，两模式拿到同一对。

3. **KD loss 用 KD-NAS 库**：distill mode 内 lazy import
   `kd.compose.build_kd_loss`（task_loss + KD 组合）+ `kd.wrapper.KDStudentWrapper`
   + `kd.wrapper.TeacherCache.load` + `kd.ema.MeanTeacherEMA`。**不用**
   `nas_agent.train.distillation`。Lazy import 让 teacher mode 不依赖
   `kd/` 在 sys.path。

4. **删 sandwich + DDP/torchrun**：单卡 + `--device {auto,cuda,cpu}` CLI，
   零 `DistributedDataParallel` / `setup_distributed` / `sandwich` /
   `set_sample_config` / `torchrun` / `launcher.sh` 残留。AST 扫描验证
   （见测试 `test_template_no_forbidden_code_tokens`）。

5. **用户 train.py 边界更窄**：用户只提供 `compute_loss` +
   `build_dataloader`（参 `examples/kd-nas-demo/train.py`）。optimizer/
   scheduler：用户有就搬，没有用 `torch.optim.Adam` + no scheduler fallback，
   带 `# TODO(kd-train-script):` 注释显式标注 fallback 位置。

### 3. 校验闭环（参 nas-agent-pipeline 两层 + 适配）

- **Layer 1 静态**：`py_compile` + `--help` + CLI 一致性（每个 --flag 真能解析）。
- **Layer 2 功能 smoke**（小预算 CPU，1 epoch + batch_size 2）：
  - teacher 模式：build teacher + forward + 存 ckpt + state_dict 可 load 回。
  - distill 模式：build student + load teacher_cache + KD loss + 存 ckpt +
    student_state_dict 可 load 回。
- **Layer 3 verifier 子 agent**：SKILL.md 写 prompt 模板，仿
  `supernet-train-script` 的 workflow-verifier 模式——拿生成 workflow doc +
  checklist + 生成 artifacts + 用户原 train.py（cross-ref 只读）+ CONTRACTS.md，
  核查"生成脚本是否忠实复用用户 loss/dataloader/optimizer 逻辑"，迭代到
  `all-pass`。
- **删除** nas-agent-pipeline 的 DDP 校验、sandwich 校验、端到端 torchrun
  校验（KD-NAS 单卡不需要）。

### 4. CLI 契约（stable base CLI）

shared：`--mode {teacher,distill}` (required) + `--out_ckpt` + `--epochs` +
`--lr` + `--batch_size` + `--device` + `--seed` + `--variant_id` + `--build_fn`
+ `--build_cfg JSON`；teacher：`--model_path`；distill：`--student_model_path`
+ `--teacher_cache` + `--kd_config JSON`；user train.py 注入：
`--user_train_import` + `--user_loss_fn` + `--project_root`；env：`--env_anchor`。

stdout keys：
- teacher：`TEACHER_CKPT: <path>` + `TASK_LOSS_FINAL: <float>`
- distill：`STUDENT_CKPT: <path>` + `KD_LOSS_FINAL: <float>` + `KD_PROXY_MSE: <float>`

### 5. 测试套件（22 个，全绿）

`tests/workflows/test_kd_train_script.py`：
- 结构契约（5）：files all present / agent.md 强执行指令 / SKILL.md 3 步 /
  generation workflow 9 sections / checklist critical items。
- 静态校验（5）：py_compile / --help lists stable CLI / AST 扫描无禁用 token /
  用 kd 库（lazy import）/ mode dispatch AST 校验（强化版：AST 验证
  `if args.mode == "teacher"` 块内真调 `run_teacher_mode`，不仅验顺序）。
- 功能 smoke（5）：teacher placeholder fallback + 真 user train.py；
  distill 端到端（构造 teacher_cache.pt → mse KD loss → STUDENT_CKPT schema）；
  distill KD loss 有限数；distill + **fitnets feature-KD**（覆盖
  OFD/FitNets adapter 经 `prepare()` 预建 + `kd_parameters()` 进 optimizer 的复杂路径）。
- fail-loud（7）：teacher 缺 --model_path / distill 缺 --student_model_path /
  缺 --teacher_cache / teacher_cache 文件不存在 / **空 dataloader → NaN-loss
  guard（teacher + distill，不写 NaN ckpt）** / **user train.py 缺 loss_fn →
  AttributeError**。

测试 AST 扫描 forbidden token：避免误判模块 docstring 里的说明性文字
（"no DDP / torchrun / sandwich sampling" 出现在 docstring 是合规的），
只检查代码级（Import / Name / Attribute）。

## Code Review 闭环

派 `code-reviewer` 子 agent 做两轮审查，verdict 从 conditional-pass 升到
**pass**。共闭环 3 个 🟡 finding + 2 个 🟢 nit，记录如下：

### 🟡-1（已修） — NaN-loss fail-loud 漏洞
**问题**：`run_teacher_mode` / `run_distill_mode` 在 dataloader 跨所有 epoch
0 batch 时静默 `break` + 写 NaN ckpt + return 0（违反 CLAUDE.md Rule 12）。
`_compute_proxy_mse` 在空 dataloader 时返回 `0.0`（fake signal）。下游爆炸
路径：NaN teacher → NaN student → proxy_mse=0 全程 returncode=0。

**修法**：
- 两模式 `torch.save` 前 `if not math.isfinite(last_avg): raise SystemExit(...)`
  + stderr 明示"dataloader 空，请检查 build_dataloader"。
- `_compute_proxy_mse` 空 dataloader `if seen == 0: raise SystemExit(...)`。
- 加 [CRITICAL] checklist 项 13b（fail-loud guard）+ workflow §5/§6 文档化。
- 加 2 个测试守门（teacher + distill empty dataloader → 非零退出 + ckpt 不存在）。

### 🟡-2（已修） — feature-KD 路径无端到端 smoke
**问题**：smoke 只测 mse KD term，OFD/FitNets adapter 经 `prepare()` 预建 +
`kd_parameters()` 进 optimizer 这条最复杂路径未被端到端验证。

**修法**：加 `test_smoke_distill_mode_fitnets_kd`——用 spt_alt + teacher_model
（都暴露 `feature_hook_names()` 长度 2）跑 `kd_losses=["fitnets"]`，断言
STUDENT_CKPT 落盘 + KD_LOSS_FINAL 有限 + ckpt mode=="distill"。

### 🟡-3（已修） — `__inlined__` sentinel 策略与代码不一致（契约漂移）
**问题**：SKILL.md + workflow §3 文档化了"inline copy"策略（设 sentinel
`USER_TRAIN_MODULE = "__inlined__"` + 在 `_load_user_train` 加 dispatch
分支），但模板 `_load_user_train` 没对应分支——agent 严格按 SKILL 指引但
忘了改 `_load_user_train` → `ModuleNotFoundError`。

**修法（与 reviewer 建议 (a) 分歧，选 (b)）**：删除 inline 策略，**统一为
单一 path/module injection 策略**。Surface conflict 说明：
- reviewer 建议 (a)：保留双策略，模板加哨兵 guard。
- 我的决策 (b)：**KISS 优先**——单一策略减少 agent 决策面（无需判断 loss
  长度选策略）+ path injection 一次性加载已足够（用户 loss 改了重新跑即可）+
  模板无需 sentinel 分支 + 避免"文档承诺代码没兑现"的契约漂移。
- 取舍：丢失"用户 loss 极短时 inline"的便利性，但 path injection 的 init
  cost 可忽略，不值得为这种边角便利引入双策略。

### 🟢 修复（小成本）
- 删除 distill prepare 阶段未使用的 `y0`（仅 `x0` 用于 KD adapter 形状探测）。
- `ema = MeanTeacherEMA(...).to(device)` 合并双重赋值为单表达式。

### 🟢 不修（取舍说明）
- **DRY 5 处重复**（`_load_user_train` / `_load_model_by_path` / `_make_live_push`
  / `_compute_proxy_mse` / distill loop 与 `train_adapter_template.py` 重复）：
  本任务范围隔离（不退役 `train_adapter_template.py`），统一阶段才能抽
  `kd/training_utils.py` 共享。记入 Open Questions。
- **typing 风**（`Tuple` → `tuple`）：与 `train_adapter_template.py` 保持一致，
  不单独改。
- **`sys.modules[module_name]` 用文件 stem 做 key**：理论上两个同名不同目录的
  model.py 会互踩，但 KD-NAS teacher/student 文件 stem 不同（`teacher_model`
  vs `spt_alt`），实际不触发。

## 偏离计划之处

无大偏离。两处小决定：

1. **测试构造 teacher_cache.pt 直接用 `kd.wrapper.TeacherCache.build` + 跑
   fresh teacher（零参默认 init）**，不调 `teacher_setup.py`。原因：
   teacher_setup 依赖 onnxruntime 做 latency 测量，与 train_pipeline smoke
   关注的 KD 训练流水正交；用 TeacherCache.build 直接构造 4 字段 cache blob
   更精确、依赖更少。
2. **distill mode 的 KD imports 完全 lazy**（在 `run_distill_mode` 函数体内），
   比起 train_adapter_template.py 的 top-level import 更严格——目的是让
   teacher mode smoke 不需要 `ORCA_KD_SCRIPTS_DIR` 在 sys.path 就能跑（更
   清晰的模式隔离）。

## Verification

```
$ wsl.exe -- bash -lc 'cd /mnt/d/Projects/Orca && /home/mozzie/miniconda3/envs/orca/bin/python -m pytest tests/workflows/test_kd_train_script.py -v'

============================= 22 passed in 30.25s ==============================
```

相邻隔离测试无回归：

```
$ wsl.exe -- bash -lc '... pytest tests/workflows/test_kd_redesign.py tests/workflows/test_struct_kd_p7.py -q'
105 passed in 24.33s
```

## Commit

未 commit（待主 session 与用户确认后再 commit；范围隔离要求）。

## Open Questions（未覆盖决策）

1. **stdout keys 是否需要在 KD-NAS workflow 嵌入阶段正式写入 CONTRACTS.md §3？**
   当前 `train_pipeline.py` 的 stdout keys（`TEACHER_CKPT` / `TASK_LOSS_FINAL`
   / `STUDENT_CKPT` / `KD_LOSS_FINAL` / `KD_PROXY_MSE`）只在该 agent 的生成
   规则文档里定义。嵌入 workflow 时若新 train 节点要消费这些 keys，应在
   CONTRACTS.md §3 加 `train_pipeline.py` CLI 行（与现有
   `train_adapter_template.py` 行并列），并明确两者过渡关系（统一阶段退役
   `train_adapter_template.py` 还是保留？）。

2. **ckpt `mode` 字段（新增）需在 CONTRACTS.md 记入 schema 演进。**
   `train_pipeline.py` teacher ckpt 加 `"mode": "teacher"`，distill ckpt 加
   `"mode": "distill"`——相对 `train_adapter_template.py:371-381`（旧 distill
   ckpt 无 mode 字段）是 schema 演进。当前 `train_pool.py` / `measure_student.py`
   等消费者不读 `mode`，向后兼容；但未来统一阶段消费者会按 mode dispatch。
   建议在 CONTRACTS §3 加注："v3 起新模板（train_pipeline.py）ckpt 额外带
   `mode` 字段（`teacher`/`distill`），下游消费者（统一阶段）按 mode dispatch"。
   本任务**未改 CONTRACTS.md**（已被并行 agent 改动，避免 conflict）。

3. **DRY 5 处重复 → 统一阶段抽 `kd/training_utils.py`。**
   `train_pipeline.py` 与 `train_adapter_template.py` 在 5 处 helper 重复：
   `_load_user_train` / `_load_user_by_path` / `_make_live_push` /
   `_compute_proxy_mse` / distill 训练 loop。本任务范围隔离（不退役
   `train_adapter_template.py`），统一阶段应抽到 `workflows/agents/_kd_scripts/kd/training_utils.py`
   让两模板共享。当前重复有 reviewer 守门（指出但允许过渡期保留）。

4. **`--variant_id` 在 teacher 模式是否冗余？**
   当前 teacher mode 也接受 `--variant_id`（默认 `"model"`），主要用于
   chart label/title + ckpt 元数据。但 KD-NAS teacher 是唯一的（10 层 t1/t2
   交替），不像 student 有多个变体——`variant_id` 在 teacher mode 的语义
   是"teacher run id"而非"variant"。保留是为了 ckpt schema 一致性 + 嵌入
   阶段不需要做 mode-conditional 元数据。如果 reviewer 觉得冗余可改。

5. **CLI 一致性测试的 stable base CLI 列表与生成规则文档对齐维护方式。**
   当前 `STABLE_BASE_CLI` 在 `test_kd_train_script.py` 里硬编码，与生成
   规则文档 §1 文本对齐靠人工。未来如果加 flag 需要同步两处。可考虑从
   argparse 自动 introspect（`python -c "import argparse; ..."`），但代价
   是要 import 模块（可能拉 deps）。当前 KISS：硬编码 + 测试守门。

6. **`--env_anchor` bootstrap 路径无自动化测试。**
   成功 / 失败两路径都只靠 checklist 02 item 7 给 verifier 核查，无独立
   单测（因为 orca.chart._env 在测试环境不可用，mock 又增加测试复杂度）。
   可接受——env bootstrap 是 best-effort sidecar，本就不该阻塞训练。

## 不在范围内（明确不做）

- ❌ 嵌入 workflow yaml（DAG 修改、kd-{setup,train} 节点消费 train_pipeline）
  → 留到统一阶段。
- ❌ teacher 模型生成（teacher-gen agent）→ 下一轮。
- ❌ 退役 `train_adapter_template.py` → 嵌入阶段统一处理（避免双轨期破坏
  现有 kd-nas workflow）。
- ❌ 修改 KD 库（`kd.compose` / `kd.wrapper` / `kd.ema`）→ 只读消费。
- ❌ 修改范围隔离清单的任何文件。
