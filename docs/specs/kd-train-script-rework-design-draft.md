# 设计草稿：kd-train-script 重写 —— 模板占位符 → 基于用户代码的强制特化生成

> 状态：草稿 v1（2026-08-03）。待 review agent 比对确认后升 SPEC。
> 背景：用户指出当前 kd-train-script 生成策略是「拷贝参考模板 + 填占位符」，
> 与 NAS-AGENT-PIPLINE（supernet-train-script）「读用户原代码 → 从零生成特化脚本」
> 的设计不一致，导致远程产物残留 `{{...}}` 占位符、loss/eval 指标与用户原逻辑不符。
> 本草稿对齐 NAS-AGENT-PIPLINE 的设计（生成 + 校验两层），结合 KD-NAS 需求全面重写。

---

## 1. 问题诊断（现状缺陷）

| # | 缺陷 | 证据 | 后果 |
|---|---|---|---|
| D1 | 生成 = 拷贝模板 + 填 4 个占位符 | `train_pipeline_script_generation.md:53-54` "copy it … and specialise the placeholders" | 产物是「通用模板 + 字符串」，非项目特化脚本 |
| D2 | 占位符未填 → 静默 dummy fallback | 模板 `_placeholder_user_loss`(MSE) / `_PlaceholderDataLoader` / `_placeholder_user_eval`(dummy NMSE) | 远程真训练跑 dummy loss，显示指标与用户原训练不一致 |
| D3 | smoke 用 `--user_train_import` CLI 覆盖占位符 | `train_pipeline_script_generation.md` Layer 2 smoke 命令 | **未特化也能过全部验证** —— 校验层抓不住占位符泄漏 |
| D4 | loss/eval 是运行时 importlib path-injection，非自包含拷贝 | `_load_user_train` / `_load_user_eval` 运行时加载用户模块 | 与 agent.md 红线「生成脚本绝不 import 用户项目模块（必须自包含拷贝）」自相矛盾 |
| D5 | eval 指标靠 agent 主动发现移植，失败静默降级 dummy | `_load_user_eval` 占位符分支 | train 用用户 compute_loss、eval 用 dummy NMSE —— 「显示和原来的训练不一致」根因 |

**根因**：模板被设计为「自带 fallback 的可独立运行产物」，占位符是产物的一部分而非生成过程的中间态。

## 2. 设计目标（对齐 NAS-AGENT-PIPLINE）

NAS-AGENT-PIPLINE（`supernet-train-script`）的既定设计：

- **生成**：「Build the script from the Step 1 project context, the user's own training code… The generated script must follow the user's dataset, preprocessing, batch format, model-call signature, loss, metrics, optimizer, scheduler, logging, checkpoint, and runtime conventions.」（`train_supernet_script_generation.md:9`）—— 无模板占位符，产物逐字搬入用户逻辑。
- **校验**：「verifier 拿用户原训练脚本交叉核对」（SKILL.md Step 2.2 cross-references: 用户原训练脚本），checklist 含 [CRITICAL] 逐项比对（optimizer 类名 / scheduler 粒度 / loss 公式）。

**KD-NAS 新目标**：

1. `train_pipeline.py` 必须**基于用户原代码特化生成**：loss / dataloader / optimizer / scheduler / eval 指标全部**逐字搬入**，产物零占位符、零 dummy fallback、零运行时加载用户模块。
2. 占位符只允许存在于**模板本身**（生成起点），且模板未填 slot 的行为是 **fail loud（NotImplementedError）** 而非静默降级。
3. 校验从「能跑通」升级为「**与用户原逻辑一致**」：新增数值级等价性验证（fidelity check）+ 函数体交叉核对，直接守门用户痛点「train 的 loss / test 的指标 与原来不一致」。

## 3. 生成策略改造（模板 → 骨架 + 固定用户接口）

### 3.1 模板 `references/templates/train_pipeline.py` 骨架化

> **骨架 = 非可运行中间态**：模板头 docstring 显式声明「本文件是骨架，slot 未填时任何模式都不可运行（NotImplementedError fail loud）；只有 kd-train-script agent 特化搬入后才可运行」。防 LLM 把模板当「gold example」直接产出。
> 生成动作措辞统一为「**实例化骨架并填充 slot**」，不再用「拷贝模板」。

