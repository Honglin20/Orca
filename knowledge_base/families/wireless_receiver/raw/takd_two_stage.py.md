# takd_two_stage.py.md — D14：两段式 TA（Teacher Assistant）launcher 骨架

> **这是什么 / 一句话**：D14 TAKD 的两段式训练 launcher——先训 TA（teacher → TA 蒸馏），再训 student（TA → student 蒸馏）。每段独立训练，TA 训完冻结作 stage 2 的 teacher。骨架只编排流程，KD loss / optimizer / dataloader 复用用户 train.py 与 kd-nas 的 train_kd.py adapter。

---

## 可跑骨架

```python
import os
import subprocess
import json
from pathlib import Path


def build_ta_build_cfg(teacher_cfg: dict, student_cfg: dict) -> dict:
    """TA 容量选择经验：C_TA ≈ √(C_T · C_S)（参数量几何平均）。
    输入: teacher_cfg (如 {num_blocks:6, embed_dim:128}),
          student_cfg (如 {num_blocks:2, embed_dim:32})
    输出: ta_cfg 中间值（同 family 的结构参数取几何平均）
    """
    ta_cfg = {}
    for k in teacher_cfg:
        if k in student_cfg and isinstance(teacher_cfg[k], (int, float)):
            # 几何平均（取整）
            import math
            ta_cfg[k] = int(round(math.sqrt(teacher_cfg[k] * student_cfg[k])))
        else:
            ta_cfg[k] = teacher_cfg[k]     # 非数值参数继承 teacher
    return ta_cfg


def stage1_train_ta(teacher_ckpt: str, teacher_build_cfg: dict,
                    ta_family: str, ta_build_cfg: dict,
                    kd_config: dict,
                    train_kd_path: str, output_dir: str,
                    epochs: int = 30, dummy_input: dict = None) -> str:
    """Stage 1: teacher → TA 蒸馏。
    复用 kd-train-script agent 生成的 train_kd.py adapter。
    返回: TA ckpt 路径。
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    ta_ckpt = os.path.join(output_dir, "ta_ckpt.pt")

    # 临时 TA model.py（若 TA 同 family，复用 student family 的 build_model）
    # engineer 在 kd-nas workflow 的 engineer 节点里写 ta_model.py
    ta_model_path = os.path.join(output_dir, "ta_model.py")
    if not os.path.exists(ta_model_path):
        raise FileNotFoundError(
            f"TA model.py not found at {ta_model_path}; "
            f"engineer must write TA model (same family as student, wider channels)."
        )

    # 构造 teacher_cache（复用 teacher_setup.py 的输出）
    # 假设 teacher_cache.pt 已由 kd-nas 的 teacher_setup 节点生成
    teacher_cache = os.path.join(output_dir, "teacher_cache.pt")
    if not os.path.exists(teacher_cache):
        raise FileNotFoundError(
            f"Teacher cache not found at {teacher_cache}; "
            f"run teacher_setup.py first (CONTRACTS §4)."
        )

    cmd = [
        "python3", train_kd_path,
        "--student_family", ta_family,
        "--student_cfg", json.dumps(ta_build_cfg),
        "--kd_config", json.dumps(kd_config),
        "--teacher_cache", teacher_cache,
        "--student_model_path", ta_model_path,
        "--build_fn", "build_model",
        "--epochs", str(epochs),
        "--out_ckpt", ta_ckpt,
    ]
    print(f"[TAKD stage1] launching: {' '.join(cmd)}")
    ret = subprocess.run(cmd, check=False)
    if ret.returncode != 0 or not os.path.exists(ta_ckpt):
        raise RuntimeError(f"TA training failed (exit={ret.returncode})")
    print(f"[TAKD stage1] TA ckpt saved: {ta_ckpt}")
    return ta_ckpt


def stage2_train_student(ta_ckpt: str, ta_build_cfg: dict, ta_model_path: str,
                         student_family: str, student_build_cfg: dict,
                         kd_config: dict,
                         train_kd_path: str, output_dir: str,
                         epochs: int = 30) -> str:
    """Stage 2: TA → student 蒸馏。
    TA 现在作 teacher；需要先把 TA 的输出/feature cache 出来（复用 teacher_setup.py
    但 target model 换成 TA）。
    返回: student final ckpt 路径。
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    # TA cache
    ta_cache = os.path.join(output_dir, "ta_cache.pt")
    if not os.path.exists(ta_cache):
        # 复用 teacher_setup.py，但 target = TA
        cmd = [
            "python3", os.path.join(os.path.dirname(train_kd_path), "teacher_setup.py"),
            "--teacher_model_path", ta_model_path,
            "--teacher_ckpt", ta_ckpt,
            "--build_fn", "build_model",
            "--dummy_input", json.dumps({"shape": [1, 4, 48, 64, 1], "dtype": "float32"}),
            "--output_dir", output_dir,
        ]
        print(f"[TAKD stage2] building TA cache: {' '.join(cmd)}")
        ret = subprocess.run(cmd, check=False)
        if ret.returncode != 0 or not os.path.exists(ta_cache):
            raise RuntimeError(f"TA cache build failed (exit={ret.returncode})")

    student_ckpt = os.path.join(output_dir, "student_final.pt")
    cmd = [
        "python3", train_kd_path,
        "--student_family", student_family,
        "--student_cfg", json.dumps(student_build_cfg),
        "--kd_config", json.dumps(kd_config),
        "--teacher_cache", ta_cache,        # ← TA cache 作 teacher
        "--student_model_path", "(student_model_path_here)",
        "--build_fn", "build_model",
        "--epochs", str(epochs),
        "--out_ckpt", student_ckpt,
    ]
    print(f"[TAKD stage2] launching: {' '.join(cmd)}")
    ret = subprocess.run(cmd, check=False)
    if ret.returncode != 0 or not os.path.exists(student_ckpt):
        raise RuntimeError(f"Student stage2 training failed (exit={ret.returncode})")
    print(f"[TAKD stage2] student ckpt saved: {student_ckpt}")
    return student_ckpt


def run_takd(teacher_ckpt: str, teacher_build_cfg: dict,
             student_family: str, student_build_cfg: dict,
             train_kd_path: str, output_dir: str,
             ta_family: str = None,
             stage1_epochs: int = 30, stage2_epochs: int = 30,
             kd_config: dict = None) -> str:
    """TAKD 两段式入口。
    1. 构造 TA cfg（几何平均容量）。
    2. stage1: teacher → TA。
    3. stage2: TA → student。
    返回: student final ckpt 路径。
    """
    if kd_config is None:
        kd_config = {
            "kd_losses": ["mse"],
            "weights": {"mse": 0.5},
        }

    # TA family 默认同 student（跨架构 TA 复杂度叠加，不推荐）
    if ta_family is None:
        ta_family = student_family

    ta_build_cfg = build_ta_build_cfg(teacher_build_cfg, student_build_cfg)
    print(f"[TAKD] TA cfg = {ta_build_cfg}")

    # Stage 1
    ta_ckpt = stage1_train_ta(
        teacher_ckpt=teacher_ckpt, teacher_build_cfg=teacher_build_cfg,
        ta_family=ta_family, ta_build_cfg=ta_build_cfg,
        kd_config=kd_config,
        train_kd_path=train_kd_path, output_dir=output_dir,
        epochs=stage1_epochs,
    )

    # TA model.py path（同 student family 的 model.py，只是 build_cfg 不同）
    ta_model_path = os.path.join(output_dir, "ta_model.py")

    # Stage 2
    student_ckpt = stage2_train_student(
        ta_ckpt=ta_ckpt, ta_build_cfg=ta_build_cfg, ta_model_path=ta_model_path,
        student_family=student_family, student_build_cfg=student_build_cfg,
        kd_config=kd_config,
        train_kd_path=train_kd_path, output_dir=output_dir,
        epochs=stage2_epochs,
    )
    return student_ckpt


if __name__ == "__main__":
    # smoke test（不实际跑，只 print 计划）
    ta_cfg = build_ta_build_cfg(
        teacher_build_cfg={"num_blocks": 6, "embed_dim": 128},
        student_build_cfg={"num_blocks": 2, "embed_dim": 32},
    )
    print(f"TA cfg (geometric mean): {ta_cfg}")
    # 预期: num_blocks=√(12)≈3, embed_dim=√(4096)=64
```

