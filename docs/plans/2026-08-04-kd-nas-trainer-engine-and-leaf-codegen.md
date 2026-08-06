# 计划：KD-NAS Trainer 引擎化 + 叶子化 codegen + 产物拍平

> 2026-08-04 立；**v3.2 = 经 spec-reviewer 四轮对抗审查闭环**（v1 25 issue → v2 5 blocker/3 决策 → v3 4 blocker/2 决策 → v3.1 1 blocker+3 high+3 决策）。**第四轮明确放行 Phase 1 开工（隔离安全）**。
> 演进自 2026-08-03 串行迭代重写——**DAG 拓扑不变**，只重构 codegen（`gen_train_script`）节点及其下游消费方式 + 产物布局 + agent prompt 去源化。
>
> 决策来源：单体 `train_pipeline.py`（~700 行、5 个 `user_*` slot 内联用户代码）是"一坨"的根因。解法 = 训练循环固化为库引擎 `KDTrainer`，LLM 只产 ~30 行叶子文件被引擎 import。
>
> **v3.1 关键修订**（vs v3）：
> 1. **N4**：Phase 1 引擎**孤儿化**（仅单测驱动，**不改 gen_train_script emit**）；Phase 2（产叶子+改 SKILL）+ 切 emit + 5 调用点 **同一 commit**（接口原子切换）。
> 2. **N1**：§3.3 新增「调用点 × 字段 × 数据源」矩阵（distill kd_config 写 yaml / student_model_path·build_cfg·ckpt inline；finalize eval champion 三字段强制 inline）。
> 3. **M1**：引擎 print stdout；调用方 redirect 到 `runs/<exp>/train.log`（沿用现状）；引擎**不 own** 文件（删 v3 "引擎写 train.log" 措辞）。
> 4. **N3**：迁移 rewrite **全字段清单**（ledger.{ckpt,student_path} + champions.{snapshot} + teacher_meta.{teacher_model_path,teacher_onnx,teacher_cache}）+ 幂等条款。
> 5. **A4 澄清**：`--artifacts_dir`（per-run 叶子目录，引擎注入恒存在）≠ durable `kd_artifacts_dir`（拍平目标）；两者不同参数，分属 Phase 2/3 不冲突。
> 6. **决策采纳**：叶子加载 (c) eager 校验存在性+签名 / lazy exec body；迁移磁盘峰值接受 2x + 声明。
> 7. high 补全：scheduler.step() None 守卫、proxy_mse 每 batch `.to(device)`、CONTRACTS §3.1 flag diff 表。

## 1. 动机与根因

当前 `kd-train-script` 产出单体 `train_pipeline.py`（骨架 + 用户 loss/dataloader/eval/optimizer 整段塞 5 slot）。外部性：用户看不懂 / 无法逐脚本 review / 无 mid-epoch 续训 / 无循环内 evaluator+早停 / `logs/` 半孤儿 / agent prompt 渗 SPEC 来源叙事 / 产物层级过深。

**对比 nas-agent-pipeline**（`workflows/nas-agent-pipeline.yaml` + `nas-agent/nas_agent/` + `workflows/agents/{pytorch-model-optimizer,supernet-train-script,nas-search-pipeline,nas-train-runner,nas-select}/`）：固定引擎 + 项目薄壳。KD 照此收敛，但 **KD 有 nas-agent 没有的特性（OFD/FitNets/EMA + teacher_cache）须引擎自理**（Q23）。

## 2. 设计目标

1. 引擎固化（loop/resume/ckpt/early-stop/eval/live push/stdout 协议/日志前缀协议）。
2. 叶子化 codegen（4 叶子 + run_config.yaml + run.sh 人类用）。
3. 逐脚本 review（4 叶子并行 review + fidelity 数值等价 + AST 自包含 + workflow-verifier）。
4. 断点续训（原子 latest.pt + `--resume`）。
5. 循环内 evaluator + 早停（kind 方向生成时写死 + 硬校验）。
6. 启动器（run.sh 人类手动专用，节点不走 run.sh）。
7. 产物拍平（方案 A + 原子迁移）。
8. agent prompt 去源化。
9. 不破坏铁律（自包含 + tape 真相源 + 确定性路由 + fail loud + 单向依赖）。

## 3. 新架构：引擎 / 叶子 / 启动器

### 3.1 引擎层（固定，库代码，LLM 不碰）

```
workflows/agents/_kd_scripts/
├── train_pipeline.py        # ★ 固定入口：argparse + 构 TrainConfig + 调 KDTrainer.train()
│                            #   ★ N4：Phase 1 此文件作为引擎孤儿存在（仅单测驱动）；
│                            #     gen_train_script emit 的 train_pipeline_path 在 Phase 2 原子切换后才指向它
└── kd/
    ├── trainer.py           # ★ KDTrainer（loop/resume/ckpt/early-stop/eval/live-push/协议）
    ├── _leaves.py           # ★ importlib 单文件加载（eager 校验存在性+签名 / lazy exec body，决策 c）
    ├── _resume.py           # ★ latest.pt 原子读写 + hash 校验
    ├── compose.py / wrapper.py / ema.py / losses.py   # 既有（不变）
```