**保留**（与运行协议相关的骨架）：
- CLI（**删除** `--user_train_import` / `--user_loss_fn` / `--user_eval_import` / `--user_eval_fn` 4 个覆盖占位符的 flag）
- `--mode {teacher,distill,eval}` 三分派（eval 只读）
- `_load_model_by_path`（importlib by path 加载 teacher/student 模型——这是**模型**契约，非用户代码）
- ckpt schema（teacher/distill）+ stdout 协议（TEACHER_CKPT / STUDENT_CKPT / STUDENT_ACCURACY 等）
- `_make_live_push` / `_maybe_bootstrap_env`（web live loss sidecar）
- fail-loud guards（NaN loss / 空 dataloader / eval 非有限值）
- `--project_root`：**语义收窄**为「数据文件/路径解析用」（用户数据文件相对项目根定位），docstring + checklist 4 的旧理由（「for user-side from-X-import-Y」运行时注入）随注入机制消亡；distill 命令仍传（`kd-nas.yaml:171`），语义不变即零命令改动

**删除**（占位符体系）：
- `USER_TRAIN_MODULE` / `USER_LOSS_FN` / `USER_EVAL_MODULE` / `USER_EVAL_FN` 常量
- `_placeholder_user_loss` / `_PlaceholderDataLoader` / `_placeholder_build_dataloader` / `_placeholder_user_eval`
- `_load_user_train` / `_load_user_eval` 的 importlib path/module 注入分支（运行时加载用户模块）
- 4 个 `--user_*` CLI flag

**新增固定用户接口**（agent 生成时必须填的 slot；模板内以 `raise NotImplementedError` 占位 → 漏填 fail loud）：

```python
def user_compute_loss(s_out: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """kd-train-script: 逐字搬入用户 train.py 的 compute_loss（或等价 loss）函数体。
    未搬入（NotImplementedError）→ 任何模式 smoke 直接崩，fail loud。"""
    raise NotImplementedError("kd-train-script 必须搬入用户 compute_loss 函数体")

def user_build_dataloader(batch_size: int = 4):
    """kd-train-script: 逐字搬入用户 build_dataloader（或等价数据加载逻辑）。
    必须 re-iterable（每 epoch 重新 yield）。"""
    raise NotImplementedError("kd-train-script 必须搬入用户 build_dataloader 函数体")

def user_eval_metric(student: nn.Module, device) -> tuple[float, str]:
    """kd-train-script: 从用户 eval 脚本搬入指标计算 + eval 数据加载。
    返回 (value, kind)，kind ∈ {nmse,mse,ber,snr,acc}。"""
    raise NotImplementedError("kd-train-script 必须从用户 eval 脚本搬入指标函数")

def build_user_optimizer(params, lr) -> torch.optim.Optimizer | None:
    """用户 train.py 有 optimizer 时逐字搬入；无则返回 None（训练循环走显式 fallback + 注释）。"""
    return None

def build_user_scheduler(optimizer, epochs):
    """用户 train.py 有 scheduler 时逐字搬入（step 粒度与用户一致）；无则 None。"""
    return None
```

- 训练循环改为直接调 `user_compute_loss(out, y)` / `iter(user_build_dataloader(batch_size=args.batch_size))`；
  distill 的 KD composite 内部仍调 `user_compute_loss`（`kd.compose.build_kd_loss` 契约不变）。
- **固定函数名 = 机器可验证的接口**：fidelity_check / train-script-verify / verifier 都靠这 5 个名字 grep + 调用比对。

**搬入边界（MAJOR-1 修订）**：搬入 = 函数体 + **其引用的模块级依赖闭包一并拷贝**（常量、helper 类如 demo 的 `_SHAPE` / `_RandomDataLoader`，`examples/kd-nas-demo/train.py:23-59` 即此类）。拷贝后仍依赖用户项目符号（`from <user_pkg> import ...`）→ **fail loud**（不许运行时加载用户模块兜底）。

### 3.2 生成流程（agent.md / SKILL.md 重写，对齐 supernet-train-script 结构）

**Step 1 — Load Context**（读用户代码，不 exec）：
1. 读用户 `train.py`：找任务 loss（函数名不限于 `compute_loss`——按语义识别：接收 (output, target) 返回标量 loss）、数据加载逻辑（`build_dataloader` 或训练循环里的 dataset/loader 构造）、optimizer / scheduler。
2. 发现并读用户 eval 脚本（`test_*.py` / `eval*.py` / `evaluate*.py` / `test.py`，或 train.py 内 eval/metric 函数）：指标公式 + eval 数据加载。**找不到 → fail loud**（维持现契约）。
3. 读 teacher / student 模型契约（build_model + DUMMY_INPUT + feature_hook_names）+ KD 库 surface + 骨架模板。