---

## 变异提示（不要照抄）

- **TA family 选择**：默认同 student family；跨架构 TA（如 Transformer teacher → Transformer TA → conv student）会让 stage 2 变成跨架构 KD，复杂度叠加；不推荐。
- **stage1_epochs**：默认 30；TA 不必训到收敛，short-cycle 即可；过长会让 TA 过拟合 teacher 软目标。
- **stage2_epochs**：默认 30；与直接 KD（D11）的 epoch 数一致，便于对比。
- **TA 容量选择**：`√(C_T·C_S)` 是经验公式；可 sweep `{0.7·√, √, 1.3·√}` 三个比例。
- **fail-loud**：若 stage1 TA 精度 << teacher 5dB+，多半是 TA 太小（geometry 过激进）或 stage1 epoch 不够；放大 TA 或延长 stage1。
- **与 D15 Mean-Teacher 组合**：stage2 训 student 时同时挂 Mean-Teacher（TA + EMA 双重 teacher），收益小但稳定；可作 ablation。
- **kd-nas workflow 集成**：TAKD 成本高（2× 训练），只在 finalize 阶段使用；Phase1 sweep 不用 TAKD（用直接 KD 排序）。
- **teacher_setup.py 复用**：stage2 的 TA cache 构造直接调 `teacher_setup.py`，把 target model 换成 TA；CONTRACTS §4 的 CLI 支持任意 model 路径。
- **online TAKD 不要**：同时训 T/TA/S 三方梯度链不稳；offline 两段式（本骨架）更稳，无额外收益损失。