**★ 节点入口（D-1 b）**：train-teacher / distill / finalize 直调引擎 inline flag + `--artifacts_dir = {{ setup.output.per_run_artifacts_dir }}`（per-run 叶子目录，引擎注入恒存在）。**run.sh 仅人类手动用**。`train_pipeline_path` 字段在 Phase 2 原子切换后指向此固定入口。

**`KDTrainer` 脊柱**（Q2 distill 顺序命门）：

```python
@dataclass
class TrainConfig:
    mode: Literal["teacher","distill","eval"]
    artifacts_dir: Path          # per-run user/ 所在（叶子 loader 扫此，不进 sys.path）
    experiment: str = ""         # = variant_id → runs/<exp>/
    model_path: Path; build_fn: str; build_cfg: dict
    teacher_cache: Path|None; student_ckpt: Path|None
    kd_config: dict; epochs: int; lr: float; batch_size: int
    device: str; seed: int; variant_id: str
    out_ckpt: Path               # 收 best 拷贝（Q13）
    resume_ckpt: Path|None       # None/不存在 → 从头
    eval_every: int = 1; early_stop_patience: int = 0
    metric_baseline: float|None; metric_kind: str|None  # 仅 sanity

class KDTrainer:
    def train(self) -> int:
        leaves = _leaves.load(self.cfg.artifacts_dir / "user")  # eager 校验 + lazy exec
        model = _load_model_by_path(self.cfg.model_path, ...)
        start_epoch, best = _resume.maybe_load(self.cfg, model)
        if self.cfg.mode == "teacher":  return self._train_teacher(model, leaves, start_epoch, best)
        if self.cfg.mode == "distill":  return self._train_distill(model, leaves, start_epoch, best)
        return self._eval(model, leaves)

    def _train_distill(self, student_raw, leaves, start_epoch, best):
        wrapper = KDStudentWrapper.from_model(student_raw).to(device)   # B3 factory
        teacher = TeacherCache.load(self.cfg.teacher_cache).to(device)
        kd_loss = build_kd_loss(leaves.compute_loss, self.cfg.kd_config)
        dl = leaves.build_dataloader(batch_size)
        x0, _ = next(iter(dl)); x0 = x0.to(device)
        wrapper.eval()
        with torch.no_grad():
            _, t_feats0 = teacher(x0); _, s_feats0 = wrapper(x0)
        wrapper.train()
        kd_loss.prepare(s_feats0, t_feats0)            # OFDAdapter lazy 创建（compose.py:137）
        opt_params = list(wrapper.parameters()) + list(kd_loss.kd_parameters())  # Q2：prepare 后合并
        opt = leaves.build_optimizer(opt_params, lr) or Adam(opt_params, lr=lr)
        sch = leaves.build_scheduler(opt, epochs)
        for epoch in range(start_epoch, epochs):
            for x, y in iter(dl): self._step_distill(wrapper, teacher, kd_loss, opt, x, y, epoch)
            if sch is not None: sch.step()             # ★ M3：None 守卫
            best = self._on_epoch_end(epoch, best, wrapper, leaves)
        _resume.copy_best_to(self.cfg.out_ckpt)        # Q13
        self._emit_final()
```

**emit 协议（双契约，Q9/Q24）**：
- **stdout 协议键**：teacher `TEACHER_CKPT`+`TASK_LOSS_FINAL`；distill `STUDENT_CKPT`+`KD_LOSS_FINAL`+`KD_PROXY_MSE`；eval `STUDENT_ACCURACY`+`STUDENT_ACCURACY_KIND`+`MET_ACCURACY`+`ACCURACY_CONFIDENCE`。
- **★ 日志前缀协议**（`metrics_tail._LOSS_LINE_RE` 锚定 `metrics_tail.py:72-75`）：teacher/distill 每 epoch 末**必须** print 字面 `[train_pipeline:<mode>] epoch=<N> loss_avg=<f>`（teacher）/ `kd_loss_avg=<f>`（distill）；**eval mode 不 emit 此行**；metrics_tail mode 词表钉死 {teacher, distill}（B2）。
- **★ train.log 归属（M1）**：引擎只 print stdout；**调用方（agent 节点）redirect stdout → `<per-run>/runs/<exp>/train.log`**（沿用现状 `train-teacher:114`/`distill:210` 重定向模式）。引擎**不 own 文件、无 FileHandler**。distill agent `metrics_tail --source_log` 指此 redirect 文件。
- `KD_PROXY_MSE` 由引擎算（`_compute_proxy_mse`，max_batches=3，**每 batch `.to(device)` 后 forward**——M4 防 device mismatch；dataloader batch 数 < max_batches 时 graceful 取实际数，禁 StopIteration，B6/Q22）。
- **live push 降级**（B5）：`orca.chart` lazy import 失败 → stderr WARN + 训练继续 + stdout 协议行照发（metrics_tail 兜底）。`orca.chart` 必须 lazy import（Q18）。

