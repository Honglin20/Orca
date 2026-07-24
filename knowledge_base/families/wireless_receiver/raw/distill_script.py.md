# distill_script.py.md — M14/D11：在你现有训练脚本里嫁接 KD（4 行增量）

> **这是什么 / 一句话**：**蒸馏本质是一个 loss 项，不是新 pipeline**。用户的训练脚本（数据、dataloader、task loss、优化器循环）**全部不变**，只在每个 step 加 ① teacher 一次 `no_grad` forward 拿软目标、② `total_loss = task_loss + λ·MSE(student_out, teacher_out)`。**学生模型 = 你的模型类不动一行**（只是从头 / 从 ckpt 重新训），**老师 = 同一个模型类的 pre-trained checkpoint**（加载后 `eval()` + 冻结）。零模型代码改动，零 pipeline 改动。

---

## 嫁接增量（核心）

假设你的训练循环长这样（**这是你已有的，不动**）：

```python
# === 你的现有训练循环（全部保留，不动） ===
for epoch in range(epochs):
    for batch in your_dataloader:           # 你的数据
        x, y_target = batch                  # 你的数据格式
        out = student_model(x)               # 你的模型 forward
        task_loss = your_task_loss(out, y_target)   # 你的 task loss（如 MSE / CE）
        optimizer.zero_grad()
        task_loss.backward()                 # 你的 backward
        optimizer.step()
```

蒸馏**只插入以下增量**（注释里标 `# 新增`）：

```python
# === 蒸馏增量：加载 teacher（脚本启动时，循环外，一次性） ===
teacher_model = StudentModelClassSameAsStudent()      # 同一个模型类
teacher_ckpt = torch.load(args.distill_from, map_location="cpu")
teacher_model.load_state_dict(teacher_ckpt["model"])  # 用户 pre-trained 权重
teacher_model = teacher_model.to(device).eval()
for p in teacher_model.parameters():
    p.requires_grad_(False)                           # 冻结
# 新增结束

# === 你的现有训练循环（保留），仅 step 内插 2 行 ===
for epoch in range(epochs):
    for batch in your_dataloader:
        x, y_target = batch
        out = student_model(x)
        task_loss = your_task_loss(out, y_target)     # 你的 task loss 不动

        # 新增 ①：teacher forward（no_grad，一次性拿软目标）
        with torch.no_grad():
            teacher_out = teacher_model(x)            # [B, P, F, S, 1]，与 out 同形

        # 新增 ②：蒸馏 loss 项 + 加权
        distill_loss = F.mse_loss(out, teacher_out)   # 输出级 MSE
        lam = args.distill_lambda                     # 典型 0.1–1.0
        total_loss = task_loss + lam * distill_loss   # task 不动，只加一项

        optimizer.zero_grad()
        total_loss.backward()                         # 用 total_loss 替 task_loss
        optimizer.step()
```

**就这 4 行增量**（加 teacher 加载共 8 行）：①teacher forward、②distill_loss、③total_loss 合并、④backward 用 total_loss。

---

## CLI 入口（同样嫁接到你现有的 argparse）

```python
# 你的现有 argparse（保留）……
parser.add_argument("--distill-from", type=str, default=None,
                    help="teacher checkpoint 路径；若 None 则不蒸馏（纯 task loss 训练）")
parser.add_argument("--distill-lambda", type=float, default=0.5,
                    help="蒸馏 loss 权重 λ；task_loss + λ·distill_loss")
args = parser.parse_args()

# teacher 仅在 --distill-from 给出时加载
if args.distill_from is not None:
    teacher_model = StudentModelClassSameAsStudent()
    teacher_model.load_state_dict(torch.load(args.distill_from, map_location="cpu")["model"])
    teacher_model = teacher_model.to(device).eval()
    for p in teacher_model.parameters():
        p.requires_grad_(False)
else:
    teacher_model = None

# 训练循环里：
if teacher_model is not None:
    with torch.no_grad():
        teacher_out = teacher_model(x)
    total_loss = task_loss + args.distill_lambda * F.mse_loss(out, teacher_out)
else:
    total_loss = task_loss     # 无 --distill-from 时纯 task loss（你原来的训练）
```

---

## 为什么"零模型代码改动"

