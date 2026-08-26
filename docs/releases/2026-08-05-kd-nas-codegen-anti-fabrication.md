# 2026-08-05 — KD-NAS codegen 反造假数据修复

## 背景

审计 run `kd-nas-20260805-011253-6c2ebe` 发现 KD-NAS 训练零学习的真根因（最严重）：

- teacher acc=0.1201（target 0.90），10 epoch loss 锁死 ln(10)=2.30；r2 student acc=0.0908（随机基线）。
- 根因：`kd-train-script` codegen 因 `torchvision` **不在叶子 import 白名单** → 无法 port 用户真实 MNIST dataloader → 用 `torch.rand`（随机像素）+ `torch.randint`（随机标签）**冒充 "ported verbatim"**。
- 标签与像素解耦 → 模型只能学到常数分布 → 永远 ~10% acc。
- eval.py 用同样 rand 造假 → 测得精度是"随机标签上的随机准确率"。
- **违反铁律**：严禁造假、缺数据问用户。codegen 应 fail loud / emit ask-user 哨兵，却造假。

## 改动（架构对齐，非补丁）

**原则重申**：「自包含」= 不依赖用户项目树（禁 `from user_pkg import`），**不等于**禁标准包。torch/torchvision/numpy/PIL 等是 pip 标准包，允许；只禁用户项目模块。

### 1. 扩 import 白名单（`_leaves.py` + `fidelity_check.py`）

加入 `torchvision` / `torchaudio` / `scipy` / `sklearn` / `PIL` + stdlib（`os` / `sys` / `json` / `pathlib` / `io` / `abc` / `copy` / `re` / `warnings` / `time`）。两处白名单刻意镜像（fidelity_check 是 codegen CLI，不能 import 引擎包），由新增 `test_leaf_import_whitelist_contains_standard_scipy_stack` 测试锁 parity。

### 2. 反造假 AST 检测（`fidelity_check.py::_check_no_random_fabrication`）

- 扫描 `data.py` / `eval.py`：`torch.rand/randn/randint/normal/rand_like/randn_like`、`numpy.random.*` / `np.random.*`（排除 `seed`/`default_rng`）、stdlib `random.<func>`（排除 `seed`/`Random`）、以及 in-place 方法 `uniform_/normal_/exponential_/cauchy_/log_normal_/geometric_`。
- `torch.randperm` **不在**造假集（仅产索引、非数据/标签，doc 一致声明合法用作 DataLoader sampler）。
- 用户 train.py / eval 自身用 random（kd-nas-demo 合成数据 / 去噪自编码器等）→ 视为 verbatim port，跳过检查（`_user_uses_random_data`）。
- 输出 `LEAF_FABRICATION_OK: true|false` 并纳入 `FIDELITY: PASS/FAIL` 聚合。

### 3. 反造假硬规则（SKILL.md / agent.md / workflow doc / 2 个 checklist / 4 个 leaf skel）

- data.py 必须 port 用户**真实** dataloader（含其 torchvision / PIL / numpy import + 真实数据路径）。
- 用户 dataloader 依赖用户项目模块 / 不可得数据 → **fail loud** + emit ask-user 哨兵，**绝不**用随机数冒充。
- eval.py 同理（必须 port 真实 eval 数据加载）。

### 4. CONTRACTS 更新

- 删 flag-diff 表迁移叙事行（`单体 inline user_* slot | 已移除 | ...`）+ 标题去「相对单体」措辞。
- §6 叶子契约：扩白名单说明（标准科学计算包允许，禁用户项目模块）+ 新增反造假段。

### 5. 守门测试扩范围（`test_kd_prompt_no_source_narrative.py`）

- `HISTORICAL_NARRATIVE` regex 加 `已移除` / `相对单体` —— 锁迁移叙事零回归。

### 6. fail-loud 强化（`fidelity_check.py`）

- `--user_train` / `--user_eval` 文件缺失 → rc=2 + `USER_TRAIN_MISSING` / `USER_EVAL_MISSING` stderr（原裸 FileNotFoundError traceback rc=1 违反 fail-loud 契约）。

## 验证

- 守门测试 `test_kd_prompt_no_source_narrative.py` 1/1 绿。
- `tests/workflows/test_kd_train_script.py` 25/25 绿（6 新测：catches_data_fabrication / allows_user_synthetic_data / whitelist_parity / factory_variants / seed_shuffle_allow / missing_user_train）。
- 全 kd 测试套件 **175 passed / 2 skipped**（原 169 + 6 新），零回归。
- `tars validate workflows/kd-nas.yaml` 通过。
- audit-run `kd-nas-20260805-011253-6c2ebe` artifact 经新 fidelity_check 复测：4 处造假被准确捕获，`FIDELITY: FAIL`。
- 反造假扫描手动覆盖 13 种造假工厂 + 5 种合法 seed/shuffle primitive 路径。

## 不动范围（铁律守住）

- 不动 Phase 1-5 引擎 / 接口 / 拍平（trainer / `_leaves` loader 机制不变，只扩白名单 + 加反造假）。
- 不动 P0-P6 修复。
- 新写指令零来源叙事 / 决策标签（守门测试绿）。

## 待验证（headless e2e）

deepseek 账号已空，待重跑 `tars run workflows/kd-nas.yaml` 对 `examples/mnist_kd/`（max_rounds=2, full_epochs=2, device=cpu）。**核心验证 = teacher 真训练 acc > 0.90**（不再是 0.12 随机）；gen_train_script 产的 data.py 应 port 真实 torchvision MNIST（非 torch.rand）。

## Commit

待 commit。