### 3.2 叶子层（LLM 唯一产物，~30 行/个）

落 per-run `$ORCA_ARTIFACTS_DIR/user/`：`loss.py`(`compute_loss`)/`data.py`(`build_dataloader`)/`eval.py`(`eval_metric`→`(value,kind)`, kind∈{nmse,mse,ber,db,snr,acc})/`optim.py`(`build_optimizer`/`build_scheduler`，可返 None)。

**★ 自包含校验标准（Q6）**：loader 不注入 sys.path；禁 sibling/相对 import（常量/helper 内联同文件）；AST 禁入清单（`ImportFrom.level==0 and module not in 白名单`→FAIL；`level>0`→FAIL）。`_leaves.load` 错误契约（B7）：缺文件→FileNotFoundError 带名；exec 失败→LeafExecError wrap。**加载策略（决策 c）**：启动 eager 校验 4 文件存在 + AST 签名（fail early）；body 在首次调用时 lazy exec（不预 exec 未用文件）。

**★ 方向单一真相源 + 硬校验（D-2 b，Q5）**：运行时方向只信 leaf return kind（→`accuracy_direction`）。`run_config.metric_kind` 仅 sanity（防笔误），不一致 fail loud。**硬校验**：leaf kind 方向组（{snr,acc}=max/{mse,nmse,ber,db}=min）必须与 `inputs.accuracy_baseline_kind` 方向组一致，否则 fail loud。~~baseline+kind WARN~~ 删除。`metric_baseline` 来源链：`inputs.accuracy_baseline` → agent 写 run_config。

### 3.3 配置 + 启动器 + 字段矩阵

**`run_config.yaml`**：gen_train_script 产 teacher-mode 模板；**distill 每轮 read→patch 3 字段→dump**（`student_model_path`/`build_cfg`=accepted_cfg/`kd_config`；保留 epochs/lr/batch_size/eval_every/patience/metric_baseline/metric_kind；禁全量重建）。**优先级**：CLI `--flag` > env `MODE/RESUME/EXPERIMENT` > run_config.yaml > 引擎默认。**yaml 不写 `mode`**（A8）。EPOCHS/LR/PATIENCE 不进 run.sh env（用户改 yaml）。

**★ 调用点 × 字段 × 数据源矩阵（N1）**：

| 调用点 | mode | student_model_path | build_cfg | kd_config | epochs/lr | ckpt | accuracy_baseline |
|---|---|---|---|---|---|---|---|
| train-teacher train | teacher | n/a（用 `--model_path`=teacher wrapper） | `{}` inline | n/a | inline（gen 提取） | inline `--out_ckpt` | n/a |
| train-teacher eval（teacher_setup --eval_command） | eval | `--student_model_path`=teacher wrapper inline | `{}` inline | n/a | inline | `--student_ckpt`=teacher_ckpt inline | inline |
| distill train | distill | inline（每轮 student 不同） | inline（=accepted_cfg） | **yaml**（AST 决策） | yaml | inline `--out_ckpt` | n/a |
| distill eval | eval | inline | inline（=accepted_cfg） | n/a | n/a | inline `--student_ckpt`=本轮 ckpt | inline |
| finalize eval（champion） | eval | **inline**（champion 真相源） | **inline** | n/a | n/a | **inline** `--student_ckpt`=champion ckpt | inline |

**核心规则**（N1）：(a) 每轮变化的字段（student_model_path/build_cfg/ckpt）走 inline；(b) distill kd_config 写 yaml（AST ofd 决策结果）；(c) **finalize eval champion 三字段强制 inline**（yaml 被末轮 distill 覆盖，不可信；`finalize_kd.py:110-124` 现状已 inline 对齐）；**(d) E4：distill 移除 inline `--kd_config`（`distill/agent.md:200`），唯一真相源 = run_config.yaml**（CLI>yaml 否则 yaml 形同虚设）。所有调用点额外加 `--artifacts_dir {{ setup.output.per_run_artifacts_dir }}`（叶子定位 = workflow-run-scope 共享，非 per-node，D-A）。

**finalize eval 行注（E5）**：`--artifacts_dir` = workflow-scope per-run（整 run 单一 leaves 集），不是 champion 那轮 round-scoped。**矩阵第 2 行注（E6）**：train-teacher eval 经 teacher_setup `--eval_command` 的 shell 字符串嵌套，`--artifacts_dir` 须拼入该字符串字面量。

**distill redirect 片段（E13/M1）**：
```bash
PER_RUN="{{ setup.output.per_run_artifacts_dir }}"
EXP="r${ROUND}_student"   # = variant_id = experiment
mkdir -p "$PER_RUN/runs/$EXP"
python3 "$TRAIN_PIPELINE" --mode distill --artifacts_dir "$PER_RUN" --experiment "$EXP" ... \
  > "$PER_RUN/runs/$EXP/train.log" 2>&1   # 引擎只 print stdout；此处 redirect → train.log（M1）
# metrics_tail --source_log "$PER_RUN/runs/$EXP/train.log"
```