**Step 2 — Generate**（特化，非填空）：
1. 拷贝骨架模板 → `$OUTPUT_DIR/train_pipeline.py`。
2. **逐字搬入**：`user_compute_loss`（原样函数体，同 ops 同 reduction）、`user_build_dataloader`（保 re-iterable，one-shot generator 包 re-iterable 适配器）、`user_eval_metric`（指标公式 + eval 数据加载自包含搬入）、`build_user_optimizer` / `build_user_scheduler`（存在才搬）。
3. 选 KD 项（保守默认纯 task_loss；feature-KD 仅当 hook 对齐）+ 更新 `--kd_config` 默认。
4. 提取 teacher 默认 lr/epochs（现有 Step 4 grep 逻辑保留；提取不到 fail loud）。
5. 校验 CLI 一致性（--help + stable base CLI，**已无 user_* flag**）。

**Step 3 — Validate**（四层，见 §4）。

### 3.3 用户契约微调（CONTRACTS.md 更新）

| 契约项 | 现状 | 新 |
|---|---|---|
| 用户 train.py | `compute_loss(s_out,y)` + `build_dataloader()` | 同上（函数名按语义识别）；**新增**：有 optimizer/scheduler 必须搬入；`build_dataloader` 缺失 → 从训练循环找等价数据加载搬入，找不到 **fail loud**（**删除**「lenient on loader with explicit fallback」静默降级） |
| 用户 eval 脚本 | 发现 + 移植，找不到 fail loud | 不变（移植 = 逐字搬入 `user_eval_metric`，不再有 dummy 降级） |
| train_pipeline CLI | 含 4 个 `--user_*` 覆盖 flag | **删除**（逻辑已自包含，无需运行时注入） |

## 4. 校验设计（四层，对齐 NAS-AGENT-PIPLINE 并强化）

对照 NAS-AGENT-PIPLINE 的校验骨架（静态 → 组件 smoke → 端到端 smoke → verifier 交叉核对），KD-NAS 版：

### Layer 1 — 静态 + 无残留
- `py_compile` + `--help` + stable base CLI 一致性（**无 `--user_*` flag**）。
- **AST 扫描零占位符残留**（新）：`{{` 字面量、`_placeholder_*` 标识符、`USER_TRAIN_MODULE` 常量、`importlib` 加载用户 train/eval 模块的代码路径（`_load_user_train` / `_load_user_eval` 函数名不得存在）。

### Layer 2 — 功能 smoke（三模式，CPU 小预算，**不再传覆盖 flag**）
- teacher：1 epoch，`user_compute_loss` + `user_build_dataloader` 生效（NotImplementedError 未填 → 直接崩 = fail loud 守门）。
- distill：**teacher_cache 来源（MAJOR-2 修订）**——DAG 时序上 gen_train_script 在 train_teacher 之前（SPEC §5：`gen_train_script → train_script_verify → train_teacher`），真 cache 生成时点不可得。二选一（SPEC 定版时选 1）：
  1. **测试 cache**：用未训练 teacher 经 `kd.wrapper.TeacherCache.build` 构造测试 cache（in-repo 先例 `tests/workflows/test_kd_train_script.py:133-158`）→ distill smoke 可跑，守门更全；
  2. 恢复「无 cache 时 distill smoke 标 Skipped」+ 由 train-script-verify / 真 distill 节点守门。
  两种都**不允许**用 placeholder fallback 替代。
- eval：真 `user_eval_metric` 对真 student ckpt → STUDENT_ACCURACY 协议。
- **关键差异**：旧 smoke 传 `--user_train_import` 覆盖占位符 → 未特化也 pass；新 smoke 无覆盖 flag，**脚本必须自带搬入逻辑才能跑**。

### Layer 3 — fidelity_check.py（新写确定性脚本，数值级等价性）
> 位置：`workflows/agents/kd-train-script/scripts/fidelity_check.py`（agent 资源；gen_train_script smoke + train-script-verify 复核共用）。
> CLI（MAJOR-3 修订）：`--train_pipeline <path> --user_train <path> [--user_eval <path>] --dummy_input <json> [--model_path <path>] [--build_fn build_model] [--build_cfg <json>] [--project_root <path>]`。
> `--model_path/--build_fn/--build_cfg` 供「模型 I/O」与「eval 指标数值一致」两项实例化模型用；缺省时这两项由 Layer 2 smoke（checklist 20b）覆盖，fidelity 只做函数级比对。
> 输出：`FIDELITY: PASS|FAIL` + `FIDELITY_LEVEL: numeric|ast` + 逐项 `KEY: bool|float`；非零退出 fail loud。

