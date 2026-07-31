"""test_push_describe.py —— 锁定 baseline→elastic 对比表的 4 项富化意图。

不依赖 nas_agent / orca.chart 运行时：直接调 push_describe 的纯函数
（_build_symbols / _collect_baseline 走 AST；_build_rows 喂手搓 SearchSpace dict）。
覆盖用户 2026-07-31 反馈的 4 个信息缺口：
  ① 层名用赋值目标真名（features[i] / head），不再 conv{idx}/fc{idx}；
  ② 替换前维度解析符号表消解变量名（in_channels/num_classes），不再 ?；
  ③ 超网维度(后) 列暴露 stage 宽度 / head super_in→super_out；
  ④ 组件/深度/核候选列暴露 stage_layer_configs 的 block 选择。
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "workflows/agents/pytorch-model-optimizer/scripts/push_describe.py"


def _load_pd():
    spec = importlib.util.spec_from_file_location("_pd_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# 复刻 tiny_cnn 形态：Sequential 内 2 conv + 1 head Linear，构造参用变量（消解对象）。
_FLAT_SRC = """
import torch.nn as nn
class Demo(nn.Module):
    def __init__(self, num_classes: int = 10, in_channels: int = 3):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(in_channels, 16, 3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(16, 32, 3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
        )
        self.head = nn.Linear(32, num_classes)
    def forward(self, x):
        return self.head(self.features(x))
if __name__ == "__main__":
    Demo(num_classes=10, in_channels=3)
"""


# 多 Linear（用户的 fc2 场景）：fc1 非 head、fc2 head。
_MLP_SRC = """
import torch.nn as nn
class MLP(nn.Module):
    def __init__(self, dim: int = 64, num_classes: int = 10):
        super().__init__()
        self.fc1 = nn.Linear(dim, 32)
        self.fc2 = nn.Linear(32, num_classes)
    def forward(self, x):
        return self.fc2(self.fc1(x))
if __name__ == "__main__":
    MLP(dim=64, num_classes=10)
"""


@pytest.fixture()
def baseline():
    pd = _load_pd()
    tree = ast.parse(_FLAT_SRC)
    return pd._collect_baseline(tree, pd._build_symbols(tree))


def test_layer_names_are_real_attr_paths(baseline):
    """① 层名 = 赋值目标真名（Sequential 内按下标），不是 conv1/fc1。"""
    names = [b["attr"] for b in baseline]
    assert names == ["features[0]", "features[3]", "head"]


def test_baseline_dims_resolve_variables(baseline):
    """② in_channels / num_classes 被符号表消解成常量，不再是 ?。"""
    by_attr = {b["attr"]: b for b in baseline}
    assert by_attr["features[0]"]["info"] == {"in_ch": 3, "out_ch": 16, "kernel": 3}
    assert by_attr["features[3]"]["info"] == {"in_ch": 16, "out_ch": 32, "kernel": 3}
    assert by_attr["head"]["info"] == {"in_feat": 32, "out_feat": 10}


def test_rows_five_columns_and_supernet_dim(baseline):
    """③④ 5 列；conv stage 显 super_out_ch，head 显 super_in(末级宽度)→super_out；stem 固定。"""
    pd = _load_pd()
    d = {
        "stage_widths": (32, 64),
        "stage_depth_candidates": ((1, 2), (1, 2)),
        "stage_layer_configs": (
            {"tiny_conv": {"kernel_size": (3, 5)}},
            {"res_conv": {"kernel_size": (3, 5), "hidden_channels": (64, 128)}},
        ),
    }
    rows = pd._build_rows(baseline, d)
    assert [r["层名"] for r in rows] == ["features[0]", "features[3]", "head"]

    stem, stage0, head = rows
    # out_ch=16 不属 {32,64} → stem 固定，无超网维度 / 候选
    assert stem["替换后"] == "stem（固定）"
    assert stem["超网维度(后)"] == "—"
    # out_ch=32 → stage0（width=32），ElasticConv2d + 组件候选
    assert stage0["替换后"] == "ElasticConv2d"
    assert stage0["超网维度(后)"] == "super_out_ch=32"
    assert "depth∈{1,2}" in stage0["组件/深度/核候选"]
    assert "tiny_conv" in stage0["组件/深度/核候选"]
    # head：super_in=末级宽度 64（扩张），super_out=num_classes 10
    assert head["替换后"] == "ElasticLinear"
    assert head["超网维度(后)"] == "super_in=64→super_out=10"
    # 替换前维度已解析（无 ?）
    assert stem["替换前"] == "Conv2d(3→16, k=3)"
    assert head["替换前"] == "Linear(32→10)"


def test_columns_contract():
    """表头契约：5 列顺序固定。"""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert 'columns=["层名", "替换前", "替换后", "超网维度(后)", "组件/深度/核候选"]' in src


def test_non_head_linear_shows_dash_not_baseline_dims():
    """非 head Linear 的超网维度 SearchSpace 不标准化 → 显 —，不用 baseline 维度冒充（不臆造）。"""
    pd = _load_pd()
    tree = ast.parse(_MLP_SRC)
    baseline = pd._collect_baseline(tree, pd._build_symbols(tree))
    d = {"stage_widths": (32,), "stage_depth_candidates": ((1,),), "stage_layer_configs": ({},)}
    rows = pd._build_rows(baseline, d)
    fc1, fc2 = rows
    assert fc1["层名"] == "fc1"
    assert fc1["超网维度(后)"] == "—"  # 非 head：不臆造
    assert fc2["层名"] == "fc2"
    assert fc2["超网维度(后)"] == "super_in=32→super_out=10"  # head：super_in=末级宽度 32


def test_non_constant_out_ch_fails_loud_not_fabricates():
    """out_ch 消解不出（无默认 + __main__ 位置传参）→ 替换后显 —（非常量），不编造 stage 匹配。"""
    pd = _load_pd()
    src = """
import torch.nn as nn
class M(nn.Module):
    def __init__(self, hidden):  # 无默认
        super().__init__()
        self.conv = nn.Conv2d(3, hidden, 3)
    def forward(self, x):
        return self.conv(x)
if __name__ == "__main__":
    M(64)  # 位置传参 → __main__ kwargs 抓不到
"""
    tree = ast.parse(src)
    baseline = pd._collect_baseline(tree, pd._build_symbols(tree))
    rows = pd._build_rows(baseline, {"stage_widths": (64,), "stage_depth_candidates": ((1,),), "stage_layer_configs": ({},)})
    (row,) = rows
    assert row["替换前"] == "Conv2d(3→?, k=3)"  # hidden 消解不了 → ?
    assert "非常量" in row["替换后"]
    assert row["超网维度(后)"] == "—"
