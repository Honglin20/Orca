# ofd_hook.py.md — D13：多 stage feature hook + 1×1 HintRegressor adapter 注册器

> **这是什么 / 一句话**：D13 特征级 KD（OFD / FitNets）的可嫁接实现——用 `register_forward_hook` 抓 teacher / student 多 stage 中间 feature，挂一个 1×1 conv adapter（HintRegressor）自动对齐通道维度。Adapter 只在训练期存在，部署丢弃。完全不改 student / teacher 模型类代码。

---

## 可跑骨架（对齐 CONTRACTS §3 `kd/losses.py` 的 `ofd_feature_loss`）

```python
import torch
import torch.nn as nn
import torch.nn.functional as F
from collections import OrderedDict


# ============================================================
# 1) 多 stage feature 抓取：按模块名注册 forward hook
# ============================================================
class FeatureExtractor(nn.Module):
    """按 module path 注册 forward hook，前向时收集各 stage 输出。
    用法:
        ext_t = FeatureExtractor(teacher, hook_names=["main.0", "main.2", "main.4"])
        ext_s = FeatureExtractor(student, hook_names=["backbone.block0", "backbone.block2"])
        with torch.no_grad():
            t_out = ext_t(x)            # teacher forward
        s_out = ext_s(x)                # student forward
        t_feats = ext_t.features        # list[Tensor], len=len(hook_names)
        s_feats = ext_s.features        # list[Tensor]
    """
    def __init__(self, model: nn.Module, hook_names: list):
        super().__init__()
        self.model = model
        self.hook_names = hook_names
        self._handles = []
        self.features = []
        self._register()

    def _register(self):
        self.features = [None] * len(self.hook_names)

        def make_hook(idx):
            def hook(module, inp, out):
                # out 可能是 tuple（如 transformer block 返回 (x, attn_weights)），取第一个
                if isinstance(out, tuple):
                    out = out[0]
                self.features[idx] = out
            return hook

        for idx, name in enumerate(self.hook_names):
            # 按 "main.0.block.attn" 这样的点分路径找子模块
            mod = self.model
            for part in name.split("."):
                mod = getattr(mod, part)
            h = mod.register_forward_hook(make_hook(idx))
            self._handles.append(h)

    def forward(self, x):
        out = self.model(x)
        # features 已在 hook 里填好
        return out

    def clear(self):
        self.features = [None] * len(self.hook_names)


# ============================================================
# 2) 1×1 HintRegressor adapter：自动对齐 student channel → teacher channel
# ============================================================
class HintRegressor(nn.Module):
    """OFD 风格 adapter：student_feat → 1×1 conv 升/降维 → ReLU → 1×1 conv 重建 → match teacher_feat。
    adapter 只在训练期用，部署丢弃（不写入 student.onnx）。
    """
    def __init__(self, s_channels: int, t_channels: int, hidden_ratio: float = 1.0):
        super().__init__()
        hidden = max(s_channels, t_channels)
        hidden = int(hidden * hidden_ratio)
        self.proj = nn.Conv1d(s_channels, hidden, kernel_size=1, bias=False)
        self.relu = nn.ReLU(inplace=True)
        self.rebuild = nn.Conv1d(hidden, t_channels, kernel_size=1, bias=False)
        # init: proj 用单位矩阵近似（通道数不同时退化为 partial identity）
        nn.init.eye_(self.proj.weight.squeeze(-1)) if s_channels == hidden else None
        nn.init.eye_(self.rebuild.weight.squeeze(-1)) if hidden == t_channels else None

    def forward(self, s_feat: torch.Tensor) -> torch.Tensor:
        """s_feat: [B, C_s, L] → [B, C_t, L]"""
        x = self.proj(s_feat)
        x = self.relu(x)
        x = self.rebuild(x)
        return x


class OFDAdapterBank(nn.Module):
    """一组 HintRegressor，每个 stage 一个；按 teacher/student 的 channel list 自动构造。
    """
    def __init__(self, s_channels_list: list, t_channels_list: list):
        super().__init__()
        assert len(s_channels_list) == len(t_channels_list), \
            f"stage count mismatch: s={len(s_channels_list)} t={len(t_channels_list)}"
        self.adapters = nn.ModuleList([
            HintRegressor(s_c, t_c) for s_c, t_c in zip(s_channels_list, t_channels_list)
        ])

    def forward(self, s_feats: list, t_feats: list) -> torch.Tensor:
        """返回 OFD 总 loss（margin-based reconstruction）。
        s_feats / t_feats: list[Tensor]，每个 [B, C, L]
        """
        assert len(s_feats) == len(t_feats) == len(self.adapters), "stage 数对不上"
        total = 0.0
        margin = 0.5        # OFD 默认 margin
        for adapter, s_f, t_f in zip(self.adapters, s_feats, t_feats):
            s_aligned = adapter(s_f)
            t_f = t_f.detach()        # teacher detach
            # margin-based L2: max(0, ‖s_aligned - t‖ - margin)
            diff = (s_aligned - t_f).pow(2).sum(dim=1).sqrt()    # [B, L]
            loss = F.relu(diff - margin).pow(2).mean()
            total = total + loss
        return total / len(self.adapters)


# ============================================================
# 3) 完整使用：在 train_kd.py adapter 里
# ============================================================
def ofd_loss_step(student, teacher, x, hook_names_s, hook_names_t,
                  s_channels_list, t_channels_list, adapter_bank):
    """一次性算 OFD loss（每个 batch 调用一次）。
    返回: s_out, ofd_loss
    """
    ext_s = FeatureExtractor(student, hook_names_s)
    ext_t = FeatureExtractor(teacher, hook_names_t)

    # teacher forward (no grad, 缓存复用见 CONTRACTS §3 TeacherCache)
    with torch.no_grad():
        _ = ext_t(x)
    t_feats = list(ext_t.features)

    # student forward
    s_out = ext_s(x)
    s_feats = list(ext_s.features)

    # OFD loss
    ofd_loss = adapter_bank(s_feats, t_feats)
    return s_out, ofd_loss


# ============================================================
# 4) smoke test
# ============================================================
if __name__ == "__main__":
    # 假 teacher: 3 stage Transformer；假 student: 3 stage ConvNeXt-pointwise
    class FakeTeacher(nn.Module):
        def __init__(self):
            super().__init__()
            self.main = nn.Sequential(*[
                nn.Sequential(nn.Linear(48, 128), nn.ReLU()) for _ in range(3)
            ])
        def forward(self, x):  # x: [B, 48]
            return self.main(x)

    class FakeStudent(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = nn.Sequential(*[
                nn.Sequential(nn.Linear(48, 64), nn.ReLU()) for _ in range(3)
            ])
        def forward(self, x):
            return self.backbone(x)

    # wrap to expose hook path; reshape for Conv1d adapter (需要 [B, C, L])
    teacher = FakeTeacher()
    student = FakeStudent()

    # 通道维 = Linear out_features，L 维 = 输入特征维（这里 48，用作序列）
    # 实际 model8 需 reshape 到 [B, C, L=F 或 S]
    hook_s = ["backbone.0", "backbone.1", "backbone.2"]
    hook_t = ["main.0", "main.1", "main.2"]
    s_chs = [64, 64, 64]
    t_chs = [128, 128, 128]

    bank = OFDAdapterBank(s_chs, t_chs)

    x = torch.randn(4, 48)        # batch=4, feature=48
    # 因为 FakeTeacher 是 Linear 不是 Conv1d，adapter 这里只示意
    # 实际使用时把 Linear 输出 reshape 成 [B, C, L] 喂给 Conv1d adapter
    s_out, ofd_loss = ofd_loss_step(student, teacher, x, hook_s, hook_t, s_chs, t_chs, bank)
    print(f"OFD loss = {ofd_loss.item():.4f}, s_out shape = {s_out.shape}")
```