| 校验项 | 方法 | 守门的问题 |
|---|---|---|
| loss 数值一致 | 固定种子同输入张量 → 用户 `compute_loss` vs 生成 `user_compute_loss` → `torch.allclose`（rtol=1e-5） | 「train 调用一个函数」—— loss 显示与原来不一致 |
| dataloader 契约一致 | 两者产 batch shape 相同；re-iterable（两次 iter 均 yield ≥1 batch） | 空 loader / one-shot generator 静默跑空 |
| eval 指标数值一致 | 同输入（固定种子数据）→ 用户 eval 逻辑 vs 生成 `user_eval_metric` → allclose + kind 一致。**用户 callable 提取规则 = 镜像 agent Step 1 的 eval 发现规则**（glob `test_*.py` / `eval*.py` / `evaluate*.py` / `test.py` → 指标函数；demo 是 `_compute_nmse(model, n_samples)`，`test_student.py:80`，非 `(student, device)->(value,kind)` 签名 → fidelity 用 `functools.partial` 适配或声明该项降级 AST 比对）；**数据种子与用户脚本对齐**（demo 用 `torch.manual_seed(20260725)`，`test_student.py:82`） | 「test 调另外一个计算方式」—— 指标显示与原来不一致 |
| optimizer 类型一致（用户有时） | `build_user_optimizer` 构造的类名 == 用户 train.py optimizer 类名 | 用户 AdamW 被换 Adam |
| 模型 I/O | 模型 forward `DUMMY_INPUT` → shape == baseline（沿用 checklist 20b）；需 `--model_path` 等参数 | 形状漂移 |

**降级条款**：用户 train.py 无法被干净 import（import 副作用 / 依赖缺失）→ fidelity 数值项降级为 **AST 函数体比对**（Layer 4 verifier 语义核对），stdout 标注 `FIDELITY_LEVEL: ast` + 降级原因（fail loud 报告，不静默跳过）。KD-NAS 用户契约（纯函数 `compute_loss` + `build_dataloader`）下应极少触发。

### Layer 4 — workflow-verifier（语义级交叉核对，checklist 升级）
- **01_training.md 新增 [CRITICAL]**：
  - C21 无占位符残留（`{{` / `_placeholder_*` / `USER_TRAIN_MODULE` / `_load_user_train` / 4 个 `--user_*` flag）
  - C22 loss 函数体逐字比对：用户 `compute_loss` vs `user_compute_loss` —— AST 同 ops、同 reduction、同 shape 假设；**任何静默替换（MSE→L1、加归一化因子）= FAIL**（对齐 supernet checklist 12「Optimizer And Scheduler Type From User Project」的逐字比对精神）
  - C23 eval 指标函数体逐字比对：用户 eval 脚本指标 vs `user_eval_metric`（同公式、同归一化、同数据源）
  - C24 fidelity_check.py PASS 证据（`FIDELITY: PASS` + `FIDELITY_LEVEL: numeric`，或 AST 降级已声明原因）
- **01_training.md 改写/删除占位符语义项**（MAJOR-4 修订，否则与 verifier 冲突）：
  - **C5**「placeholder fallback is only kept when unexpanded」→ 改写为「**零占位符 fallback**：生成产物不得含 `_placeholder_*` 任何路径」；
  - **C17**「Placeholder Fallback Keeps Script Runnable」→ **删除**（骨架不可独立运行是设计，产物必须特化）；
  - **C20** 反模式里的 `_PlaceholderDataLoader` 引用 → 改写为「一切 shape 字面量必须读 DUMMY_INPUT」不涉 fallback。
- **02_cli.md 更新**：删 `--user_*` flag 检查项（§1 列表逐 flag 点名删除）；新增 [CRITICAL] 「无 `--user_*` 覆盖 flag」（防回退）。
- **保留**：三 mode / 无 DDP+sandwich 残留 / importlib by path（模型）/ 自包含 / optimizer verbatim（原 C7）/ ckpt schema / stdout keys / fail-loud guards / DUMMY_INPUT shape / feature_hook / live push。

