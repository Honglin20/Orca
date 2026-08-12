"""utils/train_rx.py —— rx-sweep fixture 的确定性假训练脚本。

**这不是真训练**——是端到端测试用的 fixture：
  - 合成确定性数据（``torch.manual_seed(0)``，每次同输入同输出）；
  - 假训练循环（前向 + 反向，epoch 数小，结果确定性）；
  - 打印 ``[RX-GATE]`` / ``[train]`` / ``[RESULT]`` 行供 gate_check.py / launch_sweep.py 解析。

CLI（已适配状态，contracts §3 / §5）：
  ``--variant {model8, pure_cnn, pure_cnn_pilot, pure_cnn_lmmse, pure_cnn_pilot_lmmse}``
  ``--kd`` / ``--teacher-ckpt <path>`` / ``--epochs N`` / ``--gpu N`` / ``--exp-id <str>``
  ``--data <path>``（可选：data/gen.py 生成的 .pt；未给则用内联合成）

fail loud：未知 variant → raise。
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
import time
from pathlib import Path

import torch
import torch.nn as nn

try:
    from torch.utils.tensorboard import SummaryWriter
except ImportError:  # tensorboard 未装 → 跳过 TB 日志（fail-soft，不阻断训练）
    SummaryWriter = None

# fixture 自包含：把 fixture 根目录加进 sys.path，使 model8_baseline / pure_cnn_model / kd_helper 可 import
_FIXTURE_ROOT = Path(__file__).resolve().parent.parent
if str(_FIXTURE_ROOT) not in sys.path:
    sys.path.insert(0, str(_FIXTURE_ROOT))

from model8_baseline import build_model as build_model8  # noqa: E402
import pure_cnn_model  # noqa: E402
from kd_helper import KDHelper, create_fake_teacher_ckpt  # noqa: E402

# ---------------------------------------------------------------------------
# 常量（contracts §1 / §3）
# ---------------------------------------------------------------------------
DUMMY_SHAPE = [1, 4, 48, 64, 1]
TRAIN_BATCH = 8

_KNOWN_VARIANTS = (
    "model8",
    "pure_cnn",
    "pure_cnn_pilot",
    "pure_cnn_lmmse",
    "pure_cnn_pilot_lmmse",
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="rx-sweep fixture 确定性假训练")
    p.add_argument("--variant", required=True,
                   help="模型变体名（决定 pilot/lmmse 开关；contracts §1）。"
                        "未知 variant 在 build_for_variant raise（fail loud）")
    p.add_argument("--kd", action="store_true", help="走知识蒸馏路径（需 teacher）")
    p.add_argument("--teacher-ckpt", default=None,
                   help="teacher state_dict 路径；--kd 未给则自动构造确定性假 teacher")
    p.add_argument("--epochs", type=int, default=2, help="假训练轮数（默认 2）")
    p.add_argument("--gpu", type=int, default=0, help="GPU 编号（fixture 仅占位，固定 CPU 跑）")
    p.add_argument("--exp-id", default=None, help="实验 ID（影响 [RESULT] 的 exp_id 字段）")
    p.add_argument("--data", default=None,
                   help="可选：data/gen.py 生成的 .pt 路径；未给用内联 torch.randn 合成")
    p.add_argument("--tb-dir", default=None,
                   help="TensorBoard 日志根目录（每实验写 <tb-dir>/<exp_id>/，"
                        "用 tensorboard --logdir <tb-dir> 看训练曲线）")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# 模型构造（variant → 模型）
# ---------------------------------------------------------------------------
def build_for_variant(variant):
    """据 variant 选模型，返回 (model, gate_cfg)。未知 variant → raise（fail loud）。"""
    if variant not in _KNOWN_VARIANTS:
        raise ValueError(
            f"build_for_variant: unknown variant={variant!r}; "
            f"allowed={list(_KNOWN_VARIANTS)}"
        )
    gate_cfg = dict(num_blocks=4, embed_dim=16, dilations=(1, 2, 4, 8), noise_var=0.01)
    if variant == "model8":
        return build_model8(num_blocks=4, embed_dim=16), gate_cfg
    # pure_cnn 族
    m = pure_cnn_model.build_model(
        variant=variant,
        num_blocks=4,
        embed_dim=16,
        dilations=(1, 2, 4, 8),
        noise_var=0.01,
    )
    return m, gate_cfg


# ---------------------------------------------------------------------------
# GATE 打印（contracts §3）
# ---------------------------------------------------------------------------
def _fmt_shape(shape):
    return "[" + ",".join(str(int(x)) for x in shape) + "]"


def _fmt_dilations(dilations):
    return "(" + ",".join(str(d) for d in dilations) + ")"


def print_gate(variant, model, kd, gate_cfg):
    """载入模型后立即打印 [RX-GATE] 行。先做 smoke forward 验 I/O，过才 gate=PASS。"""
    pilot_on = "pilot" in variant
    lmmse_on = "lmmse" in variant

    dummy = torch.randn(*DUMMY_SHAPE)
    gate = "FAIL"
    io_out = [-1]
    try:
        with torch.no_grad():
            out = model(dummy)
        if list(out.shape) == DUMMY_SHAPE:
            gate = "PASS"
            io_out = list(out.shape)
        else:
            io_out = list(out.shape)
    except Exception as e:  # noqa: BLE001 — gate 必须 fail-loud 但不崩进程
        print(f"[RX-GATE] smoke forward raised: {e!r}", file=sys.stderr)
        gate = "FAIL"

    line = (
        f"[RX-GATE] variant={variant} "
        f"pilot={'on' if pilot_on else 'off'} "
        f"lmmse={'on' if lmmse_on else 'off'} "
        f"kd={'on' if kd else 'off'} "
        f"num_blocks={gate_cfg['num_blocks']} "
        f"embed_dim={gate_cfg['embed_dim']} "
        f"dilations={_fmt_dilations(gate_cfg['dilations'])} "
        f"noise_var={gate_cfg['noise_var']} "
        f"io_in={_fmt_shape(DUMMY_SHAPE)} "
        f"io_out={_fmt_shape(io_out)} "
        f"gate={gate}"
    )
    print(line, flush=True)
    return gate == "PASS"


# ---------------------------------------------------------------------------
# 合成数据
# ---------------------------------------------------------------------------
def make_synthetic_data(device, n=TRAIN_BATCH, data_path=None):
    """确定性合成数据（fixture 允许的合成测试数据，非真实信道）。"""
    torch.manual_seed(0)
    if data_path is not None and Path(data_path).exists():
        blob = torch.load(data_path, map_location=device)
        x, y = blob["x"], blob["y"]
        # 只取前 n 个样本，保持确定性
        return x[:n].to(device), y[:n].to(device)
    x = torch.randn(n, 4, 48, 64, 1, device=device)
    y = torch.randn(n, 4, 48, 64, 1, device=device)
    return x, y


# ---------------------------------------------------------------------------
# 确定性「精度」：从 final loss 推
# ---------------------------------------------------------------------------
def deterministic_accuracy(final_loss: float) -> float:
    """从 final loss 推一个确定性的伪 nmse（越小越好；非真实精度）。

    用 ``loss / (1 + loss)`` 把 loss 映射到 [0, 1)，单调，确定性可复现。
    """
    return final_loss / (1.0 + final_loss)


# ---------------------------------------------------------------------------
# KD teacher ckpt
# ---------------------------------------------------------------------------
def ensure_teacher_ckpt(teacher_ckpt_arg):
    """--kd 模式下保证 teacher_ckpt 存在：给了路径且文件在 → 用；否则造一个假 ckpt。"""
    if teacher_ckpt_arg and Path(teacher_ckpt_arg).exists():
        return teacher_ckpt_arg
    fake_path = Path(tempfile.gettempdir()) / "rx_fixture_fake_teacher.pt"
    build_fn = lambda: build_model8(num_blocks=4, embed_dim=16)
    create_fake_teacher_ckpt(build_fn, str(fake_path), device="cpu")
    return str(fake_path)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main(argv=None):
    args = parse_args(argv)

    # 确定性种子（整个脚本最先）
    torch.manual_seed(0)

    # fixture 固定 CPU（不依赖 GPU；--gpu 仅占位以对齐 CLI 契约）
    device = torch.device("cpu")

    # 1) 构造模型
    model, gate_cfg = build_for_variant(args.variant)
    model.to(device)

    # 2) 立即打印 GATE（contracts §3：载入后、训练循环前）
    gate_ok = print_gate(args.variant, model, args.kd, gate_cfg)
    if not gate_ok:
        exp_id = args.exp_id or args.variant
        print(
            f"[RESULT] exp_id={exp_id} accuracy=0.0 accuracy_kind=nmse "
            f"latency_ms=0.0 status=FAIL_gate",
            flush=True,
        )
        return 1

    # 2.5) TensorBoard writer（可选：--tb-dir 给了且 tensorboard 装了才开）
    exp_id = args.exp_id or args.variant
    writer = None
    if args.tb_dir and SummaryWriter is not None:
        writer = SummaryWriter(str(Path(args.tb_dir) / exp_id))

    # 3) 合成数据
    x, y = make_synthetic_data(device, n=TRAIN_BATCH, data_path=args.data)

    # 4) KD 或 scratch
    kd = None
    if args.kd:
        teacher_ckpt = ensure_teacher_ckpt(args.teacher_ckpt)
        teacher_build = lambda: build_model8(num_blocks=4, embed_dim=16)
        student_hooks = model.feature_hook_names()
        kd = KDHelper(teacher_build, teacher_ckpt, student_hooks, device)

    params = list(model.parameters()) + (kd.kd_parameters() if kd else [])
    optimizer = torch.optim.Adam(params, lr=1e-3)
    task_loss_fn = nn.MSELoss()

    # 5) 假训练循环
    final_loss_val = 0.0
    for ep in range(args.epochs):
        optimizer.zero_grad()
        if kd is not None:
            loss = kd(model, x, task_loss_fn, y)
        else:
            out = model(x)
            loss = task_loss_fn(out, y)
        loss.backward()
        optimizer.step()
        final_loss_val = float(loss.item())
        print(f"[train] epoch={ep} loss_avg={final_loss_val:.4f}", flush=True)
        if writer is not None:
            writer.add_scalar("train/loss", final_loss_val, ep)

    # epochs=0 时也跑一次前向拿 loss
    if args.epochs <= 0:
        with torch.no_grad():
            if kd is not None:
                loss = kd(model, x, task_loss_fn, y)
            else:
                loss = task_loss_fn(model(x), y)
        final_loss_val = float(loss.item())

    # 6) 确定性 accuracy（从 loss 推）
    accuracy = deterministic_accuracy(final_loss_val)

    # 7) latency：单次前向 wall-clock（非确定性，但 fixture 不要求 latency 复现）
    model.eval()
    dummy_one = torch.randn(1, 4, 48, 64, 1, device=device)
    t0 = time.time()
    with torch.no_grad():
        _ = model(dummy_one)
    latency_ms = (time.time() - t0) * 1000.0

    # 7.5) TensorBoard：末尾写 accuracy / latency 标量，关 writer
    if writer is not None:
        writer.add_scalar("result/accuracy", accuracy, args.epochs)
        writer.add_scalar("result/latency_ms", latency_ms, args.epochs)
        writer.close()

    # 8) [RESULT] 行（contracts §4）
    exp_id = args.exp_id or args.variant
    print(
        f"[RESULT] exp_id={exp_id} accuracy={accuracy:.3f} accuracy_kind=nmse "
        f"latency_ms={latency_ms:.1f} status=SUCCESS",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