**★ A4 澄清**：`--artifacts_dir`（per-run，叶子目录，引擎注入恒存在）≠ durable `kd_artifacts_dir`（拍平目标）。两者不同参数：前者 Phase 2 接入（指向 per-run 叶子），后者 Phase 3 拍平。分属不同 Phase 不冲突。

**`run.sh`（人类手动专用，D-1 b）**：

```bash
#!/usr/bin/env bash
set -euo pipefail
export ORCA_KD_SCRIPTS_DIR="<abs _kd_scripts>"
KD_ENTRY="$ORCA_KD_SCRIPTS_DIR/train_pipeline.py"
ARTIFACTS_DIR="<per-run abs>"        # 叶子 + run_config
MODE="${MODE:-teacher}"; EXPERIMENT="${EXPERIMENT:-$MODE}"; RESUME="${RESUME:-}"
python3 "$KD_ENTRY" --config "$ARTIFACTS_DIR/run_config.yaml" \
  --artifacts_dir "$ARTIFACTS_DIR" --mode "$MODE" --experiment "$EXPERIMENT" \
  ${RESUME:+--resume "$RESUME"}   # 手动续训：RESUME=runs/<exp>/latest.pt
```

节点不走 run.sh；teacher_setup `--eval_command` = inline flag 调引擎 `--mode eval`（Q11）。

### 3.4 产物布局（方案 A + 原子迁移 + 全字段 rewrite）

```
<per-run $ORCA_ARTIFACTS_DIR>/
├── user/{loss,data,eval,optim}.py
├── run_config.yaml / run.sh
└── runs/<exp>/{latest.pt, best.pt, train.log}   # 引擎写 ckpt；train.log 由调用方 redirect（M1）

<project>/artifacts/              # ★ durable 跨 run 根（拍平，无 kd-nas 层）
├── ledger.jsonl / champions.jsonl
├── checkpoints/{teacher_cache.pt, teacher_ckpt.pt, r{N}_student.pt}
├── meta/teacher_meta.json
├── reports/final_report.md
└── models/{baseline/, teacher/, students/}
```

**★ ledger 迁移原子化 + 全字段 rewrite + sentinel 幂等（Q10/N3/E1/E2/E3）**——`kd-setup` 检测旧 `artifacts/kd-nas/` 存在时跑：
1. **copy**（非 move，总覆盖语义）旧 `checkpoints/meta/models` → 拍平新位置（**磁盘峰值 ≈ 旧+新 checkpoints 约 2x，迁移完释放**，D10；`tune_cache.json` 不迁移，删旧重建，R2）；
2. rewrite 路径字段 → `.new` 文件。**rewrite 算法（E3）**：`Path(p).relative_to(kd_old) → flat_new / rel`，**禁裸 string replace**（防 `kd-nas-artifacts` 同前缀 / 项目根含 "kd-nas" 误伤）。**全字段清单按拓扑分类（E2，实施前 grep 锁死）**：
   - **kd-nas 须 rewrite**：`ledger.{ckpt, student_path}`（`kd_reducer.py:76,88`）+ `champions.{snapshot}`（`:92`）+ `teacher_meta.{teacher_onnx, teacher_cache, teacher_ckpt}`（teacher_ckpt 在 kd-nas checkpoints/，须加；`teacher_setup.py:387,447` + `train-teacher/agent.md:58`）；
   - **per-run 禁 rewrite**：`teacher_meta.teacher_model_path`（teacher wrapper .py 落 per-run `$ORCA_ARTIFACTS_DIR`，run scope，不在 kd-nas 子树，`teacher-gen/agent.md:69`）；`teacher_cache.pt` 内部无路径字段，无需 pickle rewrite；
3. 校验新 ledger/champions 行数 == 旧；
4. `os.replace` 原子替换 ledger/champions/teacher_meta（逐文件）；
5. **sentinel `.migration_done`（manifest 含文件列表+行数+sha256）作最后一步原子 touch（E1/D-B）**；
6. sentinel 写成功后才删旧 `kd-nas/` 子树。
任一步失败 → 不动旧、不替换、fail loud。提供 `--dry-run`。
**幂等条款（E1 修正）**：sentinel 缺 → **从 copy 重跑**（copy 总覆盖语义读未动的 kd-nas 原始，flat 中间态被覆盖；多文件 partial-replace 后续步骤幂等）。sentinel 在 → 校验 flat 文件存在 → 直接进步骤 6 删旧。**不再用"检测新 ledger 已存在"判 done**（多文件 partial-replace hole）。

**`logs/` 顶层目录删除**：调用方 redirect stdout → `runs/<exp>/train.log`（M1）；`metrics_tail` 读此；删 distill `DISTILL_LOG`。

## 4. 关键设计决策