### workflow 层（train-script-verify 节点，agent.md 升级）
- 保留：grep 三 mode 函数 + `_make_live_push` / `_maybe_bootstrap_env` + micro eval（用 baseline contract 跑一步）。
- 升级（MAJOR-5 修订，**替换**而非叠加旧检查）：
  - **删除旧 substring 检查**（`'compute_loss' in tp` / `'build_dataloader' in tp`，`train-script-verify/agent.md:61-64` —— 新命名 `user_compute_loss` 下会 vacuous pass）；
  - 改为 grep 5 个固定接口定义存在（`def user_compute_loss` / `def user_build_dataloader` / `def user_eval_metric` / `def build_user_optimizer` / `def build_user_scheduler`）；
  - grep **无 `{{` 残留**；
  - 调 `fidelity_check.py` 复核 `FIDELITY: PASS`。
- verified=false → fail loud 阻塞（不进 train_teacher）—— 维持现协议。

## 5. 改动文件清单

| # | 文件 | 改动 |
|---|---|---|
| 1 | `workflows/agents/kd-train-script/agent.md` | 执行流程重写（读→特化搬入→四层验证）；红线增「零占位符残留 / 禁运行时加载用户模块」；删 `--user_*` 相关 |
| 2 | `workflows/agents/kd-train-script/SKILL.md` | Step 2 改「特化生成」非「copy + fill placeholders」；Step 3 四层校验 + fidelity；**verifier prompt 模板同步**（加 5 接口 grep 证据 + `FIDELITY: PASS` 证据项，SKILL.md:152-201） |
| 3 | `workflows/agents/kd-train-script/references/templates/train_pipeline.py` | 骨架化：删占位符/fallback/`--user_*`/`_load_user_*`；加 5 个 NotImplementedError slot；**模块 docstring 同步改写**（现 :23-26 描述占位符 fallback 行为的 docstring 含 `{{...}}`，一并删除，否则 Layer 1 `{{` 扫描需区分 docstring） |
| 4 | `workflows/agents/kd-train-script/references/workflows/train_pipeline_script_generation.md` | §1 stable base CLI 列表逐 flag 点名删 4 个 `--user_*`；§3 改自包含搬入策略（含依赖闭包规则）；§3.1 eval 搬入；「smoke-testable gold example」措辞改写；Validation 改四层 + fidelity |
| 5 | `workflows/agents/kd-train-script/references/workflow-checklists/.../01_training.md` | 新增 C21-C24；**改写 C5/C17/C20**（删占位符语义项，防 verifier 冲突） |
| 6 | `workflows/agents/kd-train-script/references/workflow-checklists/.../02_cli.md` | 删 `--user_*` 检查；加「无覆盖 flag」[CRITICAL] |
| 7 | `workflows/agents/kd-train-script/scripts/fidelity_check.py` | **新写**：数值级等价性校验（§4 Layer 3） |
| 8 | `workflows/agents/train-script-verify/agent.md` | 升级：**替换**旧 substring 检查（防 vacuous pass）→ 5 接口 grep + 无 `{{` 残留 + fidelity 复核 |
| 9 | `workflows/agents/_kd_scripts/CONTRACTS.md` | train_pipeline CLI 契约删 4 个 `--user_*`；用户契约增 optimizer/scheduler 搬入 + 删 loader 静默降级；**:23「teacher+distill 两模式」陈旧措辞改三模式** |
| 10 | `tests/workflows/test_kd_train_script.py` | STABLE_BASE_CLI 删 4 flag；**测试侧特化 helper**（程序化填 slot 生成可测产物，替代 `--user_train_import` 注入）——列明 8 个受影响用例逐一映射（:393/:423/:464/:519/:552/:677/:709/:742）；placeholder smoke 改版（无覆盖 flag 跑）；新增 fidelity 测试 |
| 11 | `workflows/kd-nas.yaml` | **仅注释级**：gen_train_script 节点描述同步（train_teacher/distill/finalize 命令本就无 `--user_*`，零命令改动） |
| 12 | `examples/kd-nas-demo/README.md` | **例外**：脚本级自验命令（:198）删 `--user_train_import`（现依赖已删 flag） |

**不改**：`examples/kd-nas-demo/train.py` / `test_student.py`（用户侧契约不变）；`_kd_scripts/kd/*`（KD 库只读）；下游 `train_teacher` / `distill` / `decide` / `finalize` 节点命令（无 `--user_*`，`kd-nas.yaml:104-112/161-173/449-468` 已核实）。

## 6. 边界情况