- **学生 = 你的模型类，原样实例化**（`StudentModelClassSameAsStudent()` 就是 `SignalProcessingTransformer` 或任何你的类）。
- **老师 = 同一个类，加载你已训好的 checkpoint**。teacher 和 student 结构一致时叫**自蒸馏**；如果想"大 teacher → 小 student"，则 student 是缩水版（如 `embed_dim` 减半、`num_blocks` 减半），但**类的代码不变**，只改实例化参数。
- **task loss 不变**：`your_task_loss` 保持原样（BER surrogate / MSE / 等）。蒸馏**加**一个 loss 项，不替换。
- **dataloader 不变**：用同一份数据，teacher 和 student 看相同 batch。
- **优化器不变**：仍只对 student 参数 backward；teacher 在 `no_grad` 上下文里，不参与图。

---

## 可选升级：FitNets feature-KD（forward-hook 形式，注释保留）

输出级 MSE（上面那段）是**最简**的 KD。若想蒸馏**中间特征**（FitNets, ICLR'15），用 forward hook 抓 teacher / student 中间层，加一项 feature-MSE。**仍不改模型代码**——只挂 hook：

```python
# === 可选升级：FitNets feature-KD（不改模型类，用 hook） ===
# 适用：student 比 teacher 窄（embed_dim 小），中间层 shape 不同，需 1×1 adapter 对齐

# 1) 抓 teacher / student 的同一逻辑层（如 main[1] = 第二个 transformer block 输出）
feat_teacher = {}
feat_student = {}

def make_hook(store, key):
    def hook(module, inp, out):
        store[key] = out
    return hook

teacher_model.main[1].register_forward_hook(make_hook(feat_teacher, "block1"))
student_model.main[1].register_forward_hook(make_hook(feat_student, "block1"))

# 2) 若 teacher/student 中间层通道数不同，挂一个可学 adapter（1×1 conv 升/降维）
#    adapter 只在蒸馏期用，部署时 student 单独跑（adapter 丢弃）
adapter = nn.Conv1d(student_embed_dim, teacher_embed_dim, kernel_size=1).to(device)

# 3) 在训练循环里加一项 feature loss：
# feat_loss = F.mse_loss(adapter(feat_student["block1"]), feat_teacher["block1"])
# total_loss = task_loss + λ_out * distill_loss + λ_feat * feat_loss

# 注意：
# - hook 是 forward 阶段抓输出，不影响计算图，无额外 forward 开销
# - adapter 是可学参数，要加到 optimizer 里
# - 部署时丢弃 adapter + teacher_model，student 单独推理（仍是零模型代码改动）
```

---

## 变异提示（不要照抄）

- **λ 是主轴**：`λ ∈ {0.1, 0.3, 0.5, 1.0, 2.0}`；λ 太大 → student 过度平滑到 teacher、丢失 task 信号；λ 太小 → 蒸馏无效。**用 grid search + early stop**。
- **温度（softmax KD）**：经典 Hinton KD 用 `softmax(logits/T)`；本骨架用**输出级 MSE**，跳过 logits（更适合回归型接收机）。若改成 LLR/logits 级，加 T=2-4 软化。
- **teacher 选择**：① 同结构自蒸馏（最简单）；② D0 大 Transformer teacher → D1 conv-only student（SPEC §4 D11 的典型路径，99.3% 参数省）；③ ensemble teacher。
- **teacher 不必可导**：因为 `no_grad`，teacher 可以是 ONNX/`.om` 推理引擎——部署期 teacher 不存在，只是训练时用。
- **加 M6**：teacher 是 4-block baseline，student 是 2-block 缩水版 → KD 弥补 block 数减少的精度损失。
- **加 D1**：teacher = baseline Transformer，student = DeepRx conv-only → KD 把 Transformer 学到的非线性先验"烤"进 conv。
- **fail-loud**：若 `distill_loss` 远大于 `task_loss`（数量级以上） → 输出尺度不匹配，检查是否漏了 alpha 归一化 / 是否 teacher 没 `eval()`（BN/Dropout 没关）。
- **训练成本**：teacher forward 增加约 30-50% 每 step 时间（视模型大小）；但 student 推理时延不变（teacher 部署期不存在），这是 KD 的关键卖点。
- **反例**：不要把 teacher 的权重直接初始化 student（那是 EMA / warm-start，不是 KD，没有 teacher 软目标的正则效果）。
- **零代码改动强调**：本 move 的核心卖点是**不碰模型类**——所有改动都在训练脚本里，模型代码 review 时不受影响，CI 跑同一份 model.py。