- **D1**：循环归引擎；节点入口 inline flag + `--artifacts_dir`；run.sh 人类专用。`train_pipeline_path` Phase 2 原子切换后指向固定引擎（Q1）。
- **D2**：方向生成时写死 kind + 硬校验（leaf kind 方向组 vs `inputs.accuracy_baseline_kind`）；删 WARN。
- **D3**：resume 原子化（latest.pt = 同 fs tmpfile + os.replace；sort_keys hash；mode 存 ckpt；hash 不符 fail loud；scheduler_state 不匹配 WARN+丢弃；`--out_ckpt` 收 best）。
- **D4**：早停 = 引擎特性（patience 轮无改进 break）。
- **D5**：启动器（人类用，通用 run.sh + per-round yaml）。
- **D6**：产物拍平 + ledger 原子迁移 + 全字段 rewrite。
- **D7**：agent prompt 去源化（grep 范围含 CONTRACTS.md；引擎留设计注释，禁来源叙事）。
- **D8**：模板边界 + AST 检测 + override（override = 声明 false-positive；真 GAN/RL/DDP → silently 错模型，用户承担，建议改用 nas-agent）。
- **D9（决策 c）**：叶子 eager 校验存在性+签名 / lazy exec body。
- **D10（决策 a）**：迁移磁盘峰值接受 2x + 声明。

## 5. 实施阶段（每 Phase 独立可 commit；Phase 1 隔离安全）

> **Q1/N4 保证**：Phase 1 引擎孤儿化（不改 gen_train_script emit、不动 DAG），单测驱动；接口原子切换集中在 Phase 2。Phase 1 可独立先行，不卡于 Phase 2+ 的细节。

### Phase 1 — 引擎孤儿 + 叶子 skel + 单测（不改 DAG 契约）
- [ ] `kd/trainer.py`：`KDTrainer`+`TrainConfig`，三 mode（distill 脊柱含 prepare→kd_parameters→opt + **scheduler None 守卫**，Q2/M3）+ resume（Q8）+ 早停 + ckpt + stdout 协议 + 日志前缀协议（Q9/Q24，eval 不发 loss 行，B2）+ `_compute_proxy_mse`（**每 batch .to(device)** + batch<3 graceful，M4/B6/Q22）+ `orca.chart` lazy import + 降级（B5/Q18）。**引擎只 print stdout，不写 train.log（M1）。**
- [ ] `kd/_leaves.py`：单文件加载 + 不注入 sys.path + 错误契约（B7/Q6）+ **eager 校验存在性+签名 / lazy exec**（D9）。
- [ ] `kd/_resume.py`：latest.pt 原子写（tmpfile+os.replace）+ sort_keys hash + mode/hash 校验 + scheduler_state WARN（B4/Q8/Q14）。
- [ ] `train_pipeline.py` 作为固定入口**存在**（孤儿，仅单测调；**gen_train_script emit 此路径暂不变**，N4）。
- [ ] **叶子 skel**（Q3）：`references/templates/leaves/{loss,data,eval,optim}.py.skel`（NotImplementedError + 签名 + 方向提示）。
- [ ] **单测 fixture 落 `tests/`**（M2）：实现者写 minimal leaf（4 最小实现），与 skel **共用签名契约**（AST 通过）。三 mode + resume（断言 `start_epoch==ckpt.epoch+1`）+ 早停 + kind 各方向 + mode/hash fail loud + live push 降级 + proxy_mse batch<3 + scheduler WARN + `.to(device)`。

### Phase 2 — 接口原子切换（产叶子 + 改 SKILL + 切 emit + 5 调用点 + CONTRACTS diff，同 commit）
- [ ] `kd-train-script` SKILL.md/agent.md/workflow doc 重写：产 4 叶子 + run_config.yaml + run.sh + 提 lr/epochs + D8 AST 检测。
- [ ] gen_train_script output_schema：**切** `train_pipeline_path` 指向固定引擎入口 + ADD `leaves_dir`/`run_config_path`/`run_sh_path`（additive）+ 保留 lr/epochs。
- [ ] **5 调用点**（train-teacher train+eval、distill train+eval、finalize eval）按 §3.3 矩阵改 inline flag + 加 `--artifacts_dir`（**finalize champion 三字段强制 inline**，N1）。distill 每轮 read→patch run_config.yaml（kd_config 写 yaml）。
- [ ] teacher_setup `--eval_command` = inline flag `--mode eval`（Q11）。
- [ ] `fidelity_check.py`：逐叶子数值等价 + AST 自包含（Q6）+ kind 方向硬校验（D2）。
- [ ] `train-script-verify`：4 叶子并行 review + AST 无残留 + workflow-verifier + kind sanity。
- [ ] grep `references/workflow-checklists/` 更新 `templates/train_pipeline.py` 引用（Q20）。
- [ ] **CONTRACTS §3.1 flag diff 表**（M6）：保留/新增（`--config/--artifacts_dir/--experiment/--resume/--early_stop_patience`）/删除/改名。
- [ ] 删 `references/templates/train_pipeline.py`。

