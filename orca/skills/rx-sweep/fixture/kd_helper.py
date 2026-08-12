"""kd_helper.py —— rx-sweep fixture 的 KD（知识蒸馏）包装（contracts §2）。

纯 torch 自包含。

接口：
  ``KDHelper(teacher_build_fn, teacher_ckpt, student_hook_names, device,
             alpha_out=1.0, beta_feat=0.5)``
  ``__call__(student, x, task_loss_fn, y) -> Tensor``

loss = task_loss(s_out, y) + alpha_out·MSE(s_out, t_out) + beta_feat·Σ_i MSE(adapter_i(s_feat_i), t_feat_i)

adapter 懒建：首次调用据 s_feat / t_feat 形状自动建（同形→Identity；通道不同→Conv1d/Linear 投影）。
adapter 参数通过 ``kd_parameters()`` 暴露，由 student 优化器一并更新。
teacher 在 ``torch.no_grad()`` 下前向，参数冻结。
"""

from __future__ import annotations

from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F


def _get_module(root: nn.Module, dotted: str) -> nn.Module:
    """按 dotted name（如 ``main.0`` / ``main.2``）取子模块。"""
    mod = root
    for part in dotted.split("."):
        if part.isdigit():
            mod = mod[int(part)]
        else:
            mod = getattr(mod, part)
    return mod


class KDHelper:
    """FitNets 风格 KD：输出 + 中间特征双蒸馏。"""

    def __init__(self, teacher_build_fn, teacher_ckpt, student_hook_names,
                 device, alpha_out: float = 1.0, beta_feat: float = 0.5):
        self.device = device
        self.alpha_out = float(alpha_out)
        self.beta_feat = float(beta_feat)
        self.student_hook_names = list(student_hook_names)

        # 构建 teacher 并载 ckpt
        self.teacher = teacher_build_fn().to(device).eval()
        state = torch.load(teacher_ckpt, map_location=device)
        if isinstance(state, dict) and "state_dict" in state:
            state = state["state_dict"]
        self.teacher.load_state_dict(state)
        for p in self.teacher.parameters():
            p.requires_grad_(False)

        # teacher 特征 hook 名（取 teacher 自己声明的 feature_hook_names）
        self.teacher_hook_names = list(self.teacher.feature_hook_names())
        if len(self.teacher_hook_names) != len(self.student_hook_names):
            raise ValueError(
                f"KDHelper: student/teacher hook 数不等 "
                f"({len(self.student_hook_names)} vs {len(self.teacher_hook_names)})；"
                f"OFD/FitNets 要求等长"
            )

        # 特征缓冲
        self._t_feats: dict[str, torch.Tensor] = {}
        self._s_feats: dict[str, torch.Tensor] = {}
        # 注册 teacher 持久 hook（teacher 不变）
        for name in self.teacher_hook_names:
            self._register_hook(self.teacher, name, self._t_feats, name)

        # student hook 暂不注册（student 在 __call__ 才到位），按 id 缓存
        self._registered_student_id = None
        # adapter 懒建缓存：student_hook_name -> nn.Module
        self._adapters: dict[str, nn.Module] = {}
        self._adapter_owner = nn.ModuleList().to(device)  # 持有 adapter 参数

    @staticmethod
    def _register_hook(model, dotted, store, key):
        mod = _get_module(model, dotted)

        def hook(_m, _inp, out):
            store[key] = out

        mod.register_forward_hook(hook)

    def _register_student_hooks(self, student):
        for s_name in self.student_hook_names:
            self._register_hook(student, s_name, self._s_feats, s_name)

    @staticmethod
    def _build_adapter(s_feat, t_feat, device):
        if s_feat.shape == t_feat.shape:
            return nn.Identity().to(device)
        # 通道/特征维不同：投影到 teacher 形状。仅做最常见 2D/4D 投影。
        if s_feat.dim() == 4 and t_feat.dim() == 4:
            # [B, S, C, F] → 沿 C 投影
            s_c, t_c = s_feat.shape[2], t_feat.shape[2]
            return nn.Conv2d(s_c, t_c, kernel_size=1).to(device)
        if s_feat.dim() == 2 and t_feat.dim() == 2:
            s_c, t_c = s_feat.shape[-1], t_feat.shape[-1]
            return nn.Linear(s_c, t_c).to(device)
        raise RuntimeError(
            f"KDHelper: 无法自动建 adapter（s={tuple(s_feat.shape)}, t={tuple(t_feat.shape)}）；"
            f"请对齐 feature_hook_names 或手写 adapter"
        )

    def _ensure_adapters(self):
        for s_name, t_name in zip(self.student_hook_names, self.teacher_hook_names):
            if s_name in self._adapters:
                continue
            s_f = self._s_feats[s_name]
            t_f = self._t_feats[t_name].detach()
            adapter = self._build_adapter(s_f, t_f, self.device)
            self._adapters[s_name] = adapter
            self._adapter_owner.append(adapter)

    def kd_parameters(self):
        """adapter 参数（给 optimizer 一并更新）。Identity 无参数。"""
        return list(self._adapter_owner.parameters())

    def __call__(self, student, x, task_loss_fn, y) -> torch.Tensor:
        # 首次见到该 student 时注册 hook
        if id(student) != self._registered_student_id:
            self._register_student_hooks(student)
            self._registered_student_id = id(student)

        self._s_feats.clear()
        self._t_feats.clear()

        # student 前向（带梯度，收 s_feats）
        s_out = student(x)
        # teacher 前向（no_grad，收 t_feats）
        with torch.no_grad():
            t_out = self.teacher(x)

        self._ensure_adapters()

        task_loss = task_loss_fn(s_out, y)
        out_loss = F.mse_loss(s_out, t_out.detach())

        feat_loss = s_out.new_zeros(())
        for s_name, t_name in zip(self.student_hook_names, self.teacher_hook_names):
            s_f = self._s_feats[s_name]
            t_f = self._t_feats[t_name].detach()
            adapter = self._adapters[s_name]
            feat_loss = feat_loss + F.mse_loss(adapter(s_f), t_f)

        return task_loss + self.alpha_out * out_loss + self.beta_feat * feat_loss


def create_fake_teacher_ckpt(build_fn, path, device="cpu"):
    """构造一个随机初始化的 teacher 并 torch.save 其 state_dict（fixture 用）。

    真实场景下 teacher_ckpt 是预训好的 model8；fixture 不真训，给确定性随机初始化即可。
    """
    torch.manual_seed(0)
    t = build_fn().to(device).eval()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    torch.save(t.state_dict(), path)
    return path