---

## 变异提示（不要照抄）

- **hook 路径必须真实存在**：`FeatureExtractor._register` 用 `getattr` 逐级查找，路径错会抛 `AttributeError`（fail loud，符合 CONTRACTS）。**engineer 在写 student 时必须暴露 `feature_hook_names()` 方法**（CONTRACTS §1），否则 hypothesizer 在 SelectionSpec 里瞎猜路径会全 fail。
- **stage 数选择**：OFD 推荐 ≥2 stage；stage 过多（>5）adapter bank 开销大、收益递减；默认 3 stage。
- **margin 是个轴**：`m ∈ {0, 0.3, 0.5, 0.7}`；m=0 等价于纯 MSE；m 过大 loss 恒为 0，要先 log teacher feat norm 范围。
- **hidden_ratio**：HintRegressor 的 hidden 通道比例，1.0 默认（max(s,t)）；>1.0（如 1.5）增表达力，但 adapter 参数变多（训练慢）。
- **FitNets 单点版**：把 `OFDAdapterBank` 换成单个 `HintRegressor`，只对一个 stage（"hint"）做对齐——参数最少、最轻量；短训 proxy（kd-nas Phase1）默认用 FitNets。
- **MGD 变体**：在 HintRegressor 前加随机二值掩码（`mask = torch.rand_like(s_feat) > 0.5`，`s_masked = s_feat * mask`），让 adapter 学重建——生成式正则；mask 率 0.5 默认。
- **Channel 自动探测**：`s_channels_list` 不要手填——写个 `infer_channels(model, hook_names)`：跑一次 dummy forward，读 `features[i].shape[1]`。CONTRACTS §3 `kd/wrapper.py` 应提供这个工具。
- **Adapter 优化器**：`HintRegressor` 的参数要加到 student optimizer 里（或单独 optimizer，lr × 0.1）；漏掉会让 adapter 不学，OFD loss 不降。
- **部署丢弃**：训练完成后 `del adapter_bank, ext_t`；导出 ONNX 时**只导 student**（`torch.onnx.export(student, ...)`），adapter / teacher_cache 都不进 ONNX。
- **fail-loud**：若 OFD loss 初期接近 0，adapter 没初始化（默认随机 init 的重建输出碰巧接近 teacher）——给 `proj` 用 `nn.init.kaiming_normal_` 或加 warmup。