### Phase 3 — 产物拍平 + logs 折叠 + 原子迁移（同 commit）
- [ ] `kd-setup`：`kd_artifacts_dir` 去 kd-nas 层；删 `logs/` mkdir；**5 步原子迁移 + 全字段 rewrite + 幂等**（§3.4，Q10/N3）。
- [ ] 调用方 redirect stdout → `runs/<exp>/train.log`（M1）；`metrics_tail` 读此；删 distill `DISTILL_LOG`。
- [ ] `teacher_setup.py`/`finalize_kd.py`/`viz_kd_stage.py`：durable 路径调整（finalize_kd 的 durable `--kd_artifacts_dir` 与 Phase 2 的 `--artifacts_dir` 是不同参数，A4 澄清）。
- [ ] `kd-setup` output_schema + `kd-nas.yaml` 路径字段同步。

### Phase 4 — agent prompt 去 SPEC 化（Q15）
- [ ] grep `workflows/agents/**/*.md` + `_kd_scripts/**/*.md`（含 CONTRACTS.md）扫除来源叙事（D7）。
- [ ] distill agent prompt 补「ofd fail → 降级 mse-only 重试一次」提示（M8）。

### Phase 5 — 端到端验证
- [ ] E2E：`examples/kd-nas-demo`（**已确认存在**：README/baseline_model.py/train.py/latency_provider.py/knowledge_base/；opencode + deepseek-v4-flash）全链路。
- [ ] resume：单测断言 start_epoch 对齐 + 手动 smoke（**多时点**：tmpfile 创建前/写中途/replace 前/replace 后，每次 resume 不 EOFError；kill 后 tmpfile 孤儿清理，N5/Q14）。
- [ ] 早停：patience 触发 → best.pt + out_ckpt 收 best + 协议键。
- [ ] release note → CHANGELOG → CURRENT.md。

## 6. 文件改动清单

| 文件 | 改动 | Phase |
|---|---|---|
| `_kd_scripts/kd/{trainer,_leaves,_resume}.py` | **NEW** | 1 |
| `_kd_scripts/train_pipeline.py` | 孤儿存在（P1）→ 固定入口接入（P2） | 1,2 |
| `kd-train-script/references/templates/leaves/*.py.skel` | **NEW** | 1 |
| `tests/...`（引擎单测 + 契约 fixture） | **NEW** | 1 |
| `_kd_scripts/CONTRACTS.md` | §0 目录 / §3.1 CLI + 日志前缀协议 + **flag diff 表** / 叶子契约 + AST / kind 词表 | 2,4 |
| `kd-train-script/SKILL.md`/`agent.md`/workflow doc/checklists | 重写 + 更新引用 + 去源化 | 2,4 |
| `kd-train-script/references/templates/train_pipeline.py` | **DEL** | 2 |
| `kd-train-script/scripts/fidelity_check.py` | 逐叶子等价 + AST + kind 硬校验 | 2 |
| `train-script-verify/agent.md` | 并行 review + kind sanity + 去源化 | 2,4 |
| `kd-nas.yaml` | gen_train_script schema（切+additive）+ 路径 | 2,3 |
| `train-teacher/agent.md` | inline flag + --artifacts_dir + eval_command + 去源化 | 2,4 |
| `distill/agent.md` | read→patch yaml + inline --resume + redirect train.log + 删 DISTILL_LOG + ofd 重试提示 + 去源化 | 2,3,4 |
| `kd-setup/agent.md` | 拍平 + 原子迁移 + 删 logs/ + 去源化 | 3,4 |
| `model-flatten/gen-student/decide/teacher-gen/finalize` agent.md | 去源化 | 4 |
| `_kd_scripts/{teacher_setup,finalize_kd,viz_kd_stage,metrics_tail,kd_common}.py` | durable 路径 + log 源 + 前缀契约 | 3 |

## 7. 风险与权衡

| 风险 | 缓解 |
|---|---|
| R1 引擎 bug 影响全局 | P1 单测先行（三 mode+resume+早停+kind+mode/hash+降级+proxy batch<3+.to(device)+scheduler WARN）；fixture=skel 同签名落 tests/（Q3/B10/M2） |
| R2 叶子契约漂移 | 骨架 + AST 签名 + 自包含（Q6）+ kind 硬校验（D2）+ fidelity 数值等价 + 并行 review；P1 契约测试作 P2 回归门（M2） |
| R3 resume 状态不一致 | sort_keys hash + mode 存 ckpt + hash 不符 fail loud + scheduler WARN（Q8/B4）+ 原子写（Q14） |
| R4 拍平碰撞 | per-run 隔离 + durable 文件名专属 + 单 wf 假设 + 原子迁移（Q6/Q10） |
| R5 非标准训练 | D8 AST 检测 + fail loud + override（用户承担 silently 错模型风险） |
| R6 去源化丢上下文 | 引擎留设计注释；CONTRACTS 留接口真相；客观边界 D7（Q15） |
| R7 计划过大 | 5 Phase + Q1/N4 中间态可跑；P1 隔离安全可先行 |
| R8 distill 脊柱顺序错 | Q2 显式伪代码 + 单测覆盖 prepare→kd_parameters→opt |
| R9 迁移磁盘峰值 | D10 接受 2x + 声明 + 幂等续跑 |

## 8. 非目标