| 场景 | 处理 |
|---|---|
| 用户 loss 函数名不是 `compute_loss` | 按语义识别（`(output, target) -> scalar`）搬入，固定命名为 `user_compute_loss` |
| 用户无 `build_dataloader` | 从训练循环找等价数据加载逻辑搬入；找不到 → fail loud（不静默 fallback） |
| 用户 eval 指标在 `main()` 内联 | 抽取指标计算 + 数据加载，搬入 `user_eval_metric` |
| 用户 train.py 有副作用 import | fidelity 降级 AST 比对 + 记录原因（fail loud 报告） |
| 用户有 optimizer 但无 scheduler | 只搬 optimizer，scheduler 返回 None（显式注释，不发明） |
| 用户有 scheduler | 搬入且 **step 粒度与用户一致**（per-epoch vs per-batch，对齐 supernet checklist 14） |
| 模板 slot 未填 | NotImplementedError → 任何 smoke 崩 → fail loud（**不是**静默 dummy） |
| 测试需要注入合成 user code | 测试侧特化 helper（程序化填 slot），替代已删的 `--user_train_import` 注入 |
| 用户函数体引用模块级常量/helper/类 | 依赖闭包一并拷贝（§3.1 搬入边界）；拷贝后仍依赖用户项目符号 → fail loud |

## 7. 验收标准

1. `tars validate kd-nas` 0 error。
2. 生成产物零 `{{` 占位符、零 `_placeholder_*`、零 `--user_*` flag（静态 AST 扫描 PASS）。
3. 无覆盖 flag 的 teacher/distill/eval 三模式 smoke 全跑通（证明搬入生效；distill 用测试 cache 或显式 Skipped）。
4. `fidelity_check.py` 对 demo fixture（`examples/kd-nas-demo/`）PASS：loss / eval 指标与用户原函数数值一致。
5. 用户 train.py 缺 build_dataloader / eval 脚本找不到 → gen_train_script fail loud（非静默降级）。
6. workflow-verifier 四层清单全过（C21-C24 无残留/loss 逐字/eval 逐字/fidelity；C5/C17/C20 已清理）；train-script-verify 复核 PASS → train_teacher 正常进入。
7. 单测更新后全绿（`tests/workflows/test_kd_train_script.py` 8 个受影响用例迁移 + 新增 fidelity 测试）。

## 8. Review 闭环记录（2026-08-03，独立 review agent 比对）

review agent 总体结论：**PASS with conditions**（无 CRITICAL；3 个 MAJOR 升 SPEC 前已并入本稿）。

| 编号 | 级别 | 问题 | 处理 |
|---|---|---|---|
| MAJOR-1 | MAJOR | 搬入边界未定义（依赖闭包） | §3.1 增「搬入 = 函数体 + 模块级依赖闭包一并拷贝」 |
| MAJOR-2 | MAJOR | distill smoke 的 teacher_cache 在 DAG 时序不可得 | §4 Layer 2 二选一：测试 cache（TeacherCache.build）或显式 Skipped |
| MAJOR-3 | MAJOR | fidelity_check CLI 缺模型来源 / `--user_eval` 提取契约未定义 | §4 Layer 3 增 `--model_path/--build_fn/--build_cfg`；eval 提取规则镜像 agent Step 1 + 种子对齐 |
| MAJOR-4 | MAJOR | 旧 checklist C5/C17/C20 未清理会与 verifier 冲突 | §4 Layer 4 改写/删除占位符语义项 |
| MAJOR-5 | MAJOR | 测试波及面被低估（8 用例）+ train-script-verify 旧 grep vacuous pass | §5 item 10 列用例映射 + 测试侧特化 helper；§4 workflow 层「替换」旧检查 |
| MINOR-1 | MINOR | 「拷贝模板」措辞残留原缺陷语义 | §3.1 改「实例化骨架并填充 slot」+ 骨架 docstring 声明不可运行 |
| MINOR-2 | MINOR | `examples/kd-nas-demo/README.md:198` 自验命令用已删 flag | §5 item 12 例外 |
| MINOR-3 | MINOR | `--project_root` 语义随注入机制消亡未定义 | §3.1 收窄为「数据文件/路径解析用」 |
| MINOR-4 | MINOR | CONTRACTS.md:23「teacher+distill 两模式」陈旧 | §5 item 9 措辞修正 |
| MINOR-5 | MINOR | SKILL.md verifier prompt 模板不同步 | §5 item 2 加证据项 |
| MINOR-6 | MINOR | workflow doc §1 CLI 列表未点名删 4 flag | §5 item 4 逐 flag 点名 |
| MINOR-7 | MINOR | 模板 docstring 含 `{{...}}`（Layer 1 扫描干扰） | §5 item 3 docstring 同步改写 |