不改 DAG 拓扑；不实现 GAN/RL/DDP 退化生成（v1 只检测+fail loud+override）；不动 nas-agent workflow；不改 ledger/champions **schema**（只改路径值 + 原子迁移）；不做 multi-workflow artifacts 隔离；ofd hook 决策移引擎留 v1.1（D-3 b）。

## 9. 验收标准

1. `kd-train-script` 产出 = 4 叶子 + run_config.yaml + run.sh（无单体生成）；`train_pipeline_path` 切指向固定引擎。
2. 引擎单测（落 `tests/`）：三 mode + resume（断言 `start_epoch==ckpt.epoch+1`）+ 早停 + kind 各方向 + mode/hash fail loud + live push 降级 + proxy_mse batch<3 graceful + `.to(device)` + scheduler_state WARN 全绿；fixture 与 skel 共用签名（AST 通过，Q3/B10/M2）。
3. E2E `examples/kd-nas-demo`（已存在）全链路跑通，产物落拍平 `artifacts/`；旧 `kd-nas/` 原子迁移成功（含全字段 rewrite，Q10/N3）。
4. resume：§9.2 单测 + 手动 smoke（多时点 kill → 不 EOFError 崩 + tmpfile 孤儿清理，N5/Q14）。
5. 早停 patience 触发 → best.pt + `--out_ckpt` 收 best + 协议键 emit（Q13）。
6. grep `workflows/agents/**/*.md` + `_kd_scripts/**/*.md`（含 CONTRACTS.md）`SPEC|cleanup 2026-|SPEC-REVIEW|deleted` = 0 命中（引擎设计注释除外，D7，Q15）。
7. `logs/` 顶层目录消失；stdout 由调用方 redirect 到 `runs/<exp>/train.log`，`metrics_tail` 锚 `[train_pipeline:<mode>]` 前缀命中（M1/Q9/Q12）。
8. code-reviewer 自检：依赖铁律（trainer.py orca.chart lazy import）、无重复、fail loud、测试覆盖意图（Q18）。

## 10. 审查闭环表（三轮 issue → v3.1 落点）

| ID | 严重度 | 摘要 | v3.1 落点 |
|---|---|---|---|
| Q1 | blocker | schema 断链 | D1 + Phase 2 原子切换（train_pipeline_path 切指向引擎） |
| Q2 | blocker | distill optimizer prepare 后构造 | §3.1 `_train_distill` 显式顺序 + R8 |
| Q3 | blocker | fixture 与 skel 时序 | Phase 1 前移 skel + fixture 落 tests/ 共用签名（M2） |
| Q4 | high | kind 词表漂移 | §3.2 词表含 db + D2 |
| Q5 | high | kind 双真相源 | D2 单一真相源=leaf kind + 硬校验 |
| Q6 | high | 自包含机器标准 | §3.2 AST 标准 + B7 错误契约 + D9 加载策略 |
| Q7 | high | D8 检测机制 | D8 AST 规则 + override 语义 |
| Q8 | high | resume hash/mode | D3 sort_keys hash + mode 存 + 不符 fail loud |
| Q9 | high | metrics_tail 前缀≠协议键 | §3.1 日志前缀协议（含字段名） |
| Q10 | high | 拍平破坏 ledger | §3.4 5 步原子迁移 + D6 |
| Q11 | high | teacher_setup eval_command | §3.3 inline flag --mode eval（D-1 b） |
| Q12 | high | 删 logs 后 metrics_tail 读啥 | §3.3/§3.4 调用方 redirect runs/<exp>/train.log |
| Q13 | high | best/latest/out_ckpt | D3 out_ckpt 收 best |
| Q14 | medium | resume 验收不可观测 | §9.4 单测 + 多时点 smoke（N5） |
| Q15 | medium | grep 漏 CONTRACTS | D7 + §9.6 |
| Q16 | medium | run_config 产者歧义 | §3.3 distill read→patch + 矩阵（N1） |
| Q17 | medium | distill run.sh 参数化 | D-1 b 节点不走 run.sh |
| Q18 | medium | lazy import | §3.1 + §9.8 + B5 |
| Q19 | medium | 方向误映射 | D2 硬校验（删 WARN） |
| Q20 | medium | checklists 回归 | Phase 2 grep |
| Q21 | low | hook AST 边界 | D-3 b v1.1 |
| Q22 | low | proxy_mse 归属 | §3.1 + batch<3（B6）+ .to(device)（M4） |
| Q23 | low | nas-agent 指针 | §1 |
| Q24 | low | 字段名 | §3.1 日志前缀协议 |
| Q25 | low | decide/viz 不消费 | 无需改 |
| B1 | blocker | finalize _run_eval 走 run.sh | D-1 b inline flag+--artifacts_dir |
| B2 | low | metrics_tail mode 词表 | §3.1 {teacher,distill} |
| B3 | medium | wrapper hook 来源 | §3.1 from_model factory |
| B4 | medium | scheduler_state 不匹配 | D3 WARN+丢弃 |
| B5 | medium | live push 降级 | §3.1 |
| B6 | medium | proxy_mse batch<3 | §3.1 graceful |
| B7 | low | _leaves.load 错误契约 | §3.2 |
| B9 | low | champions 字段 | §3.4 snapshot（已核实 kd_reducer.py:92） |
| B10 | low | fixture 填空语义 | §9.2 共用签名 |
| B11 | 权衡 | hook 双真相源 | D-3 b v1.1 |
| N1 | blocker | inline vs yaml 字段分工 | §3.3 矩阵表 |
| N3 | blocker | 迁移漏改 student_path/teacher_meta | §3.4 全字段清单 + 幂等 |
| N4 | blocker | Phase 1 train_pipeline_path 假声明 | Phase 1 引擎孤儿化 + Phase 2 原子切换 |
| N5 | 权衡 | smoke 单时点 | §9.4 多时点 + tmpfile 清理 |
| M1 | blocker | train.log 归属双真相源 | §3.1 引擎只 print，调用方 redirect |
| M2 | 权衡 | fixture 不经 gen | §9.2 落 tests/ 契约门 |
| M3 | high | scheduler.step None 守卫 | §3.1 伪代码补 |
| M4 | high | proxy_mse .to(device) | §3.1 + §9.2 |
| M6 | high | CONTRACTS flag diff | Phase 2 diff 表 |
| M8 | 权衡 | ofd fail 降级 | Phase 4 prompt 提示 |
| D-1 | 决策 | 节点入口 | (b) inline flag + --artifacts_dir |
| D-2 | 决策 | kind WARN | (b) 删 WARN + 硬校验 |
| D-3 | 决策 | hook v1/v1.1 | (b) v1.1 |
| D-9 | 决策 | 叶子加载 | (c) eager 校验+签名 / lazy exec |
| D-10 | 决策 | 迁移磁盘峰值 | (a) 接受 2x + 声明 |
| demo | 假阳性 | examples/kd-nas-demo 不存在 | **已核实存在**，§9.3 有效 |

## 11. v3.2 修订（第四轮闭环补充，Phase 2/3 落地清单）

> 第四轮审查收敛达成稳态（evaluator 第二轮无新反驳），**明确放行 Phase 1 开工**。以下为 Phase 2/3 开工前须落的精度项（已在 §3.3/§3.4 落 E1-E4；余项汇总于此）。

**决策（全采纳 a）**：
- **D-A**：`per_run_artifacts_dir` = **workflow-run-scope 共享**（非 per-node；证据 `teacher-gen:69` + 所有节点引用同一 `setup.output.per_run_artifacts_dir`）。Phase 2 smoke 加一行对拍（gen_train_script vs distill 的 `$ORCA_ARTIFACTS_DIR` 字面相等）兜底——若实为 per-node，leaves 共享设计崩，须改落 durable `student_models_dir`。
- **D-B**：幂等 = **sentinel `.migration_done`**（已落 §3.4）。
- **D-C**：per-phase smoke **纳入 §9 验收**（见下方 9.2b/9.2c）。

**Phase 2/3 落地项**：
- **E4**：distill 移除 inline `--kd_config`，唯一 yaml（已落 §3.3 规则 d）。
- **E1/E2/E3**：迁移 sentinel + 字段拓扑 + relative_to 算法（已落 §3.4）。
- **E9**：AST 签名相等规则——不 exec，函数名相等 + 必填位置参数集相等，默认参数 additive（落 §3.2 自包含校验 + fidelity_check）。
- **E5**：finalize leaves 生命周期 = workflow-run-scope 共享（同 D-A）。
- **E6/E13**：teacher_setup eval_command shell 嵌套 + distill redirect 片段（已落 §3.3）。
- **E7**：CONTRACTS §3.1 line 121 「`setup 调`」→「`train_teacher 调`」（Phase 2 改）。
- **E8**：§9.3 E2E 客观判据细化（见下）。
- **E10**：Phase 2/3 末加 per-phase smoke checkpoint（见 9.2b/9.2c）。
- **E12**：§9.7 eval log 0-match 预期非缺陷（只 teacher/distill log 命中；eval 不发 loss 行）。
- **R1**：Phase 1 单测断言 **ckpt dict 不含 abs path**（只 state_dict/optimizer_state/epoch/hash/mode），从源头杜绝未来 .pt 迁移需求。
- **R2**：`tune_cache.json` 迁移声明"不迁移，删旧重建"（已落 §3.4）。

**§9.2b（Phase 2 smoke）**：minimal teacher train+eval 跑通 + **per-run 拓扑对拍**（gen_train_script 产 leaves 的 `$ORCA_ARTIFACTS_DIR` == distill 读的字面值）。
**§9.2c（Phase 3 smoke）**：迁移 `--dry-run` 报差异 + **多时点幂等续跑**（kill 在 copy/rewrite/replace/sentinel 各阶段 → 重跑从 sentinel 缺判起 → 终态一致）。
**§9.3 E2E 客观判据（E8）**：ledger 行数 == max_rounds+1 / champions 末行 == min-latency admitted student / final_report 含 All Architectures 表 / 旧 `kd-nas/` 不存在 / flat 路径文件存在。
